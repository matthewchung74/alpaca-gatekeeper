"""The model chooses from spreads that can actually pass, and we count why the rest cannot."""
from datetime import datetime

from agent import candidates
from agent.config import ET, RiskLimits
from agent.models import occ_symbol
from agent.regime import TapeRead

LIMITS = RiskLimits()
NOW = datetime(2026, 9, 21, 11, 0, tzinfo=ET)
EXPIRY = "2026-10-02"                       # 11 days out
T = "2026-09-21T14:59:30.123456789Z"        # 30s before NOW


def chain(spot=760.0, oi=lambda k: 2000):
    """SPY calls 765..800 and puts 720..755, deltas falling away from the money."""
    ch = {}
    for k in range(720, 801):
        right = "C" if k > spot else "P"
        dist = abs(k - spot)
        delta = max(0.02, 0.50 - dist * 0.0125)             # 0.30 at 16 pts, 0.10 at 32
        px = max(0.05, 6.0 - dist * 0.17)
        ch[occ_symbol("SPY", EXPIRY, right, float(k))] = {
            "greeks": {"delta": delta if right == "C" else -delta},
            "impliedVolatility": 0.15, "openInterest": oi(k),
            "latestQuote": {"bp": round(px - 0.02, 2), "ap": round(px + 0.02, 2),
                            "bs": 40, "as": 40, "t": T}}
    return ch


def tape(**kw):
    base = dict(regime="sideways", range_position=0.5, lookback_high=772.0, lookback_low=748.0,
                trend_pct=0.0, avg_range_pct=0.008, detail="t")
    base.update(kw)
    return TapeRead(**base)


def run(ch, **kw):
    args = dict(chains={"SPY": ch}, quotes={"SPY": {"bp": 759.95, "ap": 760.05}},
                tape={"SPY": tape()}, sides={"SPY": ("P", "C")}, now=NOW, expiry=EXPIRY,
                limits=LIMITS, open_spreads=[], profile="dev")
    args.update(kw)
    found, funnel, rows = candidates.enumerate_candidates(**args)
    run.rows = rows
    return found, funnel


def test_survivors_clear_every_shape_gate_and_sit_beyond_the_range():
    found, funnel = run(chain())
    assert found, funnel
    for c in found:
        assert c.width <= 5 and c.credit / c.width >= LIMITS.min_credit_pct_of_width
        assert (c.short_strike > 772.0) if c.right == "C" else (c.short_strike < 748.0)
        assert LIMITS.min_short_delta <= c.short_delta <= LIMITS.max_short_delta
    assert funnel["pairs"] > len(found) and funnel["survivors"] == len(found)
    assert funnel["failed"]["range_buffer"] > 0           # near-the-money pairs die here


def test_thin_open_interest_is_counted_as_the_reason():
    """The 2026-09-28 Monday weekly: fine quotes, almost no open interest."""
    found, funnel = run(chain(oi=lambda k: 40))
    assert found == []
    assert funnel["failed"]["liquidity"] == funnel["pairs"]


def test_only_permitted_sides_are_enumerated():
    found, _ = run(chain(), sides={"SPY": ("P",)})
    assert found and all(c.right == "P" for c in found)


def test_a_contract_already_held_is_not_offered():
    held = [dict(id="a", underlying="SPY", expiry=EXPIRY, right="C", short_strike=780.0,
                 long_strike=785.0, qty=5, entry_credit=0.6, sleeve="core")]
    found, _ = run(chain(), open_spreads=held)
    assert all(780.0 not in (c.short_strike, c.long_strike) and
               785.0 not in (c.short_strike, c.long_strike) for c in found if c.right == "C")


def test_the_snapshot_lists_candidates_and_the_funnel():
    found, funnel = run(chain())
    text = "\n".join(candidates.render(found, funnel, per_side=3))
    assert "ELIGIBLE CANDIDATES" in text and "SPY" in text and "survive" in text
    assert "range_buffer" in text


# --- the shadow ledger rows ------------------------------------------------------

def test_every_pair_becomes_a_ledger_row_with_its_failure_codes():
    # bottom of the range: the tape forbids short calls, and so must the rows say
    found, funnel = run(chain(), tape={"SPY": tape(range_position=0.05)}, sides={"SPY": ("P",)})
    rows = run.rows
    assert len(rows) > funnel["pairs"]                      # both sides and a wider delta range
    calls = [r for r in rows if r["r"] == "C"]
    assert calls and all("regime_direction" in r["fail"] for r in calls)      # refused, but recorded
    assert {(c.right, c.short_strike, c.long_strike) for c in found} == \
           {(r["r"], r["ks"], r["kl"]) for r in rows if not r["fail"]}
    assert all(r["chosen"] is False and r["traded"] is False for r in rows)


def test_ledger_rows_carry_the_metrics_the_learning_step_needs():
    run(chain(oi=lambda k: 250 if k == 783 else 900))
    r = next(x for x in run.rows if x["r"] == "C" and x["ks"] == 781 and x["kl"] == 783)
    assert r["u"] == "SPY" and r["w"] == 2 and r["spot"] == 760.0
    assert r["oi"] == 250                                   # the thinner leg
    assert 0 < r["spr"] < 0.10 and r["iv"] == 0.15 and 0.05 < r["d"] < 0.45
    assert r["emr"] > 1.0                                   # 21 points out against a ~19.7 expected move
    assert r["cr"] > 0 and r["nat"] <= r["cr"]
    assert "liquidity:oi" in r["fail"]


def test_deltas_outside_the_live_band_are_recorded_but_never_offered():
    found, _ = run(chain())
    near = [r for r in run.rows if r["d"] > LIMITS.max_short_delta]
    assert near and all("delta_band:high" in r["fail"] for r in near)
    assert all(c.short_delta <= LIMITS.max_short_delta for c in found)
