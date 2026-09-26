"""Every model call records what it cost, so spend is measured, not guessed."""
import json

import pytest

from agent import usage as usage_mod
from agent.brain import Brain
from agent.config import RiskLimits
from agent.journal import SQLiteJournal
from agent.models import AgentDecision

LIMITS = RiskLimits()


class FakeUsage:
    input_tokens = 9904
    output_tokens = 3120
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class FakeResponse:
    stop_reason = "end_turn"
    usage = FakeUsage()
    parsed_output = AgentDecision(reasoning="r", proposal=None)


class FakeClient:
    class messages:
        @staticmethod
        def parse(**kw):
            return FakeResponse()

        @staticmethod
        def create(**kw):
            return FakeResponse()


def test_decide_records_what_the_call_used():
    b = Brain(client=FakeClient())
    b.decide("snapshot", LIMITS)
    u = b.last_usage
    assert u["input"] == 9904 and u["output"] == 3120 and u["model"] == b.model
    assert u["cost_usd"] == pytest.approx(9904 * 4 / 1e6 + 3120 * 20 / 1e6)


def test_a_refusal_is_still_counted():
    class Refusing(FakeClient):
        class messages:
            @staticmethod
            def parse(**kw):
                r = FakeResponse(); r.stop_reason = "refusal"; return r
    b = Brain(client=Refusing())
    b.decide("snapshot", LIMITS)
    assert b.last_usage["output"] == 3120


def test_preflight_is_counted_too():
    b = Brain(client=FakeClient())
    b.preflight()
    assert b.last_usage["input"] == 9904


def test_pricing_covers_the_models_we_run_and_falls_back_loudly():
    assert usage_mod.price("claude-opus-5-5") == (4.0, 20.0)
    assert usage_mod.price("claude-opus-5") == (5.0, 25.0)
    assert usage_mod.price("some-future-model") is None
    u = usage_mod.summarise(FakeUsage(), "some-future-model")
    assert u["cost_usd"] is None and u["input"] == 9904


def test_the_journal_stores_usage_on_the_cycle(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    j.record_cycle(profile="igk", action="submitted", equity=50_000.0,
                   usage={"input": 9904, "output": 3120, "cost_usd": 0.1020, "model": "claude-opus-5-5"})
    row = j.recent_cycles(limit=1, profile="igk")[0]
    got = json.loads(row["usage"])
    assert got["input"] == 9904 and got["cost_usd"] == pytest.approx(0.1020)


def test_spend_report_adds_up_what_was_recorded(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    for i in range(3):
        j.record_cycle(profile="igk", action="submitted",
                       usage={"input": 10_000, "output": 3_000, "cost_usd": 0.10, "model": "claude-opus-5-5"})
    j.record_cycle(profile="igk", action="error")                      # no model call
    s = usage_mod.spend(j, "igk")
    assert s["calls"] == 3 and s["input"] == 30_000 and s["output"] == 9_000
    assert s["cost_usd"] == pytest.approx(0.30) and s["per_call"] == pytest.approx(0.10)


def test_an_older_journal_gains_the_usage_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS does not alter an existing table, so a
    journal written before this change must be migrated, not crash."""
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        profile TEXT NOT NULL, regime TEXT, equity REAL, snapshot TEXT, reasoning TEXT,
        proposal TEXT, gates TEXT, action TEXT NOT NULL, order_id TEXT, error TEXT);""")
    con.execute("INSERT INTO cycles (ts, profile, action) VALUES ('2026-09-01T00:00:00','igk','submitted')")
    con.commit(); con.close()
    j = SQLiteJournal(path)
    j.record_cycle(profile="igk", action="submitted", usage={"input": 1, "output": 2, "cost_usd": 0.1})
    rows = j.recent_cycles(limit=2, profile="igk")
    assert json.loads(rows[0]["usage"])["input"] == 1 and rows[1]["usage"] is None
