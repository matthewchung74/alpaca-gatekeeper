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
        satellite_rights=("C",),  # buy the trend
        rationale="uptrend: sell put premium below the move, buy call spreads with it",
    ),
    # The way short-premium accounts die is selling puts into a downtrend.
    "bear": RegimePolicy(
        size_multiplier=0.35,
        allowed_rights=("C",),
        satellite_rights=("P",),  # buy the trend
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
