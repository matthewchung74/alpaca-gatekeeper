"""The hostile second look: deterministic flags, recorded, never (yet) a veto."""
from datetime import date, datetime

import pytest

from agent import redteam
from agent.config import ET, RiskLimits
from agent.regime import TapeRead

LIMITS = RiskLimits()


def row(**kw):
    base = dict(u="SPY", r="C", ks=781.0, kl=784.0, w=3.0, cr=0.45, d=0.17, oi=2000,
                spr=0.03, emr=1.2, spot=760.0, fail=[])
    base.update(kw)
    return base


def tape(**kw):
    base = dict(regime="sideways", range_position=0.5, lookback_high=770.0, lookback_low=750.0,
                trend_pct=0.0, avg_range_pct=0.008, detail="t")
    base.update(kw)
    return TapeRead(**base)


def test_a_macro_print_inside_the_holding_window_is_flagged():
    # payrolls 2026-10-02, expiry 2026-10-09
    f = redteam.flags(row(), expiry="2026-10-09", now=datetime(2026, 10, 1, 9, 46, tzinfo=ET),
                      tape=tape(), open_spreads=[], limits=LIMITS)
    assert "event:payrolls" in f["flags"] and "2026-10-02" in f["detail"]
    # an expiry before the print carries none of it
    clear = redteam.flags(row(), expiry="2026-10-01", now=datetime(2026, 9, 30, 9, 46, tzinfo=ET),
                          tape=tape(), open_spreads=[], limits=LIMITS)
    assert not [x for x in clear["flags"] if x.startswith("event:")]


def test_a_fed_day_and_megacap_earnings_both_land_on_the_same_october_week():
    """2026-10-28 is an FOMC decision AND MSFT/GOOGL/META results."""
    f = redteam.flags(row(u="QQQ"), expiry="2026-10-30",
                      now=datetime(2026, 10, 26, 9, 46, tzinfo=ET), tape=tape(),
                      open_spreads=[], limits=LIMITS)
    assert "event:fomc" in f["flags"] and "earnings" in " ".join(f["flags"])


def test_earnings_only_count_for_the_index_that_holds_the_name():
    """NVDA moves QQQ and SPY; it is not in IWM, which is small caps."""
    when = datetime(2026, 11, 20, 9, 46, tzinfo=ET)
    qqq = redteam.flags(row(u="QQQ"), expiry="2026-11-27", now=when, tape=tape(), open_spreads=[], limits=LIMITS)
    iwm = redteam.flags(row(u="IWM"), expiry="2026-11-27", now=when, tape=tape(), open_spreads=[], limits=LIMITS)
    assert any(x.startswith("earnings:") for x in qqq["flags"])
    assert not any(x.startswith("earnings:") for x in iwm["flags"])


def test_a_third_bet_on_the_same_side_is_flagged():
    held = [dict(underlying="QQQ", right="C", sleeve="core"), dict(underlying="IWM", right="C", sleeve="core")]
    f = redteam.flags(row(r="C"), expiry="2026-10-09", now=datetime(2026, 10, 1, 9, 46, tzinfo=ET),
                      tape=tape(), open_spreads=held, limits=LIMITS)
    assert "crowded:C" in f["flags"]
    other = redteam.flags(row(r="P"), expiry="2026-10-09", now=datetime(2026, 10, 1, 9, 46, tzinfo=ET),
                          tape=tape(), open_spreads=held, limits=LIMITS)
    assert "crowded:P" not in other["flags"]


def test_a_strike_the_market_says_is_likely_tested_is_flagged():
    """Delta is the market's own probability the short strike finishes in the money."""
    assert "itm_risk" in redteam.flags(row(d=0.33), expiry="2026-10-09",
                                       now=datetime(2026, 10, 1, 9, 46, tzinfo=ET), tape=tape(),
                                       open_spreads=[], limits=LIMITS)["flags"]
    assert "itm_risk" not in redteam.flags(row(d=0.12), expiry="2026-10-09",
                                           now=datetime(2026, 10, 1, 9, 46, tzinfo=ET), tape=tape(),
                                           open_spreads=[], limits=LIMITS)["flags"]


def test_a_late_entry_is_flagged_but_not_blocked():
    late = datetime(2026, 10, 1, 15, 46, tzinfo=ET)
    early = datetime(2026, 10, 1, 9, 46, tzinfo=ET)
    assert "late_entry" in redteam.flags(row(), expiry="2026-10-09", now=late, tape=tape(),
                                         open_spreads=[], limits=LIMITS)["flags"]
    assert "late_entry" not in redteam.flags(row(), expiry="2026-10-09", now=early, tape=tape(),
                                             open_spreads=[], limits=LIMITS)["flags"]


def test_flags_are_advisory_only_for_now():
    f = redteam.flags(row(d=0.33, r="C"), expiry="2026-10-09",
                      now=datetime(2026, 10, 28, 15, 46, tzinfo=ET), tape=tape(), open_spreads=[],
                      limits=LIMITS)
    assert len(f["flags"]) >= 2 and f["veto"] is False      # recorded, never blocking


def test_a_print_that_already_happened_this_morning_is_not_future_risk():
    """PCE at 08:30; a 09:46 entry that day carries the aftermath, not the event."""
    before = redteam.flags(row(), expiry="2026-10-01", now=datetime(2026, 9, 30, 8, 0, tzinfo=ET),
                           tape=tape(), open_spreads=[], limits=LIMITS)
    after = redteam.flags(row(), expiry="2026-10-01", now=datetime(2026, 9, 30, 9, 46, tzinfo=ET),
                          tape=tape(), open_spreads=[], limits=LIMITS)
    assert "event:pce" in before["flags"] and "event:pce" not in after["flags"]
