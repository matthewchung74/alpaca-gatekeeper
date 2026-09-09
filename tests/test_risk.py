from datetime import datetime

import pytest

from agent.config import (
    ET, KICKOFF, RiskLimits, TARGET_EXPIRY, AccountGuardError, assert_may_trade,
)
from agent.models import TradeProposal, occ_symbol
from agent.regime import TapeRead
from agent import risk

LIMITS = RiskLimits()
MIDDAY = datetime(2026, 8, 28, 13, 0, tzinfo=ET)   # after kickoff, mid-session
WEEK_OUT = "2026-09-04"       # 7 days after MIDDAY


def tape(**kw) -> TapeRead:
    base = dict(regime="sideways", range_position=0.5, lookback_high=770.0,
                lookback_low=750.0, trend_pct=0.0, avg_range_pct=0.008, detail="t")
    base.update(kw)
    return TapeRead(**base)


def gate(gates, name):
    return next(g for g in gates if g.name == name)


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


def chain_with_iv(p: TradeProposal, iv=0.15, **kw) -> dict:
    ch = make_chain(p, **kw)
    for snap in ch.values():
        snap["impliedVolatility"] = iv
    return ch


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
    p = make_proposal(expiry=WEEK_OUT, short_strike=740.0, long_strike=735.0, net_price=1.10)
    gates = evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                     tape=tape(), target_expiry=WEEK_OUT)
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


# --- post-mortem: strikes lived inside the range at 1-4 DTE ---------------

def test_expiry_floor_is_a_week_out():
    """Every hackathon trade was 1-4 DTE, where a 0.25-delta strike sits
    inside one ordinary day's range. Seven days puts it outside."""
    from agent.config import MIN_DAYS_TO_EXPIRY
    assert MIN_DAYS_TO_EXPIRY >= 7


def test_delta_band_floor_admits_one_expected_move():
    """One expected move at 7-14 DTE is roughly 0.16 delta; the old 0.20
    floor would reject every strike the range gate permits."""
    assert LIMITS.min_short_delta <= 0.10


# --- strike placement and premium ------------------------------------------

def test_range_buffer_blocks_a_strike_inside_the_recent_range():
    # spot 760, 10-session range 750-770: a 752 put is inside it
    p = make_proposal(expiry=WEEK_OUT, short_strike=752.0, long_strike=747.0)
    g = gate(evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                      tape=tape()), "range_buffer")
    assert not g.passed and "inside" in g.detail


def test_range_buffer_blocks_a_strike_inside_one_expected_move():
    # 7 DTE at 15% IV: expected move = 760 * 0.15 * sqrt(7/365) ~ 15.8
    # 748 is outside the 750-770 range but only 12 from spot
    p = make_proposal(expiry=WEEK_OUT, short_strike=748.0, long_strike=743.0)
    g = gate(evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                      tape=tape()), "range_buffer")
    assert not g.passed and "expected move" in g.detail


def test_range_buffer_passes_a_strike_beyond_both():
    p = make_proposal(expiry=WEEK_OUT, short_strike=740.0, long_strike=735.0)
    g = gate(evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                      tape=tape()), "range_buffer")
    assert g.passed, g.detail


def test_range_buffer_fails_closed_without_history_or_iv():
    p = make_proposal(expiry=WEEK_OUT, short_strike=740.0, long_strike=735.0)
    q = {"SPY": {"bp": 759.9, "ap": 760.1}}
    no_hist = gate(evaluate(p, chain=chain_with_iv(p), quotes=q,
                            tape=tape(lookback_high=None, lookback_low=None)), "range_buffer")
    no_iv = gate(evaluate(p, chain=make_chain(p), quotes=q, tape=tape()), "range_buffer")
    no_tape = gate(evaluate(p, chain=chain_with_iv(p), quotes=q), "range_buffer")
    assert not no_hist.passed and not no_iv.passed and not no_tape.passed


def test_credit_floor_rejects_thin_premium():
    p = make_proposal(net_price=0.47)          # 5-wide: 9.4% of width
    assert not gate(evaluate(p), "credit_floor").passed


def test_credit_floor_passes_a_fair_credit():
    p = make_proposal(net_price=1.10)          # 22% of width
    assert gate(evaluate(p), "credit_floor").passed


def test_credit_floor_ignores_the_satellite():
    p = make_proposal(sleeve="satellite", short_strike=747.0, long_strike=752.0, net_price=0.47)
    assert gate(evaluate(p), "credit_floor").passed


# --- direction at the edges of a range ---------------------------------------

def test_direction_gate_forbids_short_calls_at_the_bottom_of_a_range():
    """The 2026-09-01 trade: sideways tape, spot at the range low, sell calls."""
    p = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    g = gate(evaluate(p, tape=tape(range_position=0.02)), "regime_direction")
    assert not g.passed and "range" in g.detail


def test_direction_gate_forbids_short_puts_at_the_top_of_a_range():
    p = make_proposal(right="P")
    g = gate(evaluate(p, tape=tape(range_position=0.95)), "regime_direction")
    assert not g.passed


def test_direction_gate_permits_puts_at_the_bottom_of_a_range():
    p = make_proposal(right="P")
    assert gate(evaluate(p, tape=tape(range_position=0.02)), "regime_direction").passed


def test_direction_gate_keeps_the_bear_rule_when_the_tape_is_a_trend():
    p = make_proposal(right="P")
    g = gate(evaluate(p, tape=tape(regime="bear", range_position=0.02)), "regime_direction")
    assert not g.passed


# --- the shape of the whole book ---------------------------------------------

def row(**kw) -> dict:
    base = dict(id="r1", underlying="QQQ", right="C", sleeve="core", short_strike=740.0,
                long_strike=743.0, qty=10, entry_credit=0.60, status="open",
                ts_open="2026-08-27T15:46:00+00:00", ts_close=None)
    base.update(kw)
    return base


def test_book_risk_caps_the_whole_book_not_the_tranche():
    # 24% of 100k = 24,000. Held 20,250; a 4,983 proposal tips it over.
    held = [row(id="a", entry_credit=0.5, short_strike=740.0, long_strike=745.0, qty=45)]
    p = make_proposal(qty=11)
    g = gate(evaluate(p, open_spreads=held), "book_risk")
    assert not g.passed and "24%" in g.detail


def test_book_risk_scales_with_the_regime_multiplier():
    held = [row(id="a", entry_credit=0.5, short_strike=740.0, long_strike=745.0, qty=15)]  # 6,750
    p = make_proposal(qty=5)                        # 2,265 -> 9,015 total
    assert gate(evaluate(p, open_spreads=held), "book_risk").passed                     # 24,000
    assert not gate(evaluate(p, open_spreads=held, regime="bear"), "book_risk").passed  # 8,400


def test_same_direction_caps_open_spreads_on_one_right_across_the_universe():
    held = [row(id="a", underlying="QQQ", right="C"), row(id="b", underlying="IWM", right="C")]
    p = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    g = gate(evaluate(p, open_spreads=held), "same_direction")
    assert not g.passed and "2 open" in g.detail
    assert gate(evaluate(make_proposal(right="P"), open_spreads=held), "same_direction").passed


def test_losing_side_blocks_adding_to_a_side_already_underwater():
    """09-02 11:46: QQQ calls added while IWM and SPY calls marked ~2x credit."""
    held = [row(id="a", underlying="IWM", right="C", entry_credit=0.39)]
    p = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    losing = gate(evaluate(p, open_spreads=held, open_marks={"a": 0.80}), "losing_side")
    fine = gate(evaluate(p, open_spreads=held, open_marks={"a": 0.40}), "losing_side")
    no_mark = gate(evaluate(p, open_spreads=held, open_marks={}), "losing_side")
    assert not losing.passed and "2.05x" in losing.detail
    assert fine.passed and no_mark.passed


def test_cadence_allows_one_entry_per_day():
    today = [row(id="a", ts_open="2026-08-28T15:46:00+00:00")]      # 11:46 ET on MIDDAY's date
    g = gate(evaluate(make_proposal(), recent_spreads=today), "cadence")
    assert not g.passed and "1 entr" in g.detail
    yesterday = [row(id="a", ts_open="2026-08-27T15:46:00+00:00")]
    assert gate(evaluate(make_proposal(), recent_spreads=yesterday), "cadence").passed


def test_cadence_cools_down_after_a_close_in_the_same_name_and_side():
    closed = [row(id="a", underlying="SPY", right="P", status="closed",
                  ts_open="2026-08-27T15:46:00+00:00", ts_close="2026-08-28T14:00:00+00:00")]
    g = gate(evaluate(make_proposal(right="P"), recent_spreads=closed), "cadence")
    assert not g.passed and "closed" in g.detail
    other_side = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    assert gate(evaluate(other_side, recent_spreads=closed), "cadence").passed
    old = [row(id="a", underlying="SPY", right="P", status="closed",
               ts_open="2026-08-25T15:46:00+00:00", ts_close="2026-08-26T14:00:00+00:00")]
    assert gate(evaluate(make_proposal(right="P"), recent_spreads=old), "cadence").passed
