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
