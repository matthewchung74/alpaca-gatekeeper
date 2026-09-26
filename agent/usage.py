"""What each model call cost, recorded rather than estimated.

The API returns token counts on every response. Journaling them turns
"roughly fifteen cents a cycle" into a number, which is what the effort-level
and model-choice questions actually need.

Prices are per million tokens, from Anthropic's pricing page (2026-09-23). An
unknown model records its tokens with a null cost rather than guessing.
"""
from __future__ import annotations

import json

PRICES: dict[str, tuple[float, float]] = {        # model -> (input $/MTok, output $/MTok)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


def price(model: str) -> tuple[float, float] | None:
    return PRICES.get(model)


def summarise(raw, model: str) -> dict:
    """Token counts and cost from an API usage object."""
    def g(name: str) -> int:
        try:
            return int(getattr(raw, name, 0) or 0)
        except (TypeError, ValueError):
            return 0
    inp, out = g("input_tokens"), g("output_tokens")
    cache_read, cache_write = g("cache_read_input_tokens"), g("cache_creation_input_tokens")
    p = price(model)
    cost = None if p is None else round(inp * p[0] / 1e6 + out * p[1] / 1e6, 6)
    return {"model": model, "input": inp, "output": out, "cache_read": cache_read,
            "cache_write": cache_write, "cost_usd": cost}


def spend(journal, profile: str, since: str | None = None, limit: int = 500) -> dict:
    """Everything the journal has recorded about model spend for one account."""
    calls = inp = out = 0
    cost = 0.0
    unpriced = 0
    for c in journal.recent_cycles(limit=limit, profile=profile):
        if since and (c.get("ts") or "") < since:
            continue
        u = c.get("usage")
        if not u:
            continue
        u = json.loads(u) if isinstance(u, str) else u
        calls += 1
        inp += int(u.get("input") or 0)
        out += int(u.get("output") or 0)
        if u.get("cost_usd") is None:
            unpriced += 1
        else:
            cost += float(u["cost_usd"])
    return {"calls": calls, "input": inp, "output": out, "cost_usd": round(cost, 4),
            "per_call": round(cost / calls, 4) if calls else None, "unpriced": unpriced}
