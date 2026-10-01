from datetime import datetime

import pytest

from agent.config import ET, RiskLimits, TARGET_EXPIRY
from agent.manage import decide_exit, mark_to_close
from agent.models import OpenSpread

LIMITS = RiskLimits()
MIDWEEK = datetime(2026, 8, 31, 12, 0, tzinfo=ET)          # not expiry day
EXPIRY_MID = datetime(2026, 9, 3, 12, 0, tzinfo=ET)        # expiry day, midday
EXPIRY_LATE = datetime(2026, 9, 3, 15, 45, tzinfo=ET)      # expiry day, near close


def spread(**kw) -> OpenSpread:
    base = dict(id=1, underlying="SPY", expiry=TARGET_EXPIRY, right="P",
                short_strike=752.0, long_strike=747.0, qty=5, entry_credit=0.47)
    base.update(kw)
    return OpenSpread(**base)


def quotes(short_ask=0.30, long_bid=0.10, s=None):
    sp = s or spread()
    return {
        sp.short_symbol(): {"ap": short_ask, "bp": short_ask - 0.02},
        sp.long_symbol(): {"ap": long_bid + 0.02, "bp": long_bid},
    }


# --- mark ----------------------------------------------------------------

def test_mark_uses_unfavourable_side_of_both_quotes():
    sp = spread()
    # pay the ask on the short (0.30), receive the bid on the long (0.10)
    assert mark_to_close(sp, quotes(0.30, 0.10)) == pytest.approx(0.20)


def test_mark_is_none_when_a_leg_is_unquoted():
    sp = spread()
    assert mark_to_close(sp, {}) is None


def test_mark_floors_at_zero():
    sp = spread()
    assert mark_to_close(sp, quotes(0.05, 0.20)) == 0.0


# --- profit target -------------------------------------------------------

def test_profit_target_fires_at_configured_fraction():
    sp = spread(entry_credit=0.47)
    target = sp.entry_credit * (1 - LIMITS.profit_target_pct)
    d = decide_exit(sp, target - 0.01, now=MIDWEEK, spot=770.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "profit_target"


def test_profit_target_does_not_fire_just_above_target():
    sp = spread(entry_credit=0.47)
    target = sp.entry_credit * (1 - LIMITS.profit_target_pct)
    d = decide_exit(sp, target + 0.02, now=MIDWEEK, spot=770.0, limits=LIMITS)
    assert d.action == "hold"


# --- stop loss -----------------------------------------------------------

def test_stop_loss_fires_at_configured_multiple():
    sp = spread(entry_credit=0.47)
    stop = min(sp.entry_credit * LIMITS.stop_loss_multiple, sp.width * 0.90)
    d = decide_exit(sp, stop, now=MIDWEEK, spot=755.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "stop_loss"


def test_stop_is_capped_below_width_so_it_stays_reachable():
    """A fat credit x a wide multiple can exceed the spread's own max loss.

    Uncapped, the stop would never fire and every loser would ride to max loss.
    """
    sp = spread(entry_credit=2.00)          # 2.00 x 4 = 8.00, wider than the 5.0 spread
    assert sp.entry_credit * LIMITS.stop_loss_multiple > sp.width
    d = decide_exit(sp, 4.50, now=MIDWEEK, spot=750.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "stop_loss"


def test_stop_loss_does_not_fire_below_threshold():
    sp = spread(entry_credit=0.47)
    stop = min(sp.entry_credit * LIMITS.stop_loss_multiple, sp.width * 0.90)
    target = sp.entry_credit * (1 - LIMITS.profit_target_pct)
    d = decide_exit(sp, (stop + target) / 2, now=MIDWEEK, spot=758.0, limits=LIMITS)
    assert d.action == "hold"


def test_stop_loss_takes_priority_over_nothing_else_midweek():
    sp = spread(entry_credit=0.47)
    d = decide_exit(sp, 4.90, now=MIDWEEK, spot=750.0, limits=LIMITS)
    assert d.rule == "stop_loss"


# --- assignment risk -----------------------------------------------------

def test_assignment_risk_fires_when_spot_near_short_put_on_expiry_day():
    sp = spread(short_strike=752.0, right="P")
    d = decide_exit(sp, 0.10, now=EXPIRY_MID, spot=752.4, limits=LIMITS)
    assert d.action == "close" and d.rule == "assignment_risk"


def test_assignment_risk_beats_profit_target():
    # Mark says take profit, but the short strike is threatened. Assignment wins.
    sp = spread(short_strike=752.0, right="P", entry_credit=0.47)
    d = decide_exit(sp, 0.05, now=EXPIRY_MID, spot=752.0, limits=LIMITS)
    assert d.rule == "assignment_risk"


def test_assignment_risk_ignores_comfortably_otm_puts():
    sp = spread(short_strike=752.0, right="P")
    d = decide_exit(sp, 0.30, now=EXPIRY_MID, spot=770.0, limits=LIMITS)
    assert d.action == "hold"


def test_assignment_risk_direction_is_inverted_for_calls():
    sp = spread(short_strike=780.0, long_strike=785.0, right="C")
    # For a short call, danger is spot rising THROUGH the strike.
    assert decide_exit(sp, 0.10, now=EXPIRY_MID, spot=779.8, limits=LIMITS).rule == \
        "assignment_risk"
    # Well below the short call, assignment is not the concern. Use a mark that
    # sits between the profit target and the stop so no other rule fires either.
    d = decide_exit(sp, 0.30, now=EXPIRY_MID, spot=760.0, limits=LIMITS)
    assert d.action == "hold" and d.rule is None


def test_assignment_risk_only_applies_on_expiry_day():
    sp = spread(short_strike=752.0, right="P")
    d = decide_exit(sp, 0.30, now=MIDWEEK, spot=752.0, limits=LIMITS)
    assert d.rule != "assignment_risk"


# --- expiry flatten ------------------------------------------------------

def test_expiry_flatten_fires_near_the_close():
    sp = spread()
    d = decide_exit(sp, 0.20, now=EXPIRY_LATE, spot=790.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "expiry_flatten"


def test_no_flatten_midday_on_expiry_when_far_otm():
    sp = spread()
    d = decide_exit(sp, 0.30, now=EXPIRY_MID, spot=790.0, limits=LIMITS)
    assert d.action == "hold"


# --- degraded data -------------------------------------------------------

def test_holds_when_mark_unavailable_and_no_expiry_pressure():
    sp = spread()
    d = decide_exit(sp, None, now=MIDWEEK, spot=770.0, limits=LIMITS)
    assert d.action == "hold" and "no reliable mark" in d.reason


def test_still_flattens_on_expiry_day_without_a_mark():
    # Losing quotes must not strand us into assignment.
    sp = spread()
    d = decide_exit(sp, None, now=EXPIRY_LATE, spot=790.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "expiry_flatten"


# --- realized P&L --------------------------------------------------------

def test_realized_pnl_credit_minus_debit():
    sp = spread(entry_credit=0.47, qty=8)
    assert sp.realized_pnl(0.20) == pytest.approx(216.0)     # (0.47-0.20)*100*8
    assert sp.realized_pnl(0.94) == pytest.approx(-376.0)


def test_closing_legs_reverse_the_opening_order():
    sp = spread()
    legs = sp.closing_legs()
    assert legs[0]["symbol"] == "SPY260903P00752000"
    assert legs[0]["side"] == "buy" and legs[0]["position_intent"] == "buy_to_close"
    assert legs[1]["symbol"] == "SPY260903P00747000"
    assert legs[1]["side"] == "sell" and legs[1]["position_intent"] == "sell_to_close"


# --- cloud backend compatibility -----------------------------------------

def test_open_spread_accepts_firestore_string_id():
    """Firestore document ids are strings, SQLite rowids are ints.

    Typing this int-only meant every cloud spread failed validation on the way
    into exit management -- openable but never closable.
    """
    sp = OpenSpread(id="aB3xYz9QwLm", underlying="SPY", expiry=TARGET_EXPIRY,
                    right="P", short_strike=752.0, long_strike=747.0,
                    qty=5, entry_credit=0.47)
    assert sp.id == "aB3xYz9QwLm"
    assert sp.short_symbol() == "SPY260903P00752000"
    assert sp.closing_legs()[0]["position_intent"] == "buy_to_close"


def test_open_spread_still_accepts_sqlite_int_id():
    assert OpenSpread(id=7, underlying="SPY", expiry=TARGET_EXPIRY, right="P",
                      short_strike=752.0, long_strike=747.0, qty=5,
                      entry_credit=0.47).id == 7


def test_spread_from_row_handles_both_backends():
    from agent.manage import spread_from_row
    base = dict(underlying="SPY", expiry=TARGET_EXPIRY, right="P",
                short_strike=752.0, long_strike=747.0, qty=5, entry_credit=0.47)
    assert spread_from_row({**base, "id": 3}).id == 3
    assert spread_from_row({**base, "id": "docabc123"}).id == "docabc123"


# --- fill verification ---------------------------------------------------

def _pos(sym, qty="-5"):
    return {"symbol": sym, "qty": qty, "market_value": "-100"}


def test_held_when_short_leg_is_in_positions():
    from agent.manage import is_actually_held
    sp = spread()
    assert is_actually_held(sp, [{"symbol": sp.short_symbol(), "qty": "-5"},
                                 {"symbol": sp.long_symbol(), "qty": "5"}])
    # the short alone is a naked leg, not a spread we can close as one
    assert not is_actually_held(sp, [{"symbol": sp.short_symbol(), "qty": "-5"}])


def test_not_held_when_positions_empty():
    """A journaled spread whose order was accepted but never filled.

    Closing it would be rejected 422 (nothing to close) and 403 (our own
    resting order on the other side reads as a wash trade).
    """
    from agent.manage import is_actually_held
    assert not is_actually_held(spread(), [])


def test_not_held_when_quantity_is_zero():
    from agent.manage import is_actually_held
    sp = spread()
    assert not is_actually_held(sp, [_pos(sp.short_symbol(), qty="0")])


def test_not_held_when_a_different_contract_is_open():
    from agent.manage import is_actually_held
    assert not is_actually_held(spread(), [_pos("SPY260903P00700000")])


# --- size is taken from the broker, never the journal --------------------

def test_held_qty_reads_the_short_leg():
    from agent.manage import held_qty
    sp = spread(qty=12)
    assert held_qty(sp, [{"symbol": sp.short_symbol(), "qty": "-12"},
                         {"symbol": sp.long_symbol(), "qty": "12"}]) == 12


def test_held_qty_reports_a_partial_fill():
    """Journal says 12, broker says 7. Closing 12 could open a 5-lot short."""
    from agent.manage import held_qty
    sp = spread(qty=12)
    assert held_qty(sp, [{"symbol": sp.short_symbol(), "qty": "-7"},
                         {"symbol": sp.long_symbol(), "qty": "7"}]) == 7


def test_held_qty_zero_when_absent():
    from agent.manage import held_qty
    assert held_qty(spread(), []) == 0


# --- satellite (debit) exits invert the inequalities ---------------------

def debit_spread(**kw) -> OpenSpread:
    """Call debit spread: we PAID entry_credit; mark is what we'd receive back."""
    base = dict(id=9, underlying="SPY", expiry=TARGET_EXPIRY, right="C",
                short_strike=785.0, long_strike=780.0, qty=2,
                entry_credit=1.50, sleeve="satellite")
    base.update(kw)
    return OpenSpread(**base)


def test_debit_mark_is_what_we_receive_not_what_we_pay():
    from agent.manage import mark_to_close
    sp = debit_spread()
    q = {sp.short_symbol(): {"ap": 0.60, "bp": 0.55},
         sp.long_symbol():  {"ap": 2.90, "bp": 2.85}}
    # sell the long at its bid (2.85), buy back the short at its ask (0.60)
    assert mark_to_close(sp, q) == pytest.approx(2.25)


def test_debit_max_loss_is_only_the_debit():
    assert debit_spread(entry_credit=1.50, qty=2).max_loss_per_spread == pytest.approx(150.0)


def test_debit_profit_target_fires_when_value_rises():
    sp = debit_spread(entry_credit=1.50)      # width 5, max value 5
    target = 1.50 + (5.0 - 1.50) * LIMITS.satellite_profit_target_pct
    d = decide_exit(sp, target + 0.05, now=MIDWEEK, spot=790.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "profit_target"


def test_debit_stop_fires_when_value_decays():
    sp = debit_spread(entry_credit=1.50)
    stop = 1.50 * (1 - LIMITS.satellite_stop_pct)
    d = decide_exit(sp, stop - 0.05, now=MIDWEEK, spot=760.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "stop_loss"


def test_debit_holds_between_stop_and_target():
    sp = debit_spread(entry_credit=1.50)
    d = decide_exit(sp, 1.60, now=MIDWEEK, spot=782.0, limits=LIMITS)
    assert d.action == "hold"


def test_debit_realized_pnl_is_the_reverse_of_credit():
    sp = debit_spread(entry_credit=1.50, qty=2)
    assert sp.realized_pnl(3.00) == pytest.approx(300.0)    # sold higher than paid
    assert sp.realized_pnl(0.50) == pytest.approx(-200.0)
    core = spread(entry_credit=0.75, qty=2)
    assert core.realized_pnl(0.25) == pytest.approx(100.0)  # bought back cheaper
    assert core.realized_pnl(1.75) == pytest.approx(-200.0)


def test_a_rising_mark_means_opposite_things_per_sleeve():
    """Same mark movement: good for the debit sleeve, bad for the credit sleeve."""
    dbt = decide_exit(debit_spread(entry_credit=1.50), 4.00,
                      now=MIDWEEK, spot=795.0, limits=LIMITS)
    crd = decide_exit(spread(entry_credit=0.75), 4.00,
                      now=MIDWEEK, spot=740.0, limits=LIMITS)
    assert dbt.rule == "profit_target"
    assert crd.rule == "stop_loss"


# --- a worthless protective leg is a price, not missing data ------------------

def test_zero_bid_on_the_long_leg_still_marks_and_stops():
    """Codex review 2026-09-18: credit 0.50, short ask 1.50, long bid 0.
    A one-sided market (nobody bids, someone offers) values the long at zero;
    the close costs 1.50, exactly the 3x stop. It used to return None and hold."""
    sp = spread(entry_credit=0.50)
    q = {sp.short_symbol(): {"ap": 1.50, "bp": 1.45},
         sp.long_symbol(): {"ap": 0.03, "bp": 0}}
    mark = mark_to_close(sp, q)
    assert mark == pytest.approx(1.50)
    d = decide_exit(sp, mark, now=MIDWEEK, spot=760.0, limits=LIMITS)
    assert d.action == "close" and d.rule == "stop_loss"


def test_zero_bid_on_the_long_leg_lets_the_profit_target_fire():
    sp = spread(entry_credit=0.50)
    q = {sp.short_symbol(): {"ap": 0.05, "bp": 0.03},
         sp.long_symbol(): {"ap": 0.02, "bp": 0}}
    mark = mark_to_close(sp, q)
    assert mark == pytest.approx(0.05)
    assert decide_exit(sp, mark, now=MIDWEEK, spot=790.0, limits=LIMITS).rule == "profit_target"


def test_a_long_leg_with_no_market_at_all_is_still_missing():
    """Bid 0 AND ask 0 is no quote, not a worthless option."""
    sp = spread()
    q = {sp.short_symbol(): {"ap": 1.50, "bp": 1.45},
         sp.long_symbol(): {"ap": 0, "bp": 0}}
    assert mark_to_close(sp, q) is None
    assert mark_to_close(sp, {sp.short_symbol(): {"ap": 1.5, "bp": 1.45},
                              sp.long_symbol(): {"ap": 0.03}}) is None


def test_partial_close_pnl_is_per_quantity():
    sp = spread(qty=10, entry_credit=0.50)
    assert sp.realized_pnl(1.50, qty=4) == pytest.approx(-400.0)
    assert sp.realized_pnl(2.00, qty=6) == pytest.approx(-900.0)
    assert sp.realized_pnl(2.00) == pytest.approx(-1500.0)      # default is the full size


def test_expiry_flatten_follows_an_early_close():
    """13:00 close: a 15:30 flatten is two and a half hours too late."""
    sp = spread(expiry="2026-11-27")
    close_t = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    at_1235 = datetime(2026, 11, 27, 12, 35, tzinfo=ET)
    d = decide_exit(sp, 0.30, now=at_1235, spot=790.0, limits=LIMITS, close_t=close_t)
    assert d.action == "close" and d.rule == "expiry_flatten"
    assert decide_exit(sp, 0.30, now=at_1235, spot=790.0, limits=LIMITS).action == "hold"


# --- early assignment around an ex-dividend date -----------------------------

def _call(**kw):
    return spread(right="C", short_strike=770.0, long_strike=775.0, entry_credit=0.50,
                  expiry="2026-09-25", **kw)

EVE = datetime(2026, 9, 17, 12, 0, tzinfo=ET)        # SPY went ex-dividend 2026-09-18, 1.89


def test_short_call_near_the_money_is_closed_the_session_before_ex_dividend():
    """A short call that is in or near the money the night before ex-date can be
    exercised for the dividend, leaving short stock and the dividend owed."""
    d = decide_exit(_call(), 0.90, now=EVE, spot=769.8, limits=LIMITS,
                    ex_dividend=("2026-09-18", 1.89), next_session="2026-09-18")
    assert d.action == "close" and d.rule == "dividend_assignment_risk"
    assert "1.89" in d.reason


def test_a_far_otm_short_call_rides_through_ex_dividend():
    d = decide_exit(_call(), 0.30, now=EVE, spot=755.0, limits=LIMITS,
                    ex_dividend=("2026-09-18", 1.89), next_session="2026-09-18")
    assert d.action == "hold"


def test_dividend_rule_waits_for_the_last_session_and_ignores_puts():
    early = decide_exit(_call(), 0.90, now=EVE, spot=769.8, limits=LIMITS,
                        ex_dividend=("2026-09-22", 1.89), next_session="2026-09-18")
    assert early.rule != "dividend_assignment_risk"
    over_a_weekend = decide_exit(_call(), 0.90, now=EVE, spot=769.8, limits=LIMITS,
                                 ex_dividend=("2026-09-19", 1.89), next_session="2026-09-21")
    assert over_a_weekend.rule == "dividend_assignment_risk"      # ex-date lands before the next session
    put = spread(right="P", short_strike=770.0, long_strike=765.0, entry_credit=0.50, expiry="2026-09-25")
    assert decide_exit(put, 0.90, now=EVE, spot=770.2, limits=LIMITS,
                       ex_dividend=("2026-09-18", 1.89), next_session="2026-09-18").rule != "dividend_assignment_risk"


def test_close_size_is_the_lot_not_the_whole_short_leg():
    """Codex follow-up: a 10-lot 770/775 and a 5-lot 770/780 share the 770
    short. held_qty returned |-15| for both, so each tried to close 15."""
    from agent.manage import held_qty
    positions = [{"symbol": "SPY260925C00770000", "qty": "-15"},
                 {"symbol": "SPY260925C00775000", "qty": "10"},
                 {"symbol": "SPY260925C00780000", "qty": "5"}]
    ten = spread(right="C", short_strike=770.0, long_strike=775.0, qty=10, expiry="2026-09-25")
    five = spread(right="C", short_strike=770.0, long_strike=780.0, qty=5, expiry="2026-09-25")
    assert held_qty(ten, positions) == 10 and held_qty(five, positions) == 5


def test_a_leg_held_the_wrong_way_round_is_not_a_holding():
    from agent.manage import held_qty
    sp = spread(right="C", short_strike=770.0, long_strike=775.0, qty=10, expiry="2026-09-25")
    long_the_short = [{"symbol": "SPY260925C00770000", "qty": "10"},
                      {"symbol": "SPY260925C00775000", "qty": "10"}]
    assert held_qty(sp, long_the_short) == 0
