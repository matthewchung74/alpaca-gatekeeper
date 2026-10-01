"""The learning protocol: one ladder step, one change in flight, evidence first."""
from datetime import datetime, timedelta

import pytest

from agent import learning, rules
from agent.config import ET, RiskLimits
from agent.journal import SQLiteJournal

BASE = RiskLimits()
NOW = datetime(2026, 10, 30, 17, 0, tzinfo=ET)


def row(**kw):
    base = dict(u="SPY", r="P", ks=740.0, kl=735.0, w=5.0, cr=1.0, d=0.15, oi=2000, spr=0.03,
                emr=1.2, fail=[], chosen=False, traded=False, held=True, ret_hold=0.10,
                ret_mgd=0.10, mgd_rule="profit_target")
    base.update(kw)
    return base


def journal_with(tmp_path, docs):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    for i, (expiry, rows) in enumerate(docs):
        sid = j.record_shadow(profile="igk", ts=f"2026-10-{1 + i % 28:02d}T13:46:00+00:00",
                              expiry=expiry, rules_version=1, rows=rows)
        j.settle_shadow(sid, rows)
    return j


def clusters(n, mk):
    """n distinct clusters (underlying x right x expiry), 3 rows each, from mk(i)."""
    docs = []
    for i in range(n):
        u = ("SPY", "QQQ", "IWM")[i % 3]
        r = ("P", "C")[(i // 3) % 2]
        expiry = f"2026-{10 + (i // 6) // 4:02d}-{2 + 7 * ((i // 6) % 4):02d}"
        docs.append((expiry, [mk(i, u, r) for _ in range(3)]))
    return docs


# --- rules versions ---------------------------------------------------------------

def test_rules_start_at_the_base_limits_and_overrides_must_sit_on_the_ladder(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    lim, version = rules.limits_for(j, "igk", BASE)
    assert lim == BASE and version == 0
    j.put_rules("igk", {"version": 2, "overrides": {"min_open_interest": 300, "max_tranche_risk_pct": 0.5,
                                                    "min_credit_pct_of_width": 0.11}, "in_flight": None, "locks": {}, "history": []})
    lim, version = rules.limits_for(j, "igk", BASE)
    assert version == 2 and lim.min_open_interest == 300
    assert lim.max_tranche_risk_pct == BASE.max_tranche_risk_pct          # never tunable
    assert lim.min_credit_pct_of_width == BASE.min_credit_pct_of_width      # 0.11 is not on the ladder


# --- the learning step ----------------------------------------------------------------

def test_nothing_happens_below_the_minimum_effective_n(tmp_path):
    j = journal_with(tmp_path, clusters(6, lambda i, u, r: row(u=u, r=r, fail=["liquidity:oi"], ret_hold=0.30)))
    assert learning.step(j, "igk", BASE, NOW) == []
    assert (j.get_rules("igk") or rules.empty_doc())["overrides"] == {}


def test_a_clearly_profitable_refused_set_loosens_exactly_one_step(tmp_path):
    docs = clusters(24, lambda i, u, r: row(u=u, r=r, fail=["liquidity:oi"], oi=350, ret_hold=0.30))
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, fail=[], ret_hold=0.05))
    j = journal_with(tmp_path, docs)
    events = learning.step(j, "igk", BASE, NOW)
    assert len(events) == 1 and events[0]["action"] == "loosen" and events[0]["param"] == "min_open_interest"
    doc = j.get_rules("igk")
    assert doc["overrides"]["min_open_interest"] == 300 and doc["version"] == 1
    assert doc["in_flight"]["param"] == "min_open_interest" and doc["in_flight"]["old"] == 500


def test_two_qualifying_parameters_change_only_the_stronger(tmp_path):
    docs = clusters(24, lambda i, u, r: row(u=u, r=r, fail=["liquidity:oi"], ret_hold=0.30))
    # a credit of 9% of width: refused by the 10% floor, admitted at the next rung (8%)
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, fail=["credit_floor"], cr=0.45, ret_hold=0.90))
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, fail=[], ret_hold=0.05))
    j = journal_with(tmp_path, docs)
    events = learning.step(j, "igk", BASE, NOW)
    assert len(events) == 1 and events[0]["param"] == "min_credit_pct_of_width"
    assert set(j.get_rules("igk")["overrides"]) == {"min_credit_pct_of_width"}


def test_nothing_is_proposed_while_a_change_is_in_flight(tmp_path):
    docs = clusters(24, lambda i, u, r: row(u=u, r=r, fail=["liquidity:oi"], ret_hold=0.30))
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, fail=[], ret_hold=0.05))
    j = journal_with(tmp_path, docs)
    learning.step(j, "igk", BASE, NOW)
    assert learning.step(j, "igk", BASE, NOW + timedelta(days=1)) == []
    assert j.get_rules("igk")["overrides"] == {"min_open_interest": 300}


def test_a_losing_newly_admitted_band_reverts_and_locks(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    j.put_rules("igk", {"version": 1, "overrides": {"min_open_interest": 300}, "locks": {}, "history": [],
                        "in_flight": {"param": "min_open_interest", "old": 500, "new": 300,
                                      "since": "2026-10-10T20:30:00+00:00", "direction": "loosen"}})
    # since the change: the band it newly admits (OI 300-499) has lost money, across 9 clusters
    docs = clusters(9, lambda i, u, r: row(u=u, r=r, oi=350, fail=[], ret_hold=-0.40))
    for i, (expiry, rows) in enumerate(docs):
        sid = j.record_shadow(profile="igk", ts=f"2026-10-{12 + i:02d}T13:46:00+00:00", expiry=expiry,
                              rules_version=1, rows=rows)
        j.settle_shadow(sid, rows)
    events = learning.step(j, "igk", BASE, NOW)
    assert events and events[0]["action"] == "revert"
    doc = j.get_rules("igk")
    assert doc["overrides"] == {} and doc["in_flight"] is None
    assert "min_open_interest" in doc["locks"] and doc["version"] == 2


def test_a_winning_newly_admitted_band_is_confirmed_and_frees_the_slot(tmp_path):
    j = SQLiteJournal(str(tmp_path / "j.db"))
    j.put_rules("igk", {"version": 1, "overrides": {"min_open_interest": 300}, "locks": {}, "history": [],
                        "in_flight": {"param": "min_open_interest", "old": 500, "new": 300,
                                      "since": "2026-10-10T20:30:00+00:00", "direction": "loosen"}})
    docs = clusters(9, lambda i, u, r: row(u=u, r=r, oi=350, fail=[], ret_hold=0.20))
    for i, (expiry, rows) in enumerate(docs):
        sid = j.record_shadow(profile="igk", ts=f"2026-10-{12 + i:02d}T13:46:00+00:00", expiry=expiry,
                              rules_version=1, rows=rows)
        j.settle_shadow(sid, rows)
    events = learning.step(j, "igk", BASE, NOW)
    assert events[0]["action"] == "confirm"
    doc = j.get_rules("igk")
    assert doc["overrides"] == {"min_open_interest": 300} and doc["in_flight"] is None


def test_a_locked_parameter_is_left_alone(tmp_path):
    docs = clusters(24, lambda i, u, r: row(u=u, r=r, fail=["liquidity:oi"], ret_hold=0.30))
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, fail=[], ret_hold=0.05))
    j = journal_with(tmp_path, docs)
    j.put_rules("igk", {"version": 0, "overrides": {}, "in_flight": None, "history": [],
                        "locks": {"min_open_interest": (NOW + timedelta(days=30)).isoformat()}})
    assert learning.step(j, "igk", BASE, NOW) == []


def test_a_losing_marginal_band_tightens_one_step(tmp_path):
    """Admitted spreads with OI 500-999 lost; the rest of the admitted set won.
    The next tighter rung is 1000, and that is where the floor goes."""
    docs = clusters(24, lambda i, u, r: row(u=u, r=r, oi=700, fail=[], ret_hold=-0.50))
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, oi=3000, fail=[], ret_hold=0.20))
    j = journal_with(tmp_path, docs)
    events = learning.step(j, "igk", BASE, NOW)
    assert events and events[0]["action"] == "tighten" and events[0]["param"] == "min_open_interest"
    assert j.get_rules("igk")["overrides"]["min_open_interest"] == 1000


def test_learning_can_be_switched_off(tmp_path):
    docs = clusters(24, lambda i, u, r: row(u=u, r=r, fail=["liquidity:oi"], ret_hold=0.30))
    docs += clusters(24, lambda i, u, r: row(u=u, r=r, fail=[], ret_hold=0.05))
    j = journal_with(tmp_path, docs)
    from dataclasses import replace
    assert learning.step(j, "igk", replace(BASE, learn_enabled=False), NOW) == []
