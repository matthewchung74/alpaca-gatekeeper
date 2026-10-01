"""An outside check that the agent is alive, and loud when it is not.

The agent has had no alerting since the laptop watchdog came off: a crashed
job, a dead API key or a position the journal has lost all look exactly like a
quiet day. This runs on its own schedule, in its own container, reads only, and
writes one CRITICAL log line per problem. A Cloud Monitoring policy turns those
lines into email, so the alerting path does not depend on the thing it watches.

Checks: an entry cycle in the last ~2.5 hours, a sweep in the last 25 minutes,
every broker position known to the journal, no journalled error in the last
hour, and no order of ours working for more than half an hour. Outside the
session it expects nothing and says so.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta

from .config import ET, Settings, now_et

CYCLE_STALE_MIN = 150       # entries run every 2h; 2.5h means one was missed
SWEEP_STALE_MIN = 25        # sweeps run every 10 min
ORDER_STALE_MIN = 30
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _age(ts, now) -> float | None:
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    d = d if d.tzinfo else d.replace(tzinfo=ET)
    return (now - d).total_seconds() / 60.0


def in_session(now) -> bool:
    local = now.astimezone(ET)
    if local.weekday() >= 5:
        return False
    return (9, 45) <= (local.hour, local.minute) <= (16, 0)


def check(*, cycles: list[dict], marks: list[dict], positions: list[dict],
          journal_open: list[dict], open_orders: list[dict], now) -> dict:
    """Everything that should be true right now, and what is not."""
    if not in_session(now):
        return {"ok": True, "alerts": [], "note": "market closed; nothing expected"}

    alerts: list[str] = []
    entries = [c for c in cycles if c.get("action") in
               ("submitted", "blocked", "stood_down", "unfilled", "error", "halt")]
    age = min([a for a in (_age(c.get("ts"), now) for c in entries) if a is not None], default=None)
    if age is None or age > CYCLE_STALE_MIN:
        alerts.append(f"no entry cycle for {'ever' if age is None else f'{age:.0f} min'} "
                      f"(expected within {CYCLE_STALE_MIN})")
    mark_age = min([a for a in (_age(m.get("ts"), now) for m in marks) if a is not None], default=None)
    if mark_age is None or mark_age > SWEEP_STALE_MIN:
        alerts.append(f"no sweep for {'ever' if mark_age is None else f'{mark_age:.0f} min'} "
                      f"(expected within {SWEEP_STALE_MIN}); exits are not being managed")

    known = set()
    for s in journal_open:
        for k in ("short_strike", "long_strike"):
            try:
                known.add(f"{s['underlying']}{str(s['expiry'])[2:].replace('-', '')}"
                          f"{s['right']}{int(round(float(s[k]) * 1000)):08d}")
            except (KeyError, TypeError, ValueError):
                continue
    for p in positions:
        sym = str(p.get("symbol", ""))
        if OCC.match(sym) and sym not in known:
            alerts.append(f"broker holds {sym} x{p.get('qty')} that the journal does not know about")

    for c in cycles:
        a = _age(c.get("ts"), now)
        if c.get("action") == "error" and a is not None and a <= 60:
            alerts.append(f"journalled error {a:.0f} min ago: {str(c.get('error'))[:140]}")

    for o in open_orders:
        if not str(o.get("client_order_id") or "").startswith(("hack-", "exit-")):
            continue
        a = _age(str(o.get("submitted_at", "")).replace("Z", "+00:00"), now)
        if a is not None and a > ORDER_STALE_MIN:
            alerts.append(f"order {o.get('id')} still working after {a:.0f} min")

    return {"ok": not alerts, "alerts": alerts, "note": "in session"}


def main() -> int:
    from . import alpaca_cli as cli
    from .journal import open_journal
    settings = Settings()
    now = now_et()
    # Runs hourly around the clock as well as every 15 min during the session.
    # The off-hours runs exist only to leave a log line: a Monitoring policy
    # alerts when these stop, which is the one failure the watchdog cannot
    # otherwise report -- the scheduler dying, so that nothing runs at all.
    if not in_session(now):
        print(f"watch {now:%Y-%m-%d %H:%M %Z}: heartbeat (market closed; nothing expected)")
        return 0
    journal = open_journal(settings.journal_path)
    try:
        positions = cli.positions(settings.profile)
        orders = cli.open_orders(settings.profile)
    except cli.CLIError as e:
        print(f"GATEKEEPER ALERT broker unreachable: {e}", file=sys.stderr)
        return 2
    out = check(cycles=journal.recent_cycles(limit=40, profile=settings.profile),
                marks=journal.equity_curve(settings.profile)[-5:],
                positions=positions, journal_open=journal.open_spreads(settings.profile),
                open_orders=orders, now=now)
    for a in out["alerts"]:
        print(f"GATEKEEPER ALERT {a}", file=sys.stderr)
    print(f"watch {now:%Y-%m-%d %H:%M %Z}: {'OK' if out['ok'] else str(len(out['alerts'])) + ' alert(s)'}"
          f" ({out['note']})")
    return 1 if out["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
