# Phase 6 — Context Assembly, Synthesis & Integration

The last phase: enforce the "Section 1 = verified numbers, never
recalculated; Section 2/3 = cited prose" separation from the diagram, and
wire the whole graph (Phases 1-5) into a real endpoint so it's actually
reachable, instead of only testable via `/debug/search` and standalone scripts.

**Integration target: `services/research_copilot.py` / `POST
/analysis/query-data`.** This endpoint already exists, already answers ad-hoc
grounded questions, and — importantly — **already returns exactly the
response shape this phase needs**: `QueryDataResponse` has `table_markdown`,
`citations: list[QueryDataCitation]` (`period`, `section`, `excerpt`), and
`analytical_note`. No new response schema, no frontend changes required.

---

## [NEW] `backend/services/context_builder.py`

```python
def build_context(
    sql_results: list[dict], footnote_results: list[dict], search_results: list[SearchHit],
) -> str:
    """
    Renders the two-section Markdown block the diagram specifies:

    # Context
    ## 1. Verified Financial Facts (Source: SEC EDGAR XBRL — structured_db)
    | Fiscal Period | Concept | Value | Unit |
    |---|---|---|---|
    ...one row per sql_results entry, values copied VERBATIM (no rounding/
    recomputation here — that's the LLM's #1 temptation to avoid)...

    ## 1b. Related Footnotes (Source: statement_footnote_links)
    ...only present if footnote_results is non-empty...

    ## 2. Grounded Excerpts (Source: Earnings Calls / News / Filing Text)
    - [Doc_ID: {doc_id} | {doc_type} | {period or published_at}]: "{text}"
    ...one entry per search_results hit, in rrf_score/rerank order...

    Section 1 is built from structured_db rows ONLY — it is never sent through
    an LLM before this point, so what the model sees is exactly what's in the
    database. Section 2 entries are tagged with the SAME doc_id used in
    Chroma/FTS5 (Phase 0's stable-ID convention) so a citation can be traced
    back to source.
    """
```

---

## [MODIFY] `backend/orchestration/graph.py` — implement `_assemble_node` / `_synthesize_node`

```python
_SYNTHESIS_SYSTEM = """\
You are an equity-research assistant. You are given VERIFIED FINANCIAL FACTS
(a table, already correct — never recompute or "round" these numbers) and
GROUNDED EXCERPTS (quoted text, each tagged [Doc_ID: ...]).

ABSOLUTE RULES:
1. Every number in your answer MUST come from Section 1's table, copied
   verbatim. If Section 1 doesn't have a number the question asks for, say
   the data isn't available — do not estimate or recall it from training data.
2. Every qualitative claim (what management said, what a news article
   reported) MUST end with its [Doc_ID: ...] tag, copied from Section 2.
3. If Section 2 is empty, do not fabricate commentary — answer from Section 1
   alone, or say the commentary isn't available in what was retrieved.
4. Output ONLY JSON matching:
   {"table_markdown": "<markdown table or null>",
    "citations": [{"period": "...", "section": "...", "excerpt": "..."}],
    "analytical_note": "<2-3 sentences, direct answer + brief context>"}
"""
# Reuses the EXACT QueryDataResponse shape research_copilot.py already defines
# — this endpoint's contract doesn't change, only what powers it.

async def _assemble_node(state: ResearchState) -> ResearchState:
    state["context"] = context_builder.build_context(
        state.get("sql_results", []), state.get("footnote_results", []),
        state.get("search_results", []),
    )
    return state

async def _synthesize_node(state: ResearchState) -> ResearchState:
    raw = await gemini_chat.gemini_generate(
        _SYNTHESIS_SYSTEM,
        f"Question: {state['question']}\n\n{state['context']}",
        temperature=0.2, max_output_tokens=4096,
    )
    parsed = _parse_json_response(raw)  # existing pattern elsewhere in this codebase — see llm_utils
    state["answer"] = parsed
    state["citations"] = [h.doc_id for h in state.get("search_results", [])]
    return state
```

---

## [MODIFY] `backend/services/research_copilot.py`

Add a new scope value that routes to the new graph instead of the old
in-memory context dump, leaving the existing four scopes (`financials`,
`sec_text`, `earnings`, `peers`) exactly as they are today — they're already
narrow and fast for their specific job:

```python
VALID_SCOPES = ("financials", "sec_text", "earnings", "peers", "all", "hybrid")

async def query_data(*, company_store, debate_store, ticker, query, data_scope) -> QueryDataResponse:
    if data_scope == "hybrid":
        result_state = await orchestration.graph.run_graph(query, ticker)
        parsed = result_state["answer"]   # already QueryDataResponse-shaped dict
        return QueryDataResponse(**parsed)
    # ...existing branches for financials/sec_text/earnings/peers/all, unchanged...
```

`"all"` (today's broadest option, dumping everything currently in memory into
one prompt) stays as-is — it's the fallback for a cold ticker with nothing
indexed yet. `"hybrid"` is the new, better option once Phase 2's ingestion has
actually run for a ticker: it searches the FULL persisted history (every
fetched period, not just what's in the live `CompanyStore`), not a token-
budget-limited dump of the current session's memory.

## [MODIFY] `backend/schemas/api_schemas.py` (`QueryDataRequest`)

Update its `data_scope` field's validation/description to include `"hybrid"`.

## [OPTIONAL, follow-up] `frontend/src/types.ts` / the Research Copilot UI

`QueryDataScope` (frontend type) gains `"hybrid"`; `ResearchCopilot.tsx` gets
a way to pick it (e.g. default to `"hybrid"` when Phase 2 ingestion is
confirmed present for the active ticker, per a new `GET /data/status`-style
check — reuse the status endpoint from the prior Unified Data Tab work
rather than inventing a new one). Not required for this backend architecture
to be complete and testable; sequence it whenever the UI work is convenient.

---

## Deprecation notes (what NOT to touch)

- `rag/sec_rag.py` / `rag/earnings_rag.py` — keep exactly as-is. They serve
  the MAS pipeline's fixed-rubric agents, a different consumer with a
  different token-budget problem than ad-hoc user questions. Do not merge or
  redirect them into `hybrid_search.py`.
- `gemini_chat.build_context` — keeps serving the `"financials"`/`"sec_text"`/
  `"earnings"`/`"peers"`/`"all"` scopes and the general chat assistant
  (`routers/chat.py`) unchanged.

---

## Execution Order

| Step | Description |
|---|---|
| 1 | `services/context_builder.py` |
| 2 | Implement `_assemble_node` / `_synthesize_node` in `orchestration/graph.py` |
| 3 | Add `"hybrid"` to `VALID_SCOPES`, wire `query_data`'s new branch |
| 4 | Update `QueryDataRequest`'s scope validation |
| 5 | End-to-end test via `POST /analysis/query-data` with `data_scope: "hybrid"` |
| 6 | (Optional/follow-up) frontend `QueryDataScope` + UI toggle |

## Verification Plan

- **Numeric fidelity**: ask a question whose answer requires a number
  (`"What was MRVL's Q3 FY2025 revenue?"`). Confirm the returned value
  matches `structured_db.query_facts` exactly, character for character — no
  rounding drift, no unit confusion.
- **Citation discipline**: ask a qualitative question
  (`"What did management say about AI demand?"`). Confirm every sentence in
  `analytical_note` that makes a factual claim is backed by a citation whose
  `excerpt` is verbatim-traceable to a real chunk (spot-check 3-5 by hand
  against the source transcript).
- **Honest gaps**: ask about something genuinely not in any store for that
  ticker (e.g. a metric the company doesn't report). Confirm the model says
  so instead of fabricating a table — this is the architecture's core promise
  and the easiest thing to silently regress.
- **Full end-to-end, real ticker**: pick a ticker with all of Phase 2's
  tracks run (SEC facts + footnotes, earnings indexed, news indexed). Ask a
  question spanning all three (numbers + earnings commentary + news) and
  confirm the final answer correctly draws from all three sources in one
  response — this is the acceptance test for the whole 6-phase migration.
- Confirm the old scopes (`financials`/`sec_text`/`earnings`/`peers`/`all`)
  are byte-for-byte unaffected — this phase must be additive, not a silent
  behavior change to the existing, working copilot.
