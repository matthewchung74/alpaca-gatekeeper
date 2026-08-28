from datetime import datetime

import pytest

from agent import regime, risk
from agent.config import ET, RiskLimits, TARGET_EXPIRY
from agent.models import TradeProposal, occ_symbol

LIMITS = RiskLimits()
MIDDAY = datetime(2026, 8, 28, 13, 0, tzinfo=ET)


def proposal(**kw) -> TradeProposal:
    base = dict(underlying="SPY", expiry=TARGET_EXPIRY, right="P",
                short_strike=752.0, long_strike=747.0, qty=5,
                net_price=0.47, sleeve="core", rationale="t")
    base.update(kw)
    return TradeProposal(**base)


def chain_for(p: TradeProposal) -> dict:
    return {
        occ_symbol(p.underlying, p.expiry, p.right, s):
            {"latestQuote": {"bp": 1.48, "ap": 1.54}, "openInterest": 5000}
        for s in (p.short_strike, p.long_strike)
    }


def gates_for(p, regime_name):
    return risk.evaluate(
        p, profile="dev", now=MIDDAY, equity=100_000.0, day_start_equity=100_000.0,
        open_positions=[], chain=chain_for(p), limits=LIMITS, regime=regime_name,
    )


# --- policy shape --------------------------------------------------------

def test_regime_never_increases_risk_above_base():
    """The configured budget is a ceiling. No regime may exceed it."""
    for name, pol in regime.POLICY.items():
        assert pol.size_multiplier <= 1.0, name


def test_sideways_gets_full_budget():
    assert regime.effective_tranche_pct("sideways", 0.04) == pytest.approx(0.04)


def test_bear_is_the_most_defensive():
    pcts = {r: regime.effective_tranche_pct(r, 0.04) for r in regime.POLICY}
    assert pcts["bear"] == min(pcts.values())
    assert pcts["bear"] < pcts["bull"] < pcts["sideways"]


def test_unknown_regime_falls_back_to_most_defensive():
    assert regime.policy_for("garbage") == regime.POLICY["bear"]
    assert regime.policy_for("") == regime.POLICY["bear"]


# --- direction control ---------------------------------------------------

def test_bull_permits_puts_and_forbids_calls():
    assert regime.direction_allowed("bull", "P")
    assert not regime.direction_allowed("bull", "C")


def test_bear_forbids_short_puts():
    """Selling puts into a downtrend is the failure mode this exists to prevent."""
    assert not regime.direction_allowed("bear", "P")
    assert regime.direction_allowed("bear", "C")


def test_sideways_permits_both_wings():
    assert regime.direction_allowed("sideways", "P")
    assert regime.direction_allowed("sideways", "C")


def test_direction_gate_blocks_put_spread_in_bear_regime():
    g = next(x for x in gates_for(proposal(right="P"), "bear")
             if x.name == "regime_direction")
    assert not g.passed


def test_direction_gate_passes_call_spread_in_bear_regime():
    p = proposal(right="C", short_strike=780.0, long_strike=785.0)
    g = next(x for x in gates_for(p, "bear") if x.name == "regime_direction")
    assert g.passed


# --- sizing --------------------------------------------------------------

def test_resize_leaves_a_fitting_proposal_alone():
    p = proposal(qty=8)          # 8 x 453 = 3624 <= 4000
    qty, note = regime.resize_to_budget(p, equity=100_000.0, effective_pct=0.04)
    assert qty == 8 and "fits budget" in note


def test_resize_cuts_an_oversized_proposal_rather_than_blocking():
    p = proposal(qty=50)
    qty, note = regime.resize_to_budget(p, equity=100_000.0, effective_pct=0.04)
    assert qty == 8 and "resized" in note
    assert p.max_loss_per_spread * qty <= 100_000.0 * 0.04


def test_bear_regime_cuts_size_far_below_sideways():
    p = proposal(qty=20)
    side_qty, _ = regime.resize_to_budget(
        p, equity=100_000.0, effective_pct=regime.effective_tranche_pct("sideways", 0.04))
    bear_qty, _ = regime.resize_to_budget(
        p, equity=100_000.0, effective_pct=regime.effective_tranche_pct("bear", 0.04))
    assert bear_qty < side_qty
    assert bear_qty == 3        # explicit 0.04 base above: 0.35 * 4% = 1400; 1400 // 453 = 3


def test_resize_returns_zero_when_budget_cannot_fund_one_spread():
    p = proposal(qty=1, short_strike=800.0, long_strike=700.0, net_price=1.0)
    qty, note = regime.resize_to_budget(p, equity=10_000.0, effective_pct=0.04)
    assert qty == 0 and "cannot fund" in note


# --- the gate reflects the regime budget ---------------------------------

def test_tranche_gate_tightens_in_bear_regime():
    """Same trade: fits the sideways budget, breaches the bear one."""
    side = 100_000.0 * regime.effective_tranche_pct("sideways", LIMITS.max_tranche_risk_pct)
    bear = 100_000.0 * regime.effective_tranche_pct("bear", LIMITS.max_tranche_risk_pct)
    per = proposal().max_loss_per_spread
    qty = int(((side + bear) / 2) // per)          # between the two budgets
    p = proposal(right="C", short_strike=780.0, long_strike=785.0, qty=qty)
    assert next(x for x in gates_for(p, "sideways") if x.name == "tranche_risk").passed
    assert not next(x for x in gates_for(p, "bear") if x.name == "tranche_risk").passed


def test_tranche_gate_detail_shows_the_regime_math():
    g = next(x for x in gates_for(proposal(), "bull") if x.name == "tranche_risk")
    assert "85%" in g.detail and "core budget" in g.detail


# --- macro: only derivable dates, never invented ones ----------------------

def test_nfp_is_the_first_friday():
    from datetime import date
    from agent.macro import _first_friday
    assert _first_friday(2026, 9) == date(2026, 9, 4)
    assert _first_friday(2026, 10) == date(2026, 10, 2)


def test_jobless_claims_land_on_thursdays_only():
    from datetime import date, timedelta
    from agent.macro import upcoming
    for i in range(14):
        d = date(2026, 8, 24) + timedelta(days=i)
        claims = [e for e in upcoming(0, d) if "jobless" in e["event"].lower()]
        assert bool(claims) == (d.weekday() == 3), d


def test_expiry_day_carries_a_claims_print():
    """Sep 3 is a Thursday and is also when our positions settle."""
    from datetime import date
    from agent.macro import upcoming
    ev = upcoming(0, date(2026, 9, 3))
    assert any("jobless" in e["event"].lower() for e in ev)


def test_nfp_falls_after_our_expiry():
    """NFP is the window's biggest gap risk and must land after Sep 3."""
    from datetime import date
    from agent.macro import _first_friday
    assert _first_friday(2026, 9) > date(2026, 9, 3)


def test_macro_headlines_filter_picks_out_macro():
    from agent.macro import macro_headlines
    news = [
        {"headline": "USA Initial Jobless Claims 203K Vs 208K Est."},
        {"headline": "Acme Corp announces new CFO"},
        {"headline": "Fed's Goolsbee says inflation is stubborn"},
    ]
    got = [n["headline"] for n in macro_headlines(news)]
    assert len(got) == 2 and "Acme" not in " ".join(got)


# --- the barbell: two sleeves that lean opposite ways --------------------

def satellite(**kw) -> TradeProposal:
    """A call DEBIT spread: long strike nearer the money than the short."""
    base = dict(underlying="SPY", expiry=TARGET_EXPIRY, right="C",
                short_strike=785.0, long_strike=780.0, qty=2,
                net_price=1.50, sleeve="satellite", rationale="t")
    base.update(kw)
    return TradeProposal(**base)


def test_satellite_economics_invert_the_core():
    """A debit spread can only lose the debit; profit is the rest of the width."""
    sat = satellite(net_price=1.50, qty=2)          # 5-wide
    assert sat.max_loss_per_spread == pytest.approx(150.0)
    assert sat.max_profit_per_spread == pytest.approx(350.0)
    core = proposal(net_price=1.50, qty=2)
    assert core.max_loss_per_spread == pytest.approx(350.0)
    assert core.max_profit_per_spread == pytest.approx(150.0)


def test_signed_limit_flips_by_sleeve():
    """Alpaca mleg: negative = credit required, positive = debit paid."""
    assert proposal(net_price=0.80).signed_limit == pytest.approx(-0.80)
    assert satellite(net_price=1.50).signed_limit == pytest.approx(1.50)


def test_sleeves_lean_opposite_ways_in_a_bull_tape():
    """Core sells puts against the move; satellite buys calls with it."""
    assert regime.direction_allowed("bull", "P", "core")
    assert not regime.direction_allowed("bull", "C", "core")
    assert regime.direction_allowed("bull", "C", "satellite")
    assert not regime.direction_allowed("bull", "P", "satellite")


def test_sleeves_lean_opposite_ways_in_a_bear_tape():
    assert regime.direction_allowed("bear", "C", "core")
    assert regime.direction_allowed("bear", "P", "satellite")
    assert not regime.direction_allowed("bear", "P", "core")


def test_no_satellite_in_a_sideways_tape():
    """No trend to buy, so paying a debit for convexity is burning premium."""
    assert not regime.direction_allowed("sideways", "C", "satellite")
    assert not regime.direction_allowed("sideways", "P", "satellite")
    assert regime.direction_allowed("sideways", "P", "core")
    assert regime.direction_allowed("sideways", "C", "core")


def test_satellite_gets_a_much_smaller_budget():
    for r in ("bull", "bear"):
        core = regime.budget_pct_for(r, "core", LIMITS)
        sat = regime.budget_pct_for(r, "satellite", LIMITS)
        assert sat < core, r
        assert sat == pytest.approx(core * LIMITS.max_satellite_risk_pct
                                    / LIMITS.max_tranche_risk_pct)


def test_satellite_blocked_in_sideways_by_the_gate():
    g = next(x for x in gates_for(satellite(), "sideways") if x.name == "regime_direction")
    assert not g.passed and "nothing" in g.detail


def test_satellite_passes_the_gate_in_a_bull_tape():
    g = next(x for x in gates_for(satellite(), "bull") if x.name == "regime_direction")
    assert g.passed
