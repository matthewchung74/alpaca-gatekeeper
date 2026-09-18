"""Scheduled-event awareness.

Alpaca has no economic-calendar endpoint, and an invented date is worse than no
date: the agent treats these as fact and sizes on them. So this file holds ONLY
dates that are either structural or published in advance by the agency itself.

  * Initial jobless claims: every Thursday, 08:30 ET (structural).
  * Employment Situation (payrolls), CPI: BLS release schedule, 08:30 ET.
  * PCE (Personal Income and Outlays): BEA release schedule, 08:30 ET.
  * FOMC decisions: second day of each scheduled meeting, 14:00 ET.

Payrolls used to be "the first Friday". In 2026 that was wrong five times out
of twelve (Jan 9, Feb 11, May 8, Jul 2, Aug 7), so the published dates are
used instead. Sources, copied 2026-09-18:
  BLS dates via the St. Louis Fed release calendar
    fred.stlouisfed.org/releases/calendar?rid=50 (Employment Situation), rid=10 (CPI)
    (bls.gov refuses automated retrieval)
  BEA  bea.gov/news/schedule  -- BEA lists upcoming releases only
  Fed  federalreserve.gov/monetarypolicy/fomccalendars.htm

UPDATE POLICY: each table covers the years in COVERED_YEARS. Outside them the
agent is told the calendar is out of date rather than being handed a guess.
Refresh every December from the three sources above.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

COVERED_YEARS = {2026}

# Decision day (second day) of each scheduled 2026 FOMC meeting.
FOMC_DECISIONS = (
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
)
EMPLOYMENT_SITUATION = (
    date(2026, 1, 9), date(2026, 2, 11), date(2026, 3, 6), date(2026, 4, 3),
    date(2026, 5, 8), date(2026, 6, 5), date(2026, 7, 2), date(2026, 8, 7),
    date(2026, 9, 4), date(2026, 10, 2), date(2026, 11, 6), date(2026, 12, 4),
)
CPI = (
    date(2026, 1, 13), date(2026, 2, 13), date(2026, 3, 11), date(2026, 4, 10),
    date(2026, 5, 12), date(2026, 6, 10), date(2026, 7, 14), date(2026, 8, 12),
    date(2026, 9, 11), date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10),
)
PCE = (   # BEA publishes upcoming releases only; earlier 2026 dates are past
    date(2026, 9, 30), date(2026, 10, 29), date(2026, 11, 25), date(2026, 12, 23),
)

_PUBLISHED = (
    (EMPLOYMENT_SITUATION, "Employment Situation / non-farm payrolls (08:30 ET)",
     "largest recurring scheduled gap risk for short premium"),
    (CPI, "Consumer Price Index (08:30 ET)",
     "rate-path repricing; index gaps on a surprise in either direction"),
    (PCE, "PCE / Personal Income and Outlays (08:30 ET)",
     "the Fed's preferred inflation gauge; usually smaller than CPI, not always"),
    (FOMC_DECISIONS, "FOMC rate decision and press conference (14:00 ET)",
     "index vol event; short gamma into it is a gap a stop cannot protect"),
)

# Headline patterns worth pulling out of the news feed and showing separately.
MACRO_PATTERNS = (
    "pce", "cpi", "inflation", "payroll", "jobless claims", "unemployment",
    "fomc", "fed's", "fed chair", "rate cut", "rate hike", "ism", "gdp",
    "retail sales", "consumer confidence", "treasury yield",
)


def upcoming(within_days: int = 3, today: date | None = None) -> list[dict]:
    """Scheduled releases between today and the horizon."""
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
        for dates, event, impact in _PUBLISHED:
            if d in dates:
                out.append({"date": d.isoformat(), "days_away": i,
                            "event": event, "impact": impact})
    horizon = today + timedelta(days=within_days)
    if today.year not in COVERED_YEARS or horizon.year not in COVERED_YEARS:
        out.append({
            "date": today.isoformat(), "days_away": 0,
            "event": "MACRO CALENDAR OUT OF DATE for part of this window",
            "impact": ("payrolls, CPI, PCE and FOMC dates are unknown here; treat any "
                       "holding period as possibly containing one, and rely on headlines"),
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
