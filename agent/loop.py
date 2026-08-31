"""One trading cycle, end to end.

    observe -> reason -> gate -> execute -> journal

Run it from cron or a scheduler. Each invocation is independent and idempotent;
there is no in-memory state to lose.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import timedelta

from . import alpaca_cli as cli
from . import regime
from . import risk
from .manage import decide_exit, held_qty, is_actually_held, mark_to_close, spread_from_row
from .brain import Brain, build_snapshot
from .config import (
    COMPETITION_PROFILE, STARTING_EQUITY, TARGET_EXPIRY, UNIVERSE,
    Settings, in_no_trade_window, now_et,
)
from .journal import open_journal
from .models import AgentDecision


def observe(profile: str) -> dict:
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
                sym, profile, expiry=TARGET_EXPIRY, option_type="put",
                strike_gte=round(mid * 0.94), strike_lte=round(mid * 1.00),
            )
            calls = cli.option_chain(
                sym, profile, expiry=TARGET_EXPIRY, option_type="call",
                strike_gte=round(mid * 1.00), strike_lte=round(mid * 1.06),
            )
            chains[sym] = {**puts, **calls}
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


def manage_open_spreads(
    settings: Settings, journal, obs: dict, now, *, dry_run: bool
) -> None:
    """Close anything the exit rules call for, before considering new risk.

    Exits are never gated on the account guard the way entries are: if the
    judged account somehow holds a position, we must always be able to get out
    of it.
    """
    profile = settings.profile
    rows = journal.open_spreads(profile)
    if not rows:
        return

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
        q = obs["quotes"].get(sp.underlying) or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        spot = (bid + ask) / 2 if bid and ask else None

        d = decide_exit(sp, mark, now=now, spot=spot, limits=settings.limits)
        mark_s = f"{mark:.2f}" if mark is not None else "n/a"
        print(f"    #{sp.id} {sp.underlying} {sp.short_strike}/{sp.long_strike} "
              f"cr {sp.entry_credit:.2f} mark {mark_s} -> {d.action}"
              + (f" ({d.rule})" if d.rule else ""))
        print(f"        {d.reason}")

        if d.action != "close":
            continue

        # Pay up to the stop level to get out; a limit at the last mark can sit
        # unfilled exactly when exiting matters most.
        limit = min(mark + 0.05, sp.entry_credit * settings.limits.stop_loss_multiple) \
            if mark is not None else sp.width
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
        pnl = sp.realized_pnl(mark if mark is not None else 0.0)
        journal.close_spread(sp.id, exit_debit=mark or 0.0, exit_rule=d.rule or "manual",
                             realized_pnl=pnl, close_order_id=oid)
        print(f"        CLOSED @ {limit:.2f}  realized ${pnl:+,.0f}  order={oid}")


def run_cycle(settings: Settings, *, dry_run: bool = False,
              manage_only: bool = False) -> int:
    profile = settings.profile
    journal = open_journal(settings.journal_path)
    now = now_et()

    print(f"[{now:%H:%M:%S}] cycle start  profile={profile}  dry_run={dry_run}")

    if manage_only and in_no_trade_window(now, settings.limits):
        # Sweeps run every 10 minutes across the whole day. Outside the session
        # a closing order cannot fill, so skip rather than queue dead orders.
        print("  outside the trading session; sweep is a no-op")
        return 0

    try:
        obs = observe(profile)
    except cli.CLIError as e:
        journal.record_cycle(profile=profile, action="error", error=str(e))
        print(f"  observe failed: {e}", file=sys.stderr)
        return 1

    equity = obs["equity"]
    day_start = journal.day_start_equity(now.strftime("%Y-%m-%d")) or equity
    journal.record_mark(profile=profile, equity=equity,
                        cash=float(obs["account"].get("cash", 0)),
                        positions=obs["positions"])

    # Exits before entries, always.
    manage_open_spreads(settings, journal, obs, now, dry_run=dry_run)

    if manage_only:
        # Exit sweeps run far more often than entry cycles: stops and profit
        # targets need to be responsive, but a fresh LLM opinion every few
        # minutes costs money and adds nothing.
        print("  manage-only pass complete")
        return 0

    snapshot = build_snapshot(
        now=now, equity=equity, day_start_equity=day_start,
        positions=obs["positions"], quotes=obs["quotes"], chains=obs["chains"],
        bars=obs.get("bars", {}), news=obs.get("news", []),
        limits=settings.limits,
    )

    try:
        decision: AgentDecision = Brain(model=settings.model).decide(snapshot, settings.limits)
    except Exception as e:  # noqa: BLE001 - a brain failure must not trade
        journal.record_cycle(profile=profile, action="error", snapshot=snapshot,
                             equity=equity, error=f"brain: {e}")
        print(f"  brain failed: {e}", file=sys.stderr)
        return 1

    print(f"  regime: {decision.regime}")
    print(f"  reasoning: {decision.reasoning[:300]}")

    if decision.proposal is None:
        journal.record_cycle(
            profile=profile, action="stood_down", snapshot=snapshot,
            reasoning=decision.reasoning, regime=decision.regime, equity=equity,
        )
        print("  stood down (no proposal)")
        return 0

    p = decision.proposal
    kind = "cr" if p.is_credit else "db"
    print(f"  proposal: {p.underlying} {p.expiry} {p.right} "
          f"{p.short_strike}/{p.long_strike} x{p.qty} @ {p.net_price:.2f} {kind} "
          f"({p.sleeve}); max loss ${p.total_max_loss:,.0f} / "
          f"max profit ${p.total_max_profit:,.0f}")

    # Regime binds the size before the gates see it. Downsizing beats blocking:
    # a good trade at smaller size beats a wasted cycle.
    eff_pct = regime.budget_pct_for(decision.regime, p.sleeve, settings.limits)
    new_qty, note = regime.resize_to_budget(p, equity=equity, effective_pct=eff_pct)
    print(f"  regime policy [{decision.regime}/{p.sleeve}]: {note}")
    if new_qty != p.qty:
        p = p.model_copy(update={"qty": new_qty})

    gates = risk.evaluate(
        p, profile=profile, now=now, equity=equity, day_start_equity=day_start,
        open_positions=obs["positions"], chain=obs["chains"].get(p.underlying, {}),
        limits=settings.limits, regime=decision.regime,
    )
    for g in gates:
        print(f"    {g}")

    gate_payload = [g.model_dump() for g in gates]

    if not risk.all_passed(gates):
        journal.record_cycle(
            profile=profile, action="blocked", snapshot=snapshot,
            reasoning=decision.reasoning, proposal=p.model_dump(),
            gates=gate_payload, regime=decision.regime, equity=equity,
        )
        print(f"  BLOCKED by {len(risk.blockers(gates))} gate(s)")
        return 0

    # What we asked for, frozen before the fill overwrites it. The gates above
    # were evaluated against these numbers, so journaling the filled proposal
    # next to them would put a verdict and a different price in one document
    # and make slippage unauditable afterwards.
    requested = p

    coid = f"hack-{uuid.uuid4().hex[:24]}"
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
            gates=gate_payload, regime=decision.regime, equity=equity, error=str(e),
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
                gates=gate_payload, regime=decision.regime, equity=equity,
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
                gates=gate_payload, regime=decision.regime, equity=equity,
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
        gates=gate_payload, regime=decision.regime, equity=equity, order_id=order_id,
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
