"""
pdf_utils.py
────────────
PDF extraction utilities for SEC 10-K / 10-Q filings.

This module handles two distinct extraction tasks:

  1. TEXT EXTRACTION (for Lower Pane — qualitative data)
     - Uses PyMuPDF (fitz) to convert PDF pages → plain text
     - Skips the first N pages to avoid Table of Contents regex traps
     - Uses flexible, form-type-aware regex patterns to locate section
       boundaries (e.g., "Item 7" → "Item 7A" captures MD&A)
     
  2. TABLE EXTRACTION (for Upper Pane — quantitative data)
     - Uses pdfplumber to detect and parse tabular layouts
     - Classifies tables into Balance Sheet / Income Statement / Cash Flow
       using keyword heuristics
     - Merges tables across filing periods via pandas outer join

The regex patterns and section mapping design is inspired by the
sec_10kq_parser.py reference code provided by the user.
"""

from __future__ import annotations

import re
import logging
from pathlib import Path

import fitz          # PyMuPDF — used for fast full-text extraction
import pdfplumber    # Used for structured table detection
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: Section Mapping Configuration
# =============================================================================
#
# Each section config dict defines:
#   - start_patterns : regex patterns that mark WHERE a section begins
#   - end_patterns   : regex patterns that mark WHERE the NEXT section begins
#                      (i.e., where our target section ends)
#   - label          : human-readable title (shown in the UI)
#   - exclude        : patterns to reject false positives
#                      (e.g., "Item 1A" should NOT match when looking for "Item 1")
#
# We maintain separate maps for 10-K and 10-Q because they use different
# Item numbers for the same content:
#   - 10-K: MD&A is Item 7,  Financial Statements is Item 8
#   - 10-Q: MD&A is Item 2,  Financial Statements is Item 1
# =============================================================================

SECTION_MAP_10K: dict[str, dict] = {
    "mda": {
        "start_patterns": [
            r"item\s*7[.\s\-–—]+management",        # "Item 7. Management's Discussion..."
            r"item\s*7[.\s\-–—]+man",                # Truncated: "Item 7. Man..."
            r"item\s*7[.\s]+",                        # Just "Item 7." or "Item 7 "
            r"management.{0,10}s?\s+discussion\s+and\s+analysis",  # Full title without item number
        ],
        "end_patterns": [
            r"item\s*7\s*a",                          # "Item 7A" marks end of MD&A
            r"item\s*8[.\s]",                         # "Item 8" (Financial Statements)
            r"quantitative\s+and\s+qualitative\s+disclosures",  # Item 7A title text
        ],
        "label": "Management's Discussion and Analysis",
        "exclude": [r"item\s*7\s*a"],                 # Don't match "Item 7A" as "Item 7"
    },
    "footnotes": {
        "start_patterns": [
            r"item\s*8[.\s\-–—]+financial\s+statements",  # "Item 8. Financial Statements..."
            r"item\s*8[.\s]+",                              # Just "Item 8."
            r"notes\s+to\s+(consolidated\s+)?financial\s+statements",  # Notes heading
        ],
        "end_patterns": [
            r"item\s*9[.\s]",                          # "Item 9" marks end
            r"item\s*9\s*a",                            # "Item 9A" also marks end
        ],
        "label": "Financial Statements & Supplementary Data",
        "exclude": [],
    },
    "supplementary": {
        "start_patterns": [
            r"item\s*15[.\s\-–—]+exhibit",             # "Item 15. Exhibits..."
            r"item\s*15[.\s]+",                         # Just "Item 15."
            r"item\s*16[.\s]+",                         # Some filings use Item 16
            r"exhibits?\s*,?\s*(financial\s+statement\s+)?schedules?",  # Title text
        ],
        "end_patterns": [
            r"item\s*16[.\s]",                          # "Item 16" marks end
            r"signatures?\s*$",                         # "SIGNATURES" section at doc end
        ],
        "label": "Exhibits & Financial Statement Schedules",
        "exclude": [],
    },
    "risk_factors": {
        "start_patterns": [
            r"item\s*1\s*a[.\s\-–—]+risk\s+factors",   # "Item 1A. Risk Factors"
            r"item\s*1\s*a[.\s]+",                      # Just "Item 1A."
            r"risk\s+factors",                           # Title text without item number
        ],
        "end_patterns": [
            r"item\s*1\s*b",                             # "Item 1B" marks end
            r"item\s*2[.\s]",                             # "Item 2" also marks end
            r"unresolved\s+staff\s+comments",             # Item 1B title text
        ],
        "label": "Risk Factors",
        "exclude": [],
    },
    "business": {
        "start_patterns": [
            r"item\s*1[.\s\-–—]+business",              # "Item 1. Business"
            r"item\s*1[.\s]+",                            # Just "Item 1."
        ],
        "end_patterns": [
            r"item\s*1\s*a",                              # "Item 1A" (Risk Factors)
            r"item\s*1\s*b",                              # "Item 1B"
            r"risk\s+factors",                             # Title text of next section
        ],
        "label": "Business",
        "exclude": [r"item\s*1\s*a", r"item\s*1\s*b"],   # Reject 1A and 1B matches
    },
}

# 10-Q uses different item numbers for the same content types
SECTION_MAP_10Q: dict[str, dict] = {
    "mda": {
        "start_patterns": [
            r"item\s*2[.\s\-–—]+management",             # 10-Q: MD&A is Item 2
            r"item\s*2[.\s\-–—]+man",
            r"item\s*2[.\s]+",
            r"management.{0,10}s?\s+discussion\s+and\s+analysis",
        ],
        "end_patterns": [
            r"item\s*3[.\s]",                              # "Item 3" marks end
            r"quantitative\s+and\s+qualitative",
        ],
        "label": "Management's Discussion and Analysis",
        "exclude": [r"item\s*2\s*a"],
    },
    "footnotes": {
        "start_patterns": [
            r"item\s*1[.\s\-–—]+financial\s+statements",  # 10-Q: Financial Statements is Item 1
            r"item\s*1[.\s]+",
            r"notes\s+to\s+(condensed\s+)?(consolidated\s+)?financial\s+statements",
        ],
        "end_patterns": [
            r"item\s*2[.\s]",                              # "Item 2" marks end
            r"management.{0,5}s?\s+discussion",
        ],
        "label": "Financial Statements",
        "exclude": [r"item\s*1\s*a"],                      # Don't match "Item 1A"
    },
    "risk_factors": {
        "start_patterns": [
            r"item\s*1\s*a[.\s\-–—]+risk\s+factors",
            r"item\s*1\s*a[.\s]+",
            r"risk\s+factors",
        ],
        "end_patterns": [
            r"item\s*2[.\s]",
            r"item\s*3[.\s]",
        ],
        "label": "Risk Factors",
        "exclude": [],
    },
}


# Human-readable labels for each section key (used in API responses)
SECTION_LABELS: dict[str, str] = {
    "mda": "Management's Discussion and Analysis",
    "footnotes": "Financial Statements & Notes",
    "supplementary": "Exhibits & Schedules",
    "risk_factors": "Risk Factors",
    "business": "Business",
}


# =============================================================================
# SECTION 2: PDF → Plain Text Conversion (PyMuPDF)
# =============================================================================

def pdf_to_text(
    pdf_path: str | Path, skip_pages: int = 5,
) -> tuple[str, list[tuple[int, int, int]]]:
    """
    Convert a PDF file to plain text using PyMuPDF.

    Why skip pages?
    ───────────────
    SEC filings typically have a Table of Contents in the first few pages.
    The ToC lists every "Item 7", "Item 8", etc. as entries.  If we include
    these pages, our regex will match the ToC line FIRST instead of the
    actual section — a classic "regex trap".  Skipping the first ~5 pages
    (configurable) avoids this problem.

    Args:
        pdf_path:    Path to the PDF file.
        skip_pages:  Number of leading pages to skip (default: 5).

    Returns:
        A tuple of:
          - full_text: Concatenated plain text from page (skip_pages) onward.
          - page_offsets: List of (page_num, char_start, char_end) tuples that
            map each page's text to its character range in full_text.  This
            allows reverse-mapping a character position back to a page number.
    """
    doc = fitz.open(str(pdf_path))

    total_pages = len(doc)
    # Don't skip more pages than the document has
    start_page = min(skip_pages, total_pages)

    parts: list[str] = []
    page_offsets: list[tuple[int, int, int]] = []  # (page_num, char_start, char_end)
    current_offset = 0

    for page_num in range(start_page, total_pages):
        page = doc[page_num]
        text = page.get_text("text")  # Extract as plain text (not HTML/XML)
        page_start = current_offset

        if text:
            parts.append(text)
            current_offset += len(text)
        parts.append("\n\n")  # Double newline to mark page boundaries
        current_offset += 2    # len("\n\n")

        page_offsets.append((page_num, page_start, current_offset))

    doc.close()

    full_text = "".join(parts)
    logger.info(
        f"Extracted text from {Path(pdf_path).name}: "
        f"{total_pages} pages total, skipped first {start_page}, "
        f"{len(full_text):,} chars extracted"
    )
    return full_text, page_offsets


# =============================================================================
# SECTION 3: Regex-Based Section Extraction
# =============================================================================

def _find_section_span(
    text: str,
    start_patterns: list[str],
    end_patterns: list[str],
    exclude_patterns: list[str] | None = None,
) -> tuple[int, int] | None:
    """
    Find the character positions (start, end) of a section in the text.

    Algorithm:
    ──────────
    1. Try each start_pattern in order.  For each, find the FIRST match
       in the text that is NOT excluded by exclude_patterns.
    2. Pick the earliest valid start position across all patterns.
    3. From that start position, try each end_pattern to find where the
       section ends.  Pick the earliest end match.
    4. If no end marker is found, cap at 100,000 characters (safety limit).

    Args:
        text:              Full filing text to search.
        start_patterns:    Regex patterns for the section's beginning.
        end_patterns:      Regex patterns for the next section's beginning.
        exclude_patterns:  Regex patterns to reject false-positive starts.

    Returns:
        (start_index, end_index) tuple, or None if section not found.
    """
    exclude_patterns = exclude_patterns or []

    # --- Step 1: Find the best (earliest) start position ---
    best_start: int | None = None

    for pattern in start_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            matched_text = match.group(0)

            # Check if this match should be excluded
            # e.g., "Item 1A" should be excluded when searching for "Item 1"
            excluded = any(
                re.search(ep, matched_text, re.IGNORECASE)
                for ep in exclude_patterns
            )
            if excluded:
                continue  # Skip this match, try the next one

            pos = match.start()
            if best_start is None or pos < best_start:
                best_start = pos
            break  # Take first non-excluded match per pattern

    if best_start is None:
        return None  # Section not found in the text

    # --- Step 2: Find the best (earliest) end position ---
    # Start searching 20 chars after the header to skip past it
    search_from = best_start + 20
    best_end: int | None = None

    for pattern in end_patterns:
        match = re.search(pattern, text[search_from:], re.IGNORECASE | re.MULTILINE)
        if match:
            pos = search_from + match.start()
            if best_end is None or pos < best_end:
                best_end = pos

    # If no end marker found, cap the section at 100k characters
    if best_end is None:
        best_end = min(len(text), best_start + 100_000)

    return (best_start, best_end)


def extract_section_text(
    full_text: str,
    section_key: str,
    form_type: str = "10-K",
) -> str | None:
    """
    Extract a single named section from the full filing text.

    Uses the section mapping config (SECTION_MAP_10K or SECTION_MAP_10Q)
    to find the right regex patterns for the given section and form type.

    Args:
        full_text:    Full text of the filing (after skipping ToC pages).
        section_key:  One of: "mda", "footnotes", "supplementary",
                      "risk_factors", "business".
        form_type:    "10-K" or "10-Q" — determines which regex map to use.

    Returns:
        Extracted section text (cleaned up), or None if not found.
    """
    # Pick the right section map based on form type
    section_map = SECTION_MAP_10K if form_type == "10-K" else SECTION_MAP_10Q
    config = section_map.get(section_key)

    if config is None:
        logger.warning(f"Unknown section '{section_key}' for form type '{form_type}'")
        return None

    # Find where the section starts and ends in the text
    span = _find_section_span(
        full_text,
        start_patterns=config["start_patterns"],
        end_patterns=config["end_patterns"],
        exclude_patterns=config.get("exclude", []),
    )

    if span is None:
        logger.info(f"Section '{section_key}' not found in text")
        return None

    start, end = span
    extracted = full_text[start:end].strip()

    # Clean up: collapse runs of 4+ newlines into 3 (remove excessive gaps)
    extracted = re.sub(r"\n{4,}", "\n\n\n", extracted)

    logger.info(
        f"Extracted section '{section_key}': "
        f"chars {start:,}–{end:,} ({len(extracted):,} chars)"
    )
    return extracted


def extract_all_sections(
    pdf_path: str | Path,
    form_type: str = "10-K",
    skip_pages: int = 5,
) -> tuple[dict[str, str | None], list[tuple[int, int, int]]]:
    """
    Extract ALL target sections from a single filing PDF.

    This is the main entry point for text extraction.  It converts the
    PDF to text once, then runs regex extraction for each section defined
    in the section map.

    Args:
        pdf_path:    Path to the PDF file.
        form_type:   "10-K" or "10-Q" — determines which sections to look for.
        skip_pages:  Pages to skip for ToC avoidance.

    Returns:
        A tuple of:
          - sections: Dict mapping section_key → extracted text (or None).
          - page_offsets: Page offset map from pdf_to_text for reverse-mapping.
    """
    # Step 1: Convert entire PDF to plain text (skip ToC pages)
    full_text, page_offsets = pdf_to_text(pdf_path, skip_pages=skip_pages)

    # Step 2: Determine which sections to extract based on form type
    section_map = SECTION_MAP_10K if form_type == "10-K" else SECTION_MAP_10Q
    sections: dict[str, str | None] = {}

    # Step 3: Extract each section using regex
    for section_key in section_map:
        sections[section_key] = extract_section_text(full_text, section_key, form_type)

    # Log summary of what was found
    found = [k for k, v in sections.items() if v is not None]
    logger.info(f"Sections extracted from {Path(pdf_path).name}: {found}")

    return sections, page_offsets


def chars_to_pages(
    char_start: int,
    char_end: int,
    page_offsets: list[tuple[int, int, int]],
) -> tuple[int, int] | None:
    """
    Map a character span in the full text back to a (start_page, end_page) range.

    Uses the page offset map produced by pdf_to_text() to determine which
    PDF pages contain the characters from char_start to char_end.

    Args:
        char_start:    Start character index in the full text.
        char_end:      End character index in the full text.
        page_offsets:  List of (page_num, char_start, char_end) from pdf_to_text.

    Returns:
        (first_page, last_page) tuple (0-indexed PDF page numbers),
        or None if no overlap is found.
    """
    first_page: int | None = None
    last_page: int | None = None

    for page_num, p_start, p_end in page_offsets:
        # Check if this page overlaps with the character span
        if p_end > char_start and p_start < char_end:
            if first_page is None:
                first_page = page_num
            last_page = page_num

    if first_page is None or last_page is None:
        return None

    return (first_page, last_page)


def extract_section_pages(
    pdf_path: str | Path,
    start_page: int,
    end_page: int,
) -> bytes:
    """
    Extract a range of pages from a PDF into a new standalone PDF.

    Uses PyMuPDF to create a new document containing only the specified
    pages, and returns it as raw PDF bytes suitable for streaming to a client.

    Args:
        pdf_path:    Path to the original PDF file.
        start_page:  First page to include (0-indexed).
        end_page:    Last page to include (0-indexed, inclusive).

    Returns:
        Raw bytes of the new PDF containing only the requested pages.
    """
    src_doc = fitz.open(str(pdf_path))
    new_doc = fitz.open()  # Create a new empty document

    # Clamp page range to valid bounds
    start_page = max(0, start_page)
    end_page = min(end_page, len(src_doc) - 1)

    # Copy pages from source to new document
    new_doc.insert_pdf(src_doc, from_page=start_page, to_page=end_page)

    # Get the raw PDF bytes
    pdf_bytes = new_doc.tobytes()

    new_doc.close()
    src_doc.close()

    logger.info(
        f"Extracted pages {start_page}-{end_page} from {Path(pdf_path).name} "
        f"({end_page - start_page + 1} pages, {len(pdf_bytes):,} bytes)"
    )
    return pdf_bytes


# =============================================================================
# SECTION 4: Filing Metadata Detection
# =============================================================================

def detect_filing_metadata(pdf_path: str | Path) -> dict:
    """
    Detect the form type (10-K/10-Q) and filing period from the first
    few pages of a PDF.

    SEC filings typically state the form type and fiscal period on
    the cover page.  We read the first 5 pages and use regex to find:
      - Form type: "10-K" or "10-Q"
      - Period: "FOR THE FISCAL YEAR ENDED DECEMBER 31, 2023"

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Dict with keys:
          - "form_type": "10-K" | "10-Q" | None
          - "period": Full period string (e.g., "December 31, 2023") | None
          - "period_key": Short key (e.g., "2023-10K") | None
    """
    doc = fitz.open(str(pdf_path))

    # Read only the first 5 pages (cover page + intro)
    header_text = ""
    for i in range(min(5, len(doc))):
        header_text += doc[i].get_text("text") + "\n"
    doc.close()

    header_upper = header_text.upper()

    # --- Detect form type ---
    form_type: str | None = None
    if re.search(r"\b10-K\b", header_upper):
        form_type = "10-K"
    elif re.search(r"\b10-Q\b", header_upper):
        form_type = "10-Q"

    # --- Detect filing period ---
    # Look for phrases like:
    #   "FOR THE FISCAL YEAR ENDED DECEMBER 31, 2023"
    #   "FOR THE QUARTERLY PERIOD ENDED MARCH 31, 2024"
    period: str | None = None
    period_match = re.search(
        r"(?:fiscal\s+year|(?:quarterly\s+)?period)\s+ended\s+"
        r"([A-Z]+\s+\d{1,2},?\s+\d{4})",
        header_upper,
    )
    if period_match:
        period = period_match.group(1).strip().title()  # "DECEMBER 31, 2023" → "December 31, 2023"
    else:
        # Fallback: look for any standalone month-day-year pattern
        date_match = re.search(
            r"((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
            r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},?\s+\d{4})",
            header_upper,
        )
        if date_match:
            period = date_match.group(1).strip().title()

    # --- Build short period key (e.g., "2023-10K") ---
    # This key is used throughout the app to identify filings
    period_key: str | None = None
    if period and form_type:
        year_match = re.search(r"(\d{4})", period)
        if year_match:
            year = year_match.group(1)
            period_key = f"{year}-{form_type}"

    return {
        "form_type": form_type,
        "period": period,         # Full period string for display
        "period_key": period_key, # Short key for data lookups
    }


# =============================================================================
# SECTION 5: Table Extraction (pdfplumber)
# =============================================================================

# Keywords used to heuristically classify what type of financial statement
# a table belongs to.  We check if these phrases appear in the first column
# (which typically contains line item names like "Total Assets").

_BALANCE_SHEET_KW = [
    "total assets", "total liabilities", "stockholders",
    "shareholders", "equity", "current assets", "current liabilities",
]

_INCOME_STMT_KW = [
    "revenue", "net income", "earnings per share", "cost of",
    "operating income", "gross profit", "diluted",
]

_CASH_FLOW_KW = [
    "operating activities", "investing activities",
    "financing activities", "cash and cash equivalents",
    "increase in cash", "decrease in cash",
]


def _classify_table(df: pd.DataFrame) -> str | None:
    """
    Heuristically classify a DataFrame as a financial statement type.

    How it works:
    ─────────────
    1. Concatenate all values in the first column (line item names)
       into a single lowercase string.
    2. Count how many keywords from each statement type appear.
    3. The type with the highest score wins (minimum 2 matches required).

    Args:
        df: DataFrame extracted from a PDF table.

    Returns:
        "balance_sheet", "income_statement", "cash_flow", or None
        if the table couldn't be classified.
    """
    # Need at least 2 columns (line items + at least one value column)
    if df.empty or df.shape[1] < 2:
        return None

    # Build a searchable blob from the first column
    first_col = df.iloc[:, 0].astype(str).str.lower()
    blob = " ".join(first_col)

    # Score each statement type by counting keyword matches
    scores = {
        "balance_sheet": sum(1 for kw in _BALANCE_SHEET_KW if kw in blob),
        "income_statement": sum(1 for kw in _INCOME_STMT_KW if kw in blob),
        "cash_flow": sum(1 for kw in _CASH_FLOW_KW if kw in blob),
    }

    # Pick the highest-scoring type, but require at least 2 matches
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] >= 2:
        return best
    return None  # Not enough evidence to classify


def extract_tables(pdf_path: str | Path) -> dict[str, list[pd.DataFrame]]:
    """
    Extract and classify financial tables from a PDF using pdfplumber.

    Iterates through every page, finds tables, converts each to a
    DataFrame, classifies it by statement type, and groups them.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Dict mapping statement_type → list of DataFrames.
        Keys: "balance_sheet", "income_statement", "cash_flow", "unclassified".
    """
    # Initialize buckets for each statement type
    classified: dict[str, list[pd.DataFrame]] = {
        "balance_sheet": [],
        "income_statement": [],
        "cash_flow": [],
        "unclassified": [],  # Tables we couldn't identify
    }

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            # pdfplumber returns a list of tables per page
            # Each "table" is a list of rows, each row is a list of cell strings
            page_tables = page.extract_tables()
            if not page_tables:
                continue

            for raw_table in page_tables:
                # Skip empty tables or tables with only a header row
                if not raw_table or len(raw_table) < 2:
                    continue

                # First row becomes column headers
                # Replace None headers with generic names like "col_0", "col_1"
                header = [
                    str(h).strip() if h else f"col_{i}"
                    for i, h in enumerate(raw_table[0])
                ]
                data = raw_table[1:]  # Remaining rows are data

                df = pd.DataFrame(data, columns=header)

                # Clean up: remove fully empty rows and columns
                df = df.dropna(how="all").dropna(axis=1, how="all")

                if df.empty or df.shape[0] < 1:
                    continue

                # Classify and store
                table_type = _classify_table(df)
                key = table_type or "unclassified"
                classified[key].append(df)

    # Log what we found
    for k, v in classified.items():
        if v:
            logger.info(f"  {k}: {len(v)} table(s) extracted")

    return classified


# =============================================================================
# SECTION 6: Cross-Period Table Merging
# =============================================================================

def merge_tables_across_periods(
    all_period_tables: dict[str, dict[str, list[pd.DataFrame]]],
) -> dict[str, pd.DataFrame]:
    """
    Merge tables of the same statement type across multiple filing periods
    using a pandas outer join on line item names.

    This creates the unified view for the Upper Pane:
      - Columns: ["Line Item", "2023-10K", "2022-10K", ...]
      - Rows: one per unique line item
      - Values: NaN where a line item doesn't exist in a given period

    Example:
    ────────
    Input:  {"2023-10K": {"balance_sheet": [df1]},
             "2022-10K": {"balance_sheet": [df2]}}

    Output: {"balance_sheet": DataFrame with columns
             ["Line Item", "2023-10K", "2022-10K"]}

    Args:
        all_period_tables: Dict of period_key → classified tables dict.

    Returns:
        Dict of statement_type → single merged DataFrame.
    """
    statement_types = ["balance_sheet", "income_statement", "cash_flow"]
    merged_results: dict[str, pd.DataFrame] = {}

    for stmt_type in statement_types:
        # Collect one DataFrame per period for this statement type
        period_dfs: list[tuple[str, pd.DataFrame]] = []

        for period_key, classified in all_period_tables.items():
            tables = classified.get(stmt_type, [])
            if not tables:
                continue

            # If a period has multiple tables of the same type,
            # concatenate them into one (e.g., two balance sheet pages).
            # First, deduplicate column names within each table —
            # pdfplumber can produce tables with duplicate headers
            # (e.g., two columns both named "2024"), and pd.concat
            # requires uniquely-valued column indices.
            deduped_tables = []
            for tbl in tables:
                cols = list(tbl.columns)
                seen: dict[str, int] = {}
                new_cols: list[str] = []
                for c in cols:
                    if c in seen:
                        seen[c] += 1
                        new_cols.append(f"{c}_{seen[c]}")
                    else:
                        seen[c] = 0
                        new_cols.append(c)
                tbl.columns = new_cols
                deduped_tables.append(tbl)

            combined = pd.concat(deduped_tables, ignore_index=True)

            if combined.empty or combined.shape[1] < 2:
                continue

            # Convention: first column = line item names, second = values
            line_item_col = combined.columns[0]
            value_col = combined.columns[1]

            # Create a two-column DataFrame: "Line Item" + period_key
            period_df = combined[[line_item_col, value_col]].copy()
            period_df.columns = ["Line Item", period_key]

            # Remove duplicate line items within the same period (keep first occurrence)
            period_df = period_df.drop_duplicates(subset="Line Item", keep="first")
            period_dfs.append((period_key, period_df))

        if not period_dfs:
            # No data for this statement type — return empty DataFrame
            merged_results[stmt_type] = pd.DataFrame(columns=["Line Item"])
            continue

        # Outer join: start with the first period, merge in each subsequent one
        # This ensures ALL line items from ALL periods appear in the result
        # (unmatched items get NaN — the "outer join semantics" from the spec)
        result = period_dfs[0][1]
        for _, next_period_df in period_dfs[1:]:
            result = result.merge(next_period_df, on="Line Item", how="outer")

        merged_results[stmt_type] = result

    return merged_results
