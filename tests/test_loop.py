"""The loop wires the computed tape into the gates and checks the API first."""
from datetime import datetime

import pytest

from agent import loop
from agent.brain import Brain
from agent.config import ET, RiskLimits


class DeadAPI:
    class messages:
        @staticmethod
        def create(**kw):
            raise RuntimeError("credit balance is too low")


class LiveAPI:
    calls = 0

    class messages:
        @staticmethod
        def create(**kw):
            LiveAPI.calls += 1
            assert kw["max_tokens"] == 1
            return object()


def test_preflight_raises_when_the_api_is_dead():
    with pytest.raises(RuntimeError, match="credit balance"):
        Brain(client=DeadAPI()).preflight()


def test_preflight_makes_one_tiny_call_when_alive():
    Brain(client=LiveAPI()).preflight()
    assert LiveAPI.calls == 1


def test_read_tape_uses_each_underlyings_own_bars():
    bars = [{"t": f"2026-08-{d:02d}T04:00:00Z", "o": 760, "h": 763, "l": 757, "c": 760}
            for d in range(10, 21)]
    obs = {"quotes": {"SPY": {"bp": 759.9, "ap": 760.1}, "QQQ": {}},
           "bars": {"SPY": bars, "QQQ": []}}
    tape, sides = loop.read_tape(obs, datetime(2026, 8, 21, 13, 0, tzinfo=ET), RiskLimits())
    assert tape["SPY"].regime == "sideways" and tape["SPY"].lookback_high == 763
    assert sides["SPY"] == ("P", "C")
    assert tape["QQQ"].lookback_high is None and sides["QQQ"] == ("P", "C")


# --- partial closes and the daily halt, against a mocked broker ---------------

from agent import alpaca_cli as cli
from agent.config import Settings
from agent.journal import SQLiteJournal
from agent.models import TradeProposal

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ET)


def _book(tmp_path, qty=10, credit=0.50):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    p = TradeProposal(underlying="SPY", expiry="2026-09-10", right="C", short_strike=770.0,
                      long_strike=775.0, qty=qty, net_price=credit, sleeve="core", rationale="t")
    j.record_spread(profile="dev", proposal=p, order_id="open-1")
    return j


def _obs(held):
    return {"positions": [{"symbol": "SPY260910C00770000", "qty": str(-held)},
                          {"symbol": "SPY260910C00775000", "qty": str(held)}],
            "quotes": {"SPY": {"bp": 765.0, "ap": 765.1}}}


def _broker(monkeypatch, short_ask, long_bid, fills):
    """fills: list of (filled_qty, price) returned by successive close orders."""
    monkeypatch.setattr(cli, "option_quotes", lambda syms, profile: {
        "SPY260910C00770000": {"ap": short_ask, "bp": short_ask - 0.02},
        "SPY260910C00775000": {"ap": long_bid + 0.02, "bp": long_bid}})
    monkeypatch.setattr(cli, "submit_mleg", lambda **kw: {"id": "close-x"})
    it = iter(fills)
    def fill_result(oid, profile, **kw):
        q, px = next(it)
        return {"qty": q, "credit": px, "status": "filled", "timed_out": False}
    monkeypatch.setattr(cli, "fill_result", fill_result)


def test_partial_close_accumulates_pnl_and_shrinks_the_position(tmp_path, monkeypatch):
    """Codex review: 4 of 10 close at 1.50, the other 6 at 2.00, credit 0.50.
    Actual -1,300. The journal used to record -1,500 (10 x the last price)."""
    j = _book(tmp_path)
    s = Settings(profile="dev")
    _broker(monkeypatch, short_ask=1.60, long_bid=0.10, fills=[(4, 1.50)])
    loop.manage_open_spreads(s, j, _obs(10), NOW, dry_run=False)
    row = j.open_spreads("dev")[0]
    assert row["qty"] == 6 and row["realized_pnl"] == pytest.approx(-400.0)

    _broker(monkeypatch, short_ask=2.05, long_bid=0.05, fills=[(6, 2.00)])
    loop.manage_open_spreads(s, j, _obs(6), NOW, dry_run=False)
    assert j.open_spreads("dev") == []
    closed = j.all_spreads("dev")[0]
    assert closed["status"] == "closed" and closed["realized_pnl"] == pytest.approx(-1300.0)


def test_daily_loss_breach_uses_the_prior_close_not_the_first_mark():
    """An overnight gap is part of today's loss. last_equity is the broker's
    own prior close; the first journal mark of the day is already post-gap."""
    lim = RiskLimits()
    assert loop.daily_loss_breached(equity=95_900.0, day_start=100_000.0, limits=lim)
    assert not loop.daily_loss_breached(equity=96_100.0, day_start=100_000.0, limits=lim)
    assert loop.day_start_equity({"last_equity": "100000", "equity": "95900"}, None) == 100_000.0
    assert loop.day_start_equity({"equity": "95900"}, 98_000.0) == 98_000.0


def test_daily_halt_flattens_the_book_and_persists_for_the_day(tmp_path, monkeypatch):
    j = _book(tmp_path)
    s = Settings(profile="dev")
    # mark 0.40 against a 0.50 credit: no ordinary exit rule fires
    _broker(monkeypatch, short_ask=0.45, long_bid=0.05, fills=[(10, 0.42)])
    loop.manage_open_spreads(s, j, _obs(10), NOW, dry_run=False, flatten="daily loss -4.2% at/over the 4% limit")
    closed = j.all_spreads("dev")[0]
    assert closed["status"] == "closed" and closed["exit_rule"] == "daily_loss_flatten"

    assert not loop.halted_today(j, "dev", NOW)
    loop.record_halt(j, "dev", "daily loss -4.2% at/over the 4% limit")
    assert loop.halted_today(j, "dev", datetime.now(ET))
    assert not loop.halted_today(j, "comp", datetime.now(ET))


# --- reconciliation: the broker's book is the truth ---------------------------

def _pos(sym, qty, avg):
    return {"symbol": sym, "qty": str(qty), "avg_entry_price": str(avg)}


def test_reconcile_is_quiet_when_broker_and_journal_agree(tmp_path):
    j = _book(tmp_path)
    assert loop.reconcile(j, "dev", _obs(10)["positions"], NOW, dry_run=False) == []


def test_reconcile_adopts_an_unjournaled_vertical(tmp_path):
    """An entry that filled after the cancel gave up: held, journaled nowhere,
    managed by nothing (2026-08-31). Now it is adopted at the broker's prices."""
    j = SQLiteJournal(str(tmp_path / "j.db"))
    positions = [_pos("QQQ260924C00728000", -15, 1.20), _pos("QQQ260924C00731000", 15, 0.75)]
    problems = loop.reconcile(j, "dev", positions, NOW, dry_run=False)
    assert problems == []                      # adopted, so nothing is unexplained
    row = j.open_spreads("dev")[0]
    assert (row["underlying"], row["right"], row["short_strike"], row["long_strike"]) == ("QQQ", "C", 728.0, 731.0)
    assert row["qty"] == 15 and row["entry_credit"] == pytest.approx(0.45) and row["sleeve"] == "core"


def test_reconcile_blocks_on_exposure_it_cannot_explain(tmp_path):
    j = _book(tmp_path)
    naked = _obs(10)["positions"] + [_pos("IWM260924P00280000", -5, 1.0)]
    assert any("IWM260924P00280000" in p for p in loop.reconcile(j, "dev", naked, NOW, dry_run=False))
    stock = _obs(10)["positions"] + [_pos("SPY", -1000, 770.0)]
    assert any("stock" in p for p in loop.reconcile(j, "dev", stock, NOW, dry_run=False))
    short_only = [_pos("SPY260910C00775000", 10, 0.2)]          # short leg gone: assignment?
    assert any("SPY260910C00770000" in p for p in loop.reconcile(j, "dev", short_only, NOW, dry_run=False))


def test_reconcile_retires_a_spread_the_broker_no_longer_holds(tmp_path):
    j = _book(tmp_path)
    later = datetime.now(ET).replace(year=2030)                 # the row is long settled
    problems = loop.reconcile(j, "dev", [], later, dry_run=False)
    assert j.open_spreads("dev") == []
    assert j.all_spreads("dev")[0]["exit_rule"] == "missing_at_broker"
    assert problems == []


def test_session_bounds_come_from_the_calendar():
    cal = [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
    o, c = loop.session_bounds(datetime(2026, 11, 27, 10, 0, tzinfo=ET), cal)
    assert (o.hour, o.minute, c.hour, c.minute) == (9, 30, 13, 0)
    assert loop.session_bounds(datetime(2026, 11, 26, 10, 0, tzinfo=ET), cal) is None   # holiday
    assert loop.in_session(datetime(2026, 11, 27, 9, 31, tzinfo=ET), (o, c))            # first minutes count for exits
    assert not loop.in_session(datetime(2026, 11, 27, 13, 1, tzinfo=ET), (o, c))


# --- one job at a time, and no order left behind ------------------------------

def test_journal_lock_is_exclusive_and_expires(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    assert j.acquire_lock("dev", "cycle-1", ttl_s=600)
    assert not j.acquire_lock("dev", "sweep-2", ttl_s=600)
    assert j.acquire_lock("comp", "sweep-2", ttl_s=600)          # a different account is a different lock
    j.release_lock("dev", "sweep-2")                             # not the holder: no effect
    assert not j.acquire_lock("dev", "sweep-3", ttl_s=600)
    j.release_lock("dev", "cycle-1")
    assert j.acquire_lock("dev", "sweep-3", ttl_s=0)             # held, but already expired
    assert j.acquire_lock("dev", "cycle-4", ttl_s=600)


def test_a_sweep_stands_aside_while_a_cycle_holds_the_lock(tmp_path, monkeypatch):
    """Without this a sweep can adopt an entry the cycle has filled but not yet
    journaled, and the position ends up in the journal twice."""
    j = SQLiteJournal(str(tmp_path / "j.db"))
    monkeypatch.setattr(loop, "open_journal", lambda path=None: j)
    ran = []
    monkeypatch.setattr(loop, "_cycle_body", lambda settings, journal, **kw: ran.append(kw) or 0)
    assert j.acquire_lock("dev", "cycle-x", ttl_s=600)
    assert loop.run_cycle(Settings(profile="dev"), manage_only=True) == 0
    assert ran == []                                              # skipped, not run
    j.release_lock("dev", "cycle-x")
    assert loop.run_cycle(Settings(profile="dev"), manage_only=True) == 0
    assert len(ran) == 1
    assert j.acquire_lock("dev", "anyone", ttl_s=600)             # and it let go afterwards


def test_the_lock_is_released_even_when_the_cycle_blows_up(tmp_path, monkeypatch):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    monkeypatch.setattr(loop, "open_journal", lambda path=None: j)
    def boom(settings, journal, **kw):
        raise RuntimeError("broker on fire")
    monkeypatch.setattr(loop, "_cycle_body", boom)
    with pytest.raises(RuntimeError):
        loop.run_cycle(Settings(profile="dev"), manage_only=True)
    assert j.acquire_lock("dev", "next", ttl_s=600)


def test_orders_left_open_by_a_dead_run_are_cancelled(tmp_path, monkeypatch):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    now = datetime(2026, 9, 18, 14, 0, tzinfo=ET)
    orders = [
        {"id": "o1", "client_order_id": "hack-abc", "submitted_at": "2026-09-18T17:45:30Z"},   # 14.5 min old
        {"id": "o2", "client_order_id": "exit-7-def", "submitted_at": "2026-09-18T17:40:00Z"},
        {"id": "o3", "client_order_id": "hack-new", "submitted_at": "2026-09-18T17:59:30Z"},   # 30s old: in flight
        {"id": "o4", "client_order_id": "placed-by-hand", "submitted_at": "2026-09-18T15:00:00Z"},
    ]
    monkeypatch.setattr(cli, "open_orders", lambda profile: orders)
    cancelled = []
    monkeypatch.setattr(cli, "cancel_order", lambda oid, profile: cancelled.append(oid) or True)
    assert loop.cancel_stale_orders(j, "dev", now, dry_run=False) == ["o1", "o2"]
    assert cancelled == ["o1", "o2"]


def test_next_session_skips_the_weekend():
    cal = [{"date": "2026-09-18", "open": "09:30", "close": "16:00"},
           {"date": "2026-09-21", "open": "09:30", "close": "16:00"}]
    assert loop.next_session_date(datetime(2026, 9, 18, 12, 0, tzinfo=ET), cal) == "2026-09-21"
    assert loop.next_session_date(datetime(2026, 9, 21, 12, 0, tzinfo=ET), cal) is None
