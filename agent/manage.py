"""Exit management for open spreads.

Runs before any new proposal each cycle. Like risk.py this is deterministic --
the model has no say in when a position is closed. Four rules, in priority
order:

  1. assignment_risk  -- expiry day, short strike near or through spot
  2. expiry_flatten   -- expiry day, near the close, anything still open
  3. stop_loss        -- cost to close has reached a multiple of the credit
  4. profit_target    -- enough of the credit has been captured

Rules 1 and 2 exist because the submission deadline is the morning after
expiry. Being assigned shares on Thursday would leave stock in the account and
distort both the P&L and the buying power on submission morning.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import ET, RiskLimits, TARGET_EXPIRY
from .models import ExitDecision, OpenSpread


def held_qty(spread: OpenSpread, positions: list[dict]) -> int:
    """How many of this spread the broker says we actually hold.

    Sized off the SHORT leg, and always the broker's number rather than the
    journal's: closing a size we do not hold can open an opposite position.
    """
    short_sym = spread.short_symbol()
    for p in positions:
        if p.get("symbol") == short_sym:
            return abs(int(float(p.get("qty") or 0)))
    return 0


def is_actually_held(spread: OpenSpread, positions: list[dict]) -> bool:
    """Is the broker really short the spread's short leg?

    A spread is journaled when the opening order is ACCEPTED, which is not the
    same as filled. Trying to close an unfilled spread gets rejected twice over:
    422 (no position to close) and 403 (our own resting order on the other side
    reads as a wash trade). So confirm the position exists before acting.
    """
    return held_qty(spread, positions) > 0


def spread_from_row(row: dict) -> OpenSpread:
    return OpenSpread(
        id=row["id"], underlying=row["underlying"], expiry=row["expiry"],
        right=row["right"], short_strike=row["short_strike"],
        long_strike=row["long_strike"], qty=row["qty"],
        entry_credit=row["entry_credit"], sleeve=row.get("sleeve") or "core",
    )


def mark_to_close(spread: OpenSpread, quotes: dict) -> float | None:
    """Conservative price to unwind, per share. Always positive.

    Credit spread: what it COSTS us to buy it back (pay the short's ask,
    receive the long's bid) -- lower is better.
    Debit spread:  what we RECEIVE to sell it (sell the long at its bid, buy
    back the short at its ask) -- higher is better.

    Both use the unfavourable side of both quotes, so an exit rule never fires
    on an optimistic mark.
    """
    short_q = quotes.get(spread.short_symbol()) or {}
    long_q = quotes.get(spread.long_symbol()) or {}
    short_ask = _f(short_q.get("ap"))
    long_bid = _f(long_q.get("bp"))
    if short_ask is None or long_bid is None:
        return None
    if spread.is_credit:
        return max(0.0, short_ask - long_bid)
    return max(0.0, long_bid - short_ask)


def decide_exit(
    spread: OpenSpread,
    mark: float | None,
    *,
    now: datetime,
    spot: float | None,
    limits: RiskLimits,
) -> ExitDecision:
    """Pure function. Given a spread and a mark, should it be closed?"""
    is_expiry_day = now.strftime("%Y-%m-%d") == spread.expiry
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    near_close = now >= close_t - timedelta(minutes=limits.flatten_minutes_before_close)

    # 1. Assignment risk -- highest priority, and independent of the mark.
    if is_expiry_day and spot is not None:
        threatened = (
            spot <= spread.short_strike + limits.itm_flatten_buffer
            if spread.right == "P"
            else spot >= spread.short_strike - limits.itm_flatten_buffer
        )
        if threatened:
            return ExitDecision(
                action="close", rule="assignment_risk",
                reason=(f"expiry day and spot {spot:.2f} is within "
                        f"{limits.itm_flatten_buffer} of the short {spread.short_strike} "
                        f"{spread.right}; closing to avoid assignment"),
            )

    # 2. Flatten anything still open into the expiry close.
    if is_expiry_day and near_close:
        return ExitDecision(
            action="close", rule="expiry_flatten",
            reason=(f"expiry day, within {limits.flatten_minutes_before_close} min of "
                    "the close; flattening so the P&L is settled before the deadline"),
        )

    if mark is None:
        return ExitDecision(action="hold", reason="no reliable mark available")

    if spread.is_credit:
        # 3. Stop loss. Capped below the width: credit x multiple can exceed the
        # spread's own max loss at higher deltas, which would make the stop
        # unreachable and silently turn every loser into a full max-loss ride.
        stop_at = min(spread.entry_credit * limits.stop_loss_multiple,
                      spread.width * 0.90)
        if mark >= stop_at:
            return ExitDecision(
                action="close", rule="stop_loss",
                reason=(f"cost to close {mark:.2f} >= {limits.stop_loss_multiple}x the "
                        f"{spread.entry_credit:.2f} credit ({stop_at:.2f})"),
            )
        # 4. Profit target.
        target = spread.entry_credit * (1.0 - limits.profit_target_pct)
        if mark <= target:
            captured = (spread.entry_credit - mark) / spread.entry_credit
            return ExitDecision(
                action="close", rule="profit_target",
                reason=(f"cost to close {mark:.2f} <= {target:.2f}; "
                        f"{captured:.0%} of the credit captured"),
            )
        return ExitDecision(
            action="hold",
            reason=f"mark {mark:.2f} between target {target:.2f} and stop {stop_at:.2f}")

    # --- debit spread: the inequalities invert ---
    # We paid entry_credit; mark is what we would now receive.
    stop_at = spread.entry_credit * (1.0 - limits.satellite_stop_pct)
    if mark <= stop_at:
        return ExitDecision(
            action="close", rule="stop_loss",
            reason=(f"value {mark:.2f} <= {stop_at:.2f}; more than "
                    f"{limits.satellite_stop_pct:.0%} of the {spread.entry_credit:.2f} "
                    "debit is gone"),
        )
    max_value = spread.width
    target = spread.entry_credit + (max_value - spread.entry_credit) * limits.satellite_profit_target_pct
    if mark >= target:
        return ExitDecision(
            action="close", rule="profit_target",
            reason=(f"value {mark:.2f} >= {target:.2f}; "
                    f"{limits.satellite_profit_target_pct:.0%} of max profit captured"),
        )
    return ExitDecision(
        action="hold",
        reason=f"value {mark:.2f} between stop {stop_at:.2f} and target {target:.2f}")


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
