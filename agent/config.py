"""Configuration and the account-safety guard.

The competition account must not be traded before the hackathon kickoff. That
rule lives here and in risk.py as gate zero -- never in a prompt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# --- Event ---------------------------------------------------------------
KICKOFF = datetime(2026, 8, 28, 11, 0, tzinfo=ET)   # first legal fill
DEADLINE = datetime(2026, 9, 4, 11, 0, tzinfo=ET)   # submission closes
TARGET_EXPIRY = "2026-09-03"                        # everything settles Thursday

# --- Accounts ------------------------------------------------------------
# `dev` is the practice account and the default. `comp` is judged; its
# credentials are not expected to exist until Thu Aug 27 evening.
PRACTICE_PROFILE = "dev"
COMPETITION_PROFILE = "comp"

# Underlyings we will trade. Liquid only: tight spreads keep paper fills
# honest, which keeps the P&L credible to judges.
UNIVERSE = ["SPY", "QQQ", "IWM"]

STARTING_EQUITY = 100_000.0


@dataclass(frozen=True)
class RiskLimits:
    """All thresholds in one place so the write-up can quote them verbatim."""
    max_daily_loss_pct: float = 0.04        # flatten + halt for the day
    max_event_drawdown_pct: float = 0.15    # halt for the event
    max_underlying_notional_pct: float = 0.35
    max_concurrent_positions: int = 8
    min_open_interest: int = 500
    max_spread_pct_of_mid: float = 0.10     # bid-ask width sanity
    # Short-leg delta band, enforced by the delta_band gate. The prompt aims at
    # 0.25-0.30; this is deliberately wider. Strikes are 1 point apart and
    # delta moves ~0.03-0.05 per strike, so a hard 0.25-0.30 gate leaves one
    # legal strike per wing and sometimes none -- measured on the live 09-03
    # chain, QQQ puts had no strike inside it. The upper bound matches the
    # chain filter in brain.py so the gate cannot reject a strike the model was
    # never shown.
    min_short_delta: float = 0.20
    max_short_delta: float = 0.35
    # Directional exposure across the WHOLE book, as a multiple of equity.
    # Every other limit here is per-trade or per-underlying, which is how three
    # short call spreads in three tickers -- each comfortably inside
    # tranche_risk, concentration and delta_band -- became one bet that lost
    # together on 2026-09-02 for -4,010.
    #
    # Measured as |sum of signed leg deltas x contracts x 100 x spot| / equity.
    # A single 15-20 lot spread on a $700 underlying already carries roughly
    # 0.6x equity in notional delta, so any cap below 1.0 blocks normal
    # operation -- the 0.30 that previously sat in this file was never read by
    # anything and its units were never checked against a real book.
    #
    # 2.0 permits two concurrent same-way spreads and blocks a third. That
    # threshold is calibrated against one week and nine trades, which is not
    # enough to call it validated. It is a starting point, chosen to make the
    # limit exist and bind, not a number anyone should trust yet.
    max_portfolio_delta: float = 2.0
    no_trade_open_minutes: int = 5
    no_trade_close_minutes: int = 5
    max_tranche_risk_pct: float = 0.12      # worst case on any one CORE tranche

    # --- Satellite sleeve ---
    # The convex half of the barbell. Core sells premium and wins slowly with
    # high probability; satellite buys direction and loses small, often, in
    # exchange for a larger payoff when a trend actually runs. Sized well under
    # core because its hit rate is much lower.
    max_satellite_risk_pct: float = 0.04
    satellite_profit_target_pct: float = 0.60   # take 60% of max profit
    satellite_stop_pct: float = 0.50            # cut at 50% of the debit paid

    # --- Exit management ---
    # Tournament calibration: P&L is a judged criterion and a 4% tranche makes
    # ~0.16% per winning trade, which finishes green but unremarkable. Sizing up
    # buys variance, not expectancy -- that is the trade being made deliberately.
    # A wide stop is the one change that helps rather than merely amplifies: a
    # tight stop on defined-risk short premium pays to avoid a loss that is
    # already capped, and closes spreads that would have expired worthless.
    profit_target_pct: float = 0.50         # close once 50% of the credit is captured
    stop_loss_multiple: float = 3.0         # close if cost to close >= 3x credit taken
    # On expiry day, never carry a short strike this close to spot into the
    # close -- ITM settlement means assignment, and assigned shares would wreck
    # both the P&L picture and the buying power on submission morning.
    itm_flatten_buffer: float = 0.50        # in dollars of underlying price
    flatten_minutes_before_close: int = 30


@dataclass(frozen=True)
class Settings:
    profile: str = field(default_factory=lambda: os.environ.get("ALPACA_PROFILE", PRACTICE_PROFILE))
    limits: RiskLimits = field(default_factory=RiskLimits)
    journal_path: str = field(
        default_factory=lambda: os.environ.get("JOURNAL_PATH", "data/journal.db"))
    model: str = "claude-opus-5"

    @property
    def is_competition(self) -> bool:
        return self.profile == COMPETITION_PROFILE


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


class AccountGuardError(RuntimeError):
    """Raised when something tries to trade the judged account too early."""


def assert_may_trade(profile: str, when: datetime | None = None) -> None:
    """Gate zero. Refuse to place orders on the competition account before kickoff.

    Deliberately a hard exception rather than a warning: the eligibility of the
    entire submission depends on the judged account having no pre-kickoff fills.
    """
    if profile != COMPETITION_PROFILE:
        return
    when = when or now_et()
    if when < KICKOFF:
        raise AccountGuardError(
            f"Refusing to trade {COMPETITION_PROFILE!r} before kickoff "
            f"({KICKOFF:%Y-%m-%d %H:%M %Z}); it is currently {when:%Y-%m-%d %H:%M %Z}. "
            "Pre-kickoff fills would make the submission ineligible."
        )
    if when > DEADLINE:
        raise AccountGuardError(
            f"Submission deadline ({DEADLINE:%Y-%m-%d %H:%M %Z}) has passed; refusing to trade."
        )


def in_no_trade_window(when: datetime, limits: RiskLimits) -> bool:
    """True during the first/last few minutes of the session, where spreads are noisy."""
    open_t = when.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = when.replace(hour=16, minute=0, second=0, microsecond=0)
    if when < open_t or when > close_t:
        return True
    if when < open_t + timedelta(minutes=limits.no_trade_open_minutes):
        return True
    if when > close_t - timedelta(minutes=limits.no_trade_close_minutes):
        return True
    return False
