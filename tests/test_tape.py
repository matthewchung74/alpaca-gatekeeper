"""The regime is computed from the bars, not asked of the model."""
from agent import regime
from agent.config import RiskLimits

LIMITS = RiskLimits()


def bars(closes, rng=0.006, start_day=1):
    """Daily bars with a fixed high-low range as a fraction of the close."""
    out = []
    for i, c in enumerate(closes):
        day = start_day + i
        out.append({"t": f"2026-08-{day:02d}T04:00:00Z", "o": c, "h": c * (1 + rng / 2),
                    "l": c * (1 - rng / 2), "c": c})
    return out


def test_needs_ten_completed_sessions():
    read = regime.classify(bars([760.0] * 6), spot=760.0, today="2026-08-20")
    assert read.regime == "sideways"
    assert read.range_position is None and read.lookback_high is None
    assert "6 completed" in read.detail


def test_today_partial_bar_is_ignored():
    """A daily bar dated today is the session in progress; it is not history."""
    b = bars([760.0] * 10 + [900.0], start_day=1)      # the 900 bar is dated 08-11
    read = regime.classify(b, spot=760.0, today="2026-08-11")
    assert read.lookback_high < 900


def test_the_09_01_tape_is_sideways_at_the_bottom_of_the_range():
    """SPY closes 08-18..08-31, spot 762 on 09-01: a 0.7% dip inside a
    13-point band. This must NOT read as bear, and it sits at the range low."""
    closes = [767.45, 769.06, 762.6, 765.72, 763.47, 765.91, 766.08, 771.1, 769.35, 767.05]
    b = bars(closes, rng=0.008, start_day=18)
    b[2]["l"] = 762.04; b[7]["h"] = 772.36; b[8]["h"] = 775.3   # the real extremes
    read = regime.classify(b, spot=762.0, today="2026-09-01")
    assert read.regime == "sideways"
    assert read.range_position < 0.25          # bottom quarter (synthetic lows widen the band)
    assert regime.core_sides(read, LIMITS) == ("P",)     # no short calls at the low


def test_top_of_range_forbids_short_puts():
    closes = [760.0] * 10
    read = regime.classify(bars(closes), spot=760.0 * 1.003, today="2026-08-20")
    assert read.regime == "sideways"
    assert read.range_position > 0.75
    assert regime.core_sides(read, LIMITS) == ("C",)


def test_middle_of_range_permits_both():
    read = regime.classify(bars([760.0] * 10), spot=760.0, today="2026-08-20")
    assert regime.core_sides(read, LIMITS) == ("P", "C")


def test_a_real_decline_is_bear_and_permits_calls_only():
    closes = [790 - 3 * i for i in range(10)]           # -3.4% over ten sessions
    read = regime.classify(bars(closes, rng=0.006), spot=760.0, today="2026-08-20")
    assert read.regime == "bear"
    assert regime.core_sides(read, LIMITS) == ("C",)


def test_a_real_rally_is_bull_and_permits_puts_only():
    closes = [740 + 3 * i for i in range(10)]
    read = regime.classify(bars(closes, rng=0.006), spot=770.0, today="2026-08-20")
    assert read.regime == "bull"
    assert regime.core_sides(read, LIMITS) == ("P",)


def test_no_range_means_no_sides_are_forbidden_by_position():
    """Insufficient history: regime falls back to sideways with no range rule.
    The range_buffer gate still blocks the entry; this only governs direction."""
    read = regime.classify(bars([760.0] * 3), spot=760.0, today="2026-08-20")
    assert regime.core_sides(read, LIMITS) == ("P", "C")
