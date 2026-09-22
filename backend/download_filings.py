"""
download_filings.py
────────────────────
Download SEC filings to local disk from the terminal.

    python download_filings.py AAPL 10-K 2022 2025
    python download_filings.py AAPL 10-Q 2024Q1 2025Q2
    python download_filings.py AAPL 8-K 2026-01-01 2026-09-18
    python download_filings.py NVDA 10-K,10-Q 2024 2025 --format html
    python download_filings.py            # no arguments -> asks interactively

Period semantics depend on the form:
  - 10-K / 10-Q : FISCAL periods, resolved the same way the app's SEC fetch
                  does (services.sec_fetch.plan_filings) — so a company whose
                  fiscal year doesn't end in December (Apple, Nvidia, Broadcom)
                  gets the right documents. 10-Q accepts YYYY or YYYYQn; SEC
                  never files a Q4 10-Q (Q4 lives in the 10-K), so Q4 bounds
                  are clamped to Q3 with a note.
  - anything else (8-K, DEF 14A, S-1, 4, 13D, …) : FILING-DATE range. YYYY
                  means the whole calendar year; YYYY-MM-DD is exact.

Files land in  <your Downloads folder>/SEC Filings/{TICKER}/{FORM}/  (the
real Windows Downloads location, even if it was moved), outside the repo. Use
--out to choose another folder. Already-downloaded files are skipped unless
--overwrite.

Everything is saved as PDF by default — the format other tools accept for
upload. --format html saves the raw SEC document instead (faster, no
Chromium), and --format both saves each filing twice. If a PDF render fails,
the HTML is saved as a fallback so the filing isn't lost.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

# Korean prompts are fine on a cp949 console; a stray em dash in an SEC title
# is not — degrade those characters instead of crashing mid-download.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except AttributeError:  # pragma: no cover — non-reconfigurable stream
    pass

_OUT_SUBFOLDER = "SEC Filings"


def _user_downloads_dir() -> Path:
    """
    The user's real Downloads folder.

    On Windows this asks the shell for the Downloads *known folder* rather than
    assuming %USERPROFILE%\\Downloads — users can move it (e.g. to D:\\), and
    Explorer's "Downloads" follows the move while the hard-coded path doesn't.
    Falls back to ~/Downloads (also right on macOS/Linux), then to home.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class _GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
                ]

            # FOLDERID_Downloads
            u = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
            guid = _GUID(u.time_low, u.time_mid, u.time_hi_version,
                         (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            path_ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)
            ) == 0:
                try:
                    return Path(path_ptr.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        except Exception:  # noqa: BLE001 — fall back to the conventional path
            pass
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


_DEFAULT_OUT = _user_downloads_dir() / _OUT_SUBFOLDER

# SEC's fair-access policy requires a declared User-Agent and caps automated
# traffic at 10 requests/second; stay comfortably under it.
_USER_AGENT = "FinAnalSupTool dev@finanalst.local"
_REQUEST_DELAY_S = 0.25
_HTTP_TIMEOUT = 60.0
_FISCAL_FORMS = ("10-K", "10-Q")
_MAX_OTHER_FILINGS = 400

_YEAR_RE = re.compile(r"^\d{4}$")
_YEAR_Q_RE = re.compile(r"^(\d{4})\s*Q([1-4])$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Target:
    """One document to download."""
    ticker: str
    form_type: str
    label: str            # "FY2024 Q1", "FY2024", or a filing date
    filing_date: str
    accession_number: str
    document_url: str


# =============================================================================
# Argument parsing
# =============================================================================

def _parse_fiscal_bound(value: str, *, is_start: bool) -> tuple[int, int | None]:
    """'2024' -> (2024, None); '2024Q2' -> (2024, 2)."""
    v = value.strip()
    if _YEAR_RE.match(v):
        return int(v), None
    m = _YEAR_Q_RE.match(v)
    if m:
        return int(m.group(1)), int(m.group(2))
    raise ValueError(
        f"'{value}' is not a fiscal period. Use YYYY or YYYYQn (e.g. 2024 or 2024Q2)."
    )


def _parse_date_bound(value: str, *, is_start: bool) -> str:
    """'2024' -> '2024-01-01' / '2024-12-31'; 'YYYY-MM-DD' passes through."""
    v = value.strip()
    if _YEAR_RE.match(v):
        return f"{v}-01-01" if is_start else f"{v}-12-31"
    if _DATE_RE.match(v):
        date.fromisoformat(v)  # validates the calendar date
        return v
    m = _YEAR_Q_RE.match(v)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        if is_start:
            return f"{year}-{(q - 1) * 3 + 1:02d}-01"
        end_month = q * 3
        end_day = {3: 31, 6: 30, 9: 30, 12: 31}[end_month]
        return f"{year}-{end_month:02d}-{end_day}"
    raise ValueError(
        f"'{value}' is not a date. Use YYYY, YYYYQn or YYYY-MM-DD."
    )


# A form name that is a single token on its own ("10-K", "8-K", "S-1", "20-F",
# "10-K/A") — used to tell "10-K 10-Q" (two forms) from "DEF 14A" (one form
# whose name contains a space).
_STANDALONE_FORM_RE = re.compile(r"^(\d+-[A-Z]+|[A-Z]+-\d+[A-Z]*)(/A)?$")


def _is_period_token(tok: str) -> bool:
    t = tok.strip()
    return bool(_YEAR_RE.match(t) or _YEAR_Q_RE.match(t) or _DATE_RE.match(t))


def _split_positionals(tokens: list[str]) -> tuple[str, str, str, str | None]:
    """
    TICKER FORM... START [END] -> (ticker, forms, start, end).

    The form part may span several shell arguments: `10-K, 10-Q` (a space
    after the comma), `10-K 10-Q`, or an unquoted `DEF 14A` all split into
    more than one argument, which used to shift START/END out of place. The
    period arguments are therefore taken from the END of the list (they have
    an unambiguous shape), and everything between the ticker and them is the
    form list.
    """
    if len(tokens) < 3:
        raise ValueError("expected TICKER FORM START [END]")
    ticker, rest = tokens[0], tokens[1:]
    periods: list[str] = []
    while rest and len(periods) < 2 and _is_period_token(rest[-1]):
        periods.insert(0, rest.pop())
    if not periods:
        raise ValueError(
            f"no period found after the filing type — expected YYYY, YYYYQn "
            f"or YYYY-MM-DD, got '{tokens[-1]}'"
        )
    if not rest:
        raise ValueError("no filing type given (e.g. 10-K)")
    forms = " ".join(rest)
    return ticker, forms, periods[0], periods[1] if len(periods) > 1 else None


def _normalize_forms(raw: str) -> list[str]:
    forms: list[str] = []
    for piece in raw.split(","):
        piece = " ".join(piece.split()).upper()
        if not piece:
            continue
        words = piece.split(" ")
        # "10-K 10-Q" is two forms; "DEF 14A" / "SCHEDULE 13G" is one.
        if len(words) > 1 and all(_STANDALONE_FORM_RE.match(w) for w in words):
            forms.extend(words)
        else:
            forms.append(piece)
    if not forms:
        raise ValueError("At least one filing type is required (e.g. 10-K).")
    return list(dict.fromkeys(forms))


# =============================================================================
# Planning
# =============================================================================

def _plan_fiscal(ticker: str, form: str, start: str, end: str) -> list[Target]:
    """10-K / 10-Q via the app's own fiscal-period resolution."""
    from services import sec_fetch

    start_year, start_q = _parse_fiscal_bound(start, is_start=True)
    end_year, end_q = _parse_fiscal_bound(end, is_start=False)

    if form == "10-K":
        start_q = end_q = None
    else:
        if start_q == 4:
            print("  note: there is no Q4 10-Q (Q4 is in the 10-K); starting from Q1 of the next year.")
            start_year, start_q = start_year + 1, 1
        if end_q == 4:
            print("  note: there is no Q4 10-Q (Q4 is in the 10-K); ending at Q3. "
                  "Add 10-K to get Q4 figures.")
            end_q = 3

    planned = sec_fetch.plan_filings(ticker, form, start_year, end_year, start_q, end_q)
    return [
        Target(
            ticker=p.ticker, form_type=p.form_type, label=p.period_label,
            filing_date=p.filing_date, accession_number=p.accession_number,
            document_url=p.document_url,
        )
        for p in planned
    ]


def _plan_other(ticker: str, form: str, start: str, end: str) -> list[Target]:
    """Any other form, selected by filing date."""
    import findata

    date_from = _parse_date_bound(start, is_start=True)
    date_to = _parse_date_bound(end, is_start=False)
    rows = findata.find_filings(
        ticker, form_type=form, date_from=date_from, date_to=date_to,
        count=_MAX_OTHER_FILINGS, include_archive=True,
    )
    targets = []
    for r in rows:
        url = r.get("document_url")
        fdate = (r.get("filing_date") or "")[:10]
        if not url or not (date_from <= fdate <= date_to):
            continue
        targets.append(Target(
            ticker=ticker, form_type=r.get("form_type") or form, label=fdate,
            filing_date=fdate, accession_number=r.get("accession_number") or "",
            document_url=url,
        ))
    targets.sort(key=lambda t: t.filing_date)
    return targets


def plan(ticker: str, forms: list[str], start: str, end: str) -> list[Target]:
    targets: list[Target] = []
    for form in forms:
        print(f"[{ticker}] resolving {form} {start} -> {end} on SEC EDGAR ...")
        try:
            found = (
                _plan_fiscal(ticker, form, start, end) if form in _FISCAL_FORMS
                else _plan_other(ticker, form, start, end)
            )
        except Exception as e:  # noqa: BLE001 — report per form, keep going
            print(f"  ! {form}: {e}")
            continue
        print(f"  {len(found)} {form} filing(s) found")
        targets.extend(found)
    return targets


# =============================================================================
# Download
# =============================================================================

def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip("-")


def _target_path(out_dir: Path, t: Target, ext: str) -> Path:
    form_dir = _safe(t.form_type)
    accession_tail = _safe(t.accession_number)[-6:] if t.accession_number else ""
    # Non-fiscal forms can have several filings on one day (e.g. multiple
    # Form 4s) — the accession tail keeps their filenames distinct.
    suffix = f"_{accession_tail}" if t.form_type not in _FISCAL_FORMS and accession_tail else ""
    name = f"{_safe(t.ticker)}_{form_dir}_{_safe(t.label)}{suffix}.{ext}"
    return out_dir / _safe(t.ticker) / form_dir / name


def _download_html(client: httpx.Client, t: Target, path: Path) -> int:
    resp = client.get(t.document_url)
    if resp.status_code == 403 or b"Request Rate Threshold Exceeded" in resp.content[:4000]:
        raise RuntimeError("SEC rate-limited this client (403). Wait a minute and retry.")
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return len(resp.content)


def _download_pdf(t: Target, path: Path) -> int:
    import findata

    data = findata.download_filing_pdf(t.document_url)
    if not data:
        raise RuntimeError("rendered PDF was empty")
    path.write_bytes(data)
    return len(data)


def download(targets: list[Target], out_dir: Path, fmt: str, overwrite: bool) -> tuple[int, int, int]:
    """Returns (downloaded, skipped, failed)."""
    exts = ["html", "pdf"] if fmt == "both" else [fmt]
    done = skipped = failed = 0
    with httpx.Client(
        headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT, follow_redirects=True
    ) as client:
        for i, t in enumerate(targets, 1):
            for ext in exts:
                path = _target_path(out_dir, t, ext)
                prefix = f"[{i}/{len(targets)}] {t.form_type} {t.label} ({ext})"
                if path.exists() and not overwrite:
                    print(f"{prefix}: already downloaded, skipped")
                    skipped += 1
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = _download_html(client, t, path) if ext == "html" else _download_pdf(t, path)
                    print(f"{prefix}: {size / 1024:,.0f} KB -> {path}")
                    done += 1
                except Exception as e:  # noqa: BLE001 — one bad filing shouldn't stop the batch
                    if path.exists() and path.stat().st_size == 0:
                        path.unlink()
                    # A PDF render can fail for reasons the raw document doesn't
                    # (Chromium missing, a page that won't finish loading). Keep
                    # the filing rather than losing it, and say what happened so
                    # the file that DID land isn't mistaken for the PDF.
                    if ext == "pdf" and fmt == "pdf":
                        html_path = _target_path(out_dir, t, "html")
                        try:
                            html_path.parent.mkdir(parents=True, exist_ok=True)
                            size = _download_html(client, t, html_path)
                            print(f"{prefix}: PDF render failed ({e}) — saved HTML instead "
                                  f"({size / 1024:,.0f} KB -> {html_path})")
                            done += 1
                            time.sleep(_REQUEST_DELAY_S)
                            continue
                        except Exception:  # noqa: BLE001 — report the original failure
                            pass
                    print(f"{prefix}: FAILED ({e})")
                    failed += 1
                time.sleep(_REQUEST_DELAY_S)
    return done, skipped, failed


# =============================================================================
# Entry point
# =============================================================================

def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default


def _interactive() -> argparse.Namespace:
    print("SEC filing downloader  (Ctrl+C to quit)\n")
    ticker = _ask("티커 (예: AAPL)")
    forms = _ask("Filing 종류 (예: 10-K, 10-Q, 8-K, DEF 14A — 쉼표로 여러 개)", "10-K")
    fiscal = all(f.strip().upper() in _FISCAL_FORMS for f in forms.split(","))
    hint = "YYYY 또는 YYYYQn" if fiscal else "YYYY 또는 YYYY-MM-DD"
    this_year = str(date.today().year)
    start = _ask(f"시작 기간 ({hint})", str(date.today().year - 2))
    end = _ask(f"종료 기간 ({hint})", this_year)
    fmt = _ask("형식 (pdf / html / both)", "pdf").lower()
    return argparse.Namespace(
        ticker=ticker, forms=forms, start=start, end=end, format=fmt,
        out=str(_DEFAULT_OUT), overwrite=False, dry_run=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download SEC filings (10-K, 10-Q, 8-K, DEF 14A, ...) to local disk.",
        epilog="Run without arguments for interactive prompts.",
    )
    parser.add_argument(
        "args", nargs="*", metavar="TICKER FORM... START [END]",
        help="e.g.  AAPL 10-K 2022 2025  |  AAPL 10-K, 10-Q 2024 2026  |  MSFT DEF 14A 2025. "
             "Periods: YYYY, YYYYQn, or YYYY-MM-DD (END defaults to START).",
    )
    parser.add_argument(
        "--format", choices=("html", "pdf", "both"), default="pdf",
        help="pdf (default) renders through headless Chromium — what other tools "
             "usually accept for upload; html is the raw SEC document (faster); "
             "both saves each filing twice.",
    )
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help=f"Output folder (default: {_DEFAULT_OUT})")
    parser.add_argument("--overwrite", action="store_true", help="Re-download files that already exist")
    parser.add_argument("--dry-run", action="store_true", help="List what would be downloaded, download nothing")
    args = parser.parse_args(argv)

    if not args.args:
        try:
            args = _interactive()
        except (KeyboardInterrupt, EOFError):
            print()
            return 130
    else:
        try:
            args.ticker, args.forms, args.start, args.end = _split_positionals(args.args)
        except ValueError as e:
            parser.error(f"{e}. Or run with no arguments for interactive mode.")

    if args.format not in ("html", "pdf", "both"):
        print(f"Unknown format '{args.format}' — use html, pdf or both.")
        return 2

    ticker = args.ticker.strip().upper()
    end = args.end or args.start
    try:
        forms = _normalize_forms(args.forms)
    except ValueError as e:
        print(e)
        return 2

    targets = plan(ticker, forms, args.start, end)
    if not targets:
        print("\nNothing to download.")
        return 1

    out_dir = Path(args.out)
    if args.dry_run:
        print(f"\n{len(targets)} filing(s) would be downloaded to {out_dir}:")
        for t in targets:
            print(f"  {t.form_type:<8} {t.label:<14} filed {t.filing_date}  {t.document_url}")
        return 0

    print(f"\nDownloading {len(targets)} filing(s) to {out_dir} ...")
    done, skipped, failed = download(targets, out_dir, args.format, args.overwrite)
    print(f"\nDone: {done} downloaded, {skipped} skipped (already present), {failed} failed.")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())
