"""Rules versions: the few gate parameters the learning step may move.

The base limits live in config. A per-account rules document in the journal
holds overrides, each of which must be a value on that parameter's ladder;
anything else is ignored, so a corrupt document cannot loosen a gate off its
rails. Every other limit is not tunable at all: tranche, book, directional and
daily-loss caps, position count, leg overlap, quote validity, direction,
cadence. Those are the walls; these are the dials.
"""
from __future__ import annotations

import sys
from dataclasses import replace

from .config import RiskLimits

# failure code -> (parameter, ladder from tightest to loosest)
TUNABLE: dict[str, tuple[str, tuple]] = {
    "liquidity:oi":     ("min_open_interest",       (1000, 500, 300, 200, 100)),
    "liquidity:spread": ("max_spread_pct_of_mid",   (0.07, 0.10, 0.15, 0.20)),
    "range_buffer:em":  ("expected_move_multiple",  (1.30, 1.15, 1.00, 0.85, 0.70)),
    "credit_floor":     ("min_credit_pct_of_width", (0.15, 0.12, 0.10, 0.08, 0.06)),
    "delta_band:low":   ("min_short_delta",         (0.15, 0.10, 0.07, 0.05)),
    "delta_band:high":  ("max_short_delta",         (0.25, 0.30, 0.35, 0.40)),
}
PARAMS = {p: ladder for p, ladder in TUNABLE.values()}
CODE_OF = {p: code for code, (p, _) in TUNABLE.items()}


def empty_doc() -> dict:
    return {"version": 0, "overrides": {}, "in_flight": None, "locks": {}, "history": []}


def limits_for(journal, profile: str, base: RiskLimits) -> tuple[RiskLimits, int]:
    """The base limits with this account's valid overrides applied, and the version."""
    doc = journal.get_rules(profile) or empty_doc()
    valid = {}
    for name, value in (doc.get("overrides") or {}).items():
        ladder = PARAMS.get(name)
        if ladder is None:
            print(f"  warn: rules override for non-tunable {name!r} ignored", file=sys.stderr)
            continue
        if not any(abs(float(value) - float(v)) < 1e-9 for v in ladder):
            print(f"  warn: rules override {name}={value} is not on its ladder; ignored", file=sys.stderr)
            continue
        valid[name] = type(ladder[0])(value)
    return replace(base, **valid), int(doc.get("version") or 0)


def step_from(param: str, current, direction: str):
    """The next value on the ladder in `direction` ('loosen' or 'tighten'), or None at the end."""
    ladder = PARAMS[param]
    idx = next((i for i, v in enumerate(ladder) if abs(float(v) - float(current)) < 1e-9), None)
    if idx is None:
        return None
    j = idx + 1 if direction == "loosen" else idx - 1
    return ladder[j] if 0 <= j < len(ladder) else None


def passes_at(row: dict, param: str, value) -> bool:
    """Would this ledger row's metric pass the parameter at `value`?"""
    if param == "min_open_interest":
        return float(row.get("oi") or 0) >= value
    if param == "max_spread_pct_of_mid":
        return float(row.get("spr") or 1) <= value
    if param == "expected_move_multiple":
        emr = row.get("emr")
        return emr is not None and float(emr) >= value
    if param == "min_credit_pct_of_width":
        return float(row.get("cr") or 0) / float(row.get("w") or 1) >= value
    if param == "min_short_delta":
        return float(row.get("d") or 0) >= value
    if param == "max_short_delta":
        return float(row.get("d") or 1) <= value
    return True
