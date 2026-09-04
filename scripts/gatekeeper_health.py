"""Deterministic health-check for the Gatekeeper cloud agent.

Layer (1) of the monitor: no model, no judgement, no orders. It answers the
questions that have a factual answer -- is the schedule still armed, did the
last invocation succeed, is the journal advancing, are we inside the risk
limits -- and exits non-zero when something needs a human.

Read-only by construction: the only writes are to stdout and, on CRIT, a macOS
notification. It queries the broker through the local CLI profile, but only
ever with GET -- it cannot place, modify, or cancel an order.

    exit 0  all clear      exit 1  warnings      exit 2  critical
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

GCLOUD = "/Users/mattc/google-cloud-sdk/bin/gcloud"
ALPACA = "/opt/homebrew/bin/alpaca"
PROFILE = "dev"
PROJECT = "alpaca-ai-agent-2026"
REGION = "us-east1"
FIRESTORE = (f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
             "/databases/(default)/documents")

# SPY260903P00767000 -> root, yymmdd, right, strike x1000
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

ET = timezone(timedelta(hours=-4))          # EDT for the whole competition window
# The hackathon is over; this now watches ongoing trading on the dev account.
# Drawdown is measured from the account's own starting balance, not the event's.
STARTING_EQUITY = 100_000.0
MAX_DAILY_LOSS_PCT = 0.04
MAX_EVENT_DD_PCT = 0.15

# The deployed Cloud Scheduler cron, not agent/scheduler.py (which is dead in
# the cloud path and disagrees -- it says 11:30/13:30/15:30).
ENTRY_SLOTS = [(9, 45), (11, 45), (13, 45), (15, 45)]
SWEEP_STALE_MIN = 25                        # sweeps run every 10 min; 25 is two misses

findings: list[tuple[str, str, str]] = []   # (severity, check, message)


def report(sev: str, check: str, msg: str) -> None:
    findings.append((sev, check, msg))


def gcloud(*args: str) -> str | None:
    """Run gcloud read-only. None on any failure -- callers degrade gracefully."""
    try:
        r = subprocess.run([GCLOUD, *args, f"--project={PROJECT}"],
                           capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        report("WARN", "gcloud", f"invocation failed: {e}")
        return None
    if r.returncode != 0:
        report("WARN", "gcloud", f"{' '.join(args[:3])} exited {r.returncode}: "
                                 f"{r.stderr.strip()[:200]}")
        return None
    return r.stdout


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def in_session(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    return (now_et.hour, now_et.minute) >= (9, 30) and now_et.hour < 16


# --- check 1: the journal, read straight from Firestore ---------------

def _val(v: dict):
    """Decode one Firestore typed value."""
    if "nullValue" in v: return None
    if "stringValue" in v: return v["stringValue"]
    if "doubleValue" in v: return float(v["doubleValue"])
    if "integerValue" in v: return int(v["integerValue"])
    if "booleanValue" in v: return v["booleanValue"]
    return None


def _query(collection: str, order_desc: str | None = None, limit: int = 200):
    """Run one Firestore query and return decoded documents.

    Deliberately single-field: filter or order, never both, so this needs no
    composite index -- the same constraint journal_firestore.py works under.
    Narrowing by profile happens in Python.
    """
    q: dict = {"from": [{"collectionId": collection}], "limit": limit}
    if order_desc:
        q["orderBy"] = [{"field": {"fieldPath": order_desc}, "direction": "DESCENDING"}]
    body = json.dumps({"structuredQuery": q}).encode()
    try:
        tok = subprocess.run([GCLOUD, "auth", "print-access-token"],
                             capture_output=True, text=True, timeout=60, check=True).stdout.strip()
        req = urllib.request.Request(
            f"{FIRESTORE}:runQuery", data=body, method="POST",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError,
            subprocess.SubprocessError, json.JSONDecodeError) as e:
        report("CRIT", "journal", f"Firestore {collection} unreadable: {e}")
        return None
    out = []
    for row in rows:
        doc = row.get("document")
        if not doc:
            continue
        d = {k: _val(v) for k, v in (doc.get("fields") or {}).items()}
        d["id"] = doc["name"].rsplit("/", 1)[-1]
        out.append(d)
    return out


def load_state() -> dict | None:
    """What the checks need, for THIS profile.

    Equity comes from the broker rather than the journal: it is the number the
    risk checks should react to, and it stays correct even if a cycle failed to
    record a mark.
    """
    spreads = _query("spreads")
    cycles = _query("cycles", order_desc="ts")
    if spreads is None or cycles is None:
        return None

    try:
        r = subprocess.run([ALPACA, "api", "GET", "/v2/account", "--profile", PROFILE],
                           capture_output=True, text=True, timeout=90)
        acct = json.loads(r.stdout) if r.returncode == 0 else {}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        report("CRIT", "broker", f"account unreadable: {e}")
        return None
    equity = float(acct.get("equity") or 0)
    if equity <= 0:
        report("CRIT", "broker", f"no equity for profile {PROFILE}: {str(acct)[:160]}")
        return None

    today = datetime.now(ET).strftime("%Y-%m-%d")
    marks = [m for m in (_query("marks", order_desc="ts") or [])
             if m.get("profile") == PROFILE and str(m.get("ts", ""))[:10] == today]
    day_start = marks[-1]["equity"] if marks else equity

    return {
        "equity": equity,
        "day_start_equity": day_start,
        "cycles": [c for c in cycles if c.get("profile") == PROFILE][:40],
        "open_spreads": [s for s in spreads
                         if s.get("profile") == PROFILE and s.get("status") == "open"],
        "closed_spreads": [s for s in spreads
                           if s.get("profile") == PROFILE and s.get("status") == "closed"],
    }


# --- check 2: is the schedule still armed ---

def check_schedulers() -> None:
    out = gcloud("scheduler", "jobs", "list", f"--location={REGION}",
                 "--format=json")
    if out is None:
        return
    jobs = {j["name"].rsplit("/", 1)[-1]: j for j in json.loads(out)}
    for name in ("entry-cycles", "exit-sweeps"):
        j = jobs.get(name)
        if j is None:
            report("CRIT", "scheduler", f"{name} does not exist")
            continue
        if j.get("state") != "ENABLED":
            report("CRIT", "scheduler", f"{name} is {j.get('state')}, not ENABLED")
        # A populated status block means the last delivery attempt errored.
        if j.get("status"):
            report("CRIT", "scheduler", f"{name} last delivery failed: {j['status']}")


# --- check 3+4: did the jobs actually run, and did they succeed ---

def check_executions(now_utc: datetime, now_et: datetime) -> None:
    out = gcloud("run", "jobs", "executions", "list", f"--region={REGION}",
                 "--limit=25", "--format=json")
    if out is None:
        return
    execs = json.loads(out)
    if not execs:
        report("WARN", "executions", "no Cloud Run executions found at all")
        return

    newest: dict[str, datetime] = {}
    for e in execs:
        job = (e.get("metadata", {}).get("labels", {}) or {}).get("run.googleapis.com/job")
        created = parse_ts(e.get("metadata", {}).get("creationTimestamp"))
        status = e.get("status", {}) or {}
        failed = status.get("failedCount") or 0
        cancelled = status.get("cancelledCount") or 0
        name = e.get("metadata", {}).get("name", "?")

        if created and (now_utc - created) < timedelta(hours=24):
            if failed or cancelled:
                report("CRIT", "executions",
                       f"{name} failed={failed} cancelled={cancelled}")
        if job and created:
            newest[job] = max(newest.get(job, created), created)

    if in_session(now_et):
        last = newest.get("agent-sweep")
        if last is None:
            report("CRIT", "executions", "no agent-sweep execution on record")
        else:
            age = (now_utc - last).total_seconds() / 60
            if age > SWEEP_STALE_MIN:
                report("CRIT", "executions",
                       f"last agent-sweep was {age:.0f} min ago "
                       f"(expected every 10); exits are not being managed")


# --- check 5: has the journal advanced for every entry slot that has passed ---

def check_entry_coverage(state: dict, now_et: datetime) -> None:
    if now_et.weekday() >= 5:
        return
    today = now_et.strftime("%Y-%m-%d")
    due = [f"{h:02d}:{m:02d}" for h, m in ENTRY_SLOTS
           if (now_et.hour, now_et.minute) >= (h, m + 8)]   # 8 min to run
    if not due:
        return
    # Any terminal action means the job ran, which is what coverage measures.
    # `unfilled` and `stood_down` are entry cycles that completed without a
    # position; counting only `submitted`/`blocked` reports a phantom gap.
    ran = ("submitted", "blocked", "dry_run", "unfilled", "stood_down", "error")
    seen = 0
    for c in state.get("cycles") or []:
        ts = parse_ts(c.get("ts"))
        if ts and ts.astimezone(ET).strftime("%Y-%m-%d") == today \
                and c.get("action") in ran:
            seen += 1
    if seen < len(due):
        report("WARN", "entry-coverage",
               f"{len(due)} entry slot(s) due today ({', '.join(due)}) but only "
               f"{seen} entry cycle(s) journaled")


# --- check 6: errors the agent recorded about itself ---

def check_journal_errors(state: dict, now_utc: datetime, *,
                         reconciled: bool) -> None:
    """Errors from the last 24h, escalated by whether the damage still stands.

    A past error stays CRIT while its consequence is live. Once a later cycle
    has completed cleanly AND reconciliation says the broker and the journal
    agree, the error is history: it stays visible as a WARN but stops paging
    every 15 minutes, which is how a real alert gets ignored.

    The reconciliation result is the load-bearing half. If it could not run we
    have no evidence the damage is repaired, so nothing is downgraded.
    """
    clean = ("submitted", "blocked", "stood_down", "dry_run")
    later_ok = max(
        (parse_ts(c.get("ts")) for c in state.get("cycles") or []
         if c.get("action") in clean and not c.get("error")
         and parse_ts(c.get("ts"))),
        default=None,
    )

    for c in state.get("cycles") or []:
        if not c.get("error"):
            continue
        ts = parse_ts(c.get("ts"))
        if not ts or (now_utc - ts) >= timedelta(hours=24):
            continue
        recovered = reconciled and later_ok is not None and ts < later_ok
        report("WARN" if recovered else "CRIT", "journal",
               f"cycle {ts.astimezone(ET):%m-%d %H:%M ET} recorded error: "
               f"{str(c['error'])[:220]}"
               + (" [recovered: a later cycle completed and the broker agrees "
                  "with the journal]" if recovered else ""))


# --- check 7: risk limits, from the equity the agent itself reported ---

def check_risk(state: dict) -> None:
    equity = state.get("equity")
    if equity is None:
        report("WARN", "risk", "no equity in state")
        return
    day_start = state.get("day_start_equity") or equity
    day_loss = (day_start - equity) / day_start if day_start else 0
    total_dd = (STARTING_EQUITY - equity) / STARTING_EQUITY

    if day_loss >= MAX_DAILY_LOSS_PCT:
        report("CRIT", "risk", f"daily loss {day_loss:.2%} at/over the "
                               f"{MAX_DAILY_LOSS_PCT:.0%} halt")
    elif day_loss >= MAX_DAILY_LOSS_PCT * 0.75:
        report("WARN", "risk", f"daily loss {day_loss:.2%} nearing the "
                               f"{MAX_DAILY_LOSS_PCT:.0%} halt")

    if total_dd >= MAX_EVENT_DD_PCT:
        report("CRIT", "risk", f"event drawdown {total_dd:.2%} at/over the "
                               f"{MAX_EVENT_DD_PCT:.0%} halt")
    elif total_dd >= MAX_EVENT_DD_PCT * 0.75:
        report("WARN", "risk", f"event drawdown {total_dd:.2%} nearing the "
                               f"{MAX_EVENT_DD_PCT:.0%} halt")


# --- check 8: expiry day must end flat ---

def check_expiry(state: dict, now_et: datetime) -> None:
    open_rows = state.get("open_spreads") or []
    today = now_et.strftime("%Y-%m-%d")
    expiring = [s for s in open_rows if s.get("expiry") == today]
    if expiring and (now_et.hour, now_et.minute) >= (15, 30):
        report("CRIT", "expiry",
               f"{len(expiring)} spread(s) expiring today still open at "
               f"{now_et:%H:%M} ET; the flatten has not completed")
    stale = [s for s in open_rows
             if s.get("expiry") and s["expiry"] < today]
    if stale:
        report("CRIT", "expiry",
               f"{len(stale)} spread(s) open past their own expiry: "
               + ", ".join(f"{s['underlying']} {s['expiry']}" for s in stale[:3]))


# --- check 9: does the broker agree with the journal ---

def broker_legs() -> dict[tuple, int] | None:
    """Held option legs as {(underlying, expiry, right, strike): signed qty}."""
    try:
        r = subprocess.run([ALPACA, "api", "GET", "/v2/positions",
                            "--profile", PROFILE],
                           capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        report("WARN", "reconcile", f"alpaca CLI unavailable: {e}")
        return None
    if r.returncode != 0:
        report("WARN", "reconcile", f"positions query failed: {r.stderr.strip()[:200]}")
        return None
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        report("WARN", "reconcile", f"positions unparseable: {e}")
        return None

    held: dict[tuple, int] = {}
    for p in rows:
        m = OCC.match(p.get("symbol", ""))
        if not m:
            continue                       # equities and anything non-option
        root, ymd, right, strike = m.groups()
        key = (root, f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}", right, int(strike) / 1000)
        held[key] = held.get(key, 0) + int(float(p.get("qty") or 0))
    return held


def check_reconciliation(state: dict) -> bool:
    """Broker positions vs journaled spreads, in both directions.

    The journal drives exit management, so a leg the broker holds and the
    journal does not know about is unmanaged risk: no profit target, no stop,
    no expiry flatten. `manage.is_actually_held` already guards the opposite
    direction, which is why that one is only a warning here.

    Returns True if the comparison actually ran; False if the broker could not
    be reached, so no caller treats silence here as evidence of agreement.
    """
    held = broker_legs()
    if held is None:
        return False

    expected: dict[tuple, int] = {}
    for s in state.get("open_spreads") or []:
        try:
            u, e, r = s["underlying"], s["expiry"], s["right"]
            qty = int(s["qty"])
            short_k = (u, e, r, float(s["short_strike"]))
            long_k = (u, e, r, float(s["long_strike"]))
        except (KeyError, TypeError, ValueError):
            report("WARN", "reconcile", f"journaled spread {s.get('id')} is malformed")
            continue
        expected[short_k] = expected.get(short_k, 0) - qty
        expected[long_k] = expected.get(long_k, 0) + qty

    def fmt(k: tuple) -> str:
        return f"{k[0]} {k[1]} {k[2]}{k[3]:g}"

    for key in sorted(set(held) | set(expected), key=fmt):
        h, x = held.get(key, 0), expected.get(key, 0)
        if h == x:
            continue
        if x == 0:
            report("CRIT", "reconcile",
                   f"broker holds {h:+d} {fmt(key)} that NO journaled spread "
                   f"accounts for -- unmanaged by every exit rule")
        elif h == 0:
            report("WARN", "reconcile",
                   f"journal expects {x:+d} {fmt(key)} but the broker holds none "
                   f"(unfilled or closed outside the journal)")
        else:
            report("CRIT", "reconcile",
                   f"size mismatch on {fmt(key)}: broker {h:+d}, journal {x:+d}")
    return True


def alert(title: str, msg: str) -> None:
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification {json.dumps(msg[:240])} '
                        f'with title {json.dumps(title)}'],
                       capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        pass


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    print(f"--- gatekeeper health {now_et:%Y-%m-%d %H:%M:%S ET} ---")

    state = load_state()
    check_schedulers()
    check_executions(now_utc, now_et)
    if state is not None:
        # Reconciliation first: it is the evidence that decides whether a past
        # journal error still matters or is already repaired.
        reconciled = check_reconciliation(state)
        check_journal_errors(state, now_utc, reconciled=reconciled)
        check_entry_coverage(state, now_et)
        check_risk(state)
        check_expiry(state, now_et)

        eq = state.get("equity")
        print(f"    equity {eq:,.2f}  day P&L {state.get('day_pnl', 0):+,.2f}  "
              f"total {state.get('total_pnl', 0):+,.2f}  "
              f"open {len(state.get('open_spreads') or [])}  "
              f"closed {len(state.get('closed_spreads') or [])}")
        last = parse_ts((state.get('cycles') or [{}])[0].get('ts'))
        if last:
            print(f"    last journaled cycle {last.astimezone(ET):%m-%d %H:%M ET} "
                  f"({(now_utc - last).total_seconds() / 60:.0f} min ago)")


    crit = [f for f in findings if f[0] == "CRIT"]
    warn = [f for f in findings if f[0] == "WARN"]
    for sev, check, msg in crit + warn:
        print(f"    [{sev}] {check}: {msg}")
    if not findings:
        print("    [OK] all checks passed")

    if crit:
        alert("Gatekeeper CRITICAL", f"{crit[0][1]}: {crit[0][2]}")
        return 2
    return 1 if warn else 0


if __name__ == "__main__":
    raise SystemExit(main())
