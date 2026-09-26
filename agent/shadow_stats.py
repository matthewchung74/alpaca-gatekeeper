"""Statistics over the settled shadow ledger.

Everything here carries an EFFECTIVE sample size. Neighbouring strikes on one
underlying, one side, one expiry share a terminal price, so three hundred such
rows are close to one observation. Inverse-Herfindahl over those clusters is
the count the reports and the learning step trust.

Returns are P&L per dollar of max loss. `field` chooses which resolution to
score: `ret_hold` (exact, held to expiry) or `ret_mgd` (approximate, managed
by our exit rules). Rows without the field are skipped.
"""
from __future__ import annotations

import math
from collections import defaultdict

DELTA_BUCKETS = ((0.05, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.45))


def cluster_of(row: dict) -> tuple:
    return (row.get("u"), row.get("r"), row.get("expiry"))


def effective_n(rows: list[dict]) -> float:
    sizes: dict = defaultdict(int)
    for r in rows:
        sizes[cluster_of(r)] += 1
    if not sizes:
        return 0.0
    total = sum(sizes.values())
    return total * total / sum(n * n for n in sizes.values())


def cluster_means(rows: list[dict], field: str) -> list[float]:
    """One number per cluster: the mean of the field inside it."""
    acc: dict = defaultdict(list)
    for r in rows:
        v = r.get(field)
        if v is not None:
            acc[cluster_of(r)].append(float(v))
    return [sum(v) / len(v) for v in acc.values()]


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def welch(a: list[float], b: list[float]) -> tuple[float | None, float | None, float | None]:
    """(difference of means a-b, its standard error, t). None below two samples."""
    if len(a) < 2 or len(b) < 2:
        return (None, None, None)
    ma, mb = _mean(a), _mean(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    diff = ma - mb
    return (diff, se, diff / se if se > 0 else (0.0 if diff == 0 else math.copysign(1e9, diff)))


def _with(rows: list[dict], field: str) -> list[dict]:
    return [r for r in rows if r.get(field) is not None]


def admitted(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("fail")]


def refused_only(rows: list[dict], code: str) -> list[dict]:
    """Rows whose ONLY failure is `code`: a clean test of that one parameter."""
    return [r for r in rows if list(r.get("fail") or []) == [code]]


def gate_regret(rows: list[dict], field: str = "ret_hold") -> dict[str, dict]:
    """Per failure code: what the spreads it alone refused went on to do,
    against what the admitted spreads did. Positive `diff` means the gate
    refused spreads that outperformed what it let through."""
    rows = _with(rows, field)
    adm = admitted(rows)
    adm_means = cluster_means(adm, field)
    codes = sorted({c for r in rows for c in (r.get("fail") or [])})
    out: dict[str, dict] = {}
    for code in codes:
        ref = refused_only(rows, code)
        if not ref:
            continue
        ref_means = cluster_means(ref, field)
        diff, se, t = welch(ref_means, adm_means)
        held = [r for r in ref if r.get("held") is not None]
        out[code] = {
            "n": len(ref), "n_eff": effective_n(ref),
            "mean": _mean([float(r[field]) for r in ref]),
            "hold_rate": (sum(1 for r in held if r["held"]) / len(held)) if held else None,
            "admitted_mean": _mean([float(r[field]) for r in adm]),
            "admitted_n_eff": effective_n(adm),
            "diff": diff, "se": se, "t": t,
        }
    return out


def calibration(rows: list[dict]) -> dict[str, dict]:
    """By short-delta bucket: the market's implied hold rate (1 - delta) against
    the realized one. A positive edge means claims held more often than the
    market priced, i.e. the premium was rich. This is the edge test."""
    out: dict[str, dict] = {}
    scored = [r for r in rows if r.get("held") is not None and r.get("d") is not None]
    for lo, hi in DELTA_BUCKETS:
        b = [r for r in scored if lo <= float(r["d"]) < hi]
        if not b:
            continue
        implied = _mean([1.0 - float(r["d"]) for r in b])
        realized = sum(1 for r in b if r["held"]) / len(b)
        out[f"{lo:.2f}-{hi:.2f}"] = {
            "n": len(b), "n_eff": effective_n(b), "implied_hold": implied,
            "realized_hold": realized, "edge": realized - implied,
            "mean_ret_hold": _mean([float(r["ret_hold"]) for r in b if r.get("ret_hold") is not None]),
        }
    return out


def model_vs_field(docs: list[dict], field: str = "ret_hold") -> dict:
    """The model's pick against the mean of every admitted candidate in the
    same cycle. Positive `diff` means the model chose better than the field."""
    picks, fields = [], []
    for d in docs:
        rows = _with(d.get("rows") or [], field)
        pick = next((r for r in rows if r.get("chosen")), None)
        field_rows = admitted(rows)
        if pick is None or not field_rows:
            continue
        picks.append(float(pick[field]))
        fields.append(_mean([float(r[field]) for r in field_rows]))
    diff, se, t = welch(picks, fields)
    return {"n": len(picks), "pick_mean": _mean(picks), "field_mean": _mean(fields),
            "diff": (_mean(picks) - _mean(fields)) if picks else None, "t": t}


def view_structure(spreads: list[dict], closes: dict[tuple, float]) -> dict[str, int]:
    """The 2x2 on real closed trades whose expiry has passed. The view is the
    claim ("the underlying finishes beyond my short strike"); the structure is
    the strikes, width and exits. A lucky win teaches nothing and is named."""
    grid = {"view_held_profit": 0, "view_held_loss_structure": 0,
            "view_failed_loss": 0, "view_failed_profit_luck": 0, "unscored": 0}
    for s in spreads:
        if s.get("status") != "closed" or s.get("realized_pnl") is None:
            continue
        close = closes.get((s.get("underlying"), s.get("expiry")))
        if close is None:
            grid["unscored"] += 1
            continue
        k = float(s["short_strike"])
        held = close <= k if s.get("right") == "C" else close >= k
        profit = float(s["realized_pnl"]) > 0
        if held and profit:
            grid["view_held_profit"] += 1
        elif held:
            grid["view_held_loss_structure"] += 1
        elif profit:
            grid["view_failed_profit_luck"] += 1
        else:
            grid["view_failed_loss"] += 1
    grid["attributable"] = (grid["view_held_profit"] + grid["view_held_loss_structure"]
                            + grid["view_failed_loss"])
    return grid


def print_report(journal, profile: str, limits) -> None:
    from . import alpaca_cli as cli
    from . import usage as usage_mod
    sp = usage_mod.spend(journal, profile)
    if sp["calls"]:
        print(f"MODEL SPEND: {sp['calls']} calls, {sp['input']:,} in / {sp['output']:,} out, "
              f"${sp['cost_usd']:.2f} total, ${sp['per_call']:.3f} per call"
              + (f" ({sp['unpriced']} calls on an unpriced model)" if sp["unpriced"] else ""))
    docs = journal.settled_shadow(profile)
    rows = [dict(r, expiry=d.get("expiry")) for d in docs for r in (d.get("rows") or [])]
    min_n = getattr(limits, "learn_min_n", 20)
    print(f"SHADOW LEDGER REPORT  profile={profile}  documents={len(docs)}  rows={len(rows)}  "
          f"effective n={effective_n(rows):.1f}  (minimum for any conclusion: {min_n})")
    for field, label in (("ret_hold", "held to expiry (exact)"), ("ret_mgd", "managed by our exits (approximate)")):
        print(f"\nGATE REGRET, {label}. diff > 0: the gate refused spreads that beat the ones it admitted.")
        reg = gate_regret(rows, field)
        if not reg:
            print("  no settled refused rows yet")
        for code, g in sorted(reg.items(), key=lambda kv: -(kv[1]["t"] or 0)):
            verdict = "not enough data" if g["n_eff"] < min_n else ("REGRET" if (g["t"] or 0) > 1.65 else
                                                                   "earning its keep" if (g["t"] or 0) < -1.65 else "no clear effect")
            print(f"  {code:22} refused n={g['n']:4} n_eff={g['n_eff']:5.1f} hold={g['hold_rate'] if g['hold_rate'] is None else f'{g['hold_rate']:.0%}':>5} "
                  f"mean={g['mean']:+.3f} vs admitted {g['admitted_mean']:+.3f}  t={g['t'] if g['t'] is None else f'{g['t']:+.2f}'}  {verdict}")
    print("\nCALIBRATION by short delta. edge > 0: claims held more often than the market priced (premium was rich).")
    cal = calibration(rows)
    if not cal:
        print("  no settled rows yet")
    for bucket, c in cal.items():
        note = "" if c["n_eff"] >= min_n else "  (not enough data)"
        print(f"  delta {bucket}: n={c['n']:4} n_eff={c['n_eff']:5.1f} implied {c['implied_hold']:.1%} "
              f"realized {c['realized_hold']:.1%} edge {c['edge']:+.1%} mean ret {c['mean_ret_hold']:+.3f}{note}")
    mv = model_vs_field(docs)
    print(f"\nMODEL vs FIELD (held to expiry): cycles with a pick={mv['n']}  pick mean="
          f"{'n/a' if mv['pick_mean'] is None else f'{mv['pick_mean']:+.3f}'}  field mean="
          f"{'n/a' if mv['field_mean'] is None else f'{mv['field_mean']:+.3f}'}  diff="
          f"{'n/a' if mv['diff'] is None else f'{mv['diff']:+.3f}'}")
    spreads = [s for s in journal.all_spreads(profile) if s.get("status") == "closed"]
    closes = {}
    for s in spreads:
        key = (s.get("underlying"), s.get("expiry"))
        if key not in closes:
            try:
                closes[key] = cli.daily_close(key[0], key[1], profile)
            except cli.CLIError:
                closes[key] = None
    grid = view_structure(spreads, closes)
    print("\nVIEW x STRUCTURE on real closed trades:")
    print(f"  view held & profit {grid['view_held_profit']}   view held & loss (structure) {grid['view_held_loss_structure']}   "
          f"view failed & loss {grid['view_failed_loss']}   view failed & profit (LUCK, teaches nothing) {grid['view_failed_profit_luck']}   "
          f"unscored {grid['unscored']}   attributable {grid['attributable']}")
