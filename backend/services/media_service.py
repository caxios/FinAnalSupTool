"""
services.media_service
───────────────────────
Turn cached media/macro/Data-tab data into Markdown context blocks for the AI
assistant, so it can answer questions across all views.

Two sources, two functions:
  - build_media_context        — the SESSION-only MediaCache, populated by the
    old /media/* viewer endpoints when actually opened this session (or by an
    isolated agent persona's own data).
  - build_persisted_data_context — the Data tab's own DISK caches (news_cache,
    price_cache, insider_cache, transcript_cache), populated by POST /data/fetch
    regardless of session or Deep Analysis history. This is what lets the
    general chat assistant answer from data fetched via the Data tab alone —
    see routers/chat.py, which concatenates both into one context.
"""

from __future__ import annotations

from schemas import NewsResponse, VideoResponse, SentimentResponse
from services.storage import MACRO_SCOPE, MediaCache


def build_media_context(cache: MediaCache, ticker: str | None = None) -> str:
    """
    Render the cached media/macro data as a Markdown block for the assistant.

    Company data comes from ``ticker``'s cache entry only — so one company's news
    and transcripts never leak into another's answer. Market-wide macro data is
    shared and always included. Pass ``ticker=None`` for a macro-only context.
    """
    company_data = cache.get(ticker) if ticker else {}
    macro_data = cache.get(MACRO_SCOPE)
    # Company keys win; macro keys fill in the market-wide sections.
    data = {**macro_data, **company_data}
    parts: list[str] = []

    cn: NewsResponse | None = data.get("company_news")
    if cn and cn.articles:
        parts.append("# Company News (recent, from web search)")
        for a in cn.articles[:10]:
            parts.append(f"- [{a.source}] {a.title} — {a.snippet[:200]}")
        parts.append("")

    mn: NewsResponse | None = data.get("macro_news")
    if mn and mn.articles:
        parts.append("# Macro / Market News (recent)")
        for a in mn.articles[:10]:
            parts.append(f"- [{a.source}] {a.title} — {a.snippet[:200]}")
        parts.append("")

    sent: SentimentResponse | None = data.get("sentiment")
    if sent and sent.configured and sent.summary:
        parts.append(
            f"# Market Sentiment: {sent.label} "
            f"({sent.score if sent.score is not None else 'n/a'}/100)"
        )
        parts.append(sent.summary)
        for ind in sent.indicators:
            parts.append(f"- {ind.theme} ({ind.direction}): {ind.note}")
        parts.append("")

    for scope_key in ("company_videos", "macro_videos"):
        vr: VideoResponse | None = data.get(scope_key)
        if vr and vr.videos:
            label = "Company" if scope_key == "company_videos" else "Macro"
            parts.append(f"# {label} Analysis Videos")
            for v in vr.videos[:8]:
                parts.append(f"- {v.title} — {v.channel}")
            parts.append("")

    transcripts: dict = data.get("transcripts", {})
    if transcripts:
        parts.append("# Video Transcript Excerpts")
        for vid, info in list(transcripts.items())[:5]:
            excerpt = info.get("text", "")[:400]
            if excerpt:
                parts.append(f"- ({vid}) {excerpt}")
        parts.append("")

    return "\n".join(parts)


def build_persisted_data_context(ticker: str) -> str:
    """
    Render the Data tab's disk-persisted caches — company news, earnings-call
    transcripts, price/technicals, insider trades (Form 4) and 8-K filings —
    as a Markdown context block for one ticker.

    Deliberately independent of MediaCache/DebateStore: this reads straight
    from services.news_cache/price_cache/insider_cache/transcript_cache, the
    same disk caches POST /data/fetch writes into — so a ticker that has only
    ever been through the Data tab (no Deep Analysis run, no /media/* viewer
    tab opened this session) still grounds the general chat assistant.
    Financials + filing text are NOT here — CompanyStore already covers those
    (see gemini_chat.build_context) once SEC 10-K/10-Q has been fetched.
    """
    from services import insider_cache, news_cache, price_cache, transcript_cache

    t = (ticker or "").strip().upper()
    if not t:
        return ""
    parts: list[str] = []

    # ── News — merged across cached windows, deduped by URL (same as GET /data/news/{ticker}) ──
    ranges = news_cache.list_cached_ranges(t)
    if ranges:
        seen: set[str] = set()
        merged: list[dict] = []
        for start, end in ranges:
            for row in news_cache.get_news(t, start, end) or []:
                url = row.get("url")
                if url and url not in seen:
                    seen.add(url)
                    merged.append(row)
        merged.sort(key=lambda r: r.get("published") or "", reverse=True)
        if merged:
            parts.append("# Company News (cached, from the Data tab)")
            for a in merged[:15]:
                snippet = (a.get("snippet") or "")[:200]
                parts.append(f"- [{a.get('source', '?')}] {a.get('title', '')} — {snippet}")
            parts.append("")

    # ── Earnings-call transcripts — an excerpt per cached quarter, newest few ──
    quarters = transcript_cache.list_cached_quarters(t)
    if quarters:
        found: list[tuple[int, int, str]] = []
        for qk in quarters:
            try:
                year, quarter = int(qk[:4]), int(qk[5])
            except (ValueError, IndexError):
                continue
            doc = transcript_cache.get_transcript(t, year, quarter)
            if doc and doc.found and doc.text:
                found.append((year, quarter, doc.text))
        if found:
            parts.append("# Earnings Call Transcripts (cached, from the Data tab)")
            for year, quarter, text in found[-4:]:
                parts.append(f"## {year} Q{quarter}")
                parts.append(text[:4000])
            parts.append("")

    # ── Price & technicals — the most recently cached window ──
    price_ranges = price_cache.list_cached_ranges(t)
    if price_ranges:
        latest_start, latest_end = price_ranges[-1]
        pdata = price_cache.get_price_data(t, latest_start, latest_end)
        if pdata:
            parts.append(f"# Price & Technicals (cached {latest_start} to {latest_end})")
            for key in ("current_price", "period_return", "sma_50", "sma_200", "rsi_14", "golden_cross"):
                if pdata.get(key) is not None:
                    parts.append(f"- {key}: {pdata[key]}")
            parts.append("")

    # ── Insider trades (Form 4) + 8-K filings ──
    trades = insider_cache.get_insider_trades(t) or []
    if trades:
        parts.append("# Insider Trades — Form 4 (cached, from the Data tab)")
        for tr in trades[:15]:
            action = (
                "Buy" if tr.get("acquired_or_disposed") == "A"
                else "Sell" if tr.get("acquired_or_disposed") == "D"
                else tr.get("transaction_code_description") or "?"
            )
            role = tr.get("officer_title") or ("Director" if tr.get("is_director") else "")
            parts.append(
                f"- {tr.get('transaction_date', '?')}: {tr.get('owner_name', '?')} "
                f"({role}) {action} {tr.get('amount', '?')} shares "
                f"@ {tr.get('price_per_share', '?')}"
            )
        parts.append("")

    filings_8k = insider_cache.get_8k_filings(t) or []
    if filings_8k:
        parts.append("# 8-K Filings (cached, from the Data tab)")
        for f in filings_8k[:10]:
            parts.append(f"- {f.get('filing_date', '?')}: {f.get('title', '?')}")
        parts.append("")

    return "\n".join(parts)
