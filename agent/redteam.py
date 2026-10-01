"""A hostile second look at a proposal, recorded rather than enforced.

The winning hackathon entries all had a named adversarial stage. This is ours,
with two deliberate differences: it is deterministic (no second model call, no
new way to rationalise), and for now it only RECORDS. Every flag is written to
the shadow ledger beside the candidate, so in a few weeks the ledger can answer
whether flagged spreads actually did worse. If they did, `veto` becomes real,
with evidence behind it. If they did not, we saved ourselves some rules.

The flags:
  event:<kind>    a scheduled macro print (FOMC, payrolls, CPI, PCE) lands
                  inside the holding window; short premium is short gamma
  earnings:<name> a megacap that moves this ETF reports inside the window
  crowded:<right> two or more open core spreads already lean this way
  itm_risk        the market itself prices a high chance the short strike
                  finishes in the money (delta at or above the band's top)
  late_entry      opened after the cutoff, so it sits overnight before any
                  sweep can manage it
"""
from __future__ import annotations

from datetime import date, datetime

from .config import ET, RiskLimits
from .macro import earnings_between, macro_between

ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MIN = 14, 30      # 14:30 ET
CROWDED_AT = 2                                     # open same-side core spreads
ITM_RISK_DELTA = 0.30


def _already_printed(kind: str, local: datetime) -> bool:
    """Macro data lands at 08:30 ET; the Fed decides at 14:00."""
    hh, mm = (14, 0) if kind == "fomc" else (8, 30)
    return (local.hour, local.minute) >= (hh, mm)


def flags(row: dict, *, expiry: str, now: datetime, tape, open_spreads: list[dict],
          limits: RiskLimits) -> dict:
    """Every hostile observation about one candidate. Never blocks: veto is False."""
    found: list[str] = []
    detail: list[str] = []
    try:
        end = date.fromisoformat(expiry)
    except (TypeError, ValueError):
        end = now.date()
    start = now.date()

    local = now.astimezone(ET)
    for kind, d in macro_between(start, end):
        if d == local.date() and _already_printed(kind, local):
            continue                      # this morning's print is history, not risk
        found.append(f"event:{kind}")
        detail.append(f"{kind} on {d.isoformat()} is inside the holding window")
    for name, d in earnings_between(str(row.get("u")), start, end):
        found.append(f"earnings:{name}")
        detail.append(f"{name} reports {d.isoformat()}, inside the window")

    same = sum(1 for s in open_spreads
               if s.get("right") == row.get("r") and (s.get("sleeve") or "core") == "core")
    if same >= CROWDED_AT:
        found.append(f"crowded:{row.get('r')}")
        detail.append(f"{same} open core {row.get('r')} spreads already lean this way")

    try:
        if float(row.get("d") or 0) >= ITM_RISK_DELTA:
            found.append("itm_risk")
            detail.append(f"delta {float(row['d']):.2f}: the market prices a "
                          f"{float(row['d']):.0%} chance the short strike finishes in the money")
    except (TypeError, ValueError):
        pass

    if (local.hour, local.minute) >= (ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MIN):
        found.append("late_entry")
        detail.append(f"entered {local:%H:%M ET}, after the {ENTRY_CUTOFF_HOUR}:{ENTRY_CUTOFF_MIN:02d} cutoff")

    return {"flags": found, "detail": "; ".join(detail), "veto": False}
