"""The size ladder: size is earned by an attributable record, and lost in a drawdown."""
import pytest

from agent import sizing

CLOSES = {("SPY", "2026-09-19"): 761.69}


def trade(pnl, held=True):
    # view held: short call at 775 with the close at 761.69; failed: short call at 750
    return dict(underlying="SPY", right="C", short_strike=775.0 if held else 750.0, long_strike=780.0,
                expiry="2026-09-19", entry_credit=0.5, qty=1, status="closed", realized_pnl=pnl)


def test_a_fresh_account_trades_at_half_size():
    mult, why = sizing.tier(closed=[], closes=CLOSES, equity=50_000, peak=50_000)
    assert mult == 0.5 and why.startswith("start: 0 attributable")


def test_promotion_needs_attributable_trades_with_a_positive_mean():
    ten_good = [trade(+40)] * 10
    assert sizing.tier(closed=ten_good, closes=CLOSES, equity=50_000, peak=50_000)[0] == 0.75
    assert sizing.tier(closed=[trade(+40)] * 25, closes=CLOSES, equity=50_000, peak=50_000)[0] == 1.0
    ten_bad = [trade(-40)] * 10
    assert sizing.tier(closed=ten_bad, closes=CLOSES, equity=50_000, peak=50_000)[0] == 0.5


def test_lucky_wins_never_promote():
    lucky = [trade(+40, held=False)] * 30                      # view failed, profited anyway
    mult, why = sizing.tier(closed=lucky, closes=CLOSES, equity=50_000, peak=50_000)
    assert mult == 0.5 and "0 attributable" in why


def test_drawdown_demotes():
    good = [trade(+40)] * 25
    assert sizing.tier(closed=good, closes=CLOSES, equity=50_000, peak=50_000)[0] == 1.0
    assert sizing.tier(closed=good, closes=CLOSES, equity=47_400, peak=50_000)[0] == 0.75    # -5.2%: one tier
    assert sizing.tier(closed=good, closes=CLOSES, equity=44_900, peak=50_000)[0] == 0.5     # -10.2%: to the floor


def test_the_multiplier_never_exceeds_one():
    assert sizing.tier(closed=[trade(+400)] * 200, closes=CLOSES, equity=90_000, peak=50_000)[0] == 1.0
