"""Statistics over the settled ledger: effective n, regret, calibration, the 2x2."""
import pytest

from agent import shadow_stats as st
from agent.config import RiskLimits

LIMITS = RiskLimits()


def row(**kw):
    base = dict(u="SPY", r="P", ks=740.0, kl=735.0, w=5.0, cr=1.0, d=0.15, oi=2000, spr=0.03,
                emr=1.2, fail=[], chosen=False, traded=False, held=True, ret_hold=0.25,
                ret_mgd=0.12, mgd_rule="profit_target", expiry="2026-10-02")
    base.update(kw)
    return base


def test_effective_n_collapses_one_cluster_to_one():
    same = [row(ks=740.0 - i) for i in range(300)]              # one underlying, right, expiry
    assert st.effective_n(same) == pytest.approx(1.0)
    six = [row(u=u, r=r) for u in ("SPY", "QQQ", "IWM") for r in ("P", "C")]
    assert st.effective_n(six) == pytest.approx(6.0)
    lopsided = [row(expiry="2026-10-02")] * 90 + [row(expiry="2026-10-09")] * 10
    assert 1.0 < st.effective_n(lopsided) < 2.0


def test_gate_regret_is_signed_the_way_the_learning_step_expects():
    admitted = [row(expiry=f"2026-10-{d:02d}", ret_hold=0.10) for d in (2, 9, 16, 23)]
    refused = [row(expiry=f"2026-10-{d:02d}", fail=["liquidity:oi"], ret_hold=0.25) for d in (2, 9, 16, 23)]
    out = st.gate_regret(admitted + refused, field="ret_hold")
    g = out["liquidity:oi"]
    assert g["n"] == 4 and g["n_eff"] == pytest.approx(4.0)
    assert g["mean"] == pytest.approx(0.25) and g["diff"] == pytest.approx(0.15)       # refused beat admitted
    assert g["hold_rate"] == 1.0 and g["t"] > 0
    # a row that failed two gates counts for neither (it is not a clean test of either)
    mixed = row(fail=["liquidity:oi", "credit_floor"], ret_hold=-1.0)
    assert st.gate_regret(admitted + refused + [mixed], field="ret_hold")["liquidity:oi"]["n"] == 4


def test_calibration_compares_implied_and_realized_hold_rates():
    rows = [row(d=0.15, held=(i % 10 != 0), expiry=f"2026-10-{2 + i:02d}", fail=[]) for i in range(20)]
    out = st.calibration(rows)
    b = out["0.10-0.20"]
    assert b["n"] == 20 and b["implied_hold"] == pytest.approx(0.85)
    assert b["realized_hold"] == pytest.approx(0.90) and b["edge"] == pytest.approx(0.05)


def test_model_vs_field_scores_the_pick_against_its_own_cycle():
    docs = [{"rows": [row(chosen=True, ret_hold=0.30), row(ret_hold=0.10), row(ret_hold=0.20),
                      row(fail=["liquidity:oi"], ret_hold=0.90)]}]         # refused rows are not the field
    out = st.model_vs_field(docs, field="ret_hold")
    assert out["n"] == 1 and out["pick_mean"] == pytest.approx(0.30)
    assert out["field_mean"] == pytest.approx(0.20) and out["diff"] == pytest.approx(0.10)


def test_view_structure_grid_names_luck():
    spreads = [dict(underlying="SPY", right="C", short_strike=775.0, long_strike=778.0, expiry="2026-09-19",
                    entry_credit=0.5, qty=1, status="closed", realized_pnl=+80.0),     # view held, profit
                dict(underlying="SPY", right="C", short_strike=750.0, long_strike=753.0, expiry="2026-09-19",
                    entry_credit=0.5, qty=1, status="closed", realized_pnl=-200.0),    # view failed, loss
                dict(underlying="SPY", right="C", short_strike=750.0, long_strike=753.0, expiry="2026-09-19",
                    entry_credit=0.5, qty=1, status="closed", realized_pnl=+40.0),     # view failed, profit: luck
                dict(underlying="SPY", right="C", short_strike=775.0, long_strike=778.0, expiry="2026-09-19",
                    entry_credit=0.5, qty=1, status="closed", realized_pnl=-60.0)]     # view held, loss: structure
    grid = st.view_structure(spreads, closes={("SPY", "2026-09-19"): 761.69})
    assert grid["view_held_profit"] == 1 and grid["view_failed_loss"] == 1
    assert grid["view_failed_profit_luck"] == 1 and grid["view_held_loss_structure"] == 1
    assert grid["attributable"] == 3            # everything except luck


def test_welch_t_is_zero_for_identical_samples_and_large_for_separated_ones():
    assert st.welch([0.1, 0.2, 0.3], [0.1, 0.2, 0.3])[2] == pytest.approx(0.0)
    assert st.welch([0.9, 1.0, 1.1, 1.0], [-1.0, -0.9, -1.1, -1.0])[2] > 10
