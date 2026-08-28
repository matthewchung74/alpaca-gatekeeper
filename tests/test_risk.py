from datetime import datetime

import pytest

from agent.config import (
    ET, KICKOFF, RiskLimits, TARGET_EXPIRY, AccountGuardError, assert_may_trade,
)
from agent.models import TradeProposal, occ_symbol
from agent import risk

LIMITS = RiskLimits()
MIDDAY = datetime(2026, 8, 28, 13, 0, tzinfo=ET)   # after kickoff, mid-session


def make_proposal(**kw) -> TradeProposal:
    base = dict(
        underlying="SPY", expiry=TARGET_EXPIRY, right="P",
        short_strike=752.0, long_strike=747.0, qty=5,
        net_price=0.47, sleeve="core", rationale="test",
    )
    base.update(kw)
    return TradeProposal(**base)


def make_chain(p: TradeProposal, *, bid=1.48, ask=1.54, oi=5000) -> dict:
    chain = {}
    for strike in (p.short_strike, p.long_strike):
        sym = occ_symbol(p.underlying, p.expiry, p.right, strike)
        chain[sym] = {"latestQuote": {"bp": bid, "ap": ask}, "openInterest": oi}
    return chain


def evaluate(p, **over):
    kw = dict(
        profile="dev", now=MIDDAY, equity=100_000.0, day_start_equity=100_000.0,
        open_positions=[], chain=make_chain(p), limits=LIMITS, halted=False,
    )
    kw.update(over)
    return risk.evaluate(p, **kw)


# --- symbol construction -------------------------------------------------

def test_occ_symbol_matches_alpaca_format():
    assert occ_symbol("SPY", "2026-09-03", "P", 752.0) == "SPY260903P00752000"
    assert occ_symbol("SPY", "2026-09-03", "C", 766.5) == "SPY260903C00766500"


# --- derived risk is computed, not trusted -------------------------------

def test_max_loss_derived_from_width_not_model():
    p = make_proposal(qty=5, net_price=0.47)   # 5-wide
    assert p.max_loss_per_spread == pytest.approx(453.0)
    assert p.total_max_loss == pytest.approx(2265.0)
    assert p.total_max_profit == pytest.approx(235.0)


# --- happy path ----------------------------------------------------------

def test_clean_proposal_passes_every_gate():
    p = make_proposal()
    gates = evaluate(p)
    assert risk.all_passed(gates), [str(g) for g in risk.blockers(gates)]


# --- gate zero: the account guard ---------------------------------------

def test_guard_blocks_competition_account_before_kickoff():
    with pytest.raises(AccountGuardError):
        assert_may_trade("comp", datetime(2026, 8, 28, 10, 59, tzinfo=ET))


def test_guard_allows_competition_account_at_kickoff():
    assert_may_trade("comp", KICKOFF)


def test_guard_allows_practice_account_any_time():
    assert_may_trade("dev", datetime(2026, 8, 1, 3, 0, tzinfo=ET))


def test_guard_blocks_after_deadline():
    with pytest.raises(AccountGuardError):
        assert_may_trade("comp", datetime(2026, 9, 4, 11, 1, tzinfo=ET))


def test_evaluate_reports_guard_as_failed_gate_not_exception():
    p = make_proposal()
    gates = evaluate(p, profile="comp", now=datetime(2026, 8, 27, 12, 0, tzinfo=ET))
    guard = next(g for g in gates if g.name == "account_guard")
    assert not guard.passed
    assert not risk.all_passed(gates)


# --- individual gates ----------------------------------------------------

def test_daily_loss_gate_blocks_past_threshold():
    p = make_proposal()
    breach = 100_000.0 * (1 - LIMITS.max_daily_loss_pct) - 100.0
    gates = evaluate(p, equity=breach, day_start_equity=100_000.0)
    assert not next(g for g in gates if g.name == "daily_loss").passed


def test_daily_loss_gate_allows_inside_threshold():
    p = make_proposal()
    inside = 100_000.0 * (1 - LIMITS.max_daily_loss_pct / 2)
    gates = evaluate(p, equity=inside, day_start_equity=100_000.0)
    assert next(g for g in gates if g.name == "daily_loss").passed


def test_event_drawdown_gate_blocks():
    p = make_proposal()
    breach = 100_000.0 * (1 - LIMITS.max_event_drawdown_pct) - 100.0
    gates = evaluate(p, equity=breach, day_start_equity=breach + 500)
    assert not next(g for g in gates if g.name == "event_drawdown").passed


def test_wrong_expiry_blocked():
    p = make_proposal(expiry="2026-09-11")
    assert not next(g for g in evaluate(p) if g.name == "expiry").passed


def test_underlying_outside_universe_blocked():
    p = make_proposal(underlying="TSLA")
    assert not next(g for g in evaluate(p) if g.name == "universe").passed


def test_inverted_strikes_rejected_for_a_core_credit_spread():
    # Core is a credit spread: for puts the short must be ABOVE the long.
    p = make_proposal(short_strike=747.0, long_strike=752.0, sleeve="core")
    assert not next(g for g in evaluate(p) if g.name == "defined_risk").passed


def test_same_strikes_valid_for_a_satellite_debit_spread():
    """Identical strike order that is wrong for core is RIGHT for satellite.

    A put debit spread buys the higher strike and sells the lower one.
    """
    p = make_proposal(short_strike=747.0, long_strike=752.0, sleeve="satellite")
    assert next(g for g in evaluate(p) if g.name == "defined_risk").passed


def test_price_exceeding_width_rejected():
    # A credit above the width is free money; a debit above it can never pay off.
    for sleeve, short, long_ in (("core", 752.0, 747.0), ("satellite", 747.0, 752.0)):
        p = make_proposal(net_price=6.0, sleeve=sleeve,
                          short_strike=short, long_strike=long_)
        assert not next(g for g in evaluate(p) if g.name == "price_sanity").passed, sleeve


def test_oversized_tranche_blocked():
    p = make_proposal(qty=50)   # ~22.6k max loss vs 4k budget
    gates = evaluate(p)
    assert not next(g for g in gates if g.name == "tranche_risk").passed


def test_tranche_at_budget_edge_allowed():
    # 4% of 100k = 4000; 8 spreads x 453 = 3624
    p = make_proposal(qty=8)
    assert next(g for g in evaluate(p) if g.name == "tranche_risk").passed


def test_concentration_gate_counts_existing_exposure():
    p = make_proposal()
    cap = 100_000.0 * LIMITS.max_underlying_notional_pct
    existing = [{"symbol": "SPY260903P00740000", "market_value": str(cap)}]
    gates = evaluate(p, open_positions=existing)
    assert not next(g for g in gates if g.name == "concentration").passed


def test_position_count_gate():
    p = make_proposal()
    many = [{"symbol": f"QQQ26090{i}P00500000", "market_value": "100"} for i in range(8)]
    gates = evaluate(p, open_positions=many)
    assert not next(g for g in gates if g.name == "position_count").passed


def test_halt_flag_blocks():
    p = make_proposal()
    assert not next(g for g in evaluate(p, halted=True) if g.name == "event_halt").passed


# --- trading window ------------------------------------------------------

@pytest.mark.parametrize("t,expected_pass", [
    (datetime(2026, 8, 28, 9, 31, tzinfo=ET), False),   # first 5 min
    (datetime(2026, 8, 28, 9, 36, tzinfo=ET), True),
    (datetime(2026, 8, 28, 15, 58, tzinfo=ET), False),  # last 5 min
    (datetime(2026, 8, 28, 12, 0, tzinfo=ET), True),
    (datetime(2026, 8, 28, 8, 0, tzinfo=ET), False),    # pre-market
    (datetime(2026, 8, 28, 17, 0, tzinfo=ET), False),   # after hours
])
def test_no_trade_windows(t, expected_pass):
    p = make_proposal()
    gates = evaluate(p, now=t)
    assert next(g for g in gates if g.name == "trading_window").passed is expected_pass


# --- liquidity -----------------------------------------------------------

def test_missing_leg_blocks_liquidity():
    p = make_proposal()
    assert not next(g for g in evaluate(p, chain={}) if g.name == "liquidity").passed


def test_wide_spread_blocks_liquidity():
    p = make_proposal()
    wide = make_chain(p, bid=1.00, ask=1.60)   # ~46% of mid
    assert not next(g for g in evaluate(p, chain=wide) if g.name == "liquidity").passed


def test_low_open_interest_blocks_liquidity():
    p = make_proposal()
    thin = make_chain(p, oi=10)
    assert not next(g for g in evaluate(p, chain=thin) if g.name == "liquidity").passed


def test_unquoted_leg_blocks_liquidity():
    p = make_proposal()
    dead = make_chain(p, bid=0, ask=0)
    assert not next(g for g in evaluate(p, chain=dead) if g.name == "liquidity").passed


# --- legs ----------------------------------------------------------------

def test_legs_are_sell_short_buy_long():
    p = make_proposal()
    legs = p.legs()
    assert legs[0]["symbol"] == "SPY260903P00752000"
    assert legs[0]["side"] == "sell"
    assert legs[0]["position_intent"] == "sell_to_open"
    assert legs[1]["symbol"] == "SPY260903P00747000"
    assert legs[1]["side"] == "buy"
