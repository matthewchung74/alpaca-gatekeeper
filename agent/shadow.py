"""Settle the shadow ledger against real prices.

Every entry cycle journals every candidate spread it saw, traded or not, with
the claim it makes: "at expiry the underlying closes beyond the short strike".
Once the expiry has passed this module resolves each claim two ways.

  held to expiry  -- exact. Intrinsic value from the underlying's close, so a
                     put spread with the close below the long strike is worth
                     the width and a call spread below the short is worth 0.
  managed         -- approximate. Walk the daily option bars from entry and ask
                     whether our own 50% target or 3x stop would have fired
                     first. A day whose worst case reaches the stop books the
                     stop; else a day whose best case reaches the target books
                     the target; a day that could do both books the stop,
                     because the order inside the day is unknown. Spread values
                     are clamped to [0, width]: paper option bars carry absurd
                     prints.

Returns are P&L per dollar of max loss, so widths compare. All of it is
measurement; nothing here can place an order.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from . import alpaca_cli as cli
from .config import ET, RiskLimits, Settings, now_et
from .journal import open_journal
from .models import occ_symbol


def _ret(cr: float, exit_value: float, w: float) -> float | None:
    risk = w - cr
    return (cr - exit_value) / risk if risk > 0 else None


def settle_row(row: dict, *, close: float | None, bars: dict | None, limits: RiskLimits) -> dict:
    """Resolve one candidate's claim. Adds held, v_exp, ret_hold, ret_mgd, mgd_rule."""
    out = dict(row)
    ks, kl, w, cr = float(row["ks"]), float(row["kl"]), float(row["w"]), float(row["cr"])
    out.update(held=None, v_exp=None, ret_hold=None, ret_mgd=None, mgd_rule=None)
    if close is None or w <= 0:
        return out

    intrinsic = max(0.0, close - ks) if row["r"] == "C" else max(0.0, ks - close)
    v_exp = min(intrinsic, w)
    out["held"] = intrinsic == 0.0
    out["v_exp"] = round(v_exp, 4)
    out["ret_hold"] = _ret(cr, v_exp, w)

    if not bars or not bars.get("short") or not bars.get("long"):
        return out
    stop = min(cr * limits.stop_loss_multiple, w * 0.90)
    target = cr * (1.0 - limits.profit_target_pct)
    by_day_long = {str(b.get("t", ""))[:10]: b for b in bars["long"]}
    for sb in sorted(bars["short"], key=lambda b: str(b.get("t", ""))):
        lb = by_day_long.get(str(sb.get("t", ""))[:10])
        if not lb:
            continue
        try:
            worst = min(max(float(sb["h"]) - float(lb["l"]), 0.0), w)   # pay the short high, get the long low
            best = min(max(float(sb["l"]) - float(lb["h"]), 0.0), w)    # the cheapest the spread got
        except (KeyError, TypeError, ValueError):
            continue
        if worst >= stop:
            out["mgd_rule"], out["ret_mgd"] = "stop_loss", _ret(cr, stop, w)
            return out
        if best <= target:
            out["mgd_rule"], out["ret_mgd"] = "profit_target", _ret(cr, target, w)
            return out
    out["mgd_rule"], out["ret_mgd"] = "expiry", out["ret_hold"]
    return out


def settle_document(doc: dict, *, profile: str, limits: RiskLimits) -> list[dict]:
    """Settle every row of one ledger document. One bars call per document."""
    expiry = str(doc["expiry"])
    rows = doc.get("rows") or []
    closes: dict[str, float | None] = {}
    for u in {r["u"] for r in rows}:
        try:
            closes[u] = cli.daily_close(u, expiry, profile)
        except cli.CLIError as e:
            print(f"  warn: no close for {u} on {expiry}: {str(e)[:120]}", file=sys.stderr)
            closes[u] = None
    symbols = sorted({occ_symbol(r["u"], expiry, r["r"], float(k))
                      for r in rows for k in (r["ks"], r["kl"])})
    start = str(doc.get("ts", ""))[:10] or expiry
    try:
        bars = cli.option_bars(symbols, start, expiry, profile) if symbols else {}
    except cli.CLIError as e:
        print(f"  warn: option bars unavailable for {doc.get('id')}: {e}", file=sys.stderr)
        bars = {}
    out = []
    for r in rows:
        legs = {"short": bars.get(occ_symbol(r["u"], expiry, r["r"], float(r["ks"]))) or [],
                "long": bars.get(occ_symbol(r["u"], expiry, r["r"], float(r["kl"]))) or []}
        out.append(settle_row(r, close=closes.get(r["u"]), bars=legs, limits=limits))
    return out


def settle_all(journal, profile: str, limits: RiskLimits, now=None) -> int:
    """Settle every document whose expiry has closed. Returns how many."""
    now = now or now_et()
    cutoff = now.strftime("%Y-%m-%d") if now.hour >= 16 else (now - timedelta(days=1)).strftime("%Y-%m-%d")
    n = 0
    for doc in journal.unsettled_shadow(profile, on_or_before=cutoff):
        rows = settle_document(doc, profile=profile, limits=limits)
        if all(r.get("held") is None for r in rows):
            print(f"  {doc.get('id')}: no close for {doc.get('expiry')} yet; left unsettled")
            continue
        journal.settle_shadow(doc["id"], rows)
        held = sum(1 for r in rows if r.get("held"))
        print(f"  {doc.get('id')}: {len(rows)} rows settled for {doc.get('expiry')}, "
              f"{held} claims held")
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Shadow ledger: settle claims, then learn.")
    ap.add_argument("command", choices=["settle", "report"])
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()
    settings = Settings(profile=args.profile) if args.profile else Settings()
    journal = open_journal(settings.journal_path)
    if args.command == "settle":
        n = settle_all(journal, settings.profile, settings.limits)
        print(f"settled {n} document(s)")
        try:
            from . import learning
            for ev in learning.step(journal, settings.profile, settings.limits, now_et()):
                print(f"  learning: {ev}")
        except ImportError:
            pass
        return 0
    from . import shadow_stats
    shadow_stats.print_report(journal, settings.profile, settings.limits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
