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


def test_reconcile_retires_a_spread_the_broker_no_longer_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "fills", lambda profile, after: [])
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
    monkeypatch.setattr(cli, "fill_result", lambda oid, profile, **kw: {
        "qty": 0, "credit": None, "status": "canceled", "timed_out": False})
    assert loop.cancel_stale_orders(j, "dev", now, dry_run=False) == (["o1", "o2"], [])
    assert cancelled == ["o1", "o2"]


def test_next_session_skips_the_weekend():
    cal = [{"date": "2026-09-18", "open": "09:30", "close": "16:00"},
           {"date": "2026-09-21", "open": "09:30", "close": "16:00"}]
    assert loop.next_session_date(datetime(2026, 9, 18, 12, 0, tzinfo=ET), cal) == "2026-09-21"
    assert loop.next_session_date(datetime(2026, 9, 21, 12, 0, tzinfo=ET), cal) is None


# --- Codex follow-up review 2026-09-18 ------------------------------------------

def test_a_liquidation_keeps_going_until_the_book_is_flat():
    """Forced close fills 4 of 10, equity recovers above the limit, and the next
    sweep used to leave the other 6 open under a halt that was still in force."""
    assert loop.flatten_reason(halted=True, breached=False, open_spreads=[{"id": 1}]) is not None
    assert loop.flatten_reason(halted=True, breached=False, open_spreads=[]) is None
    assert loop.flatten_reason(halted=False, breached=False, open_spreads=[{"id": 1}]) is None
    assert "limit" in loop.flatten_reason(halted=False, breached=True, open_spreads=[], detail="daily loss -4.1% at/over the 4% limit")


def test_an_orphan_is_only_cancelled_when_the_broker_says_so(tmp_path, monkeypatch):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    now = datetime(2026, 9, 18, 14, 0, tzinfo=ET)
    orders = [{"id": "gone", "client_order_id": "hack-a", "submitted_at": "2026-09-18T17:40:00Z"},
              {"id": "stuck", "client_order_id": "hack-b", "submitted_at": "2026-09-18T17:41:00Z"}]
    monkeypatch.setattr(cli, "open_orders", lambda profile: orders)
    monkeypatch.setattr(cli, "cancel_order", lambda oid, profile: oid == "gone")
    monkeypatch.setattr(cli, "fill_result", lambda oid, profile, **kw: {
        "qty": 0, "credit": None,
        "status": "canceled" if oid == "gone" else "pending_cancel",
        "timed_out": oid != "gone"})
    cancelled, unresolved = loop.cancel_stale_orders(j, "dev", now, dry_run=False)
    assert cancelled == ["gone"]
    assert len(unresolved) == 1 and "stuck" in unresolved[0]


def test_not_knowing_the_open_orders_is_not_the_same_as_having_none(tmp_path, monkeypatch):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    def down(profile):
        raise cli.CLIError(["order", "list"], 1, "503")
    monkeypatch.setattr(cli, "open_orders", down)
    cancelled, unresolved = loop.cancel_stale_orders(j, "dev", NOW, dry_run=False)
    assert cancelled == [] and unresolved and "unavailable" in unresolved[0]


def test_a_vanished_spread_is_not_booked_as_a_zero_pnl_trade(tmp_path, monkeypatch):
    """Retiring a spread the broker no longer holds recorded realized_pnl 0.0
    while logging 'P&L unknown'. Unknown is None, unless the fills say otherwise."""
    j = _book(tmp_path)
    later = datetime.now(ET).replace(year=2030)
    monkeypatch.setattr(cli, "fills", lambda profile, after: [])
    loop.reconcile(j, "dev", [], later, dry_run=False)
    assert j.all_spreads("dev")[0]["realized_pnl"] is None


def test_a_vanished_spread_recovers_its_pnl_from_the_brokers_fills(tmp_path, monkeypatch):
    j = _book(tmp_path)                       # SPY 770/775 calls x10 @ 0.50 credit
    later = datetime.now(ET).replace(year=2030)
    monkeypatch.setattr(cli, "fills", lambda profile, after: [
        {"symbol": "SPY260910C00770000", "side": "sell_short", "qty": "10", "price": "1.20"},   # the open
        {"symbol": "SPY260910C00775000", "side": "buy", "qty": "10", "price": "0.70"},          # the open
        {"symbol": "SPY260910C00770000", "side": "buy", "qty": "10", "price": "0.40"},          # closed by hand
        {"symbol": "SPY260910C00775000", "side": "sell", "qty": "10", "price": "0.10"}])
    loop.reconcile(j, "dev", [], later, dry_run=False)
    row = j.all_spreads("dev")[0]
    assert row["exit_debit"] == pytest.approx(0.30) and row["realized_pnl"] == pytest.approx(200.0)


# --- the whole cycle body against a mocked broker ------------------------------

def _wire(monkeypatch, observations, *, fills, marks=(0.45, 0.05)):
    """Patch every broker touchpoint _cycle_body uses. `observations` are handed
    out one per observe() call, the last one repeating."""
    today = datetime.now(ET).replace(hour=11, minute=0, second=0, microsecond=0)
    day = today.strftime("%Y-%m-%d")
    monkeypatch.setattr(loop, "now_et", lambda: today)
    monkeypatch.setattr(cli, "trading_calendar",
                        lambda s, e, profile: [{"date": day, "open": "09:30", "close": "16:00"}])
    monkeypatch.setattr(cli, "open_orders", lambda profile: [])
    monkeypatch.setattr(cli, "ex_dividend", lambda *a, **k: None)
    monkeypatch.setattr(cli, "fills", lambda profile, after: [])
    monkeypatch.setattr(loop, "resolve_expiry", lambda profile, now: "2026-09-10")
    seq = list(observations)
    monkeypatch.setattr(loop, "observe", lambda profile, expiry: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(cli, "option_quotes", lambda syms, profile: {
        "SPY260910C00770000": {"ap": marks[0], "bp": marks[0] - 0.02},
        "SPY260910C00775000": {"ap": marks[1] + 0.02, "bp": marks[1]}})
    sent = []
    monkeypatch.setattr(cli, "submit_mleg", lambda **kw: sent.append(kw) or {"id": f"o{len(sent)}"})
    it = iter(fills)
    monkeypatch.setattr(cli, "fill_result", lambda oid, profile, **kw: dict(
        zip(("qty", "credit"), next(it)), status="filled", timed_out=False))
    return sent, today


def _account_obs(equity, held, spot=765.0):
    return {"account": {"equity": str(equity), "last_equity": "100000", "cash": "0"},
            "equity": float(equity),
            "positions": [{"symbol": "SPY260910C00770000", "qty": str(-held)},
                          {"symbol": "SPY260910C00775000", "qty": str(held)}] if held else [],
            "quotes": {"SPY": {"bp": spot - 0.05, "ap": spot + 0.05}},
            "chains": {}, "bars": {}, "news": []}


def test_a_daily_liquidation_survives_a_partial_fill_and_a_recovery(tmp_path, monkeypatch):
    """Codex follow-up, reproduced through the real cycle body. Equity 95,900 on
    a 100,000 prior close; the forced close fills 4 of 10; the next sweep sees
    96,100, back over the line. The other 6 used to be left open."""
    j = _book(tmp_path)
    s = Settings(profile="dev")
    sent, _ = _wire(monkeypatch, [_account_obs(95_900, 10)], fills=[(4, 0.42), (6, 0.41)])
    assert loop._cycle_body(s, j, manage_only=True) == 0
    assert sent[0]["qty"] == 10
    assert j.open_spreads("dev")[0]["qty"] == 6                       # partial: 6 still held
    assert any(c["action"] == "halt" for c in j.recent_cycles(50, "dev"))

    monkeypatch.setattr(loop, "observe", lambda profile, expiry: _account_obs(96_100, 6))
    assert loop._cycle_body(s, j, manage_only=True) == 0
    assert sent[1]["qty"] == 6                                        # the liquidation went on
    assert j.open_spreads("dev") == []
    done = j.all_spreads("dev")[0]
    assert done["exit_rule"] == "daily_loss_flatten"
    assert done["realized_pnl"] == pytest.approx((0.5 - 0.42) * 400 + (0.5 - 0.41) * 600)


def test_the_gates_judge_the_market_as_it_is_after_the_model_answers(tmp_path, monkeypatch):
    """The tape moved while the model was thinking. Calls were permitted when it
    was asked; by the final observation spot sits in the bottom of the range and
    they are not. The verdict must come from the final observation."""
    from agent.models import AgentDecision
    j = SQLiteJournal(str(tmp_path / "j.db"))
    today = datetime.now(ET)
    from datetime import timedelta as _td
    bars = [{"t": (today - _td(days=14 - i)).strftime("%Y-%m-%dT04:00:00Z"),
             "o": 760, "h": 770, "l": 750, "c": 760} for i in range(12)]

    def market(spot):
        o = _account_obs(100_000, 0, spot=spot)
        o["bars"] = {"SPY": bars}
        o["chains"] = {"SPY": {}}
        return o

    proposal = TradeProposal(underlying="SPY", expiry="2026-09-10", right="C", short_strike=780.0,
                             long_strike=785.0, qty=2, net_price=0.60, sleeve="core", rationale="t")

    class FakeBrain:
        def __init__(self, **kw): pass
        def preflight(self): pass
        def decide(self, snapshot, limits):
            assert "core may sell: P/C" in snapshot                   # calls were allowed when asked
            return AgentDecision(reasoning="calls look fine", proposal=proposal)

    monkeypatch.setattr(loop, "Brain", FakeBrain)
    sent, _ = _wire(monkeypatch, [market(760.0), market(751.0)], fills=[])
    assert loop._cycle_body(Settings(profile="dev"), j, manage_only=False) == 0
    assert sent == []                                                 # nothing reached the broker
    blocked = next(c for c in j.recent_cycles(10, "dev") if c["action"] == "blocked")
    import json as _json
    verdict = next(g for g in _json.loads(blocked["gates"]) if g["name"] == "regime_direction")
    assert not verdict["passed"] and "range position 5%" in verdict["detail"]


def test_no_trade_when_the_final_observation_cannot_be_made(tmp_path, monkeypatch):
    from agent.models import AgentDecision
    j = SQLiteJournal(str(tmp_path / "j.db"))
    proposal = TradeProposal(underlying="SPY", expiry="2026-09-10", right="C", short_strike=780.0,
                             long_strike=785.0, qty=2, net_price=0.60, sleeve="core", rationale="t")

    class FakeBrain:
        def __init__(self, **kw): pass
        def preflight(self): pass
        def decide(self, snapshot, limits):
            return AgentDecision(reasoning="r", proposal=proposal)

    monkeypatch.setattr(loop, "Brain", FakeBrain)
    first = _account_obs(100_000, 0)
    first["chains"] = {"SPY": {}}
    sent, _ = _wire(monkeypatch, [first], fills=[])
    calls = {"n": 0}
    def observe(profile, expiry):
        calls["n"] += 1
        if calls["n"] > 1:
            raise cli.CLIError(["account", "get"], 1, "503")
        return first
    monkeypatch.setattr(loop, "observe", observe)
    assert loop._cycle_body(Settings(profile="dev"), j, manage_only=False) == 1
    assert sent == []
    assert any("final observation failed" in (c.get("error") or "") for c in j.recent_cycles(10, "dev"))


def test_the_expiry_picker_prefers_the_liquid_friday_weekly():
    """Nearest expiry >= 7 days out was the 09-28 MONDAY weekly: days old, and
    not one spread with both legs at OI >= 500. Friday weeklies had hundreds."""
    listed = {"2026-09-28", "2026-09-30", "2026-10-02", "2026-10-05", "2026-10-09"}
    assert loop.pick_expiry(listed) == "2026-10-02"
    assert loop.pick_expiry({"2026-09-28", "2026-09-30"}) == "2026-09-28"     # no Friday listed: nearest


def test_expiry_lookup_pages_past_the_first_expiry(monkeypatch):
    """One page of contracts held only the nearest expiry or two (~300 contracts
    each), so a Friday further out was never in the list to be preferred."""
    pages = {"": {"option_contracts": [{"expiration_date": "2026-09-28"}] * 3, "next_page_token": "p2"},
             "p2": {"option_contracts": [{"expiration_date": "2026-09-30"},
                                         {"expiration_date": "2026-10-02"}], "next_page_token": None}}
    seen = []
    def fake_run(*args, profile, **kw):
        path = args[2]
        seen.append(path)
        tok = path.split("page_token=")[1] if "page_token=" in path else ""
        return pages[tok]
    monkeypatch.setattr(cli, "run", fake_run)
    got = cli.list_expiries("SPY", "dev", "2026-09-28", "2026-10-08")
    assert got == ["2026-09-28", "2026-09-30", "2026-10-02"]
    assert "type=call" in seen[0] and "expiration_date_lte=2026-10-08" in seen[0] and len(seen) == 2


# --- the shadow ledger lives in the journal ------------------------------------

def _rows():
    return [dict(u="SPY", r="P", ks=740.0, kl=735.0, w=5, cr=1.10, nat=1.05, d=0.15, iv=0.15,
                 oi=2000, spr=0.03, emr=1.2, spot=760.0, fail=[], chosen=False, traded=False),
            dict(u="SPY", r="C", ks=781.0, kl=783.0, w=2, cr=0.30, nat=0.28, d=0.17, iv=0.15,
                 oi=250, spr=0.04, emr=1.3, spot=760.0, fail=["liquidity:oi"], chosen=False, traded=False)]


def test_shadow_ledger_round_trips_and_marks_one_row(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    sid = j.record_shadow(profile="dev", ts="2026-09-21T13:46:00+00:00", expiry="2026-10-02",
                          rules_version=3, rows=_rows())
    j.mark_shadow(sid, chosen=("SPY", "P", 740.0, 735.0))
    j.mark_shadow(sid, traded=("SPY", "P", 740.0, 735.0))
    docs = j.unsettled_shadow("dev", on_or_before="2026-10-02")
    assert len(docs) == 1 and docs[0]["id"] == sid and docs[0]["rules_version"] == 3
    rows = docs[0]["rows"]
    assert rows[0]["chosen"] and rows[0]["traded"] and not rows[1]["chosen"]
    assert j.unsettled_shadow("dev", on_or_before="2026-10-01") == []          # not yet expired
    assert j.unsettled_shadow("comp", on_or_before="2026-10-02") == []         # other account


def test_settled_documents_leave_the_unsettled_set(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    sid = j.record_shadow(profile="dev", ts="2026-09-21T13:46:00+00:00", expiry="2026-10-02",
                          rules_version=1, rows=_rows())
    settled = [dict(r, held=True, v_exp=0.0, ret_hold=0.28, ret_mgd=0.14, mgd_rule="profit_target")
               for r in _rows()]
    j.settle_shadow(sid, settled)
    assert j.unsettled_shadow("dev", on_or_before="2026-12-31") == []
    got = j.settled_shadow("dev")
    assert len(got) == 1 and got[0]["rows"][0]["ret_hold"] == 0.28 and got[0]["settled_at"]


def test_the_entry_cycle_journals_the_ledger_and_marks_the_pick(tmp_path, monkeypatch):
    from agent.models import AgentDecision
    j = SQLiteJournal(str(tmp_path / "j.db"))
    today = datetime.now(ET)
    from datetime import timedelta as _td
    bars = [{"t": (today - _td(days=14 - i)).strftime("%Y-%m-%dT04:00:00Z"),
             "o": 760, "h": 770, "l": 750, "c": 760} for i in range(12)]
    obs = _account_obs(100_000, 0)
    obs["bars"] = {"SPY": bars}
    obs["chains"] = {"SPY": {}}
    proposal = TradeProposal(underlying="SPY", expiry="2026-09-10", right="C", short_strike=780.0,
                             long_strike=785.0, qty=2, net_price=0.60, sleeve="core", rationale="t")

    class FakeBrain:
        def __init__(self, **kw): pass
        def preflight(self): pass
        def decide(self, snapshot, limits):
            return AgentDecision(reasoning="r", proposal=proposal)

    monkeypatch.setattr(loop, "Brain", FakeBrain)
    monkeypatch.setattr(loop.cand, "enumerate_candidates", lambda **kw: ([], {"pairs": 2, "survivors": 0, "failed": {}}, [
        dict(u="SPY", r="C", ks=780.0, kl=785.0, w=5, cr=0.6, nat=0.55, d=0.15, iv=0.15, oi=900,
             spr=0.03, emr=1.1, spot=760.0, fail=[], chosen=False, traded=False)]))
    sent, _ = _wire(monkeypatch, [obs], fills=[(2, 0.60)])
    loop._cycle_body(Settings(profile="dev"), j, manage_only=False)
    docs = j.unsettled_shadow("dev", on_or_before="2026-12-31")
    assert len(docs) == 1
    row = docs[0]["rows"][0]
    assert row["chosen"] is True
    assert row["traded"] is (len(sent) == 1)
