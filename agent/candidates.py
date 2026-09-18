"""Spreads that can actually pass, enumerated before the model chooses.

The model used to pick a spread from the raw chain and only then learn, gate
by gate, whether it was allowed. Some of what decides that was not even in
front of it: open interest was fetched after the choice, so it could pick a
pair with OI 48 while an eligible pair sat two strikes away.

This module walks every vertical the rules could permit, runs the SAME gate
functions the final verdict uses (at qty 1, at the mid), and hands the model
the survivors. It also counts how many pairs each gate rejects, so "is the
open-interest floor too tight?" is a line in the journal rather than an
argument.

Only the per-trade SHAPE gates are applied here. Book-level gates -- budget,
book risk, direction caps, cadence, losing side -- depend on size and on the
rest of the book and are judged once, on the final observation.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from . import risk
from .config import RiskLimits
from .models import TradeProposal, parse_strike

SHAPE_GATES = ("universe", "expiry", "defined_risk", "price_sanity", "regime_direction",
               "liquidity", "delta_band", "range_buffer", "credit_floor", "leg_overlap")
MAX_WIDTH = 5.0          # the prompt's 2-5 point guidance; wider pays too little per point


@dataclass(frozen=True)
class Candidate:
    underlying: str
    right: str
    short_strike: float
    long_strike: float
    width: float
    credit: float            # at the mid of both legs
    natural: float           # short bid - long ask: what a marketable order gets
    short_delta: float
    short_oi: int
    long_oi: int


def _mid(q: dict) -> float | None:
    try:
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    except (TypeError, ValueError):
        return None
    return (bid + ask) / 2 if bid > 0 and ask > 0 else None


def enumerate_candidates(*, chains: dict, quotes: dict, tape: dict, sides: dict,
                         now: datetime, expiry: str, limits: RiskLimits,
                         open_spreads: list[dict], profile: str,
                         session: tuple | None = None) -> tuple[list[Candidate], dict]:
    """Every permitted vertical up to MAX_WIDTH wide, and why the rest failed."""
    found: list[Candidate] = []
    failed: Counter = Counter()
    pairs = 0
    for sym, chain in chains.items():
        by_right: dict[str, dict[float, dict]] = {"P": {}, "C": {}}
        for osym, snap in chain.items():
            if len(osym) > len(sym) + 6 and osym.startswith(sym):
                by_right[osym[len(sym) + 6]][parse_strike(osym)] = snap
        for right in sides.get(sym, ()):
            strikes = by_right.get(right, {})
            for k_short, short in strikes.items():
                delta = (short.get("greeks") or {}).get("delta")
                if delta is None or not (limits.min_short_delta <= abs(float(delta))
                                         <= limits.max_short_delta):
                    continue            # outside the band the chain is filtered to anyway
                for k_long, long_ in strikes.items():
                    width = (k_long - k_short) if right == "C" else (k_short - k_long)
                    if not (0 < width <= MAX_WIDTH):
                        continue
                    ms, ml = _mid(short.get("latestQuote") or {}), _mid(long_.get("latestQuote") or {})
                    if ms is None or ml is None or ms - ml <= 0:
                        continue
                    pairs += 1
                    credit = round(ms - ml, 2)
                    if credit <= 0:
                        failed["price_sanity"] += 1
                        continue
                    p = TradeProposal(underlying=sym, expiry=expiry, right=right,
                                      short_strike=k_short, long_strike=k_long, qty=1,
                                      net_price=credit, sleeve="core", rationale="candidate")
                    gates = risk.evaluate(
                        p, profile=profile, now=now, equity=1.0, day_start_equity=1.0,
                        open_positions=[], chain=chain, limits=limits, quotes=quotes,
                        target_expiry=expiry, open_spreads=open_spreads,
                        tape=tape.get(sym), session=session)
                    bad = [g.name for g in gates if g.name in SHAPE_GATES and not g.passed]
                    for name in bad:
                        failed[name] += 1
                    if bad:
                        continue
                    sq, lq = short["latestQuote"], long_["latestQuote"]
                    found.append(Candidate(
                        underlying=sym, right=right, short_strike=k_short, long_strike=k_long,
                        width=width, credit=credit,
                        natural=round(float(sq["bp"]) - float(lq["ap"]), 2),
                        short_delta=abs(float(delta)),
                        short_oi=int(short.get("openInterest") or 0),
                        long_oi=int(long_.get("openInterest") or 0)))
    found.sort(key=lambda c: (c.underlying, c.right, -c.credit / c.width))
    return found, {"pairs": pairs, "survivors": len(found), "failed": dict(failed)}


def render(found: list[Candidate], funnel: dict, per_side: int = 6) -> list[str]:
    """The snapshot section: the funnel, then the best few per name and side."""
    lines = ["", "ELIGIBLE CANDIDATES (every per-trade gate already passed, at the mid):"]
    why = ", ".join(f"{k} {v}" for k, v in sorted(funnel["failed"].items(), key=lambda kv: -kv[1]))
    lines.append(f"  {funnel['pairs']} verticals up to {MAX_WIDTH:g} wide on the permitted sides; "
                 f"{funnel['survivors']} survive. Rejections (a pair can fail several): {why or 'none'}")
    if not found:
        lines.append("  NONE. No spread on the permitted sides passes the per-trade gates right "
                     "now; standing down is the correct decision.")
        return lines
    shown: Counter = Counter()
    for c in found:
        key = (c.underlying, c.right)
        if shown[key] >= per_side:
            continue
        shown[key] += 1
        lines.append(
            f"  {c.underlying} {c.right} {c.short_strike:g}/{c.long_strike:g}  w{c.width:g}  "
            f"mid {c.credit:.2f} ({c.credit / c.width:.0%} of width)  natural {c.natural:.2f}  "
            f"short delta {c.short_delta:.2f}  OI {c.short_oi}/{c.long_oi}")
    lines.append("  Choose from this list. A spread not on it fails a gate, and the cycle is wasted.")
    return lines
