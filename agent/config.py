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
TARGET_EXPIRY = "2026-09-03"                        # hackathon expiry; fallback only
# Past the event the expiry has to roll, or the agent simply stops: every
# proposal is checked against one date, and once it passes nothing can trade.
# resolve_expiry() in loop.py picks the nearest listed expiry at least this
# many days out, per cycle, from the broker rather than from a calendar.
#
# Seven, not three. At 1-4 DTE a 0.25-delta strike sits 0.5-1.0% from spot,
# inside one ordinary day's range; six of nine hackathon short strikes were in
# the money at expiry. SPY/QQQ/IWM list Mon/Wed/Fri, so this lands 7-9 days out.
MIN_DAYS_TO_EXPIRY = 7

# --- Accounts ------------------------------------------------------------
# `dev` is the practice account and the default. `comp` is judged; its
# credentials are not expected to exist until Thu Aug 27 evening.
PRACTICE_PROFILE = "dev"
COMPETITION_PROFILE = "comp"

# Underlyings we will trade. Liquid only: tight spreads keep paper fills
# honest, which keeps the P&L credible to judges.
UNIVERSE = ["SPY", "QQQ", "IWM"]

STARTING_EQUITY = 100_000.0          # the judged account; the frozen dashboard reads this

# What each account started with. The event-drawdown gate measures from here:
# against the old single constant the 50,000 account would have read as "down
# 50%" and halted on its first cycle.
_STARTING_EQUITY_BY_PROFILE = {"comp": 100_000.0, "dev": 100_000.0, "igk": 50_000.0}


def starting_equity_for(profile: str) -> float:
    return _STARTING_EQUITY_BY_PROFILE.get(profile, STARTING_EQUITY)


@dataclass(frozen=True)
class RiskLimits:
    """All thresholds in one place so the write-up can quote them verbatim."""
    # Flatten + halt for the day, measured from the broker's prior close so an
    # overnight gap counts. Enforced in loop.run_cycle on every cycle AND every
    # sweep: the book is closed, a halt row is journaled, and no entry is
    # considered until the next session even if equity recovers.
    max_daily_loss_pct: float = 0.04
    max_event_drawdown_pct: float = 0.15    # halt for the event
    max_underlying_notional_pct: float = 0.35
    max_concurrent_positions: int = 8       # open SPREADS (it used to count legs: 8 legs = 4 spreads)
    min_open_interest: int = 500
    max_spread_pct_of_mid: float = 0.10     # bid-ask width sanity
    # A quote older than this is not a market. The snapshot is taken before
    # the model call, which can run a minute or two; the loop refreshes both
    # legs just before the gates, so anything this old is genuinely stale.
    max_quote_age_minutes: int = 10
    # Short-leg delta band, enforced by the delta_band gate. The range_buffer
    # gate now decides where the strike goes (outside the recent range, one
    # expected move out), which lands near 0.15 delta at 7-14 DTE. The floor
    # is 0.10 so the band cannot reject a strike range_buffer requires; the
    # ceiling matches the chain filter in brain.py so the gate cannot reject a
    # strike the model was never shown.
    min_short_delta: float = 0.10
    max_short_delta: float = 0.35
    # Directional risk across the WHOLE book, as a fraction of equity.
    # Max loss signed by the move that hurts: call spreads lose on a rally,
    # put spreads on a selloff, so holding both sides nets toward zero --
    # an iron condor can only lose one wing.
    #
    # Be clear about what this does NOT do. It would not have prevented the
    # 2026-09-02 loss. Those three same-way call spreads totalled 7,855, or
    # 7.9% of equity, and pass any cap that also lets a single full-size
    # tranche through -- tranche_risk permits 12% on one trade. They did not
    # lose because the book was too large; they lost because three small bets
    # were the same bet, on a strategy that needs roughly 65% winners and got
    # 56%. Correlation is not the same problem as size, and this gate measures
    # size.
    #
    # What it does do is stop the book becoming lopsidedly large -- several
    # full tranches all leaning one way. That is worth having on its own terms.
    #
    # It replaces a notional-delta version that penalised WIDTH rather than
    # risk: a 5-wide spread carries far more delta than a 2-wide one while
    # losing no more than its width, and that gate blocked a $9,416 trade
    # tranche_risk was happy with.
    max_directional_risk_pct: float = 0.20
    # --- Tape read (computed regime) ---
    # The 2026-09-01 loss: the model called "bear" after a 1.2% dip to the
    # bottom of a 15-session band, and bear -> calls-only sold four call
    # spreads at the range low. Regime is now computed from the bars, and in
    # a range the position inside it decides which side may be sold.
    range_lookback: int = 10            # completed sessions
    trend_range_multiple: float = 2.0   # trend if |move| > this x mean daily range
    range_edge_quantile: float = 0.25   # sideways: no short calls below this range
                                        # position, no short puts above 1 - this
    # --- Strike placement and premium ---
    # The short strike must clear BOTH the recent range and one expected move
    # (spot x IV x sqrt(DTE/365)). Eight of nine hackathon strikes sat inside
    # the prior five sessions' range; this would have rejected all eight.
    expected_move_multiple: float = 1.0
    # Credit as a fraction of width. For a narrow spread this is roughly the
    # short leg's delta, and range_buffer puts the short leg near 0.15, so a
    # floor above that can never be met. 10% rejects the wide, thin structure
    # (the first live dry run proposed a 10-wide paying 6%) without rejecting
    # the strategy. It is a payoff sanity check, not a volatility filter.
    min_credit_pct_of_width: float = 0.10
    # --- Book shape ---
    # Per-tranche limits let four bear-sized tranches add up to more than one
    # sideways tranche, and three call spreads in three tickers that move
    # together were one bet. These look at the whole book.
    max_book_risk_pct: float = 0.24         # open max loss + proposal, x regime multiplier
    # Open core spreads on one right, whole universe. 5 x 4% = the 20%
    # directional cap, so this and that gate agree on where a side is full.
    max_same_direction: int = 5
    losing_side_multiple: float = 1.5       # no add-on where a spread marks >= this x credit
    # --- Cadence ---
    # Four cycles a day produced a proposal in 13 of 13 cycles with budget, so
    # this started at 1. Raised to 2 on 2026-09-18 at Matt's call, during the
    # rules freeze and against the advice to wait for survival data: it is a
    # paper account and he wants to see how it does. Every other gate still
    # applies to the second entry (same-direction cap, book risk, cooldown).
    # ... and to 4 the same day, with the smaller tranches: one per scheduled cycle.
    max_entries_per_day: int = 4
    reentry_cooldown_hours: int = 24        # same underlying and right, after any close
    no_trade_open_minutes: int = 5
    no_trade_close_minutes: int = 5
    # Worst case on any one CORE tranche. Was 0.12 for the tournament, where two
    # full-size positions filled the 24% book and nothing else could be
    # entered until one closed: two to four entries a week. Cut to a third on
    # 2026-09-18 at Matt's call ("option A"): the same TOTAL risk -- book cap,
    # directional cap and daily limit are unchanged -- split into more, smaller
    # positions, so one stop-out costs a third as much and the record grows
    # three times faster. A rule change during the freeze, by his decision.
    max_tranche_risk_pct: float = 0.04

    # --- Satellite sleeve ---
    # The convex half of the barbell. Core sells premium and wins slowly with
    # high probability; satellite buys direction and loses small, often, in
    # exchange for a larger payoff when a trend actually runs. Sized well under
    # core because its hit rate is much lower.
    max_satellite_risk_pct: float = 0.013      # a third of a core tranche, as before; the sleeve is OFF
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

    # --- Learning (agent/learning.py) ---
    # The shadow ledger may move ONE tunable gate parameter ONE ladder step
    # when the refused (or marginal admitted) set has this much independent
    # evidence and beats the comparison set with one-sided 95% confidence.
    # The change is then judged on data collected after it, and reverted and
    # locked if the newly admitted band loses money. Measured in effective n
    # (clusters of underlying x side x expiry), not rows.
    learn_enabled: bool = True
    learn_min_n: float = 20.0
    learn_min_review_n: float = 8.0
    learn_lock_days: int = 56


@dataclass(frozen=True)
class Settings:
    profile: str = field(default_factory=lambda: os.environ.get("ALPACA_PROFILE", PRACTICE_PROFILE))
    limits: RiskLimits = field(default_factory=RiskLimits)
    journal_path: str = field(
        default_factory=lambda: os.environ.get("JOURNAL_PATH", "data/journal.db"))
    model: str = "claude-opus-5"
    rules_version: int = 0                  # set per run from the journal (agent/rules.py)

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


def in_no_trade_window(when: datetime, limits: RiskLimits,
                       session: tuple[datetime, datetime] | None = None) -> bool:
    """True outside the session and during its first/last few minutes.

    ENTRIES only. `session` is the exchange's own (open, close) for the day,
    from the broker's calendar, so an early close moves the lockout with it.
    Without it, the regular 09:30-16:00 hours are assumed.
    """
    if session is not None:
        open_t, close_t = session
    else:
        open_t = when.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = when.replace(hour=16, minute=0, second=0, microsecond=0)
    if when < open_t or when > close_t:
        return True
    if when < open_t + timedelta(minutes=limits.no_trade_open_minutes):
        return True
    if when > close_t - timedelta(minutes=limits.no_trade_close_minutes):
        return True
    return False
