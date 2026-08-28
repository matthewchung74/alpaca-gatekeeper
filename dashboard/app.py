"""Public dashboard: the agent's decisions, live.

This is the submission's demo URL, the video's content, and the source of the
social posts. It reads the journal only -- it never trades and holds no
credentials beyond the read-only CLI profile.

    uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from agent import alpaca_cli as cli
from agent.config import DEADLINE, KICKOFF, STARTING_EQUITY, TARGET_EXPIRY, Settings, now_et
from agent.journal import open_journal
from agent.manage import mark_to_close, spread_from_row
from agent.regime import POLICY, effective_tranche_pct

app = FastAPI(title="Alpaca AI Trading Agent")
SETTINGS = Settings(profile=os.environ.get("ALPACA_PROFILE", "dev"))
TEMPLATE = Path(__file__).parent / "index.html"


_ESCAPES = {r"\n": "\n", r"\t": "\t", r"\"": '"', r"\\": "\\", r"\/": "/"}


def unescape(s: str | None) -> str | None:
    """Repair literal escape sequences in model-authored text.

    Opus-class models sometimes emit `\\u2014` or `\\n` as literal characters
    inside structured-output strings rather than as the characters they denote.
    Decoding only these known sequences is safer than `unicode_escape`, which
    would mangle genuine non-ASCII text.
    """
    if not s or "\\" not in s:
        return s
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    for lit, real in _ESCAPES.items():
        s = s.replace(lit, real)
    return s


def _state() -> dict:
    j = open_journal(SETTINGS.journal_path)
    profile = SETTINGS.profile
    now = now_et()

    curve = j.equity_curve()
    spreads = j.all_spreads(profile)
    open_rows = [s for s in spreads if s["status"] == "open"]

    # Live marks on anything still open.
    marks: dict[int, float | None] = {}
    if open_rows:
        objs = [spread_from_row(r) for r in open_rows]
        syms = sorted({s for o in objs for s in (o.short_symbol(), o.long_symbol())})
        try:
            quotes = cli.option_quotes(syms, profile)
        except Exception:
            quotes = {}
        for o in objs:
            marks[o.id] = mark_to_close(o, quotes)

    equity = curve[-1]["equity"] if curve else STARTING_EQUITY
    day = now.strftime("%Y-%m-%d")
    day_start = j.day_start_equity(day) or equity

    cycles = j.recent_cycles(40)
    for c in cycles:
        for f in ("gates", "proposal"):
            if c.get(f):
                try:
                    c[f] = json.loads(c[f])
                except (json.JSONDecodeError, TypeError):
                    c[f] = None
        c.pop("snapshot", None)   # too large for the wire; it lives in the DB
        c["reasoning"] = unescape(c.get("reasoning"))
        if isinstance(c.get("proposal"), dict):
            c["proposal"]["rationale"] = unescape(c["proposal"].get("rationale"))

    # Which gates have actually stopped a trade, and how often.
    trips: dict[str, int] = {}
    for c in cycles:
        for g in (c.get("gates") or []):
            if not g.get("passed"):
                trips[g["name"]] = trips.get(g["name"], 0) + 1

    realized = sum(s["realized_pnl"] or 0 for s in spreads if s["status"] == "closed")
    closed = [s for s in spreads if s["status"] == "closed"]
    wins = [s for s in closed if (s["realized_pnl"] or 0) > 0]

    return {
        "profile": profile,
        "now": now.isoformat(),
        "kickoff": KICKOFF.isoformat(),
        "deadline": DEADLINE.isoformat(),
        "target_expiry": TARGET_EXPIRY,
        "equity": equity,
        "starting_equity": STARTING_EQUITY,
        "day_start_equity": day_start,
        "total_pnl": equity - STARTING_EQUITY,
        "day_pnl": equity - day_start,
        "realized_pnl": realized,
        "curve": curve,
        "open_spreads": [{**r, "mark": marks.get(r["id"])} for r in open_rows],
        "closed_spreads": closed,
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "cycles": cycles,
        "gate_trips": sorted(trips.items(), key=lambda kv: -kv[1]),
        "regime_policy": {
            r: {
                "budget_pct": effective_tranche_pct(r, SETTINGS.limits.max_tranche_risk_pct),
                "rights": list(p.allowed_rights),
                "rationale": p.rationale,
            }
            for r, p in POLICY.items()
        },
        "limits": {
            "max_tranche_risk_pct": SETTINGS.limits.max_tranche_risk_pct,
            "max_daily_loss_pct": SETTINGS.limits.max_daily_loss_pct,
            "max_event_drawdown_pct": SETTINGS.limits.max_event_drawdown_pct,
            "profit_target_pct": SETTINGS.limits.profit_target_pct,
            "stop_loss_multiple": SETTINGS.limits.stop_loss_multiple,
        },
    }


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse(_state())


# NOT /healthz -- Google Front End reserves that path on Cloud Run and
# answers it with its own 404 before the container is reached.
@app.get("/_health")
def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(TEMPLATE.read_text())
