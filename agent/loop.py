"""One trading cycle, end to end.

    observe -> reason -> gate -> execute -> journal

Run it from cron or a scheduler. Each invocation is independent and idempotent;
there is no in-memory state to lose.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from . import alpaca_cli as cli
from . import candidates as cand
from . import regime
from . import risk
from .regime import TapeRead, classify, core_sides
from .manage import decide_exit, held_qty, is_actually_held, mark_to_close, spread_from_row
from .brain import Brain, build_snapshot
from .config import (
    COMPETITION_PROFILE, ET, MIN_DAYS_TO_EXPIRY, STARTING_EQUITY, TARGET_EXPIRY,
    UNIVERSE, Settings, now_et,
)
from .journal import open_journal
from .models import AgentDecision, ExitDecision, TradeProposal, parse_strike

OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def observe(profile: str, expiry: str) -> dict:
    """Pull everything a decision needs, in as few calls as possible."""
    acct = cli.account(profile)
    equity = float(acct["equity"])
    positions = cli.positions(profile)
    start = (now_et() - timedelta(days=30)).strftime("%Y-%m-%d")
    quotes, chains, bars = {}, {}, {}
    for sym in UNIVERSE:
        try:
            q = cli.latest_quote(sym, profile)
            quotes[sym] = q
            mid = (float(q.get("bp", 0)) + float(q.get("ap", 0))) / 2
            # Both wings: the regime policy permits call spreads in sideways and
            # bear tapes, so the model needs call strikes to pick from. ~6%
            # either side of spot covers the 0.05-0.35 delta band.
            puts = cli.option_chain(
                sym, profile, expiry=expiry, option_type="put",
                strike_gte=round(mid * 0.94), strike_lte=round(mid * 1.00),
            )
            calls = cli.option_chain(
                sym, profile, expiry=expiry, option_type="call",
                strike_gte=round(mid * 1.00), strike_lte=round(mid * 1.06),
            )
            chains[sym] = {**puts, **calls}
            # Open interest, which the chain snapshot never carries. Missing
            # stays missing, and the liquidity gate fails closed on it.
            for osym, oi in cli.contracts_oi(sym, expiry, profile).items():
                if osym in chains[sym]:
                    chains[sym][osym]["openInterest"] = oi
            bars[sym] = cli.daily_bars(sym, profile, start)[-15:]
        except cli.CLIError as e:
            print(f"  warn: {sym} data unavailable: {e}", file=sys.stderr)
    try:
        headlines = cli.news(UNIVERSE, profile, limit=12)
    except cli.CLIError as e:
        print(f"  warn: news unavailable: {e}", file=sys.stderr)
        headlines = []
    return {"account": acct, "equity": equity, "positions": positions,
            "quotes": quotes, "chains": chains, "bars": bars, "news": headlines}


def resolve_expiry(profile: str, now) -> str:
    """The expiry every proposal this cycle must use.

    Nearest date listed for EVERY name in the universe at least
    MIN_DAYS_TO_EXPIRY out. Requiring it across the whole universe matters:
    IWM lists dates SPY and QQQ do not, and picking one of those would let the
    model propose a spread whose chain comes back empty for the other two.

    Falls back to the configured constant if the broker cannot be reached, so
    a data outage degrades to the old fixed behaviour rather than to no expiry
    at all.
    """
    floor = (now + timedelta(days=MIN_DAYS_TO_EXPIRY)).strftime("%Y-%m-%d")
    # Ten days past the floor always contains a Friday, holiday weeks included.
    until = (now + timedelta(days=MIN_DAYS_TO_EXPIRY + 10)).strftime("%Y-%m-%d")
    common: set[str] | None = None
    for sym in UNIVERSE:
        try:
            found = set(cli.list_expiries(sym, profile, floor, until))
        except cli.CLIError as e:
            print(f"  warn: expiries for {sym} unavailable: {e}", file=sys.stderr)
            return TARGET_EXPIRY
        common = found if common is None else (common & found)
    if not common:
        print("  warn: no expiry common to the universe; using the configured one",
              file=sys.stderr)
        return TARGET_EXPIRY
    return pick_expiry(common)


def pick_expiry(listed: set[str]) -> str:
    """The nearest FRIDAY among the listed expiries, else the nearest of any.

    "Nearest, at least a week out" kept landing on Monday and Wednesday
    weeklies that had been listed for days. On 2026-09-18 the 09-28 Monday
    weekly had not one vertical with open interest of 500 on both legs, in
    any of the three names; the 09-25 Friday weekly had hundreds. Friday
    weeklies are where the open interest is, so they come first. Holds run
    7-13 days instead of 7-9.
    """
    from datetime import date
    fridays = [d for d in listed if date.fromisoformat(d).weekday() == 4]
    return min(fridays) if fridays else min(listed)


def read_tape(obs: dict, now, limits) -> tuple[dict[str, TapeRead], dict[str, tuple]]:
    """Classify every name in the universe from its own bars and quote.

    A name with no usable quote or history gets a defensive sideways read
    with no range, which the range_buffer gate turns into no entry.
    """
    today = now.strftime("%Y-%m-%d")
    tape: dict[str, TapeRead] = {}
    sides: dict[str, tuple] = {}
    for sym in UNIVERSE:
        q = obs["quotes"].get(sym) or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        spot = (bid + ask) / 2 if bid and ask else None
        bars = obs.get("bars", {}).get(sym) or []
        if spot is None:
            tape[sym] = TapeRead("sideways", None, None, None, None, None, "no quote")
        else:
            tape[sym] = classify(bars, spot, today, lookback=limits.range_lookback,
                                 trend_multiple=limits.trend_range_multiple)
        sides[sym] = core_sides(tape[sym], limits)
        print(f"  tape {sym}: {tape[sym].detail}; core may sell {'/'.join(sides[sym]) or 'none'}")
    return tape, sides


def session_bounds(now, calendar: list[dict]) -> tuple | None:
    """(open, close) for today from the broker's calendar; None if closed today.

    The exchange's own hours, so an early close (13:00 the day after
    Thanksgiving) moves the expiry flatten and the entry lockout with it.
    """
    today = now.strftime("%Y-%m-%d")
    for row in calendar or []:
        if row.get("date") != today:
            continue
        try:
            oh, om = (int(x) for x in str(row["open"]).split(":"))
            ch, cm = (int(x) for x in str(row["close"]).split(":"))
        except (KeyError, ValueError):
            return None
        return (now.replace(hour=oh, minute=om, second=0, microsecond=0),
                now.replace(hour=ch, minute=cm, second=0, microsecond=0))
    return None


def next_session_date(now, calendar: list[dict]) -> str | None:
    """The first trading day after today, from the broker's calendar."""
    today = now.strftime("%Y-%m-%d")
    later = sorted(str(r.get("date")) for r in calendar or [] if str(r.get("date")) > today)
    return later[0] if later else None


OUR_ORDERS = ("hack-", "exit-")          # client_order_id prefixes this agent uses


def cancel_stale_orders(journal, profile: str, now, *, dry_run: bool,
                        older_than_s: int = 180) -> tuple[list[str], list[str]]:
    """Cancel any order of ours that an earlier run left working.

    Every order this agent sends is polled to a settled state and cancelled
    if it does not fill, inside one invocation, under the account lock. So an
    open order of ours at the START of a run belongs to a run that died, and
    it can still fill into a position nobody asked for. Orders placed by hand
    are left alone.

    Returns (cancelled, unresolved). A cancel is a request: it is only counted
    once the broker reports the order terminal, and `pending_cancel` is not
    `canceled`. Anything unresolved -- a refused cancel, an order that would
    not settle, or not being able to list orders at all -- blocks entries for
    this run, because a working order can fill outside the exposure the gates
    are about to measure. Anything that FILLED while we looked is picked up by
    reconcile(), which runs after this on freshly read positions.
    """
    try:
        orders = cli.open_orders(profile)
    except cli.CLIError as e:
        print(f"  warn: open orders unavailable: {e}", file=sys.stderr)
        return [], [f"open orders unavailable ({str(e)[:80]}); cannot rule out a working order"]
    cancelled: list[str] = []
    unresolved: list[str] = []
    for o in orders:
        coid = str(o.get("client_order_id") or "")
        if not coid.startswith(OUR_ORDERS):
            continue
        try:
            sent = datetime.fromisoformat(str(o.get("submitted_at")).replace("Z", "+00:00")[:32])
        except ValueError:
            continue
        if (now - sent).total_seconds() < older_than_s:
            continue
        oid = str(o.get("id"))
        print(f"  cancelling orphaned order {oid} ({coid}) from an earlier run", file=sys.stderr)
        if dry_run:
            continue
        accepted = cli.cancel_order(oid, profile)
        state = cli.fill_result(oid, profile, tries=6, delay=5.0)
        if state["timed_out"]:
            unresolved.append(f"orphaned order {oid} still {state['status']} "
                              f"(cancel {'accepted' if accepted else 'refused'})")
        elif state["qty"] > 0:
            print(f"  orphaned order {oid} had filled {state['qty']}; reconciliation will pick "
                  "it up", file=sys.stderr)
            cancelled.append(oid)
        else:
            cancelled.append(oid)
    if cancelled or unresolved:
        journal.record_cycle(profile=profile, action="error", error=(
            "orphaned orders from an earlier run -- settled: " + (", ".join(cancelled) or "none")
            + "; unresolved: " + (" | ".join(unresolved) or "none")))
    return cancelled, unresolved


def flatten_reason(*, halted: bool, breached: bool, open_spreads: list,
                   detail: str | None = None) -> str | None:
    """Why the book must be closed right now, or None.

    A breach starts the liquidation. The HALT keeps it going: a forced close
    can fill 4 of 10, equity can bounce back over the line, and the remaining
    6 must still go. The liquidation ends when the book is flat, not when the
    number recovers.
    """
    if breached:
        return detail or "daily loss at/over the limit"
    if halted and open_spreads:
        return "daily halt in force and the book is not flat; finishing the liquidation"
    return None


def in_session(now, session: tuple) -> bool:
    """EXITS run for the whole session, first and last minutes included. The
    five-minute lockouts are an entry rule; a stop must not wait for them."""
    return session[0] <= now <= session[1]


def _recover_exit(sp, profile: str, since: str) -> tuple[float | None, float | None]:
    """What a vanished spread closed at, from the broker's own fills.

    Returns (exit_price, total_realized_pnl), or (None, None) when the fills do
    not account for the whole position. Unknown is recorded as null. It used to
    be recorded as 0.0 next to a log line saying "P&L unknown", and a zero is
    a number: it counted as a flat trade in the win rate (Codex follow-up).
    """
    try:
        rows = cli.fills(profile, since)
    except cli.CLIError:
        return None, None
    bought = sold = 0.0
    n_short = n_long = 0
    for f in rows:
        try:
            q, px = int(float(f.get("qty") or 0)), float(f.get("price") or 0)
        except (TypeError, ValueError):
            continue
        side = str(f.get("side") or "")
        if f.get("symbol") == sp.short_symbol() and side == "buy":          # closing the short
            bought += q * px
            n_short += q
        elif f.get("symbol") == sp.long_symbol() and side == "sell":        # closing the long
            sold += q * px
            n_long += q
    if sp.qty <= 0 or n_short != sp.qty or n_long != sp.qty:
        return None, None
    net = (bought - sold) / sp.qty                 # cost to close a credit spread
    exit_px = net if sp.is_credit else -net        # value received on a debit spread
    return round(exit_px, 4), sp.realized_so_far + sp.realized_pnl(exit_px, qty=sp.qty)


def reconcile(journal, profile: str, positions: list[dict], now, *, dry_run: bool) -> list[str]:
    """Make the journal agree with the broker, and name what it cannot explain.

    The exit rules only ever visit journaled spreads, so anything the broker
    holds that the journal does not know about is managed by nothing. Three
    repairs, in order:

      1. A journaled spread the broker no longer holds at all is retired
         (expired, assigned away, or closed by hand). P&L is unknown.
      2. An unjournaled clean vertical -- one short leg, one long leg, same
         name, expiry, right and size -- is ADOPTED at the broker's average
         prices, so the stops and targets manage it from the next line on.
         This is the 2026-08-31 orphan: an entry that filled after the poll
         gave up.
      3. Anything else -- a naked leg, a size mismatch, stock from an
         assignment -- is returned as a problem. The caller blocks entries
         while any problem stands.
    """
    actual: dict[str, int] = {}
    avg: dict[str, float] = {}
    stock: list[str] = []
    for p in positions:
        sym = str(p.get("symbol", ""))
        try:
            q = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            q = 0
        if not q:
            continue
        if OCC.match(sym):
            actual[sym] = q
            try:
                avg[sym] = float(p.get("avg_entry_price") or 0)
            except (TypeError, ValueError):
                avg[sym] = 0.0
        else:
            stock.append(f"{sym} x{q}")

    problems: list[str] = []
    if stock:
        problems.append("stock position(s) " + ", ".join(stock)
                        + ": possible assignment; the agent only manages option spreads")

    # 1. retire spreads the broker no longer holds in any part
    rows = journal.open_spreads(profile)
    live = []
    for r in rows:
        sp = spread_from_row(r)
        try:
            opened = datetime.fromisoformat(str(r.get("ts_open")))
            age = now - (opened if opened.tzinfo else opened.replace(tzinfo=ET))
        except (TypeError, ValueError):
            age = timedelta(0)
        gone = not actual.get(sp.short_symbol()) and not actual.get(sp.long_symbol())
        if gone and age > timedelta(minutes=5):
            print(f"  reconcile: #{sp.id} {sp.underlying} {sp.short_strike:g}/{sp.long_strike:g} "
                  "is not at the broker; retiring it (P&L unknown)", file=sys.stderr)
            if not dry_run:
                exit_px, pnl = _recover_exit(sp, profile, str(r.get("ts_open") or "")[:10])
                journal.close_spread(sp.id, exit_debit=exit_px, exit_rule="missing_at_broker",
                                     realized_pnl=pnl, close_order_id=None)
                known = (f"P&L {pnl:+,.0f} recovered from the broker's fills" if pnl is not None
                         else "P&L UNKNOWN (null, not zero): exclude from performance statistics")
                journal.record_cycle(profile=profile, action="error", error=(
                    f"reconcile: spread {sp.id} {sp.underlying} {sp.short_strike:g}/"
                    f"{sp.long_strike:g} not held at the broker; retired, {known}"))
            continue
        live.append(sp)

    expected: dict[str, int] = defaultdict(int)
    for sp in live:
        expected[sp.short_symbol()] -= sp.qty
        expected[sp.long_symbol()] += sp.qty
    diff = {s: actual.get(s, 0) - expected.get(s, 0) for s in set(actual) | set(expected)}
    diff = {s: d for s, d in diff.items() if d}

    # 2. adopt clean orphan verticals
    groups: dict[tuple, list[str]] = defaultdict(list)
    for s in diff:
        m = OCC.match(s)
        groups[(m[1], m[2], m[3])].append(s)
    for (root, yymmdd, right), syms in groups.items():
        if len(syms) != 2 or any(s in expected for s in syms):
            continue
        short = next((s for s in syms if diff[s] < 0), None)
        long_ = next((s for s in syms if diff[s] > 0), None)
        if not short or not long_ or -diff[short] != diff[long_]:
            continue
        net = avg.get(short, 0.0) - avg.get(long_, 0.0)
        if net == 0:
            continue
        try:
            adopted = TradeProposal(
                underlying=root, expiry=f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:]}", right=right,
                short_strike=parse_strike(short), long_strike=parse_strike(long_),
                qty=diff[long_], net_price=round(abs(net), 4),
                sleeve="core" if net > 0 else "satellite",
                rationale="adopted by reconciliation: held at the broker, absent from the journal")
        except ValueError:
            continue
        if not adopted.has_valid_structure():
            continue
        print(f"  reconcile: adopting unjournaled {root} {right} {adopted.short_strike:g}/"
              f"{adopted.long_strike:g} x{adopted.qty} @ {adopted.net_price:.2f}", file=sys.stderr)
        if not dry_run:
            journal.record_spread(profile=profile, proposal=adopted, order_id=None)
            journal.record_cycle(profile=profile, action="error", proposal=adopted.model_dump(),
                                 error=(f"reconcile: adopted unjournaled {root} {right} "
                                        f"{adopted.short_strike:g}/{adopted.long_strike:g} "
                                        f"x{adopted.qty}; now under exit management"))
        for s in syms:
            diff.pop(s)

    # 3. whatever is left is unexplained
    for s, d in sorted(diff.items()):
        problems.append(f"{s}: broker holds {actual.get(s, 0):+d}, journal expects "
                        f"{expected.get(s, 0):+d}")
    return problems


def current_marks(journal, profile: str, positions: list[dict]) -> dict:
    """Conservative marks for the spreads we hold, right now. Reads only."""
    spreads = [sp for sp in (spread_from_row(r) for r in journal.open_spreads(profile))
               if is_actually_held(sp, positions)]
    if not spreads:
        return {}
    symbols = sorted({s for sp in spreads for s in (sp.short_symbol(), sp.long_symbol())})
    quotes = cli.option_quotes(symbols, profile)
    marks = {}
    for sp in spreads:
        m = mark_to_close(sp, quotes)
        if m is not None:
            marks[sp.id] = m
    return marks


def final_observation(journal, profile: str, expiry: str, p, now, limits) -> dict:
    """Everything the gates judge, read again AFTER the model has answered.

    The model call takes a minute or two. The first refresh patched the two
    legs' quotes and the equity and left the rest as it was before the call:
    the tape that decides the permitted side and the budget, the Greeks and
    IV, the broker's positions, the marks of the spreads already held. That is
    a verdict assembled from two different moments (Codex follow-up). Now it
    is one observation, or no trade: any failure here raises, and the caller
    does not submit on a mix of old and new.
    """
    obs = observe(profile, expiry)
    if p.underlying not in obs["quotes"] or p.underlying not in obs["chains"]:
        raise cli.CLIError(["observe"], 1, f"no fresh market data for {p.underlying}")
    tape, sides = read_tape(obs, now, limits)
    return {"obs": obs, "equity": obs["equity"], "tape": tape, "sides": sides,
            "marks": current_marks(journal, profile, obs["positions"])}


def day_start_equity(acct: dict, first_mark: float | None) -> float | None:
    """The day's baseline: the broker's prior close, else the first mark.

    The first journal mark of a session is taken after the open, so a position
    that gapped overnight had already lost the money before the "day" began and
    the daily limit never saw it (2026-09-03: -1,900 overnight, day P&L +14).
    """
    try:
        last = float(acct.get("last_equity") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return last if last > 0 else first_mark


def daily_loss_breached(*, equity: float, day_start: float, limits) -> bool:
    return day_start > 0 and (equity - day_start) <= -abs(day_start * limits.max_daily_loss_pct)


def halted_today(journal, profile: str, now) -> bool:
    """Has the daily limit already tripped this session? Persisted in the
    journal so a bounce back above the limit does not re-open entries."""
    from datetime import datetime as _dt
    today = now.astimezone(ET).date()
    for c in journal.recent_cycles(limit=300, profile=profile):
        if c.get("action") != "halt":
            continue
        try:
            ts = _dt.fromisoformat(str(c.get("ts")))
        except ValueError:
            continue
        if (ts if ts.tzinfo else ts.replace(tzinfo=ET)).astimezone(ET).date() == today:
            return True
    return False


def record_halt(journal, profile: str, reason: str, equity: float | None = None) -> None:
    journal.record_cycle(profile=profile, action="halt", equity=equity,
                         reasoning=f"{reason}; book flattened, no entries until tomorrow")


def manage_open_spreads(
    settings: Settings, journal, obs: dict, now, *, dry_run: bool,
    flatten: str | None = None, close_t=None,
    ex_divs: dict | None = None, next_session: str | None = None,
) -> dict:
    """Close anything the exit rules call for, before considering new risk.

    Exits are never gated on the account guard the way entries are: if the
    judged account somehow holds a position, we must always be able to get out
    of it.

    Returns the conservative mark of every spread the broker holds, keyed by
    journal id, so the entry gates can see which side is already losing.
    """
    profile = settings.profile
    rows = journal.open_spreads(profile)
    marks: dict = {}
    if not rows:
        return marks

    spreads = [spread_from_row(r) for r in rows]
    symbols = sorted({s for sp in spreads for s in (sp.short_symbol(), sp.long_symbol())})
    try:
        quotes = cli.option_quotes(symbols, profile)
    except cli.CLIError as e:
        print(f"  warn: exit quotes unavailable: {e}", file=sys.stderr)
        quotes = {}

    print(f"  managing {len(spreads)} open spread(s)")
    for sp in spreads:
        if not is_actually_held(sp, obs["positions"]):
            # Journaled on order acceptance, but the fill has not happened (or
            # the order was cancelled). Closing now would be rejected as a wash
            # trade against our own resting order.
            print(f"    #{sp.id} {sp.underlying} {sp.short_strike}/{sp.long_strike} "
                  "-> not yet filled; skipping")
            continue
        mark = mark_to_close(sp, quotes)
        if mark is not None:
            marks[sp.id] = mark
        q = obs["quotes"].get(sp.underlying) or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        spot = (bid + ask) / 2 if bid and ask else None

        if flatten:
            d = ExitDecision(action="close", rule="daily_loss_flatten", reason=flatten)
        else:
            d = decide_exit(sp, mark, now=now, spot=spot, limits=settings.limits,
                            close_t=close_t, next_session=next_session,
                            ex_dividend=(ex_divs or {}).get(sp.underlying))
        mark_s = f"{mark:.2f}" if mark is not None else "n/a"
        print(f"    #{sp.id} {sp.underlying} {sp.short_strike}/{sp.long_strike} "
              f"cr {sp.entry_credit:.2f} mark {mark_s} -> {d.action}"
              + (f" ({d.rule})" if d.rule else ""))
        print(f"        {d.reason}")

        if d.action != "close":
            continue

        # Pay what it costs to get out. This used to be capped at the stop
        # level -- min(mark + 0.05, entry_credit * stop_loss_multiple) -- which
        # is fine until the position gaps THROUGH the stop. Then the cap pins
        # the bid below the market and the close can never fill: on 2026-09-02
        # IWM 293/295 marked 1.20 while the limit stayed frozen at 1.17, and the
        # agent retried an unfillable order every 10 minutes while the loss ran.
        # A stop is an instruction to be out, so the price follows the market.
        # The width is the true ceiling: no spread can cost more than that to
        # close, so this can never pay an absurd price.
        limit = min(mark + 0.05, sp.width) if mark is not None else sp.width
        # Close only what the broker says we hold.
        qty = held_qty(sp, obs["positions"])
        if qty != sp.qty:
            print(f"        size mismatch: journal {sp.qty}, broker {qty}; closing {qty}")
        try:
            res = cli.submit_mleg(
                legs=sp.closing_legs(), limit_price=limit, qty=qty,
                profile=profile, client_order_id=f"exit-{sp.id}-{uuid.uuid4().hex[:12]}",
                dry_run=dry_run,
            )
        except cli.CLIError as e:
            print(f"        close FAILED: {e}", file=sys.stderr)
            journal.record_cycle(profile=profile, action="error",
                                 error=f"close spread {sp.id}: {e}")
            continue

        if dry_run:
            print(f"        DRY RUN close @ {limit:.2f}")
            continue

        oid = res.get("id") if isinstance(res, dict) else None

        # Reconcile the close the same way entries are reconciled. `mark` is a
        # conservative estimate -- pay the ask, sell the bid -- and journaling
        # it as the exit price makes realized P&L fiction. On 2026-09-01 the
        # SPY 767/762 put spread marked 3.29 and filled at 2.25, overstating
        # the loss by $1,248 on the public dashboard.
        fill = cli.fill_result(oid, profile) if oid else {
            "qty": 0, "credit": None, "status": "no order id", "timed_out": False}
        if fill["timed_out"]:
            cli.cancel_order(oid, profile)
            fill = cli.fill_result(oid, profile)

        if fill["qty"] < qty:
            # Anything short of a full close leaves contracts still held.
            # Marking the spread closed would retire them from exit management
            # entirely -- the same failure as journaling a fill that never
            # happened. Stay open; the next sweep sizes from the broker and
            # finishes the job.
            what = (f"{fill['status']} with 0 filled" if fill["qty"] == 0
                    else f"partially closed {fill['qty']} of {qty}")
            if fill["qty"] > 0:
                # Bank what actually closed, at the price it closed at, and
                # shrink the position. The final close used to book the whole
                # original size at the last price (Codex review: 4 @ 1.50 then
                # 6 @ 2.00 on a 0.50 credit is -1,300, journaled as -1,500).
                part_px = fill["credit"] if fill["credit"] is not None else (mark or 0.0)
                banked = sp.realized_so_far + sp.realized_pnl(part_px, qty=fill["qty"])
                journal.reduce_spread(sp.id, qty=qty - fill["qty"], realized_pnl=banked)
            print(f"        close INCOMPLETE ({what}); spread stays open",
                  file=sys.stderr)
            journal.record_cycle(
                profile=profile, action="error",
                error=(f"close of spread {sp.id} {what}; still held, "
                       "next sweep will retry from the broker's size"))
            continue

        exit_px = fill["credit"] if fill["credit"] is not None else (mark or 0.0)
        pnl = sp.realized_so_far + sp.realized_pnl(exit_px, qty=qty)
        journal.close_spread(sp.id, exit_debit=exit_px, exit_rule=d.rule or "manual",
                             realized_pnl=pnl, close_order_id=oid)
        print(f"        CLOSED @ {exit_px:.2f} (limit {limit:.2f}, mark "
              f"{mark if mark is not None else float('nan'):.2f})  "
              f"realized ${pnl:+,.0f}  order={oid}")
        marks.pop(sp.id, None)
    return marks


LOCK_TTL_S = 600      # the backstop for a container that dies holding the lock


def run_cycle(settings: Settings, *, dry_run: bool = False,
              manage_only: bool = False) -> int:
    """One invocation, under the account's lock.

    The entry cycle and the exit sweep are separate Cloud Run jobs on
    overlapping schedules. Without a lock a sweep can reconcile while a cycle
    has filled an entry but not yet journaled it, adopt the position, and
    leave it in the journal twice. A sweep that finds the lock taken simply
    stands aside (the cycle manages exits first anyway, and the next sweep is
    ten minutes off); a cycle waits up to 90 seconds for a sweep to finish.
    """
    journal = open_journal(settings.journal_path)
    if dry_run:
        return _cycle_body(settings, journal, dry_run=True, manage_only=manage_only)
    profile = settings.profile
    holder = f"{'sweep' if manage_only else 'cycle'}-{uuid.uuid4().hex[:10]}"
    tries = 1 if manage_only else 10
    for i in range(tries):
        if journal.acquire_lock(profile, holder, LOCK_TTL_S):
            break
        if i < tries - 1:
            time.sleep(10)
    else:
        if manage_only:
            print("  another job holds the account lock; this sweep stands aside")
            return 0
        journal.record_cycle(profile=profile, action="error",
                             error="account lock still held after 90s; cycle skipped")
        print("  account lock still held after 90s; cycle skipped", file=sys.stderr)
        return 1
    try:
        return _cycle_body(settings, journal, dry_run=False, manage_only=manage_only)
    finally:
        journal.release_lock(profile, holder)


def _cycle_body(settings: Settings, journal, *, dry_run: bool = False,
                manage_only: bool = False) -> int:
    profile = settings.profile
    now = now_et()

    print(f"[{now:%H:%M:%S}] cycle start  profile={profile}  dry_run={dry_run}")

    # The exchange's own hours for today. A failed calendar call degrades to
    # the regular session rather than to no trading at all.
    try:
        day = now.strftime("%Y-%m-%d")
        calendar = cli.trading_calendar(
            day, (now + timedelta(days=7)).strftime("%Y-%m-%d"), profile)
        session = session_bounds(now, calendar)
        next_session = next_session_date(now, calendar)
        if session is None:
            print("  the market is closed today; nothing to do")
            return 0
    except cli.CLIError as e:
        print(f"  warn: calendar unavailable, assuming regular hours: {e}", file=sys.stderr)
        session = (now.replace(hour=9, minute=30, second=0, microsecond=0),
                   now.replace(hour=16, minute=0, second=0, microsecond=0))
        next_session = None

    if manage_only and not in_session(now, session):
        # Sweeps run every 10 minutes across the whole day. Outside the session
        # a closing order cannot fill, so skip rather than queue dead orders.
        # Inside it they always run: the first and last five minutes are an
        # ENTRY lockout, and a stop must not wait for them.
        print("  outside the trading session; sweep is a no-op")
        return 0

    # We hold the lock, so any order of ours still working is an orphan.
    _, unresolved_orders = cancel_stale_orders(journal, profile, now, dry_run=dry_run)

    expiry = resolve_expiry(profile, now)
    print(f"  target expiry: {expiry}")

    brain = None
    if not manage_only:
        # The model is the one dependency that fails silently at billing
        # time. Check it before paying for a snapshot it cannot read.
        try:
            brain = Brain(model=settings.model)
            brain.preflight()
        except Exception as e:  # noqa: BLE001 - any failure here means no cycle
            journal.record_cycle(profile=profile, action="error",
                                 error=f"anthropic preflight: {e}")
            print(f"  anthropic preflight failed: {e}", file=sys.stderr)
            return 1

    try:
        obs = observe(profile, expiry)
    except cli.CLIError as e:
        journal.record_cycle(profile=profile, action="error", error=str(e))
        print(f"  observe failed: {e}", file=sys.stderr)
        return 1

    equity = obs["equity"]
    day_start = day_start_equity(
        obs["account"], journal.day_start_equity(now.strftime("%Y-%m-%d"), profile)) or equity

    # The daily limit, as documented: flatten the book and halt for the day.
    # It used to reject new entries and nothing else.
    halted = halted_today(journal, profile, now)
    breached = daily_loss_breached(equity=equity, day_start=day_start, limits=settings.limits)
    if breached:
        detail = (f"daily loss {(equity - day_start) / day_start:+.2%} at/over the "
                  f"{settings.limits.max_daily_loss_pct:.0%} limit")
        print(f"  DAILY LIMIT: {detail}; flattening and halting", file=sys.stderr)
        if not halted and not dry_run:
            record_halt(journal, profile, detail, equity)
        halted = True
    else:
        detail = None
    # The halt, not the number, keeps a liquidation alive until the book is flat.
    flatten = flatten_reason(halted=halted, breached=breached,
                             open_spreads=journal.open_spreads(profile), detail=detail)
    if flatten and not breached:
        print(f"  {flatten}", file=sys.stderr)
    journal.record_mark(profile=profile, equity=equity,
                        cash=float(obs["account"].get("cash", 0)),
                        positions=obs["positions"])

    # Exits before entries, always.
    # Before managing anything, make sure the journal describes the book.
    unexplained = reconcile(journal, profile, obs["positions"], now, dry_run=dry_run)
    unexplained += unresolved_orders
    for msg in unexplained:
        print(f"  RECONCILE: {msg}", file=sys.stderr)
    if unexplained and not manage_only and not dry_run:
        journal.record_cycle(profile=profile, action="error", equity=equity,
                             error="reconcile: " + " | ".join(unexplained))

    # Ex-dividend dates, only for names where we are short calls. Alpaca
    # announces these a couple of days ahead, so ask every time.
    ex_divs: dict = {}
    for u in {r["underlying"] for r in journal.open_spreads(profile)
              if r.get("right") == "C" and (r.get("sleeve") or "core") == "core"}:
        try:
            found = cli.ex_dividend(u, profile, now.strftime("%Y-%m-%d"),
                                    (now + timedelta(days=7)).strftime("%Y-%m-%d"))
            if found:
                ex_divs[u] = found
                print(f"  {u} goes ex-dividend {found[0]} ({found[1]:.2f}/share)")
        except cli.CLIError as e:
            print(f"  warn: ex-dividend lookup failed for {u}: {e}", file=sys.stderr)

    open_marks = manage_open_spreads(settings, journal, obs, now, dry_run=dry_run,
                                     flatten=flatten, close_t=session[1],
                                     ex_divs=ex_divs, next_session=next_session)

    if manage_only:
        # Exit sweeps run far more often than entry cycles: stops and profit
        # targets need to be responsive, but a fresh LLM opinion every few
        # minutes costs money and adds nothing.
        print("  manage-only pass complete")
        return 0

    if halted:
        journal.record_cycle(profile=profile, action="stood_down", equity=equity,
                             reasoning="Halted for the day by the daily loss limit; no entries.")
        print("  halted for the day; no entry considered")
        return 0
    if unexplained:
        # No new risk while the broker holds something the journal cannot explain.
        print("  book not reconciled; no entry considered")
        return 0

    tape, sides = read_tape(obs, now, settings.limits)
    cycle_regime = tape["SPY"].regime      # the universe read, for the journal badge
    recent_spreads = [r for r in journal.all_spreads(profile)
                      if (r.get("ts_open") or "") >= (now - timedelta(days=7)).strftime("%Y-%m-%d")]

    found, funnel = cand.enumerate_candidates(
        chains=obs["chains"], quotes=obs["quotes"], tape=tape, sides=sides, now=now,
        expiry=expiry, limits=settings.limits, open_spreads=journal.open_spreads(profile),
        profile=profile, session=session)
    print(f"  candidates: {funnel['pairs']} pairs, {funnel['survivors']} survive; "
          f"rejections {funnel['failed']}")

    snapshot = build_snapshot(
        now=now, equity=equity, day_start_equity=day_start,
        positions=obs["positions"], quotes=obs["quotes"], chains=obs["chains"],
        bars=obs.get("bars", {}), news=obs.get("news", []),
        limits=settings.limits, target_expiry=expiry, tape=tape, sides=sides,
        recent_spreads=recent_spreads, candidate_lines=cand.render(found, funnel),
    )

    try:
        decision: AgentDecision = brain.decide(snapshot, settings.limits)
    except Exception as e:  # noqa: BLE001 - a brain failure must not trade
        journal.record_cycle(profile=profile, action="error", snapshot=snapshot,
                             equity=equity, error=f"brain: {e}")
        print(f"  brain failed: {e}", file=sys.stderr)
        return 1

    print(f"  reasoning: {decision.reasoning[:300]}")

    if decision.proposal is None:
        journal.record_cycle(
            profile=profile, action="stood_down", snapshot=snapshot,
            reasoning=decision.reasoning, regime=cycle_regime, equity=equity,
        )
        print("  stood down (no proposal)")
        return 0

    p = decision.proposal
    cycle_regime = tape[p.underlying].regime if p.underlying in tape else cycle_regime
    kind = "cr" if p.is_credit else "db"
    print(f"  proposal: {p.underlying} {p.expiry} {p.right} "
          f"{p.short_strike}/{p.long_strike} x{p.qty} @ {p.net_price:.2f} {kind} "
          f"({p.sleeve}); max loss ${p.total_max_loss:,.0f} / "
          f"max profit ${p.total_max_profit:,.0f}")

    # Regime binds the size before the gates see it. Downsizing beats blocking:
    # a good trade at smaller size beats a wasted cycle.
    eff_pct = regime.budget_pct_for(cycle_regime, p.sleeve, settings.limits)
    new_qty, note = regime.resize_to_budget(p, equity=equity, effective_pct=eff_pct)
    print(f"  regime policy [{cycle_regime}/{p.sleeve}]: {note}")
    if new_qty != p.qty:
        p = p.model_copy(update={"qty": new_qty})

    # One fresh, consistent observation for the verdict, or no trade.
    now = now_et()
    try:
        final = final_observation(journal, profile, expiry, p, now, settings.limits)
    except cli.CLIError as e:
        journal.record_cycle(profile=profile, action="error", snapshot=snapshot,
                             reasoning=decision.reasoning, proposal=p.model_dump(),
                             regime=cycle_regime, equity=equity,
                             error=f"final observation failed; not submitting on stale data: {e}")
        print(f"  final observation failed; not submitting: {e}", file=sys.stderr)
        return 1
    obs, equity, tape, open_marks = final["obs"], final["equity"], final["tape"], final["marks"]

    # A breach seen only now takes the same road as one seen at the top of the
    # cycle: halt, flatten, no entry. It used to just fail the daily_loss gate.
    if daily_loss_breached(equity=equity, day_start=day_start, limits=settings.limits):
        detail = (f"daily loss {(equity - day_start) / day_start:+.2%} at/over the "
                  f"{settings.limits.max_daily_loss_pct:.0%} limit (seen at the final observation)")
        print(f"  DAILY LIMIT: {detail}; flattening and halting", file=sys.stderr)
        if not dry_run:
            record_halt(journal, profile, detail, equity)
        manage_open_spreads(settings, journal, obs, now, dry_run=dry_run, flatten=detail,
                            close_t=session[1])
        return 0

    # Size and direction follow the FRESH tape; the book answers to the most
    # defensive read in the universe, so a sideways ticker cannot admit risk a
    # bear book would refuse.
    cycle_regime = tape[p.underlying].regime if p.underlying in tape else cycle_regime
    book_regime = min((t.regime for t in tape.values()),
                      key=lambda r: regime.policy_for(r).size_multiplier)
    eff_pct = regime.budget_pct_for(cycle_regime, p.sleeve, settings.limits)
    fresh_qty, note = regime.resize_to_budget(p, equity=equity, effective_pct=eff_pct)
    if fresh_qty != p.qty:
        print(f"  regime policy on the final observation [{cycle_regime}]: {note}")
        p = p.model_copy(update={"qty": fresh_qty})

    gates = risk.evaluate(
        p, profile=profile, now=now, equity=equity, day_start_equity=day_start,
        open_positions=obs["positions"], chain=obs["chains"].get(p.underlying, {}),
        limits=settings.limits, regime=cycle_regime,
        chains=obs["chains"], quotes=obs["quotes"], target_expiry=expiry,
        open_spreads=journal.open_spreads(profile),
        tape=tape.get(p.underlying), open_marks=open_marks,
        recent_spreads=recent_spreads, session=session, book_regime=book_regime,
    )
    for g in gates:
        print(f"    {g}")

    gate_payload = [g.model_dump() for g in gates]

    if not risk.all_passed(gates):
        journal.record_cycle(
            profile=profile, action="blocked", snapshot=snapshot,
            reasoning=decision.reasoning, proposal=p.model_dump(),
            gates=gate_payload, regime=cycle_regime, equity=equity,
        )
        print(f"  BLOCKED by {len(risk.blockers(gates))} gate(s)")
        return 0

    # What we asked for, frozen before the fill overwrites it. The gates above
    # were evaluated against these numbers, so journaling the filled proposal
    # next to them would put a verdict and a different price in one document
    # and make slippage unauditable afterwards.
    requested = p

    coid = f"hack-{uuid.uuid4().hex[:24]}"
    if not dry_run:
        # Intent on record BEFORE the order leaves. If this process dies
        # between submit and journal, reconciliation adopts the position and
        # the orphan-order sweep cancels anything still working; this row is
        # what explains, afterwards, where either came from.
        journal.record_cycle(profile=profile, action="intent", regime=cycle_regime,
                             equity=equity, proposal=requested.model_dump(), order_id=coid)
    try:
        result = cli.submit_mleg(
            # Signed net price: negative for a credit we require, positive for
            # a debit we will pay. See submit_mleg's docstring.
            legs=p.legs(), limit_price=p.signed_limit, qty=p.qty,
            profile=profile, client_order_id=coid, dry_run=dry_run,
        )
    except cli.CLIError as e:
        journal.record_cycle(
            profile=profile, action="error", snapshot=snapshot,
            reasoning=decision.reasoning, proposal=p.model_dump(),
            gates=gate_payload, regime=cycle_regime, equity=equity, error=str(e),
        )
        print(f"  submit failed: {e}", file=sys.stderr)
        return 1

    order_id = result.get("id") if isinstance(result, dict) else None
    if not dry_run and order_id:
        fill = cli.fill_result(order_id, profile)
        if fill["timed_out"]:
            # The order is still working. Walking away here is what orphans a
            # position: we would journal "unfilled" while a live order goes on
            # to fill, leaving a spread no exit rule can see. Cancel first,
            # then re-poll -- the cancel can lose the race, and the second poll
            # is what tells us which way it went.
            print(f"  order still {fill['status']} after polling; cancelling to keep "
                  "the broker and the journal in agreement")
            cli.cancel_order(order_id, profile)
            fill = cli.fill_result(order_id, profile)

        print(f"  fill: status={fill['status']} qty={fill['qty']}/{p.qty} "
              f"credit={fill['credit']}")

        if fill["timed_out"]:
            # Neither polling nor cancelling settled it. This is the one state
            # where the broker and the journal can still disagree, so claim
            # nothing and say so loudly.
            journal.record_cycle(
                profile=profile, action="error", snapshot=snapshot,
                reasoning=decision.reasoning, proposal=requested.model_dump(),
                gates=gate_payload, regime=cycle_regime, equity=equity,
                order_id=order_id,
                error=(f"order {order_id} still {fill['status']} after cancel; "
                       "broker may hold an unjournaled position -- RECONCILE MANUALLY"),
            )
            print(f"  UNSETTLED after cancel ({fill['status']}) -- "
                  "manual reconciliation required", file=sys.stderr)
            return 1

        if fill["qty"] == 0:
            # Nothing filled. Journaling a spread we do not hold would make the
            # sweep chase a phantom position for the rest of the week.
            journal.record_cycle(
                profile=profile, action="unfilled", snapshot=snapshot,
                reasoning=decision.reasoning, proposal=p.model_dump(),
                gates=gate_payload, regime=cycle_regime, equity=equity,
                order_id=order_id,
                error=f"order {fill['status']} with 0 filled; no spread recorded",
            )
            print(f"  NOT FILLED ({fill['status']}) -- no spread journaled")
            return 0
        # Record what we actually got, in both size and price.
        p = p.model_copy(update={
            "qty": fill["qty"],
            "net_price": fill["credit"] if fill["credit"] is not None else p.net_price,
        })
        if fill["qty"] < decision.proposal.qty:
            print(f"  PARTIAL FILL: {fill['qty']} of {decision.proposal.qty} -- "
                  "journaling the size actually held")
        journal.record_spread(profile=profile, proposal=p, order_id=order_id)
    elif not dry_run:
        journal.record_spread(profile=profile, proposal=p, order_id=order_id)
    journal.record_cycle(
        profile=profile, action="dry_run" if dry_run else "submitted",
        snapshot=snapshot, reasoning=decision.reasoning,
        proposal=requested.model_dump(),
        gates=gate_payload, regime=cycle_regime, equity=equity, order_id=order_id,
    )
    print(f"  {'DRY RUN' if dry_run else 'SUBMITTED'}  order_id={order_id}  coid={coid}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one trading cycle.")
    ap.add_argument("--profile", default=None,
                    help="alpaca CLI profile (default: dev / $ALPACA_PROFILE)")
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate and gate, but do not submit")
    ap.add_argument("--manage-only", action="store_true",
                    help="run exit management only; do not consult the model")
    args = ap.parse_args()

    settings = Settings(profile=args.profile) if args.profile else Settings()

    if settings.profile == COMPETITION_PROFILE and not args.dry_run:
        print(f"NOTE: targeting the JUDGED account ({COMPETITION_PROFILE}). "
              "Gate zero will refuse any fill before kickoff.", file=sys.stderr)

    return run_cycle(settings, dry_run=args.dry_run, manage_only=args.manage_only)


if __name__ == "__main__":
    raise SystemExit(main())
