"""Regime policy: turns the agent's market read into an actual control.

The agent classifies the tape as bull / bear / sideways. That classification is
not advisory -- it determines two things deterministically:

  1. how much risk a single tranche may carry
  2. which direction of credit spread is permitted at all

Design rule: a regime may only ever REDUCE risk. The configured tranche budget
is the ceiling, earned in the regime best suited to premium selling; every other
regime scales down from it. Nothing the model says can size a position up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Right, TradeProposal

Regime = Literal["bull", "bear", "sideways"]


@dataclass(frozen=True)
class RegimePolicy:
    size_multiplier: float                    # applied to the risk budget; <= 1.0
    allowed_rights: tuple[Right, ...]         # core (credit) sleeve
    satellite_rights: tuple[Right, ...]       # satellite (debit) sleeve
    rationale: str


# The satellite sleeve is switched off in every regime. Its first four live
# trades (2026-09-10 to 09-16) were all put debit spreads bought at the bottom
# of a range because the core had no legal strike, and all four stopped out
# at the next open for -1,500 together. The machinery stays so it can be
# re-enabled with a record behind it; the policy simply grants it no side.
POLICY: dict[str, RegimePolicy] = {
    # Range-bound tape is the ideal environment for selling premium: both wings
    # decay and neither is trending into the short strike.
    "sideways": RegimePolicy(
        size_multiplier=1.00,
        allowed_rights=("P", "C"),
        satellite_rights=(),      # no trend to buy; convexity has nothing to pay for
        rationale="range-bound tape is the best case for short premium; full budget, both wings",
    ),
    # Selling puts into strength is fine, but a trend can reverse; call spreads
    # are refused because selling calls into an uptrend fights the tape.
    "bull": RegimePolicy(
        size_multiplier=0.85,
        allowed_rights=("P",),
        satellite_rights=(),      # satellite disabled 2026-09-17; see below
        rationale="uptrend: sell put premium below the move, buy call spreads with it",
    ),
    # The way short-premium accounts die is selling puts into a downtrend.
    "bear": RegimePolicy(
        size_multiplier=0.35,
        allowed_rights=("C",),
        satellite_rights=(),      # satellite disabled 2026-09-17; see below
        rationale="downtrend: no short puts, size cut hard, and any conviction expressed "
                  "as a defined-risk put debit spread rather than more premium",
    ),
}


def policy_for(regime: str) -> RegimePolicy:
    """Unknown regimes fall back to the most defensive policy."""
    return POLICY.get(regime, POLICY["bear"])


def effective_tranche_pct(regime: str, base_pct: float) -> float:
    return base_pct * policy_for(regime).size_multiplier


def budget_pct_for(regime: str, sleeve: str, limits) -> float:
    """Risk budget for this sleeve in this regime.

    The two sleeves have separate budgets: core is the workhorse, satellite is
    deliberately small because it loses more often than it wins.
    """
    base = (limits.max_satellite_risk_pct if sleeve == "satellite"
            else limits.max_tranche_risk_pct)
    return effective_tranche_pct(regime, base)


def resize_to_budget(
    proposal: TradeProposal, *, equity: float, effective_pct: float
) -> tuple[int, str]:
    """Shrink quantity to fit the regime-adjusted budget.

    Returns (qty, note). Downsizing beats blocking: a good trade at smaller size
    is better than a wasted cycle. Zero means the budget cannot fund even one
    spread, and the gate layer will reject it.
    """
    budget = equity * effective_pct
    per_spread = proposal.max_loss_per_spread
    if per_spread <= 0:
        return 0, "invalid spread economics"
    allowed = int(budget // per_spread)
    if allowed >= proposal.qty:
        return proposal.qty, f"qty {proposal.qty} fits budget ${budget:,.0f}"
    if allowed <= 0:
        return 0, (f"budget ${budget:,.0f} cannot fund one spread "
                   f"(${per_spread:,.0f} each)")
    return allowed, (f"resized {proposal.qty} -> {allowed} to fit regime budget "
                     f"${budget:,.0f} (${per_spread:,.0f} per spread)")


def direction_allowed(regime: str, right: Right, sleeve: str = "core") -> bool:
    """Which way each sleeve may lean.

    Core sells premium AGAINST the direction of the move (sell puts under an
    uptrend). Satellite buys defined-risk exposure WITH it. That is what makes
    the barbell two different bets rather than the same bet twice -- and why a
    sideways tape permits no satellite at all.
    """
    pol = policy_for(regime)
    allowed = pol.satellite_rights if sleeve == "satellite" else pol.allowed_rights
    return right in allowed


def describe(regime: str, limits) -> str:
    """One line per regime for the prompt and the journal."""
    p = policy_for(regime)
    sat = "/".join(p.satellite_rights) or "none"
    return (f"{regime}: core {budget_pct_for(regime, 'core', limits):.2%} "
            f"({'/'.join(p.allowed_rights)} credit), "
            f"satellite {budget_pct_for(regime, 'satellite', limits):.2%} "
            f"({sat} debit) -- {p.rationale}")


# --- the tape read: computed, never asked ---------------------------------

@dataclass(frozen=True)
class TapeRead:
    """What the daily bars say about one underlying.

    Built from completed sessions only. The regime feeds the budget and the
    bull/bear direction rules; the range position decides which side may be
    sold in a sideways tape. `None` fields mean there was not enough history,
    and the gates that need them fail closed.
    """
    regime: str
    range_position: float | None
    lookback_high: float | None
    lookback_low: float | None
    trend_pct: float | None
    avg_range_pct: float | None
    detail: str


def classify(bars: list[dict], spot: float, today: str, *,
             lookback: int = 10, trend_multiple: float = 2.0) -> TapeRead:
    """Regime and range position from the last `lookback` completed sessions.

    Trend is spot against the close `lookback` sessions ago, measured in units
    of the mean daily high-low range. A move smaller than `trend_multiple`
    ranges is noise inside a band, not a trend -- on 2026-09-01 SPY was 0.7%
    off its 10-session-ago close with 0.8% daily ranges, and calling that
    "bear" is what sold calls at the low.
    """
    done = [b for b in bars if str(b.get("t", ""))[:10] < today]
    if len(done) < lookback:
        return TapeRead("sideways", None, None, None, None, None,
                        f"only {len(done)} completed sessions; need {lookback}")
    win = done[-lookback:]
    hi = max(float(b["h"]) for b in win)
    lo = min(float(b["l"]) for b in win)
    ref = float(win[0]["c"])
    trend = (spot - ref) / ref
    avg_range = sum((float(b["h"]) - float(b["l"])) / float(b["c"]) for b in win) / len(win)
    threshold = trend_multiple * avg_range
    if trend > threshold:
        reg = "bull"
    elif trend < -threshold:
        reg = "bear"
    else:
        reg = "sideways"
    pos = (spot - lo) / (hi - lo) if hi > lo else None
    if pos is not None:
        detail = (f"{reg}: spot {spot:.2f} is {trend:+.2%} vs {lookback} sessions ago "
                  f"(trend threshold {threshold:.2%}); range {lo:.2f}-{hi:.2f}, "
                  f"position {pos:.0%}")
    else:
        detail = f"{reg}: spot {spot:.2f}, flat range {lo:.2f}-{hi:.2f}"
    return TapeRead(regime=reg, range_position=pos, lookback_high=hi, lookback_low=lo,
                    trend_pct=trend, avg_range_pct=avg_range, detail=detail)


def core_sides(tape: TapeRead, limits) -> tuple[Right, ...]:
    """Which rights the core sleeve may sell, given the tape.

    Trends keep the policy rule (bull: puts only; bear: calls only). A range
    forbids selling into the mean reversion: no short calls in the bottom
    quarter, no short puts in the top quarter.
    """
    allowed = policy_for(tape.regime).allowed_rights
    if tape.regime != "sideways" or tape.range_position is None:
        return allowed
    q = limits.range_edge_quantile
    if tape.range_position < q:
        return tuple(r for r in allowed if r != "C")
    if tape.range_position > 1 - q:
        return tuple(r for r in allowed if r != "P")
    return allowed
