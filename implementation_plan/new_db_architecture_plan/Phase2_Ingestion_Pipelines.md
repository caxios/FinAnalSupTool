# Phase 2 — Ingestion Pipelines

Make each source type actually fill Phase 1's stores. Three independent
tracks (SEC, earnings, news) — can be built and merged in any order, or in
parallel by different people.

---

## Track A — SEC: HTML footnote graph + structured facts

### The gap

`services/sec_fetch.py::render_planned()` calls `findata.download_filing_pdf()`
— it only ever produces a **PDF**. The original filing HTML (whose URL is
already sitting right there in `PlannedFiling.document_url`) is fetched by
`findata` internally to render the PDF, then discarded. PDF has no DOM, no
anchors, no way to know that the "Long-term Debt" row on the balance sheet
points at Note 7 — that information only exists in the source HTML.

### [NEW] `backend/parsers/sec_html_parser.py`

```python
async def fetch_and_archive_html(document_url: str, ticker: str, period_label: str) -> str:
    """
    GET the raw filing HTML directly from document_url (same SEC etiquette
    User-Agent as edgar_xbrl.py), archive it via data_lake.save_raw(
    ticker, "sec_filing_html", period_label, "html", content), return the text.
    """

@dataclass
class FootnoteLink:
    statement_item: str
    note_id: str
    note_title: str | None
    note_text: str

def parse_footnote_graph(html: str) -> list[FootnoteLink]:
    """
    BeautifulSoup-parse the filing HTML:
      1. Locate the Notes/Item 8 section — headers matching
         r'Note\\s+\\d+[\\.\\-—]\\s*(.+)' become (note_id, note_title) anchors,
         with the text up to the next Note header as note_text.
      2. Walk each financial-statement table (Balance Sheet / Income Statement /
         Cash Flow — reuse the same section-detection heuristics as
         parsers/pdf_utils.py's table classifier where possible) and for each
         row, look for a footnote reference: an <a href="#noteN"> anchor, or a
         bare "(Note 7)" / "See Note 7" text pattern via regex.
      3. Emit one FootnoteLink per (statement_item, note_id) match found.
    Returns [] on any parse failure — this is best-effort enrichment, never a
    hard requirement for ingestion to succeed.
    """
```

New dependency: `beautifulsoup4` + `lxml` (parser backend).

### [MODIFY] `backend/providers/edgar_xbrl.py`

Add a thin adapter that turns the SAME facts already being extracted for
`table_store`/`metrics_store` into flat rows for `structured_db.upsert_facts`
— **no new SEC calls**, this reuses `fetch_company_facts()`'s cached response:

```python
def facts_to_rows(
    facts: dict, ticker: str, cik: int, period_end: date | None, form_type: str,
) -> list[dict]:
    """
    Walk BALANCE_SHEET_CONCEPTS / INCOME_STATEMENT_CONCEPTS / CASH_FLOW_CONCEPTS
    (the same constants build_financial_tables already uses) and, for each
    resolved concept, emit one row matching structured_db's financial_facts
    schema. Reuses _get_concept_value's selection logic so values are IDENTICAL
    to what the existing table_store shows — this is a projection, not a
    second source of truth.
    """
```

Also expose one for the Q4-derivation path already built this session
(`build_q4_synthetic_tables`) — its output should land in `financial_facts`
with `source="xbrl-derived-q4"`, same as any other row, so the SQL tool
(Phase 5) sees a complete quarterly series without needing to know Q4 is special.

### [MODIFY] `backend/services/ingestion.py` (`ingest_pdf`)

After the existing XBRL extraction step, add (best-effort, never fails the
ingestion):

```python
if xbrl_tables is not None and detected_cik is not None:
    rows = edgar_xbrl.facts_to_rows(xbrl_facts, routing_ticker, detected_cik, period_end, form_type)
    structured_db.upsert_facts(rows)
```

(`xbrl_facts` — the raw facts dict — needs to be threaded out of
`build_xbrl_statement_tables`'s return; today it only returns the already-built
`tables`. Either widen that function's return tuple, or have `ingest_pdf`
independently call `fetch_company_facts(detected_cik)` again — free, it's
cached in `_facts_cache`.)

### [MODIFY] `backend/services/sec_ingest.py` (`fetch_and_ingest_range`)

After `render_planned()` succeeds for each `PlannedFiling`, before/alongside
the existing `ingest_pdf` call:

```python
html = await sec_html_parser.fetch_and_archive_html(p.document_url, p.ticker, p.filename)
links = sec_html_parser.parse_footnote_graph(html)
if links:
    structured_db.upsert_footnote_links([
        {"ticker": p.ticker, "period_key": <resolved period_key>, **asdict(l)}
        for l in links
    ])
```

`_derive_q4_periods` (added this session) already computes Q4 via
`edgar_xbrl.build_q4_synthetic_tables` — extend it to also call
`structured_db.upsert_facts(edgar_xbrl.facts_to_rows(...))` for the derived
Q4 period, same as any other period.

---

## Track B — Earnings: persistent, speaker-aware indexing

### The gap

`rag/chunking.chunk_earnings_transcript` is already speaker-aware and good —
**reuse it as-is**. The problem is only WHERE the chunks get indexed:
`rag/earnings_rag.py::prepare_context` indexes under `scope = f"{ticker}:{run_id}"`
— ephemeral, re-embedded every MAS run, invisible to a general search.

### [MODIFY] `backend/services/research_copilot.py`

In `fetch_and_cache_earnings_transcripts` (already fetches + cleans + caches
to `transcript_cache` this session) and in the existing cache-read path
(`_format_cached_quarters`), add a persistent-index step after a transcript
is confirmed `found=True`:

```python
async def index_earnings_transcript(ticker: str, year: int, quarter: int, text: str) -> None:
    quarter_label = f"{year}Q{quarter}"
    chunks = chunking.chunk_earnings_transcript(text, quarter_label)
    for i, c in enumerate(chunks):
        doc_id = f"{ticker}_{quarter_label}_earnings_{i:03d}"
        meta = {**c["metadata"], "ticker": ticker, "doc_type": "earnings_transcript"}
        await vector_store.index_chunks("earnings_transcripts", [c], id_prefix=doc_id)  # existing, unchanged
        search_index.index_chunk(doc_id, c["text"], ticker, "earnings_transcript", meta)  # NEW
```

Call this from `routers/data.py`'s `_fetch_earnings` (the Data tab's earnings
checkbox) right after `transcript_cache.save_transcript`, and from
`fetch_and_cache_earnings_transcripts`'s on-demand recovery path. **Do not**
change `rag/earnings_rag.py` — the MAS pipeline keeps its own ephemeral,
run-scoped indexing exactly as today (see Phase 0's "out of scope").

Also archive the raw transcript text into the data lake here:
`data_lake.save_raw(ticker, "earnings_transcript", quarter_label, "txt", text)`.

---

## Track C — News: dedup + ticker tagging + indexing

### The gap

News is the one source with **zero** indexing today. `company_news_agent.py`
dedups by exact URL only; `news_cache.py` (this session's work) is a flat
per-window JSON blob with no chunk/search structure at all.

### [NEW] `backend/providers/news_dedup.py`

```python
def simhash(text: str, *, hash_bits: int = 64) -> int:
    """Standard 64-bit SimHash over word shingles (no ML dependency —
    pure hashlib + bit manipulation)."""

def hamming_distance(a: int, b: int) -> int: ...
```

### [NEW] ticker tagging without a heavy NER dependency

Reuse infrastructure that already exists rather than adding spaCy/transformers:
`providers/edgar_xbrl.py::fetch_ticker_to_cik_map()` already builds a
normalized company-name → ticker map for the whole market. Add:

```python
# backend/providers/entity_tagging.py
async def tag_tickers(text: str, primary_ticker: str) -> tuple[str, list[str]]:
    """
    primary_ticker is always returned as primary_ticker.
    mentioned_tickers: scan text for other companies' normalized names/tickers
    from the SEC ticker map (registry_db.entity_aliases first — cheaper —
    falling back to edgar_xbrl's title_to_cik list). Keyword/alias matching,
    not statistical NER — good enough for "which other tickers does this
    article also touch," not full entity extraction.
    """
```

Seed `registry_db.entity_aliases` once from `edgar_xbrl.fetch_ticker_to_cik_map()`
(a one-time backfill script, see Execution Order) so lookups are a local
SQLite query, not a re-walk of the full SEC ticker list per article.

### [MODIFY] `backend/services/data_fetcher.py` (`fetch_company_news`)

After a live fetch (cache miss), before `news_cache.save_news`:

```python
kept: list[NewsArticle] = []
for a in result.articles:
    h = news_dedup.simhash(a.title + " " + a.snippet)
    canonical = registry_db.find_near_duplicate(h, ticker or company)
    article_id = f"{ticker}_{hashlib.sha1(a.url.encode()).hexdigest()[:10]}"
    if canonical is None:
        registry_db.register_article(article_id, h, article_id, a.url, ticker or company)
        kept.append(a)
        primary, mentioned = await entity_tagging.tag_tickers(a.title + " " + a.snippet, ticker or "")
        doc_id = f"{article_id}_news_000"
        meta = {"ticker": ticker, "doc_type": "news_article", "published_at": a.published,
                 "mentioned_tickers": ",".join(mentioned), "url": a.url}
        await vector_store.index_chunks("news_articles", [{"text": f"{a.title}\n{a.snippet}", "metadata": meta}], id_prefix=doc_id)
        search_index.index_chunk(doc_id, f"{a.title}\n{a.snippet}", ticker or company, "news_article", meta)
        data_lake.save_raw(ticker or company, "news_article", article_id, "json", json.dumps(asdict(a)))
    else:
        registry_db.register_article(article_id, h, canonical, a.url, ticker or company)
        # duplicate — not kept, not re-indexed
kept_result = replace(result, articles=kept)
```

Add `"news_articles"` to `rag/vector_store.py`'s `_COLLECTIONS` dict.

News articles are short enough that one chunk = one article (no
`chunking.chunk_news_article` needed) — if a body ever needs splitting later,
extend `rag/chunking.py` the same way the other three source types work.

---

## Execution Order

| Step | Track | Description |
|---|---|---|
| 1 | A | `pip install beautifulsoup4 lxml`, create `parsers/sec_html_parser.py` |
| 2 | A | `edgar_xbrl.facts_to_rows()` |
| 3 | A | Wire into `ingestion.py` + `sec_ingest.py` (incl. Q4-derived facts) |
| 4 | C | One-time backfill: seed `registry_db.entity_aliases` from `edgar_xbrl.fetch_ticker_to_cik_map()` |
| 5 | C | `providers/news_dedup.py`, `providers/entity_tagging.py` |
| 6 | C | Wire into `data_fetcher.fetch_company_news` |
| 7 | B | `research_copilot.index_earnings_transcript`, wire into `routers/data.py` + on-demand recovery |
| 8 | — | Add `"news_articles"` collection to `vector_store.py` |

## Verification Plan

- Track A: run against a ticker already in `filing_cache/` (e.g. AVGO). Confirm `financial_facts` has rows for all 3 statements across all cached periods, including a `source='xbrl-derived-q4'` row for each fiscal year. Confirm `statement_footnote_links` has at least one row (spot-check against the real filing on sec.gov that the note number is right).
- Track B: fetch AVGO's Q3 2024 transcript (already cached from prior sessions) through the new path; confirm `search_index.search_bm25("prepared remarks", ticker="AVGO")` returns a hit, and the Chroma `earnings_transcripts` collection has chunks under the NEW stable scope (not a run_id).
- Track C: fetch AVGO news for a window; confirm near-identical syndicated articles collapse to one canonical id in `registry_db.news_dedup`, confirm `mentioned_tickers` is populated for at least one article that discusses a competitor.
- Confirm none of this breaks the existing Data tab flows from the prior implementation (`POST /data/fetch` for all 5 types still returns `status: "ok"`).
