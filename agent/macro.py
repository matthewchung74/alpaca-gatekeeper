"""Scheduled-event awareness.

Alpaca has no economic-calendar endpoint, and an invented date is worse than no
date: the agent treats these as fact and sizes on them. So this file holds ONLY
events whose timing is structural and derivable from the calendar itself.
Everything else -- PCE, CPI, ISM, ADP, FOMC -- arrives through the news feed as
it actually happens, which is real data rather than recollection.

Structural facts used here:
  * Initial jobless claims: every Thursday, 08:30 ET.
  * Non-farm payrolls: first Friday of the month, 08:30 ET.

Both are long-standing release conventions, not guesses about a given month.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# Headline patterns worth pulling out of the news feed and showing separately.
MACRO_PATTERNS = (
    "pce", "cpi", "inflation", "payroll", "jobless claims", "unemployment",
    "fomc", "fed's", "fed chair", "rate cut", "rate hike", "ism", "gdp",
    "retail sales", "consumer confidence", "treasury yield",
)


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def upcoming(within_days: int = 3, today: date | None = None) -> list[dict]:
    """Structurally-scheduled releases between today and the horizon."""
    today = today or datetime.now().date()
    out: list[dict] = []
    for i in range(within_days + 1):
        d = today + timedelta(days=i)
        if d.weekday() == 3:      # Thursday
            out.append({
                "date": d.isoformat(), "days_away": i,
                "event": "Initial jobless claims (08:30 ET, weekly)",
                "impact": "usually minor for index premium unless a large surprise",
            })
        if d == _first_friday(d.year, d.month):
            out.append({
                "date": d.isoformat(), "days_away": i,
                "event": "Non-farm payrolls (08:30 ET, first Friday)",
                "impact": "largest recurring scheduled gap risk for short premium",
            })
    return sorted(out, key=lambda e: e["date"])


def macro_headlines(news: list[dict], limit: int = 6) -> list[dict]:
    """Headlines that look macro, surfaced separately from company news."""
    hits = []
    for n in news:
        h = (n.get("headline") or "").lower()
        if any(p in h for p in MACRO_PATTERNS):
            hits.append(n)
        if len(hits) >= limit:
            break
    return hits
