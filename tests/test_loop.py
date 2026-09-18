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
