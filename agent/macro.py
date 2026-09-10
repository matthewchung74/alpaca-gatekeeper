"""Scheduled-event awareness.

Alpaca has no economic-calendar endpoint, and an invented date is worse than no
date: the agent treats these as fact and sizes on them. So this file holds ONLY
events whose timing is structural and derivable from the calendar itself.
Everything else -- PCE, CPI, ISM, ADP, FOMC -- arrives through the news feed as
it actually happens, which is real data rather than recollection.

Structural facts used here:
  * Initial jobless claims: every Thursday, 08:30 ET.
  * Non-farm payrolls: first Friday of the month, 08:30 ET.
  * FOMC decisions: the second day of each scheduled meeting, 14:00 ET. The
    Fed publishes the year's meeting dates in advance, so these are known,
    not recalled. Copied from federalreserve.gov/monetarypolicy/fomccalendars.htm
    on 2026-09-10; the 2026-09-16 decision fell on that week's target expiry
    and the agent could not see it.

Both conventions are long-standing, not guesses about a given month.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# Decision day (second day) of each scheduled 2026 FOMC meeting.
FOMC_DECISIONS = (
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
)

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
        if d in FOMC_DECISIONS:
            out.append({
                "date": d.isoformat(), "days_away": i,
                "event": "FOMC rate decision and press conference (14:00 ET)",
                "impact": "index vol event; short gamma into it is a gap a stop cannot protect",
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
