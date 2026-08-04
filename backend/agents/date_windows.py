"""
agents/date_windows.py
──────────────────────
Shared date-window helper for the news-fetching agents.

Tavily caps `max_results` per call (20-30), so a single search over a 12-month
range comes back clustered on the most recent weeks. Splitting the range into
month-sized windows and searching each one separately gives even coverage across
the whole analysis period.
"""

from __future__ import annotations

from datetime import date, timedelta


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def month_windows(start: str, end: str, max_windows: int = 12) -> list[tuple[str, str]]:
    """
    Split a YYYY-MM-DD range into calendar-month [start, end] windows.

    e.g. "2025-01-15" → "2025-03-10" yields
    [("2025-01-15","2025-01-31"), ("2025-02-01","2025-02-28"), ("2025-03-01","2025-03-10")].

    The true range bounds are preserved at both ends. If the range spans more
    than `max_windows` months, the most recent `max_windows` are kept so the
    fan-out (and API cost) stays bounded.
    """
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        s, e = e, s

    windows: list[tuple[str, str]] = []
    cursor = s
    while cursor <= e:
        month_end = _next_month(_month_start(cursor)) - timedelta(days=1)
        window_end = min(month_end, e)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)

    return windows[-max_windows:]
