"""The watchdog: silence and success must not look the same."""
from datetime import datetime, timedelta

from agent import watch
from agent.config import ET

NOW = datetime(2026, 10, 2, 11, 0, tzinfo=ET)        # Friday, mid-session


def cyc(minutes_ago, action="submitted", **kw):
    d = dict(ts=(NOW - timedelta(minutes=minutes_ago)).isoformat(), action=action, profile="igk")
    d.update(kw)
    return d


def test_quiet_when_everything_is_recent():
    out = watch.check(cycles=[cyc(70)], marks=[cyc(8)], positions=[], journal_open=[],
                      open_orders=[], now=NOW)
    assert out["alerts"] == [] and out["ok"] is True


def test_a_missing_entry_cycle_is_an_alert():
    out = watch.check(cycles=[cyc(400)], marks=[cyc(8)], positions=[], journal_open=[],
                      open_orders=[], now=NOW)
    assert any("no entry cycle" in a for a in out["alerts"])


def test_a_stalled_sweep_is_an_alert():
    """Sweeps run every ten minutes; the exits depend on them."""
    out = watch.check(cycles=[cyc(70)], marks=[cyc(45)], positions=[], journal_open=[],
                      open_orders=[], now=NOW)
    assert any("sweep" in a for a in out["alerts"])


def test_a_position_the_journal_does_not_know_about_is_an_alert():
    out = watch.check(cycles=[cyc(70)], marks=[cyc(8)],
                      positions=[{"symbol": "SPY261009C00779000", "qty": "-2"}],
                      journal_open=[], open_orders=[], now=NOW)
    assert any("broker holds" in a for a in out["alerts"])


def test_a_journalled_spread_accounts_for_its_two_legs():
    """The alert is only useful if the OCC symbols it builds match the broker's."""
    out = watch.check(cycles=[cyc(70)], marks=[cyc(8)],
                      positions=[{"symbol": "SPY261009C00779000", "qty": "-2"},
                                 {"symbol": "SPY261009C00781000", "qty": "2"}],
                      journal_open=[{"underlying": "SPY", "expiry": "2026-10-09", "right": "C",
                                     "short_strike": 779.0, "long_strike": 781.0}],
                      open_orders=[], now=NOW)
    assert out["alerts"] == []


def test_a_journalled_error_in_the_last_hour_is_an_alert():
    out = watch.check(cycles=[cyc(20, action="error", error="anthropic preflight: credit balance"),
                              cyc(70)],
                      marks=[cyc(8)], positions=[], journal_open=[], open_orders=[],
                      now=NOW)
    assert any("preflight" in a for a in out["alerts"])


def test_an_order_working_far_too_long_is_an_alert():
    out = watch.check(cycles=[cyc(70)], marks=[cyc(8)], positions=[], journal_open=[],
                      open_orders=[{"id": "o1", "client_order_id": "hack-x",
                                    "submitted_at": (NOW - timedelta(minutes=40)).isoformat()}],
                      now=NOW)
    assert any("still working" in a for a in out["alerts"])


def test_nothing_is_expected_outside_the_session():
    quiet = datetime(2026, 10, 3, 11, 0, tzinfo=ET)        # Saturday
    out = watch.check(cycles=[cyc(5000)], marks=[cyc(5000)], positions=[], journal_open=[],
                      open_orders=[], now=quiet)
    assert out["alerts"] == [] and "closed" in out["note"]
