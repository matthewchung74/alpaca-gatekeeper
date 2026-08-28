"""Long-running scheduler. One process, market-hours aware.

Two cadences, deliberately different:

  entry cycles  -- a handful per session, at fixed times. Each one costs an LLM
                   call, and a fresh opinion every few minutes buys nothing.
  exit sweeps   -- every few minutes throughout the session. Stops, profit
                   targets and the expiry-day flatten must be responsive.

Runs as the container's main process alongside the dashboard.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timedelta

from .config import DEADLINE, ET, KICKOFF, Settings, now_et
from .loop import run_cycle

# Entry cycles, ET. 09:45 lets the opening auction settle (the no-trade window
# covers the first five minutes); 15:30 is the last useful entry before the
# close-window lockout at 15:55.
ENTRY_TIMES = [(9, 45), (11, 30), (13, 30), (15, 30)]
EXIT_SWEEP_MINUTES = int(os.environ.get("EXIT_SWEEP_MINUTES", "10"))


def is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def _entry_slots(day: datetime) -> list[datetime]:
    return [day.replace(hour=h, minute=m, second=0, microsecond=0)
            for h, m in ENTRY_TIMES]


def main() -> int:
    settings = Settings()
    print(f"scheduler up: profile={settings.profile} "
          f"kickoff={KICKOFF:%Y-%m-%d %H:%M %Z} deadline={DEADLINE:%Y-%m-%d %H:%M %Z}",
          flush=True)
    print(f"entry cycles at {ENTRY_TIMES} ET; exit sweeps every "
          f"{EXIT_SWEEP_MINUTES} min", flush=True)

    fired: set[str] = set()          # entry slots already run, keyed by timestamp
    last_sweep = datetime.min.replace(tzinfo=ET)

    while True:
        try:
            now = now_et()

            if now > DEADLINE:
                print("deadline passed; scheduler idle", flush=True)
                time.sleep(300)
                continue

            if not is_market_hours(now):
                time.sleep(60)
                continue

            # Entry cycles: fire once per slot, and only after the slot time has
            # passed. A restart mid-session will not re-run a slot it already did
            # unless the process itself restarted -- the risk gates and the
            # per-underlying concentration cap are what actually prevent
            # double-sizing, not this bookkeeping.
            for slot in _entry_slots(now):
                key = slot.isoformat()
                if key in fired or now < slot or now > slot + timedelta(minutes=20):
                    continue
                print(f"\n=== entry cycle {slot:%H:%M} ET ===", flush=True)
                run_cycle(settings)
                fired.add(key)

            if (now - last_sweep).total_seconds() >= EXIT_SWEEP_MINUTES * 60:
                print(f"\n--- exit sweep {now:%H:%M} ET ---", flush=True)
                run_cycle(settings, manage_only=True)
                last_sweep = now

            time.sleep(30)

        except KeyboardInterrupt:
            print("scheduler stopped", flush=True)
            return 0
        except Exception:  # noqa: BLE001 - the scheduler must outlive any cycle
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
