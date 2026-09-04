"""Thin wrapper around the Alpaca CLI.

Every order in this project exits through this module. The CLI is the execution
boundary: structured JSON in and out, exit codes 0/1/2, automatic retry on
429/5xx, and --client-order-id for idempotency.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from typing import Any

from .config import TARGET_EXPIRY

BIN = shutil.which("alpaca") or "/opt/homebrew/bin/alpaca"


class CLIError(RuntimeError):
    def __init__(self, argv: list[str], code: int, stderr: str):
        self.code = code
        self.stderr = stderr
        super().__init__(f"alpaca {' '.join(argv)} -> exit {code}: {stderr.strip()[:400]}")


def _auth_args(profile: str) -> list[str]:
    """Prefer env credentials when present.

    In a container there is no profile file; the CLI reads ALPACA_API_KEY and
    ALPACA_SECRET_KEY directly. Locally we pass -p so the practice and judged
    accounts stay separate. ALPACA_PROFILE still names the account for the
    guard either way.
    """
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"):
        return []
    return ["-p", profile]


def run(*args: str, profile: str, parse: bool = True, timeout: int = 45) -> Any:
    """Invoke the CLI and return parsed JSON (exit code 2 means auth failure)."""
    argv = [*args, *_auth_args(profile)]
    proc = subprocess.run(
        [BIN, *argv], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise CLIError(list(argv), proc.returncode, proc.stderr)
    if not parse:
        return proc.stdout
    text = proc.stdout.strip()
    if not text:
        return None
    return json.loads(text)


# --- Reads ---------------------------------------------------------------

def account(profile: str) -> dict:
    return run("account", "get", profile=profile)


def positions(profile: str) -> list[dict]:
    return run("position", "list", profile=profile) or []


def clock(profile: str) -> dict:
    return run("clock", profile=profile)


def option_chain(
    underlying: str,
    profile: str,
    expiry: str = TARGET_EXPIRY,
    option_type: str | None = None,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    limit: int = 100,
) -> dict:
    """Fetch an option chain slice.

    Always filter. The endpoint paginates at 100 and returns sorted, so an
    unfiltered call yields nothing but the nearest expiry.
    """
    args = [
        "data", "option", "chain",
        "--underlying-symbol", underlying,
        "--expiration-date", expiry,
        "--limit", str(limit),
    ]
    if option_type:
        args += ["--type", option_type]
    if strike_gte is not None:
        args += ["--strike-price-gte", str(strike_gte)]
    if strike_lte is not None:
        args += ["--strike-price-lte", str(strike_lte)]
    data = run(*args, profile=profile)
    return (data or {}).get("snapshots", {})


def option_quotes(symbols: list[str], profile: str) -> dict:
    """Quotes for specific contracts, by OCC symbol.

    Needed for exit management: a spread that has moved against us can sit well
    outside the strike window we pull for entry, so we ask for it by name.
    """
    if not symbols:
        return {}
    data = run(
        "data", "option", "latest-quotes", "--symbols", ",".join(symbols),
        profile=profile,
    )
    return (data or {}).get("quotes", {})


def daily_bars(symbol: str, profile: str, start: str) -> list[dict]:
    """Recent daily bars.

    Without these the model has no price history at all, and its regime call --
    which binds the risk budget and the permitted direction -- would rest on
    priors rather than on what the tape actually did.
    """
    data = run("data", "bars", "--symbol", symbol, "--timeframe", "1Day",
               "--start", start, profile=profile)
    return (data or {}).get("bars", []) or []


def get_order(order_id: str, profile: str) -> dict:
    return run("order", "get", "--order-id", order_id, profile=profile) or {}


def cancel_order(order_id: str, profile: str) -> bool:
    """Ask the broker to cancel one order.

    True if the cancel was accepted (204), False if it was refused -- a 422
    means the order is no longer cancelable because it settled first, which is
    information, not a failure. Either way the caller MUST re-poll: a cancel is
    a request, not an outcome, and an order can fill in the gap.
    """
    try:
        run("order", "cancel", "--order-id", order_id, profile=profile, parse=False)
        return True
    except CLIError as e:
        if e.code == 2:                    # auth failure is never "already settled"
            raise
        return False


def fill_result(order_id: str, profile: str, tries: int = 24,
                delay: float = 5.0) -> dict:
    """Poll an order to a settled state and report what actually happened.

    Returns {"qty": int, "credit": float | None, "status": str,
             "timed_out": bool}.

    Two things must be reconciled, not one. The limit price is what we asked
    for and filled_avg_price is what we got; the requested qty is what we asked
    for and filled_qty is what we got. Recording either from the request rather
    than the fill is silently wrong -- and a partial fill is the dangerous case,
    because closing a size we do not hold can open an opposite position.

    Now that the limit actually binds (signed net price), resting and partial
    fills are live possibilities rather than theoretical ones.

    `timed_out` is the field that keeps a working order from being mistaken for
    a dead one. Without it the caller cannot distinguish "the market said no"
    from "we stopped looking": both report filled_qty 0. On 2026-08-31 an order
    filled 78.5s after submission, long after a 16s poll had given up, and the
    resulting position was journaled nowhere and managed by nothing. The window
    is now 120s, and the caller cancels rather than walking away.
    """
    settled = {"filled", "canceled", "expired", "rejected", "done_for_day"}
    o: dict = {}
    timed_out = True
    for _ in range(tries):
        o = get_order(order_id, profile)
        if o.get("status") in settled:
            timed_out = False
            break
        time.sleep(delay)
    px = o.get("filled_avg_price")
    return {
        "qty": int(float(o.get("filled_qty") or 0)),
        "credit": abs(float(px)) if px is not None else None,
        "status": o.get("status") or "unknown",
        "timed_out": timed_out,
    }


def list_expiries(underlying: str, profile: str, on_or_after: str,
                  limit: int = 400) -> list[str]:
    """Expiries actually listed for an underlying, nearest first.

    The contracts endpoint returns rows sorted by expiry, so a truncated page
    still contains the nearest dates -- which is all the caller wants. Never
    guess these from a calendar: SPY, QQQ and IWM do not share one weekly
    pattern, and a guessed date produces an empty chain and a stood-down cycle.
    """
    data = run("api", "GET",
               f"/v2/options/contracts?underlying_symbols={underlying}"
               f"&expiration_date_gte={on_or_after}&limit={limit}",
               profile=profile)
    rows = (data or {}).get("option_contracts") or []
    return sorted({r["expiration_date"] for r in rows if r.get("expiration_date")})


def news(symbols: list[str], profile: str, limit: int = 12) -> list[dict]:
    """Recent headlines for the universe (Benzinga via Alpaca)."""
    data = run("data", "news", "--symbols", ",".join(symbols),
               "--limit", str(limit), "--exclude-contentless", profile=profile)
    return (data or {}).get("news", []) or []


def trading_calendar(start: str, end: str, profile: str) -> list[dict]:
    """Session hours. Catches early closes, which shift the expiry-day flatten."""
    return run("calendar", "--start", start, "--end", end, profile=profile) or []


def latest_quote(symbol: str, profile: str) -> dict:
    data = run("data", "latest-quote", "--symbol", symbol, profile=profile)
    return (data or {}).get("quote", {})


# --- Writes --------------------------------------------------------------

def submit_mleg(
    legs: list[dict],
    limit_price: float,
    qty: int,
    profile: str,
    client_order_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Submit a multi-leg (spread) order via the raw orders endpoint.

    `alpaca order submit` does not expose mleg flags, so multi-leg goes through
    `alpaca api POST /v2/orders` with order_class=mleg.

    limit_price is a SIGNED NET PRICE:
        negative -> a credit you require (sell the spread for at least this)
        positive -> a debit you will pay (buy it back for at most this)

    This is load-bearing, and verified against the live API rather than the
    docs. A positive limit on a credit spread means "I will pay up to X", which
    any credit satisfies, so the limit never binds and the order fills at
    whatever the market gives. Tested: demanding +2.50 on a spread worth 1.23
    filled at 1.22; demanding -2.50 correctly rested unfilled.

    Each leg: {"symbol", "ratio_qty", "side", "position_intent"}.
    """
    body = {
        "order_class": "mleg",
        "qty": str(qty),
        "type": "limit",
        "limit_price": f"{limit_price:.2f}",
        "time_in_force": "day",
        "legs": legs,
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }
    if dry_run:
        # No dry-run on the raw endpoint; report what would have been sent.
        return {"dry_run": True, "body": body}
    proc = subprocess.run(
        [BIN, "api", "POST", "/v2/orders", *_auth_args(profile)],
        input=json.dumps(body), capture_output=True, text=True, timeout=45,
    )
    if proc.returncode != 0:
        raise CLIError(["api", "POST", "/v2/orders"], proc.returncode, proc.stderr)
    return json.loads(proc.stdout)


def cancel_all(profile: str) -> Any:
    return run("order", "cancel-all", profile=profile, parse=False)


def close_all(profile: str) -> Any:
    return run("position", "close-all", profile=profile, parse=False)
