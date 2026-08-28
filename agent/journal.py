"""Durable decision log.

Two backends behind one interface: SQLite for local development and tests,
Firestore for Cloud Run (whose filesystem is ephemeral and whose scheduled
invocations are separate containers). Pick with JOURNAL_BACKEND.


Every cycle writes: market snapshot -> agent reasoning -> proposed order ->
each gate's verdict -> submitted order -> fill. This table is the dashboard,
the video, the write-up and the social content. Write once, use five times.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    profile      TEXT NOT NULL,
    regime       TEXT,
    equity       REAL,
    snapshot     TEXT,           -- JSON: what the agent saw
    reasoning    TEXT,           -- the agent's own words
    proposal     TEXT,           -- JSON: TradeProposal, or null if it stood down
    gates        TEXT,           -- JSON: [{name, passed, detail}]
    action       TEXT NOT NULL,  -- submitted | blocked | stood_down | error
    order_id     TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);

CREATE TABLE IF NOT EXISTS marks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    profile  TEXT NOT NULL,
    equity   REAL NOT NULL,
    cash     REAL,
    positions TEXT              -- JSON
);
CREATE INDEX IF NOT EXISTS idx_marks_ts ON marks(ts);

CREATE TABLE IF NOT EXISTS spreads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open       TEXT NOT NULL,
    profile       TEXT NOT NULL,
    underlying    TEXT NOT NULL,
    expiry        TEXT NOT NULL,
    right         TEXT NOT NULL,
    short_strike  REAL NOT NULL,
    long_strike   REAL NOT NULL,
    qty           INTEGER NOT NULL,
    entry_credit  REAL NOT NULL,
    sleeve        TEXT,
    open_order_id TEXT,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | closed
    ts_close      TEXT,
    exit_debit    REAL,
    exit_rule     TEXT,
    realized_pnl  REAL,
    close_order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_spreads_status ON spreads(profile, status);
"""


class SQLiteJournal:
    def __init__(self, path: str = "data/journal.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_cycle(
        self,
        *,
        profile: str,
        action: str,
        snapshot: Any = None,
        reasoning: str | None = None,
        proposal: Any = None,
        gates: Any = None,
        regime: str | None = None,
        equity: float | None = None,
        order_id: str | None = None,
        error: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cycles
                   (ts, profile, regime, equity, snapshot, reasoning, proposal,
                    gates, action, order_id, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().astimezone().isoformat(),
                    profile, regime, equity,
                    _dumps(snapshot), reasoning, _dumps(proposal), _dumps(gates),
                    action, order_id, error,
                ),
            )
            return cur.lastrowid

    def record_mark(self, *, profile: str, equity: float, cash: float | None,
                    positions: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO marks (ts, profile, equity, cash, positions) VALUES (?,?,?,?,?)",
                (datetime.now().astimezone().isoformat(), profile, equity, cash,
                 _dumps(positions)),
            )

    # --- spread lifecycle ------------------------------------------------

    def record_spread(self, *, profile: str, proposal, order_id: str | None) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO spreads
                   (ts_open, profile, underlying, expiry, right, short_strike,
                    long_strike, qty, entry_credit, sleeve, open_order_id, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                (datetime.now().astimezone().isoformat(), profile,
                 proposal.underlying, proposal.expiry, proposal.right,
                 proposal.short_strike, proposal.long_strike, proposal.qty,
                 proposal.net_price, proposal.sleeve, order_id),
            )
            return cur.lastrowid

    def set_entry_credit(self, spread_id, credit: float) -> None:
        with self._conn() as c:
            c.execute("UPDATE spreads SET entry_credit = ? WHERE id = ?", (credit, spread_id))

    def open_spreads(self, profile: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM spreads WHERE profile = ? AND status = 'open' ORDER BY id",
                (profile,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close_spread(self, spread_id: int, *, exit_debit: float, exit_rule: str,
                     realized_pnl: float, close_order_id: str | None) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE spreads
                   SET status='closed', ts_close=?, exit_debit=?, exit_rule=?,
                       realized_pnl=?, close_order_id=?
                   WHERE id = ?""",
                (datetime.now().astimezone().isoformat(), exit_debit, exit_rule,
                 realized_pnl, close_order_id, spread_id),
            )

    def all_spreads(self, profile: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM spreads WHERE profile = ? ORDER BY id DESC", (profile,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_cycles(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_curve(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, equity FROM marks ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def day_start_equity(self, day: str) -> float | None:
        """First recorded equity on a given YYYY-MM-DD, for the daily-loss gate."""
        with self._conn() as c:
            row = c.execute(
                "SELECT equity FROM marks WHERE ts LIKE ? ORDER BY id ASC LIMIT 1",
                (f"{day}%",),
            ).fetchone()
        return row["equity"] if row else None


def _dumps(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, default=str)


def open_journal(path: str | None = None):
    """Return the configured journal backend.

    JOURNAL_BACKEND=firestore selects Firestore; anything else (the default)
    selects SQLite. Keeping SQLite working is deliberate: if Firestore auth or
    rules misbehave, the agent can still run locally and trade.
    """
    import os

    backend = os.environ.get("JOURNAL_BACKEND", "sqlite").lower()
    if backend == "firestore":
        from .journal_firestore import FirestoreJournal
        return FirestoreJournal()
    return SQLiteJournal(path or os.environ.get("JOURNAL_PATH", "data/journal.db"))
