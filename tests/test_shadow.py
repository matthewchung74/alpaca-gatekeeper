"""Settling shadow-ledger claims against real prices."""
import pytest

from agent import shadow
from agent.config import RiskLimits

LIMITS = RiskLimits()


def row(**kw):
    base = dict(u="SPY", r="P", ks=740.0, kl=735.0, w=5.0, cr=1.00, nat=0.95, d=0.15, iv=0.15,
                oi=2000, spr=0.03, emr=1.2, spot=760.0, fail=[], chosen=False, traded=False)
    base.update(kw)
    return base


def bars(short_seq, long_seq):
    """Daily bars per leg as (h, l) pairs, oldest first."""
    mk = lambda seq: [{"t": f"2026-09-{22 + i:02d}", "h": h, "l": l, "o": (h + l) / 2, "c": (h + l) / 2}
                      for i, (h, l) in enumerate(seq)]
    return {"short": mk(short_seq), "long": mk(long_seq)}


# --- held to expiry: exact, from the underlying's close --------------------------

def test_a_put_spread_above_the_close_is_breached_and_capped_at_width():
    r = shadow.settle_row(row(), close=730.0, bars=None, limits=LIMITS)
    assert r["held"] is False and r["v_exp"] == 5.0
    assert r["ret_hold"] == pytest.approx((1.00 - 5.0) / (5.0 - 1.00))          # -1: lost the max


def test_a_put_spread_partly_in_the_money_settles_at_intrinsic():
    r = shadow.settle_row(row(), close=738.0, bars=None, limits=LIMITS)
    assert r["held"] is False and r["v_exp"] == 2.0
    assert r["ret_hold"] == pytest.approx((1.00 - 2.0) / 4.0)


def test_a_call_spread_below_the_close_holds_and_keeps_the_credit():
    r = shadow.settle_row(row(r="C", ks=781.0, kl=783.0, w=2.0, cr=0.30), close=770.0, bars=None, limits=LIMITS)
    assert r["held"] is True and r["v_exp"] == 0.0
    assert r["ret_hold"] == pytest.approx(0.30 / 1.70)


def test_a_call_spread_run_through_loses_the_width():
    r = shadow.settle_row(row(r="C", ks=781.0, kl=783.0, w=2.0, cr=0.30), close=790.0, bars=None, limits=LIMITS)
    assert r["held"] is False and r["v_exp"] == 2.0 and r["ret_hold"] == pytest.approx(-1.0)


# --- managed by our own exit rules: approximate, from daily option bars ------------

def test_the_profit_target_fires_when_the_spread_decays():
    # credit 1.00: target at 0.50, stop at 3.00
    b = bars(short_seq=[(1.3, 1.0), (0.9, 0.6), (0.5, 0.3)], long_seq=[(0.4, 0.3), (0.3, 0.2), (0.2, 0.1)])
    r = shadow.settle_row(row(), close=780.0, bars=b, limits=LIMITS)
    assert r["mgd_rule"] == "profit_target"
    assert r["ret_mgd"] == pytest.approx((1.00 - 0.50) / 4.0)


def test_the_stop_fires_when_the_spread_blows_out():
    b = bars(short_seq=[(2.0, 1.2), (4.5, 2.5)], long_seq=[(0.6, 0.4), (1.0, 0.8)])
    r = shadow.settle_row(row(), close=760.0, bars=b, limits=LIMITS)
    assert r["mgd_rule"] == "stop_loss"
    assert r["ret_mgd"] == pytest.approx((1.00 - 3.00) / 4.0)


def test_a_day_that_could_be_either_books_the_stop():
    """A single daily bar wide enough to hit both: the order inside the day is
    unknown, and the pessimistic reading is the stop."""
    b = bars(short_seq=[(4.0, 0.2)], long_seq=[(0.6, 0.1)])
    r = shadow.settle_row(row(), close=760.0, bars=b, limits=LIMITS)
    assert r["mgd_rule"] == "stop_loss"


def test_neither_rule_firing_settles_at_expiry():
    # best case each day (short low - long high) stays above the 0.50 target,
    # worst case (short high - long low) stays below the 3.00 stop
    b = bars(short_seq=[(1.3, 1.0), (1.2, 0.95)], long_seq=[(0.4, 0.3), (0.4, 0.3)])
    r = shadow.settle_row(row(), close=780.0, bars=b, limits=LIMITS)
    assert r["mgd_rule"] == "expiry" and r["ret_mgd"] == r["ret_hold"]


def test_an_absurd_paper_print_cannot_value_the_spread_beyond_its_width():
    """QQQ 728/731 x3-wide printed a 15.88 high on 2026-09-21 in Alpaca's paper
    bars. Clamped to the width, that is the stop, not a -400% return."""
    b = bars(short_seq=[(15.88, 4.0)], long_seq=[(12.5, 2.6)])
    r = shadow.settle_row(row(r="C", ks=728.0, kl=731.0, w=3.0, cr=0.45), close=740.0, bars=b, limits=LIMITS)
    assert r["mgd_rule"] == "stop_loss"
    assert r["ret_mgd"] >= -1.0


def test_missing_bars_leave_the_managed_result_unknown():
    r = shadow.settle_row(row(), close=780.0, bars={"short": [], "long": []}, limits=LIMITS)
    assert r["ret_mgd"] is None and r["mgd_rule"] is None and r["ret_hold"] is not None
    r = shadow.settle_row(row(), close=780.0, bars=None, limits=LIMITS)
    assert r["ret_mgd"] is None


def test_settling_a_document_touches_every_row_and_fetches_once_per_leg(monkeypatch):
    from agent import alpaca_cli as cli
    calls = []
    monkeypatch.setattr(cli, "daily_close", lambda sym, day, profile: 770.0)
    def option_bars(symbols, start, end, profile):
        calls.append(sorted(symbols))
        return {s: [] for s in symbols}
    monkeypatch.setattr(cli, "option_bars", option_bars)
    doc = {"id": "x", "ts": "2026-09-21T13:46:00+00:00", "expiry": "2026-10-02", "profile": "igk",
           "rows": [row(), row(r="C", ks=781.0, kl=783.0, w=2.0, cr=0.30)]}
    out = shadow.settle_document(doc, profile="igk", limits=LIMITS)
    assert [r["held"] for r in out] == [True, True]
    assert len(calls) == 1 and len(calls[0]) == 4
