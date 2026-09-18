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
    return candidates.enumerate_candidates(**args)


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
