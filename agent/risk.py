"""Deterministic risk gates.

The LLM proposes; this module disposes. Every gate is a pure function of the
proposal plus observed account and market state -- no model output is trusted
for anything that bounds risk. A proposal must clear *every* gate to reach the
broker. Gate zero is the account guard, which cannot be reached by any prompt.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import regime as regime_mod
from .config import (
    COMPETITION_PROFILE, TARGET_EXPIRY, UNIVERSE, RiskLimits,
    AccountGuardError, assert_may_trade, in_no_trade_window,
)
from .models import GateResult, TradeProposal, occ_symbol

# SPY260903P00767000 -> root, yymmdd, right, strike x1000
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def evaluate(
    proposal: TradeProposal,
    *,
    profile: str,
    now: datetime,
    equity: float,
    day_start_equity: float,
    open_positions: list[dict],
    chain: dict,
    limits: RiskLimits,
    halted: bool = False,
    regime: str = "sideways",
    chains: dict | None = None,
    quotes: dict | None = None,
) -> list[GateResult]:
    """Run every gate. Order matters only for readability; all of them run."""
    g: list[GateResult] = []

    # --- Gate 0: account guard -------------------------------------------
    try:
        assert_may_trade(profile, now)
        g.append(GateResult(name="account_guard", passed=True,
                            detail=f"{profile} cleared to trade at {now:%Y-%m-%d %H:%M %Z}"))
    except AccountGuardError as e:
        g.append(GateResult(name="account_guard", passed=False, detail=str(e)))

    # --- Gate 1: event halt ----------------------------------------------
    g.append(GateResult(
        name="event_halt", passed=not halted,
        detail="halted for the event" if halted else "not halted",
    ))

    # --- Gate 2: daily loss ----------------------------------------------
    daily_pl = equity - day_start_equity
    daily_limit = -abs(day_start_equity * limits.max_daily_loss_pct)
    ok = daily_pl > daily_limit
    g.append(GateResult(
        name="daily_loss", passed=ok,
        detail=f"day P&L {daily_pl:+,.0f} vs limit {daily_limit:+,.0f}",
    ))

    # --- Gate 3: event drawdown ------------------------------------------
    from .config import STARTING_EQUITY
    dd = (equity - STARTING_EQUITY) / STARTING_EQUITY
    ok = dd > -limits.max_event_drawdown_pct
    g.append(GateResult(
        name="event_drawdown", passed=ok,
        detail=f"drawdown {dd:+.2%} vs limit {-limits.max_event_drawdown_pct:.2%}",
    ))

    # --- Gate 4: universe -------------------------------------------------
    ok = proposal.underlying in UNIVERSE
    g.append(GateResult(
        name="universe", passed=ok,
        detail=f"{proposal.underlying} {'in' if ok else 'NOT in'} {UNIVERSE}",
    ))

    # --- Gate 5: expiry discipline ---------------------------------------
    ok = proposal.expiry == TARGET_EXPIRY
    g.append(GateResult(
        name="expiry", passed=ok,
        detail=f"{proposal.expiry} vs required {TARGET_EXPIRY} (settles before the deadline)",
    ))

    # --- Gate 6: defined risk --------------------------------------------
    ok = proposal.has_valid_structure()
    kind = "credit" if proposal.is_credit else "debit"
    g.append(GateResult(
        name="defined_risk", passed=ok,
        detail=(f"{proposal.sleeve}/{kind}: short {proposal.short_strike} / "
                f"long {proposal.long_strike} width {proposal.width} -- "
                + ("bounded loss" if ok else f"strike order invalid for a {kind} spread")),
    ))

    # --- Gate 7: price sanity --------------------------------------------
    # A credit above the width is free money; a debit above the width is paying
    # more than the structure can ever return. Either means bad data or a
    # hallucinated price. Reject rather than discover it at fill time.
    ok = 0 < proposal.net_price < proposal.width
    g.append(GateResult(
        name="price_sanity", passed=ok,
        detail=(f"{kind} {proposal.net_price} must be > 0 and < width {proposal.width}"),
    ))

    # --- Gate 8: regime direction ----------------------------------------
    # Selling puts into a downtrend is how short-premium accounts die. The
    # agent's own regime call is what forbids it.
    ok = regime_mod.direction_allowed(regime, proposal.right, proposal.sleeve)
    pol = regime_mod.policy_for(regime)
    permitted = (pol.satellite_rights if proposal.sleeve == "satellite"
                 else pol.allowed_rights)
    g.append(GateResult(
        name="regime_direction", passed=ok,
        detail=(f"{regime} permits {'/'.join(permitted) or 'nothing'} for the "
                f"{proposal.sleeve} sleeve; proposal is {proposal.right} -- {pol.rationale}"),
    ))

    # --- Gate 9: sleeve risk budget (regime-adjusted) --------------------
    eff_pct = regime_mod.budget_pct_for(regime, proposal.sleeve, limits)
    max_tranche = equity * eff_pct
    ok = proposal.total_max_loss <= max_tranche
    g.append(GateResult(
        name="tranche_risk", passed=ok,
        detail=(f"max loss {proposal.total_max_loss:,.0f} vs {proposal.sleeve} budget "
                f"{max_tranche:,.0f} ({eff_pct:.2%} of equity, "
                f"{pol.size_multiplier:.0%} regime multiplier)"),
    ))

    # --- Gate 10: per-underlying concentration ---------------------------
    same = [p for p in open_positions
            if str(p.get("symbol", "")).startswith(proposal.underlying)]
    exposure = sum(abs(float(p.get("market_value", 0) or 0)) for p in same)
    cap = equity * limits.max_underlying_notional_pct
    ok = exposure + proposal.total_max_loss <= cap
    g.append(GateResult(
        name="concentration", passed=ok,
        detail=(f"{proposal.underlying} exposure {exposure:,.0f} + "
                f"{proposal.total_max_loss:,.0f} vs cap {cap:,.0f}"),
    ))

    # --- Gate 11: position count -----------------------------------------
    ok = len(open_positions) < limits.max_concurrent_positions
    g.append(GateResult(
        name="position_count", passed=ok,
        detail=f"{len(open_positions)} open vs max {limits.max_concurrent_positions}",
    ))

    # --- Gate 12: no-trade window ----------------------------------------
    blocked = in_no_trade_window(now, limits)
    g.append(GateResult(
        name="trading_window", passed=not blocked,
        detail=f"{now:%H:%M} {'inside' if blocked else 'outside'} the no-trade window",
    ))

    # --- Gate 13: liquidity ----------------------------------------------
    g.append(_liquidity_gate(proposal, chain, limits))

    # --- Gate 14: short-leg delta ----------------------------------------
    g.append(_delta_gate(proposal, chain, limits))

    # --- Gate 15: portfolio delta ----------------------------------------
    g.append(_portfolio_delta_gate(
        proposal, open_positions, chains or {}, quotes or {}, equity, limits))

    return g


def _liquidity_gate(proposal: TradeProposal, chain: dict, limits: RiskLimits) -> GateResult:
    """Both legs must be real, quoted, and tight.

    Doubles as P&L credibility: Alpaca paper can fill wide-spread illiquid
    contracts unrealistically well, and judges from Alpaca's own trading desk
    would spot a P&L built on that.
    """
    problems: list[str] = []
    for label, strike in (("short", proposal.short_strike), ("long", proposal.long_strike)):
        sym = occ_symbol(proposal.underlying, proposal.expiry, proposal.right, strike)
        snap = chain.get(sym)
        if not snap:
            problems.append(f"{label} leg {sym} not in chain")
            continue
        q = snap.get("latestQuote") or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        if bid <= 0 or ask <= 0:
            problems.append(f"{label} leg {sym} unquoted (bid={bid}, ask={ask})")
            continue
        mid = (bid + ask) / 2
        if mid > 0 and (ask - bid) / mid > limits.max_spread_pct_of_mid:
            problems.append(
                f"{label} leg {sym} spread {(ask - bid) / mid:.1%} > "
                f"{limits.max_spread_pct_of_mid:.0%} of mid"
            )
        oi = snap.get("openInterest")
        if oi is not None and int(oi) < limits.min_open_interest:
            problems.append(f"{label} leg {sym} OI {oi} < {limits.min_open_interest}")

    if problems:
        return GateResult(name="liquidity", passed=False, detail="; ".join(problems))
    return GateResult(name="liquidity", passed=True,
                      detail="both legs quoted with acceptable spreads")


def _delta_gate(proposal: TradeProposal, chain: dict, limits: RiskLimits) -> GateResult:
    """The short strike must sit inside the delta band the strategy claims.

    The prompt has always instructed a 0.25-0.30 short delta, and nothing
    enforced it: on 2026-08-28 a 0.304-delta call passed every gate because
    none of them looked. A limit that lives only in the prompt is the exact
    failure this project argues against, so it lives here now.

    This gate records the delta in its detail whether it passes or fails, so
    every cycle's journal carries the number and drift is measurable after the
    fact rather than only when it trips.

    Missing greeks PASS with a note, deliberately. This is a strategy
    conformance gate, not a solvency gate -- max loss is already bounded by
    defined_risk and tranche_risk regardless of delta -- so a data hiccup
    should not halt trading for the session. The gates that bound money still
    fail closed.
    """
    sym = occ_symbol(proposal.underlying, proposal.expiry, proposal.right,
                     proposal.short_strike)
    snap = chain.get(sym) or {}
    raw = (snap.get("greeks") or {}).get("delta")
    if raw is None:
        return GateResult(name="delta_band", passed=True,
                          detail=f"no delta published for short leg {sym}; "
                                 "band unenforced this cycle")

    d = abs(float(raw))
    lo, hi = limits.min_short_delta, limits.max_short_delta
    inside = lo <= d <= hi
    return GateResult(
        name="delta_band", passed=inside,
        detail=(f"short {proposal.short_strike:g}{proposal.right} delta {d:.3f} "
                f"{'within' if inside else 'OUTSIDE'} [{lo:.2f}, {hi:.2f}]"
                + ("" if inside else
                   f"; {'too far OTM to earn its risk' if d < lo else 'too close to the money'}")),
    )


def _spot(underlying: str, quotes: dict) -> float | None:
    q = quotes.get(underlying) or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    return (bid + ask) / 2 if bid > 0 and ask > 0 else None


def _leg_delta(symbol: str, chains: dict) -> float | None:
    """Published delta for one OCC symbol, from whichever chain holds it."""
    for chain in chains.values():
        snap = chain.get(symbol)
        if snap:
            d = (snap.get("greeks") or {}).get("delta")
            return float(d) if d is not None else None
    return None


def _portfolio_delta_gate(
    proposal: TradeProposal, open_positions: list[dict], chains: dict,
    quotes: dict, equity: float, limits: RiskLimits,
) -> GateResult:
    """Directional exposure across the whole book, not one ticker at a time.

    Every other limit here is per-trade or per-underlying. That is how three
    short call spreads in SPY, QQQ and IWM -- each comfortably inside
    tranche_risk, concentration and delta_band -- became a single directional
    bet that lost together on 2026-09-02 for -4,010.

    Exposure is the signed sum of leg deltas in dollars, |sum(delta x qty x
    100 x spot)|, over equity. The proposal's own legs are added to what is
    already held, so the gate blocks the marginal trade that tips the book
    over the cap rather than judging it in isolation.

    Missing greeks pass with a note, as with delta_band: this cannot be
    computed without published deltas, and halting a session over a data gap
    is worse than the exposure it would prevent. The gates that bound the loss
    on any single trade -- defined_risk and tranche_risk -- still fail closed.
    """
    if equity <= 0:
        return GateResult(name="portfolio_delta", passed=True,
                          detail="no equity reported; exposure unmeasurable")

    held = 0.0
    unpriced: list[str] = []
    for p in open_positions:
        sym = str(p.get("symbol", ""))
        m = OCC.match(sym)
        if not m:
            continue
        d = _leg_delta(sym, chains)
        spot = _spot(m.group(1), quotes)
        if d is None or spot is None:
            unpriced.append(sym)
            continue
        held += d * float(p.get("qty") or 0) * 100.0 * spot

    spot = _spot(proposal.underlying, quotes)
    short_d = _leg_delta(occ_symbol(proposal.underlying, proposal.expiry,
                                    proposal.right, proposal.short_strike), chains)
    long_d = _leg_delta(occ_symbol(proposal.underlying, proposal.expiry,
                                   proposal.right, proposal.long_strike), chains)
    if spot is None or short_d is None or long_d is None:
        return GateResult(
            name="portfolio_delta", passed=True,
            detail="proposal legs have no published delta; exposure unenforced this cycle")

    # Short leg is sold (negative contracts), long leg bought (positive).
    marginal = (long_d - short_d) * proposal.qty * 100.0 * spot
    total = held + marginal
    ratio = abs(total) / equity
    ok = ratio <= limits.max_portfolio_delta

    note = f"; {len(unpriced)} held leg(s) unpriced" if unpriced else ""
    return GateResult(
        name="portfolio_delta", passed=ok,
        detail=(f"book {total:+,.0f} of {equity:,.0f} equity = {ratio:.2f}x "
                f"{'within' if ok else 'OVER'} {limits.max_portfolio_delta:.1f}x "
                f"(held {held:+,.0f}, this trade {marginal:+,.0f}){note}"),
    )


def all_passed(gates: list[GateResult]) -> bool:
    return all(g.passed for g in gates)


def blockers(gates: list[GateResult]) -> list[GateResult]:
    return [g for g in gates if not g.passed]
