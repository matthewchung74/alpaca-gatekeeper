"""Deterministic risk gates.

The LLM proposes; this module disposes. Every gate is a pure function of the
proposal plus observed account and market state -- no model output is trusted
for anything that bounds risk. A proposal must clear *every* gate to reach the
broker. Gate zero is the account guard, which cannot be reached by any prompt.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import NamedTuple

from . import regime as regime_mod
from .config import (
    COMPETITION_PROFILE, ET, TARGET_EXPIRY, UNIVERSE, RiskLimits,
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
    target_expiry: str | None = None,
    open_spreads: list[dict] | None = None,
    tape: regime_mod.TapeRead | None = None,
    open_marks: dict | None = None,
    recent_spreads: list[dict] | None = None,
    session: tuple | None = None,
    book_regime: str | None = None,
) -> list[GateResult]:
    """Run every gate. Order matters only for readability; all of them run."""
    g: list[GateResult] = []
    if tape is not None:
        regime = tape.regime
    spot = _mid((quotes or {}).get(proposal.underlying) or {})

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
    required = target_expiry or TARGET_EXPIRY
    ok = proposal.expiry == required
    g.append(GateResult(
        name="expiry", passed=ok,
        detail=f"{proposal.expiry} vs required {required}",
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
    # Selling puts into a downtrend is how short-premium accounts die. In a
    # range, selling the side the tape just moved away from is how the
    # 2026-09-01 book died: bottom of the range forbids short calls, top
    # forbids short puts. Computed from the bars, never from the model.
    pol = regime_mod.policy_for(regime)
    if tape is not None and proposal.sleeve == "core":
        permitted = regime_mod.core_sides(tape, limits)
        why = (pol.rationale if tape.regime != "sideways" or tape.range_position is None
               else f"range position {tape.range_position:.0%}")
    else:
        permitted = (pol.satellite_rights if proposal.sleeve == "satellite"
                     else pol.allowed_rights)
        why = pol.rationale
    ok = proposal.right in permitted
    g.append(GateResult(
        name="regime_direction", passed=ok,
        detail=(f"{regime} permits {'/'.join(permitted) or 'nothing'} for the "
                f"{proposal.sleeve} sleeve; proposal is {proposal.right} -- {why}"),
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
    blocked = in_no_trade_window(now, limits, session)
    g.append(GateResult(
        name="trading_window", passed=not blocked,
        detail=f"{now:%H:%M} {'inside' if blocked else 'outside'} the no-trade window",
    ))

    # --- Gate 13: liquidity ----------------------------------------------
    g.append(_liquidity_gate(proposal, chain, limits, now))

    # --- Gate 14: short-leg delta ----------------------------------------
    g.append(_delta_gate(proposal, chain, limits))

    # --- Gate 15: directional risk ---------------------------------------
    g.append(_directional_risk_gate(proposal, open_spreads or [], equity, limits))

    # --- Gate 16: strike placement --------------------------------------
    g.append(_range_buffer_gate(proposal, tape, chain, spot, now, limits))

    # --- Gate 17: premium floor -----------------------------------------
    g.append(_credit_floor_gate(proposal, limits))

    # --- Gates 18-21: the shape of the whole book ------------------------
    g.append(_book_risk_gate(proposal, open_spreads or [], equity,
                             book_regime or regime, limits))
    g.append(_same_direction_gate(proposal, open_spreads or [], limits))
    g.append(_losing_side_gate(proposal, open_spreads or [], open_marks or {}, limits))
    g.append(_cadence_gate(proposal, recent_spreads or [], now, limits))

    return g


def _quote_age_minutes(t, now: datetime) -> float | None:
    """Age of an Alpaca quote timestamp (RFC 3339 with nanoseconds)."""
    if not t:
        return None
    s = str(t).replace("Z", "+00:00")
    if "." in s:                                   # trim nanoseconds to microseconds
        head, _, rest = s.partition(".")
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return (now - ts).total_seconds() / 60.0


def _liquidity_gate(proposal: TradeProposal, chain: dict, limits: RiskLimits,
                    now: datetime) -> GateResult:
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
        # Codex review 2026-09-18: a crossed market stamped 2020 with no open
        # interest used to pass. A quote is only a market if it is ordered,
        # recent, and someone is actually there.
        if ask < bid:
            problems.append(f"{label} leg {sym} crossed (bid {bid} > ask {ask})")
            continue
        age = _quote_age_minutes(q.get("t"), now)
        if age is None:
            problems.append(f"{label} leg {sym} quote has no timestamp")
        elif age > limits.max_quote_age_minutes:
            problems.append(f"{label} leg {sym} quote is stale ({age:.0f} min old)")
        if float(q.get("bs") or 0) <= 0 or float(q.get("as") or 0) <= 0:
            problems.append(f"{label} leg {sym} has no displayed size")
        mid = (bid + ask) / 2
        if mid > 0 and (ask - bid) / mid > limits.max_spread_pct_of_mid:
            problems.append(
                f"{label} leg {sym} spread {(ask - bid) / mid:.1%} > "
                f"{limits.max_spread_pct_of_mid:.0%} of mid"
            )
        # The chain snapshot never carries open interest; the loop fetches it
        # from the contracts endpoint before the gates. Missing fails closed:
        # for two weeks this check passed because the number was never there.
        oi = snap.get("openInterest")
        if oi is None:
            problems.append(f"{label} leg {sym} open interest unknown")
        elif int(oi) < limits.min_open_interest:
            problems.append(f"{label} leg {sym} open interest {oi} < {limits.min_open_interest}")

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


def _spread_max_loss(row: dict) -> float:
    """Worst case on one journaled spread, in dollars."""
    width = abs(float(row["short_strike"]) - float(row["long_strike"]))
    credit = float(row.get("entry_credit") or 0)
    qty = int(row.get("qty") or 0)
    # core sells premium and can lose the width less the credit; satellite pays
    # a debit and can only lose that debit.
    per = (width - credit) if (row.get("sleeve") or "core") == "core" else credit
    return max(per, 0.0) * 100.0 * qty


def _hurt_by_a_rally(right: str, sleeve: str) -> bool:
    """Which way does this structure lose?

    A short call spread loses when the market rises; a short put spread when it
    falls. The satellite sleeve buys direction, so it is the other way round.
    """
    if (sleeve or "core") == "core":
        return right == "C"
    return right == "P"


def _directional_risk_gate(
    proposal: TradeProposal, open_spreads: list[dict], equity: float,
    limits: RiskLimits,
) -> GateResult:
    """How much of the account is at risk in one direction.

    Every other limit here is per-trade or per-underlying. That is how three
    short call spreads in SPY, QQQ and IWM -- each comfortably inside
    tranche_risk, concentration and delta_band -- became one directional bet
    that lost together on 2026-09-02 for -4,010.

    Exposure is each position's MAX LOSS, signed by the move that would cause
    it: call spreads positive, put spreads negative. Holding both sides nets
    toward zero, which is right -- an iron condor can only lose one wing. The
    proposal is added to what is already held, so the gate blocks the marginal
    trade that tips the book over rather than judging it alone.

    Max loss rather than notional delta, deliberately. Delta scales with the
    width of the structure while the loss does not: a 5-wide spread carries far
    more delta than a 2-wide one and can lose no more than its own width. An
    earlier delta version of this gate blocked a $9,416 trade that tranche_risk
    was happy with, purely for being wide.
    """
    if equity <= 0:
        return GateResult(name="directional_risk", passed=True,
                          detail="no equity reported; exposure unmeasurable")

    # Each tail on its own. This used to subtract the put wing from the call
    # wing, so a 900 put spread and a 900 call spread reported zero -- but a
    # condor still loses a whole wing whichever way the market runs, and
    # across three tickers and two expiries the wings do not even share a
    # payoff. Opposite sides never offset here.
    rally = selloff = 0.0
    for row in open_spreads:
        try:
            loss = _spread_max_loss(row)
            up = _hurt_by_a_rally(row["right"], row.get("sleeve"))
        except (KeyError, TypeError, ValueError):
            continue
        if up:
            rally += loss
        else:
            selloff += loss

    mine_up = _hurt_by_a_rally(proposal.right, proposal.sleeve)
    if mine_up:
        rally += proposal.total_max_loss
    else:
        selloff += proposal.total_max_loss
    side, amount = ("a rally", rally) if mine_up else ("a selloff", selloff)
    ratio = amount / equity
    ok = ratio <= limits.max_directional_risk_pct

    return GateResult(
        name="directional_risk", passed=ok,
        detail=(f"{amount:,.0f} of {equity:,.0f} equity = {ratio:.1%} at risk on "
                f"{side} with this trade, {'within' if ok else 'OVER'} "
                f"{limits.max_directional_risk_pct:.0%} "
                f"(book after: rally {rally:,.0f}, selloff {selloff:,.0f}; sides never net)"),
    )

def _mid(q: dict) -> float | None:
    try:
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    except (TypeError, ValueError):
        return None
    return (bid + ask) / 2 if bid > 0 and ask > 0 else None


class Clearance(NamedTuple):
    ok: bool
    dist: float        # short strike's distance from spot, signed so OTM is positive
    need: float        # expected_move_multiple x expected move
    em: float          # one expected move to expiry, in points
    inside: bool       # strike inside the lookback high-low


def range_clearance(right: str, strike: float, spot: float, iv: float, dte: int,
                    tape, limits: RiskLimits) -> Clearance:
    """Does a short strike clear the range AND one expected move?

    One function, used by the gate and by the snapshot, so the number the
    model is shown is the number the gate checks. On 2026-09-09 the model
    sized the move from ATM vol and the gate from the put's own skewed vol;
    the strike was two points short and the cycle was wasted.
    """
    em = spot * float(iv) * math.sqrt(max(dte, 1) / 365.0)
    need = limits.expected_move_multiple * em
    dist = (strike - spot) if right == "C" else (spot - strike)
    # The range veto only makes sense in a range. In a trend the old extreme
    # is stale: after a 3-4% decline the 10-session high sits so far above
    # spot that no call strike under it clears the delta floor, and the core
    # had no legal trade for a week (2026-09-10 to 09-17). One expected move
    # is the whole test there.
    if tape.regime == "sideways":
        inside = (strike <= tape.lookback_high) if right == "C" else (strike >= tape.lookback_low)
    else:
        inside = False
    return Clearance(dist >= need and not inside, dist, need, em, inside)


def _range_buffer_gate(proposal: TradeProposal, tape, chain: dict,
                       spot: float | None, now: datetime, limits: RiskLimits) -> GateResult:
    """The short strike must sit outside the recent range AND one expected move out.

    Eight of nine hackathon short strikes were inside the prior five sessions'
    high-low, 0.5-1.0% from spot at 1-4 DTE. Six finished in the money. Delta
    alone cannot place a strike outside the noise at short DTE; this can.

    Fails closed. A strike we cannot place relative to the tape is a strike
    we do not sell.
    """
    if not proposal.is_credit:
        # The satellite buys direction and can only lose its debit, which
        # tranche_risk already bounds. Judging its far leg against an expected
        # move is a credit-spread rule applied to the wrong structure.
        return GateResult(name="range_buffer", passed=True, detail="debit spread; not applied")
    if tape is None or tape.lookback_high is None or tape.lookback_low is None:
        return GateResult(name="range_buffer", passed=False,
                          detail="no completed-session range available; cannot place the strike")
    if spot is None:
        return GateResult(name="range_buffer", passed=False,
                          detail=f"no quote for {proposal.underlying}; cannot measure distance")
    sym = occ_symbol(proposal.underlying, proposal.expiry, proposal.right, proposal.short_strike)
    iv = (chain.get(sym) or {}).get("impliedVolatility")
    if iv is None:
        return GateResult(name="range_buffer", passed=False,
                          detail=f"no IV published for short leg {sym}; cannot size the expected move")
    dte = max((date.fromisoformat(proposal.expiry) - now.date()).days, 1)
    k = proposal.short_strike
    c = range_clearance(proposal.right, k, spot, float(iv), dte, tape, limits)
    dist, need = c.dist, c.need
    problems: list[str] = []
    if dist < need:
        problems.append(f"{dist:.2f} from spot < {limits.expected_move_multiple:g}x "
                        f"expected move {c.em:.2f} ({dte} DTE, IV {float(iv):.1%})")
    if c.inside:
        problems.append(f"short {k:g} inside the {limits.range_lookback}-session range "
                        f"{tape.lookback_low:.2f}-{tape.lookback_high:.2f}")
    if problems:
        return GateResult(name="range_buffer", passed=False, detail="; ".join(problems))
    return GateResult(name="range_buffer", passed=True,
                      detail=(f"short {k:g} is {dist:.2f} from spot {spot:.2f} "
                              f"(>= {need:.2f}) and outside {tape.lookback_low:.2f}-"
                              f"{tape.lookback_high:.2f}"))


def _credit_floor_gate(proposal: TradeProposal, limits: RiskLimits) -> GateResult:
    """Premium must be worth the width. Satellite pays a debit; not its concern."""
    if not proposal.is_credit:
        return GateResult(name="credit_floor", passed=True, detail="debit spread; no floor")
    frac = proposal.net_price / proposal.width if proposal.width > 0 else 0.0
    ok = frac >= limits.min_credit_pct_of_width
    return GateResult(
        name="credit_floor", passed=ok,
        detail=(f"credit {proposal.net_price:.2f} is {frac:.0%} of width {proposal.width:g} "
                f"({'>=' if ok else '<'} {limits.min_credit_pct_of_width:.0%})"))


def _book_risk_gate(proposal: TradeProposal, open_spreads: list[dict], equity: float,
                    regime: str, limits: RiskLimits) -> GateResult:
    """Open max loss plus this trade, against a book budget the regime scales.

    tranche_risk caps one trade; nothing capped the sum. Under "bear" the
    tranche budget fell to 35% and the book simply opened four tranches.
    """
    held = 0.0
    for row in open_spreads:
        try:
            held += _spread_max_loss(row)
        except (KeyError, TypeError, ValueError):
            continue
    mult = regime_mod.policy_for(regime).size_multiplier
    cap = equity * limits.max_book_risk_pct * mult
    total = held + proposal.total_max_loss
    ok = total <= cap
    return GateResult(
        name="book_risk", passed=ok,
        detail=(f"open max loss {held:,.0f} + this trade {proposal.total_max_loss:,.0f} = "
                f"{total:,.0f} vs book budget {cap:,.0f} "
                f"({limits.max_book_risk_pct:.0%} of equity x {mult:.0%} {regime})"))


def _same_direction_gate(proposal: TradeProposal, open_spreads: list[dict],
                         limits: RiskLimits) -> GateResult:
    """SPY, QQQ and IWM are one bucket. Count open core spreads on this right."""
    same = [r for r in open_spreads
            if r.get("right") == proposal.right and (r.get("sleeve") or "core") == "core"]
    ok = proposal.sleeve != "core" or len(same) < limits.max_same_direction
    names = ", ".join(f"{r.get('underlying')} {float(r.get('short_strike')):g}/"
                      f"{float(r.get('long_strike')):g}" for r in same) or "none"
    return GateResult(
        name="same_direction", passed=ok,
        detail=(f"{len(same)} open core {proposal.right} spread(s) across the universe "
                f"({names}) vs max {limits.max_same_direction}"))


def _losing_side_gate(proposal: TradeProposal, open_spreads: list[dict],
                      open_marks: dict, limits: RiskLimits) -> GateResult:
    """No adding to a side that is already being run over.

    On 2026-09-02 at 11:46 ET a third short call spread was opened while the
    first two marked about twice their credit. The snapshot showed the model
    those positions; it added anyway. This is the gate that says no.
    """
    if proposal.sleeve != "core":
        return GateResult(name="losing_side", passed=True, detail="satellite; not applied")
    for r in open_spreads:
        if r.get("right") != proposal.right or (r.get("sleeve") or "core") != "core":
            continue
        mark = open_marks.get(r.get("id"))
        credit = float(r.get("entry_credit") or 0)
        if mark is None or credit <= 0:
            continue
        ratio = float(mark) / credit
        if ratio >= limits.losing_side_multiple:
            return GateResult(
                name="losing_side", passed=False,
                detail=(f"{r.get('underlying')} {float(r.get('short_strike')):g}/"
                        f"{float(r.get('long_strike')):g} {proposal.right} marks {float(mark):.2f} = "
                        f"{ratio:.2f}x its {credit:.2f} credit (>= {limits.losing_side_multiple:g}x); "
                        "not adding to a losing side"))
    return GateResult(name="losing_side", passed=True,
                      detail=f"no open {proposal.right} spread at or beyond "
                             f"{limits.losing_side_multiple:g}x its credit")


def _ts(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=ET)


def cadence_state(recent: list[dict], now: datetime, limits: RiskLimits) -> tuple[int, list[tuple[str, str, datetime]]]:
    """Entries made today, and (underlying, right, cooldown-ends) for recent closes.

    Shared with the snapshot so the model can route around a cooldown instead
    of walking into it: on 2026-09-10 two of five cycles re-proposed the name
    that had just closed.
    """
    today = now.astimezone(ET).date()
    opened_today = sum(1 for r in recent
                       if (t := _ts(r.get("ts_open"))) and t.astimezone(ET).date() == today)
    window = timedelta(hours=limits.reentry_cooldown_hours)
    cooling: list[tuple[str, str, datetime]] = []
    for r in recent:
        closed = _ts(r.get("ts_close"))
        if closed and now - closed < window:
            cooling.append((str(r.get("underlying")), str(r.get("right")), closed + window))
    return opened_today, cooling


def _cadence_gate(proposal: TradeProposal, recent: list[dict], now: datetime,
                  limits: RiskLimits) -> GateResult:
    """One entry a day, and no re-entry where a spread just closed.

    Four cycles a day produced a proposal in every cycle with budget, and each
    50% target exit was recycled the same day into a closer, shorter-dated
    spread. Frequency was the strategy's variance, not its edge.
    """
    today = now.astimezone(ET).date()
    opened_today = [r for r in recent
                    if (t := _ts(r.get("ts_open"))) and t.astimezone(ET).date() == today]
    if len(opened_today) >= limits.max_entries_per_day:
        return GateResult(
            name="cadence", passed=False,
            detail=(f"{len(opened_today)} entr{'y' if len(opened_today) == 1 else 'ies'} "
                    f"already today vs max {limits.max_entries_per_day}"))
    window = timedelta(hours=limits.reentry_cooldown_hours)
    for r in recent:
        if r.get("underlying") != proposal.underlying or r.get("right") != proposal.right:
            continue
        closed = _ts(r.get("ts_close"))
        if closed and now - closed < window:
            return GateResult(
                name="cadence", passed=False,
                detail=(f"{proposal.underlying} {proposal.right} spread closed "
                        f"{closed.astimezone(ET):%m-%d %H:%M ET}, inside the "
                        f"{limits.reentry_cooldown_hours}h cooldown"))
    return GateResult(name="cadence", passed=True,
                      detail=(f"{len(opened_today)} entries today; no {proposal.underlying} "
                              f"{proposal.right} close in the last {limits.reentry_cooldown_hours}h"))


def all_passed(gates: list[GateResult]) -> bool:
    return all(g.passed for g in gates)


def blockers(gates: list[GateResult]) -> list[GateResult]:
    return [g for g in gates if not g.passed]
