"""The size ladder: a tranche multiplier earned by an attributable record.

Size starts at half a tranche and steps up only on trades whose claim held or
failed on its own terms. A trade whose view failed but which profited anyway
(the stop or a lucky exit did the work) is excluded from both the count and
the mean: luck teaches nothing and buys no size. A drawdown from peak equity
steps size back down. The multiplier is applied inside room_for_trade as a
further cap, so it can only ever reduce a position.
"""
from __future__ import annotations

from . import shadow_stats as st

LADDER = ((0, 0.5), (10, 0.75), (25, 1.0))     # (attributable trades needed, multiplier)


def attributable(closed: list[dict], closes: dict[tuple, float]) -> list[dict]:
    out = []
    for s in closed:
        if s.get("status") != "closed" or s.get("realized_pnl") is None:
            continue
        close = closes.get((s.get("underlying"), s.get("expiry")))
        if close is None:
            continue
        k = float(s["short_strike"])
        held = close <= k if s.get("right") == "C" else close >= k
        if not held and float(s["realized_pnl"]) > 0:
            continue                                            # luck
        out.append(s)
    return out


def tier(*, closed: list[dict], closes: dict[tuple, float], equity: float, peak: float) -> tuple[float, str]:
    good = attributable(closed, closes)
    n = len(good)
    mean = (sum(float(s["realized_pnl"]) for s in good) / n) if n else 0.0
    level = 0
    for i, (need, _) in enumerate(LADDER):
        if n >= need and (need == 0 or mean > 0):
            level = i
    why = f"{'start' if level == 0 else 'earned'}: {n} attributable trades, mean {mean:+,.0f}"
    dd = (equity - peak) / peak if peak > 0 else 0.0
    if dd <= -0.10:
        level, why = 0, why + f"; drawdown {dd:.1%} resets to the floor"
    elif dd <= -0.05:
        level, why = max(0, level - 1), why + f"; drawdown {dd:.1%} drops one tier"
    return LADDER[level][1], why
