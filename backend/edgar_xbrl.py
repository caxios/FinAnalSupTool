"""
edgar_xbrl.py
─────────────
SEC EDGAR / XBRL data retrieval for the Financial Analysis Support Tool.

Why this module exists
──────────────────────
pdfplumber-based table extraction is unreliable — it blindly grabs
column[1] as "the value", but SEC PDF tables have blank columns, `$`
markers, and multi-column layouts, so the Upper Pane ends up with blank
or wrong numbers.

Instead, this module fetches machine-readable XBRL financial data from
the SEC EDGAR **Company Facts API**:

    https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

One call returns *all* historical, standardized US-GAAP facts for a
company. We then build clean per-period financial statement tables that
slot straight into the existing `_table_store` structure — no PDF table
parsing needed.

Flow (per uploaded PDF)
───────────────────────
    1. detect_cik(pdf_path, filename)      → find the company's CIK
    2. fetch_company_facts(cik)            → all XBRL facts (cached per CIK)
    3. parse_period_end(period_str)        → the filing's period-end date
    4. build_financial_tables(...)         → {statement_type: [DataFrame]}

The public convenience wrapper `build_xbrl_statement_tables()` does all
four steps and returns tables shaped exactly like `extract_tables()` in
pdf_utils, so main.py can drop them into `_table_store` unchanged.

SEC etiquette
─────────────
The SEC requires a descriptive `User-Agent` header identifying the
caller. Change `_USER_AGENT` below to your own name/email.
"""

from __future__ import annotations

import re
import logging
from datetime import date, datetime
from pathlib import Path

import httpx
import fitz          # PyMuPDF — used to read the PDF cover page for CIK detection
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

# SEC requires a descriptive User-Agent with contact info. Replace with yours.
_USER_AGENT = "FinAnalSupTool dev@finanalst.local"

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

_HTTP_TIMEOUT = 30.0

# Match tolerance (days) when lining an XBRL period `end` date up with the
# period-end date printed on the PDF cover page. Fiscal calendars (e.g. 52/53
# week retailers) rarely land on the exact same day the cover page states.
_END_DATE_TOLERANCE_DAYS = 20


# =============================================================================
# In-Memory Caches
# =============================================================================
# The prototype stores everything in memory (same philosophy as main.py).
# The first upload for a given company hits the network; later uploads for
# the same company reuse the cached facts.

# Ticker map: uppercase ticker → CIK (int). Fetched once.
_ticker_to_cik: dict[str, int] | None = None
# Company titles for name-based fallback: list of (normalized_title, cik).
_title_to_cik: list[tuple[str, int]] | None = None
# Per-CIK company facts JSON, keyed by CIK (int).
_facts_cache: dict[int, dict] = {}


# =============================================================================
# SECTION 1: Ticker → CIK Map
# =============================================================================

async def fetch_ticker_to_cik_map() -> tuple[dict[str, int], list[tuple[str, int]]]:
    """
    Download and cache SEC's `company_tickers.json`.

    The file maps every registered ticker to its CIK and company title:

        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}

    Returns:
        A tuple of:
          - ticker_to_cik:  {"AAPL": 320193, ...}  (uppercase tickers)
          - title_to_cik:   [("apple inc", 320193), ...]  (normalized titles,
                            sorted longest-first for greedy substring matching)
    """
    global _ticker_to_cik, _title_to_cik

    if _ticker_to_cik is not None and _title_to_cik is not None:
        return _ticker_to_cik, _title_to_cik

    logger.info("Fetching SEC company_tickers.json …")
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT
    ) as client:
        resp = await client.get(_TICKERS_URL)
        resp.raise_for_status()
        data = resp.json()

    ticker_map: dict[str, int] = {}
    title_map: list[tuple[str, int]] = []
    for entry in data.values():
        cik = int(entry["cik_str"])
        ticker = str(entry.get("ticker", "")).upper().strip()
        title = _normalize_company_name(str(entry.get("title", "")))
        if ticker:
            ticker_map[ticker] = cik
        if title:
            title_map.append((title, cik))

    # Longest titles first so greedy substring matching prefers the most
    # specific company name (avoids "GM" matching inside "GMETA" etc.).
    title_map.sort(key=lambda t: len(t[0]), reverse=True)

    _ticker_to_cik = ticker_map
    _title_to_cik = title_map
    logger.info(f"Loaded {len(ticker_map):,} tickers from SEC")
    return _ticker_to_cik, _title_to_cik


def _normalize_company_name(name: str) -> str:
    """
    Normalize a company name for loose substring matching.

    Lowercases, strips common corporate suffixes ("Inc", "Corp", "Ltd", …)
    and punctuation, and collapses whitespace.
    """
    name = name.lower()
    # Drop punctuation
    name = re.sub(r"[.,/&']", " ", name)
    # Drop common corporate suffixes / noise words
    name = re.sub(
        r"\b(inc|incorporated|corp|corporation|company|co|ltd|limited|"
        r"plc|llc|lp|holdings?|group|the)\b",
        " ",
        name,
    )
    name = re.sub(r"\s+", " ", name).strip()
    return name


# =============================================================================
# SECTION 2: CIK Detection From a PDF
# =============================================================================

def _ticker_from_filename(filename: str) -> str | None:
    """
    Pull a likely ticker from an SEC-style filename.

    SEC exhibit PDFs are commonly named `<ticker>-<yyyymmdd>.pdf`, e.g.
    `mrvl-20250802.pdf` → `mrvl`. We take the leading run of letters.
    """
    stem = Path(filename).stem
    m = re.match(r"^([A-Za-z]{1,6})[-_ ]?\d", stem)
    if m:
        return m.group(1).upper()
    # Filename that is just letters (e.g. "mrvl.pdf")
    m = re.match(r"^([A-Za-z]{1,6})$", stem)
    if m:
        return m.group(1).upper()
    return None


def _read_cover_text(pdf_path: str | Path, pages: int = 5) -> str:
    """Read the first few pages of a PDF as plain text (for name matching)."""
    doc = fitz.open(str(pdf_path))
    parts = []
    for i in range(min(pages, len(doc))):
        parts.append(doc[i].get_text("text"))
    doc.close()
    return "\n".join(parts)


async def detect_cik(pdf_path: str | Path, filename: str) -> int | None:
    """
    Detect the company's CIK from an uploaded filing.

    Strategy:
      1. Extract a ticker from the filename and look it up in the SEC map.
      2. Fall back to searching the PDF cover page for a company name and
         matching it (as a substring) against the SEC company titles.

    Returns:
        The CIK as an int, or None if the company couldn't be identified.
    """
    ticker_map, title_map = await fetch_ticker_to_cik_map()

    # --- Strategy 1: ticker from filename ---
    ticker = _ticker_from_filename(filename)
    if ticker and ticker in ticker_map:
        cik = ticker_map[ticker]
        logger.info(f"CIK {cik} detected from filename ticker '{ticker}'")
        return cik

    # --- Strategy 2: company name on the cover page ---
    try:
        cover = _normalize_company_name(_read_cover_text(pdf_path))
    except Exception as e:
        logger.warning(f"Could not read PDF cover for CIK name-match: {e}")
        cover = ""

    if cover:
        # title_map is sorted longest-first, so the first title that appears
        # in the cover text is the most specific match.
        for title, cik in title_map:
            # Require a reasonably specific name to avoid spurious hits.
            if len(title) >= 4 and title in cover:
                logger.info(f"CIK {cik} detected from cover-page name '{title}'")
                return cik

    logger.warning(f"Could not detect CIK for '{filename}'")
    return None


# =============================================================================
# SECTION 3: Company Facts Fetch
# =============================================================================

async def fetch_company_facts(cik: int) -> dict | None:
    """
    Fetch (and cache) all XBRL company facts for a CIK from EDGAR.

    A single call returns every historical US-GAAP fact the company has
    ever reported, so it is cached per-CIK — later uploads for the same
    company reuse it without another network round-trip.

    Returns:
        The parsed JSON dict, or None on any failure.
    """
    if cik in _facts_cache:
        logger.info(f"Company facts for CIK {cik} served from cache")
        return _facts_cache[cik]

    url = _FACTS_URL.format(cik=cik)
    logger.info(f"Fetching company facts: {url}")
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            facts = resp.json()
    except Exception as e:
        logger.error(f"EDGAR company-facts fetch failed for CIK {cik}: {e}")
        return None

    _facts_cache[cik] = facts
    entity = facts.get("entityName", "?")
    logger.info(f"Fetched facts for CIK {cik} ({entity})")
    return facts


# =============================================================================
# SECTION 4: Curated Concept Mappings
# =============================================================================
# Each line item maps to one or more XBRL concept names. Companies tag the
# same economic quantity with different US-GAAP concepts (Revenue alone has
# at least three common tags), so we try aliases in order and take the first
# that has a value for the period.
#
# `kind` controls value formatting:
#   "usd"    — monetary value, shown in millions (raw / 1e6)
#   "shares" — share count, shown in millions
#   "eps"    — per-share value, shown as-is with 2 decimals

# (label, [concept aliases], kind)
_ConceptItem = tuple[str, list[str], str]

BALANCE_SHEET_CONCEPTS: list[_ConceptItem] = [
    ("Total Assets", ["Assets"], "usd"),
    ("Current Assets", ["AssetsCurrent"], "usd"),
    ("Cash & Cash Equivalents",
     ["CashAndCashEquivalentsAtCarryingValue",
      "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], "usd"),
    ("Short-term Investments", ["ShortTermInvestments"], "usd"),
    ("Accounts Receivable, Net",
     ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"], "usd"),
    ("Inventory", ["InventoryNet"], "usd"),
    ("Property, Plant & Equipment, Net",
     ["PropertyPlantAndEquipmentNet"], "usd"),
    ("Goodwill", ["Goodwill"], "usd"),
    ("Intangible Assets, Net",
     ["IntangibleAssetsNetExcludingGoodwill",
      "FiniteLivedIntangibleAssetsNet"], "usd"),
    ("Total Liabilities", ["Liabilities"], "usd"),
    ("Current Liabilities", ["LiabilitiesCurrent"], "usd"),
    ("Accounts Payable",
     ["AccountsPayableCurrent", "AccountsPayableTradeCurrent"], "usd"),
    ("Long-term Debt",
     ["LongTermDebtNoncurrent", "LongTermDebt"], "usd"),
    ("Deferred Revenue",
     ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"], "usd"),
    ("Total Stockholders' Equity",
     ["StockholdersEquity",
      "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
     "usd"),
    ("Retained Earnings",
     ["RetainedEarningsAccumulatedDeficit"], "usd"),
]

INCOME_STATEMENT_CONCEPTS: list[_ConceptItem] = [
    ("Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax",
      "Revenues",
      "RevenueFromContractWithCustomerIncludingAssessedTax",
      "SalesRevenueNet"], "usd"),
    ("Cost of Revenue",
     ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"], "usd"),
    ("Gross Profit", ["GrossProfit"], "usd"),
    ("Research & Development",
     ["ResearchAndDevelopmentExpense"], "usd"),
    ("Selling, General & Administrative",
     ["SellingGeneralAndAdministrativeExpense",
      "GeneralAndAdministrativeExpense"], "usd"),
    ("Total Operating Expenses",
     ["OperatingExpenses", "CostsAndExpenses"], "usd"),
    ("Operating Income",
     ["OperatingIncomeLoss"], "usd"),
    ("Interest Expense",
     ["InterestExpense", "InterestExpenseNonoperating"], "usd"),
    ("Income Before Taxes",
     ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
     "usd"),
    ("Income Tax Expense",
     ["IncomeTaxExpenseBenefit"], "usd"),
    ("Net Income",
     ["NetIncomeLoss",
      "ProfitLoss",
      "NetIncomeLossAvailableToCommonStockholdersBasic"], "usd"),
    ("EPS (Basic)",
     ["EarningsPerShareBasic"], "eps"),
    ("EPS (Diluted)",
     ["EarningsPerShareDiluted"], "eps"),
    ("Weighted Avg Shares (Diluted)",
     ["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares"),
]

CASH_FLOW_CONCEPTS: list[_ConceptItem] = [
    ("Net Cash from Operating Activities",
     ["NetCashProvidedByUsedInOperatingActivities",
      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], "usd"),
    ("Net Cash from Investing Activities",
     ["NetCashProvidedByUsedInInvestingActivities",
      "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"], "usd"),
    ("Net Cash from Financing Activities",
     ["NetCashProvidedByUsedInFinancingActivities",
      "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"], "usd"),
    ("Capital Expenditures",
     ["PaymentsToAcquirePropertyPlantAndEquipment",
      "PaymentsToAcquireProductiveAssets"], "usd"),
    ("Depreciation & Amortization",
     ["DepreciationDepletionAndAmortization",
      "DepreciationAmortizationAndAccretionNet",
      "DepreciationAndAmortization"], "usd"),
    ("Net Change in Cash",
     ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
      "CashAndCashEquivalentsPeriodIncreaseDecrease"], "usd"),
]

_STATEMENT_CONCEPTS: dict[str, list[_ConceptItem]] = {
    "balance_sheet": BALANCE_SHEET_CONCEPTS,
    "income_statement": INCOME_STATEMENT_CONCEPTS,
    "cash_flow": CASH_FLOW_CONCEPTS,
}

# Balance-sheet concepts are "instant" (a point-in-time snapshot); income and
# cash-flow concepts are "duration" (measured over the reporting period).
_INSTANT_STATEMENTS = {"balance_sheet"}


# =============================================================================
# SECTION 5: Period-End Parsing & Fact Selection
# =============================================================================

def parse_period_end(period_str: str | None) -> date | None:
    """
    Parse a human period string (e.g. "December 31, 2023") into a date.

    Accepts the `period` string produced by pdf_utils.detect_filing_metadata
    as well as plain ISO dates.
    """
    if not period_str:
        return None
    period_str = period_str.strip()

    # Try a few common formats
    for fmt in ("%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(period_str, fmt).date()
        except ValueError:
            continue

    # Last resort: pull "Month DD, YYYY" out of a longer string
    m = re.search(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
        period_str,
        re.IGNORECASE,
    )
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y"
            ).date()
        except ValueError:
            return None
    return None


def _parse_iso(d: str) -> date | None:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _select_fact_entry(
    fact_entries: list[dict],
    target_end: date | None,
    form_type: str,
    is_instant: bool,
) -> dict | None:
    """
    Pick the single XBRL fact entry that matches the uploaded filing period.

    Args:
        fact_entries: The list under facts[...]["units"]["USD"] (etc.) for one
                      concept. Each entry has "end" (and "start" for durations),
                      "val", "form", "fy", and "fp".
        target_end:   The filing's period-end date (from the PDF cover page).
        form_type:    "10-K" or "10-Q" — used to prefer period lengths and forms.
        is_instant:   True for balance-sheet (point-in-time) concepts.

    Selection logic:
        - Keep only entries whose `end` is within tolerance of target_end
          (if target_end is known; otherwise keep all and take the latest).
        - For duration concepts, prefer the entry whose span length matches
          the form: ~90 days for 10-Q, ~365 days for 10-K.
        - Prefer entries whose reported `form` matches form_type.
        - Break remaining ties by the most recent `end` date.

    Returns the winning entry dict (with "val", "fy", "fp", …), or None.
    """
    if not fact_entries:
        return None

    # Expected duration span (days) for duration concepts.
    #
    # `target_span` is what scoring prefers; the [span_lo, span_hi] window is
    # what's *allowed*. For 10-Qs the income statement reports a ~91-day
    # quarter, but the cash-flow statement reports cumulative year-to-date
    # (up to ~270 days by Q3). We prefer 91 days (so income picks the quarter)
    # yet allow the longer YTD spans so cash flow isn't dropped.
    if form_type == "10-Q":
        target_span, span_lo, span_hi = 91, 45, 290
    else:  # 10-K (annual)
        target_span, span_lo, span_hi = 365, 300, 430

    candidates: list[tuple[float, dict]] = []  # (score, entry) — lower score wins

    for entry in fact_entries:
        end = _parse_iso(entry.get("end", ""))
        if end is None:
            continue

        # Filter by period-end proximity when we know the target.
        end_gap = abs((end - target_end).days) if target_end else 0
        if target_end and end_gap > _END_DATE_TOLERANCE_DAYS:
            continue

        score = float(end_gap)  # closeness to the target end date

        if not is_instant:
            start = _parse_iso(entry.get("start", ""))
            if start is None:
                # Duration concept without a start — unusable.
                continue
            span = (end - start).days
            if span < span_lo or span > span_hi:
                continue  # wrong reporting-period length (e.g. YTD vs quarter)
            score += abs(span - target_span)

        # Prefer facts actually reported on the matching form.
        if entry.get("form") != form_type:
            score += 5.0

        # When we have no target date, favour the most recent period.
        if target_end is None:
            score -= end.toordinal() / 1e6

        candidates.append((score, entry))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _select_fact_value(
    fact_entries: list[dict],
    target_end: date | None,
    form_type: str,
    is_instant: bool,
) -> float | None:
    """Return just the `val` of the best-matching fact entry (or None)."""
    entry = _select_fact_entry(fact_entries, target_end, form_type, is_instant)
    if entry is None:
        return None
    val = entry.get("val")
    return float(val) if val is not None else None


def _get_concept_value(
    facts: dict,
    concepts: list[str],
    target_end: date | None,
    form_type: str,
    is_instant: bool,
) -> float | None:
    """
    Resolve the first concept alias that yields a value for the period.

    Searches the "us-gaap" taxonomy (falling back to "dei"/"ifrs-full" if
    present) and, within a concept, the "USD", "USD/shares", or "shares"
    unit blocks as appropriate.
    """
    taxonomies = facts.get("facts", {})
    for concept in concepts:
        for taxonomy in ("us-gaap", "ifrs-full", "dei"):
            node = taxonomies.get(taxonomy, {}).get(concept)
            if not node:
                continue
            units = node.get("units", {})
            # Try each unit block; the first with a matching fact wins.
            for unit_entries in units.values():
                val = _select_fact_value(
                    unit_entries, target_end, form_type, is_instant
                )
                if val is not None:
                    return val
    return None


def get_period_label(
    facts: dict,
    period_end: date | None,
    form_type: str,
) -> str | None:
    """
    Derive an authoritative fiscal-period label for the filing, e.g.
    "Q2 FY2026" or "FY2025".

    XBRL facts carry `fy` (fiscal year) and `fp` (fiscal period: "Q1".."Q3"
    or "FY"), which is far more reliable than guessing a quarter from the
    calendar month — critical for companies whose fiscal year isn't the
    calendar year (e.g. Marvell's ends in late January/early February).

    We probe a couple of near-universal concepts (Assets, then Revenue) and
    read the fy/fp off whichever fact matches this period.
    """
    probes: list[tuple[list[str], bool]] = [
        (["Assets"], True),
        (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
         False),
    ]
    for aliases, is_instant in probes:
        for concept in aliases:
            node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
            if not node:
                continue
            for unit_entries in node.get("units", {}).values():
                entry = _select_fact_entry(
                    unit_entries, period_end, form_type, is_instant
                )
                if entry is None:
                    continue
                fy, fp = entry.get("fy"), entry.get("fp")
                if fy and fp:
                    return f"FY{fy}" if fp == "FY" else f"{fp} FY{fy}"
    return None


# =============================================================================
# SECTION 6: Value Formatting
# =============================================================================

def _format_value(val: float, kind: str) -> str:
    """
    Format a raw XBRL value for display, matching the pdfplumber path's style
    (plain numbers with thousands separators — no currency symbol).

    - "usd"/"shares" → value in millions, 1 decimal  (1_234_500_000 → "1,234.5")
    - "eps"          → per-share value, 2 decimals    (1.234 → "1.23")
    """
    if kind == "eps":
        return f"{val:,.2f}"
    # usd and shares are both shown in millions
    millions = val / 1_000_000.0
    return f"{millions:,.1f}"


# =============================================================================
# SECTION 7: Build Financial Tables
# =============================================================================

def build_financial_tables(
    facts: dict,
    period_end: date | None,
    form_type: str,
) -> dict[str, list[pd.DataFrame]]:
    """
    Build per-statement DataFrames from XBRL facts for one filing period.

    The output shape mirrors pdf_utils.extract_tables() exactly:

        {"balance_sheet": [DataFrame(["Line Item", "Value"])],
         "income_statement": [...],
         "cash_flow": [...],
         "unclassified": []}

    Each DataFrame's first column is the line-item name and its second column
    is the formatted value — which is precisely what
    merge_tables_across_periods() expects (col[0] = label, col[1] = value).

    Only line items that resolve to a value for this period are included;
    missing items are simply omitted (the cross-period outer join fills gaps
    with nulls, just as before).
    """
    result: dict[str, list[pd.DataFrame]] = {
        "balance_sheet": [],
        "income_statement": [],
        "cash_flow": [],
        "unclassified": [],
    }

    for stmt_type, concepts in _STATEMENT_CONCEPTS.items():
        is_instant = stmt_type in _INSTANT_STATEMENTS
        rows: list[tuple[str, str]] = []

        for label, aliases, kind in concepts:
            val = _get_concept_value(
                facts, aliases, period_end, form_type, is_instant
            )
            if val is None:
                continue
            rows.append((label, _format_value(val, kind)))

        if rows:
            df = pd.DataFrame(rows, columns=["Line Item", "Value"])
            result[stmt_type].append(df)

    counts = {k: len(v) and len(v[0]) for k, v in result.items() if v}
    logger.info(f"XBRL tables built (line items per statement): {counts}")
    return result


# =============================================================================
# SECTION 8: Financial Ratios
# =============================================================================
# Ratios are computed from the *same* XBRL facts already fetched to fill the
# statement tables — no extra API calls. We pull the raw numeric values (not
# the formatted display strings) so the arithmetic is exact, then format each
# ratio for display.

# Raw metrics needed to compute the ratios below.
# (key, [concept aliases], is_instant)
_RATIO_METRIC_SPECS: list[tuple[str, list[str], bool]] = [
    # Income statement (duration)
    ("revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
      "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
     False),
    ("cost_of_revenue",
     ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"], False),
    ("gross_profit", ["GrossProfit"], False),
    ("operating_income", ["OperatingIncomeLoss"], False),
    ("net_income",
     ["NetIncomeLoss", "ProfitLoss",
      "NetIncomeLossAvailableToCommonStockholdersBasic"], False),
    ("interest_expense",
     ["InterestExpense", "InterestExpenseNonoperating"], False),
    # Balance sheet (instant)
    ("total_assets", ["Assets"], True),
    ("current_assets", ["AssetsCurrent"], True),
    ("cash",
     ["CashAndCashEquivalentsAtCarryingValue",
      "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], True),
    ("inventory", ["InventoryNet"], True),
    ("receivables",
     ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"], True),
    ("total_liabilities", ["Liabilities"], True),
    ("current_liabilities", ["LiabilitiesCurrent"], True),
    ("long_term_debt", ["LongTermDebtNoncurrent", "LongTermDebt"], True),
    ("equity",
     ["StockholdersEquity",
      "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
     True),
    # Cash flow (duration)
    ("operating_cash_flow",
     ["NetCashProvidedByUsedInOperatingActivities",
      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], False),
    ("capex",
     ["PaymentsToAcquirePropertyPlantAndEquipment",
      "PaymentsToAcquireProductiveAssets"], False),
]


def extract_ratio_metrics(
    facts: dict,
    period_end: date | None,
    form_type: str,
) -> dict[str, float | None]:
    """
    Pull the raw numeric metrics needed for ratio analysis for one period.

    Reuses the same concept-resolution logic (`_get_concept_value`) that fills
    the statement tables, so the numbers behind the ratios are consistent with
    what the user sees in the Balance Sheet / Income Statement / Cash Flow tabs.
    """
    metrics: dict[str, float | None] = {}
    for key, aliases, is_instant in _RATIO_METRIC_SPECS:
        metrics[key] = _get_concept_value(
            facts, aliases, period_end, form_type, is_instant
        )

    # Derive gross profit if the company doesn't tag it directly.
    if (
        metrics.get("gross_profit") is None
        and metrics.get("revenue") is not None
        and metrics.get("cost_of_revenue") is not None
    ):
        metrics["gross_profit"] = metrics["revenue"] - metrics["cost_of_revenue"]

    return metrics


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning None if either operand is missing or the denom is 0."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _free_cash_flow(m: dict) -> float | None:
    ocf, capex = m.get("operating_cash_flow"), m.get("capex")
    if ocf is None or capex is None:
        return None
    return ocf - capex  # capex is reported as a positive outflow


# Ratio definitions: (label, compute_fn(metrics) -> float|None, format_kind)
#   "x" → multiple (1.88),  "%" → percentage (42.3%),  "$M" → millions (1,234.5)
_RATIO_DEFS: list[tuple[str, object, str]] = [
    # Liquidity
    ("Current Ratio",
     lambda m: _safe_div(m["current_assets"], m["current_liabilities"]), "x"),
    ("Quick Ratio",
     lambda m: _safe_div(
         None if m["current_assets"] is None or m["inventory"] is None
         else m["current_assets"] - m["inventory"],
         m["current_liabilities"]), "x"),
    ("Cash Ratio",
     lambda m: _safe_div(m["cash"], m["current_liabilities"]), "x"),
    # Profitability
    ("Gross Margin",
     lambda m: _safe_div(m["gross_profit"], m["revenue"]), "%"),
    ("Operating Margin",
     lambda m: _safe_div(m["operating_income"], m["revenue"]), "%"),
    ("Net Margin",
     lambda m: _safe_div(m["net_income"], m["revenue"]), "%"),
    ("Return on Assets",
     lambda m: _safe_div(m["net_income"], m["total_assets"]), "%"),
    ("Return on Equity",
     lambda m: _safe_div(m["net_income"], m["equity"]), "%"),
    # Leverage / solvency
    ("Debt-to-Equity",
     lambda m: _safe_div(m["total_liabilities"], m["equity"]), "x"),
    ("Debt-to-Assets",
     lambda m: _safe_div(m["total_liabilities"], m["total_assets"]), "x"),
    ("Long-term Debt-to-Equity",
     lambda m: _safe_div(m["long_term_debt"], m["equity"]), "x"),
    ("Interest Coverage",
     lambda m: _safe_div(m["operating_income"], m["interest_expense"]), "x"),
    # Efficiency
    ("Asset Turnover",
     lambda m: _safe_div(m["revenue"], m["total_assets"]), "x"),
    ("Inventory Turnover",
     lambda m: _safe_div(m["cost_of_revenue"], m["inventory"]), "x"),
    ("Receivables Turnover",
     lambda m: _safe_div(m["revenue"], m["receivables"]), "x"),
    # Cash flow
    ("Free Cash Flow", _free_cash_flow, "$M"),
    ("Operating Cash Flow Margin",
     lambda m: _safe_div(m["operating_cash_flow"], m["revenue"]), "%"),
]


def _format_ratio(val: float | None, kind: str) -> str | None:
    """Format a computed ratio value; None stays None (rendered as a dash)."""
    if val is None:
        return None
    if kind == "x":
        return f"{val:.2f}x"
    if kind == "%":
        return f"{val * 100:.1f}%"
    if kind == "$M":
        return f"{val / 1_000_000.0:,.1f}"
    return str(val)


def compute_ratios(metrics: dict) -> list[tuple[str, str | None]]:
    """Compute all ratios for one period, returning ordered (label, formatted)."""
    out: list[tuple[str, str | None]] = []
    for label, fn, kind in _RATIO_DEFS:
        try:
            val = fn(metrics)  # type: ignore[operator]
        except Exception:
            val = None
        out.append((label, _format_ratio(val, kind)))
    return out


def build_ratios_table(metrics_by_period: dict[str, dict]) -> pd.DataFrame:
    """
    Build the historical ratios table across all periods.

    Shape mirrors the statement tables so the same frontend renderer and CSV
    export work unchanged, except the first column is "Ratio" instead of
    "Line Item":

        columns = ["Ratio", "<period1>", "<period2>", ...]
        rows    = one per ratio, values formatted (or None where unavailable)

    Args:
        metrics_by_period: {period_key: raw-metrics dict from extract_ratio_metrics}

    Returns:
        A DataFrame, or an empty one (just the "Ratio" column) if no metrics.
    """
    if not metrics_by_period:
        return pd.DataFrame(columns=["Ratio"])

    period_keys = list(metrics_by_period.keys())
    computed = {
        pk: dict(compute_ratios(metrics_by_period[pk])) for pk in period_keys
    }

    rows: list[dict] = []
    for label, _fn, _kind in _RATIO_DEFS:
        row: dict = {"Ratio": label}
        for pk in period_keys:
            row[pk] = computed[pk].get(label)
        rows.append(row)

    return pd.DataFrame(rows, columns=["Ratio"] + period_keys)


# =============================================================================
# SECTION 9: High-Level Convenience Wrapper
# =============================================================================

async def build_xbrl_statement_tables(
    pdf_path: str | Path,
    filename: str,
    form_type: str,
    period_str: str | None,
) -> tuple[dict[str, list[pd.DataFrame]] | None, int | None, dict | None, str | None]:
    """
    End-to-end: detect CIK → fetch facts → build tables for one filing.

    This is the single entry point main.py calls. It returns tables shaped
    identically to pdf_utils.extract_tables(), ready to drop into
    `_table_store`, plus the detected CIK, the raw ratio metrics, and an
    authoritative fiscal-period label.

    Returns:
        (tables, cik, metrics, period_label) where:
          - tables is the classified-tables dict, or None if XBRL data could
            not be obtained (caller should fall back to pdfplumber).
          - cik is the detected CIK (int) or None.
          - metrics is the raw numeric ratio-input dict for this period
            (from the same facts), or None when tables is None. Reusing the
            already-fetched facts means ratios cost no extra API calls.
          - period_label is a fiscal label like "Q2 FY2026" / "FY2025", or
            None if it couldn't be determined.
    """
    try:
        cik = await detect_cik(pdf_path, filename)
    except Exception as e:
        logger.error(f"CIK detection error for '{filename}': {e}")
        return None, None, None, None

    if cik is None:
        return None, None, None, None

    facts = await fetch_company_facts(cik)
    if facts is None:
        return None, cik, None, None

    period_end = parse_period_end(period_str)
    if period_end is None:
        logger.warning(
            f"No period-end date parsed from '{period_str}'; "
            f"XBRL will use latest available facts for CIK {cik}"
        )

    tables = build_financial_tables(facts, period_end, form_type)

    # Consider it a success only if at least one statement got data.
    if not any(tables[k] for k in ("balance_sheet", "income_statement", "cash_flow")):
        logger.warning(f"XBRL yielded no statement data for CIK {cik}")
        return None, cik, None, None

    # Reuse the same facts to extract the raw metrics behind the ratios and
    # the authoritative fiscal-period label.
    metrics = extract_ratio_metrics(facts, period_end, form_type)
    period_label = get_period_label(facts, period_end, form_type)
    return tables, cik, metrics, period_label
