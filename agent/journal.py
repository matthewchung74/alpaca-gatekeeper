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
    error        TEXT,
    usage        TEXT            -- JSON: tokens and cost of this cycle's model call
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

CREATE TABLE IF NOT EXISTS locks (
    name     TEXT PRIMARY KEY,
    holder   TEXT NOT NULL,
    expires  REAL NOT NULL           -- unix seconds
);

-- The shadow ledger: every candidate spread each entry cycle saw, traded or
-- not, with the claim it registered, settled later against real prices.
CREATE TABLE IF NOT EXISTS shadow (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    profile       TEXT NOT NULL,
    expiry        TEXT NOT NULL,
    rules_version INTEGER NOT NULL DEFAULT 0,
    rows          TEXT NOT NULL,      -- JSON list
    settled_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_open ON shadow(profile, settled_at, expiry);

CREATE TABLE IF NOT EXISTS rules (
    profile  TEXT PRIMARY KEY,
    doc      TEXT NOT NULL            -- JSON: version, overrides, in_flight, locks, history
);
"""


class SQLiteJournal:
    def __init__(self, path: str = "data/journal.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Columns added after a journal was created: CREATE TABLE IF NOT
            # EXISTS leaves an existing table alone, so add them here.
            have = {r["name"] for r in c.execute("PRAGMA table_info(cycles)")}
            if "usage" not in have:
                c.execute("ALTER TABLE cycles ADD COLUMN usage TEXT")

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
        usage: Any = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cycles
                   (ts, profile, regime, equity, snapshot, reasoning, proposal,
                    gates, action, order_id, error, usage)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().astimezone().isoformat(),
                    profile, regime, equity,
                    _dumps(snapshot), reasoning, _dumps(proposal), _dumps(gates),
                    action, order_id, error, _dumps(usage),
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

    # --- one job at a time ------------------------------------------------

    def acquire_lock(self, name: str, holder: str, ttl_s: int) -> bool:
        """Take the account's lock unless someone else holds a live one."""
        import time
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT holder, expires FROM locks WHERE name = ?", (name,)).fetchone()
            if row and row["expires"] > now and row["holder"] != holder:
                return False
            c.execute("INSERT OR REPLACE INTO locks (name, holder, expires) VALUES (?,?,?)",
                      (name, holder, now + ttl_s))
            return True

    def release_lock(self, name: str, holder: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))

    # --- rules versions --------------------------------------------------------

    def get_rules(self, profile: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT doc FROM rules WHERE profile = ?", (profile,)).fetchone()
        return json.loads(row["doc"]) if row else None

    def put_rules(self, profile: str, doc: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO rules (profile, doc) VALUES (?, ?)",
                      (profile, json.dumps(doc, default=str)))

    # --- the shadow ledger ---------------------------------------------------

    def record_shadow(self, *, profile: str, ts: str, expiry: str, rules_version: int,
                      rows: list[dict]) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO shadow (ts, profile, expiry, rules_version, rows) VALUES (?,?,?,?,?)",
                (ts, profile, expiry, rules_version, json.dumps(rows, default=str)))
            return cur.lastrowid

    def mark_shadow(self, shadow_id, *, chosen=None, traded=None) -> None:
        """Flag the row matching (u, r, ks, kl) as the model's pick, or as filled."""
        with self._conn() as c:
            row = c.execute("SELECT rows FROM shadow WHERE id = ?", (shadow_id,)).fetchone()
            if not row:
                return
            rows = _mark(json.loads(row["rows"]), chosen, traded)
            c.execute("UPDATE shadow SET rows = ? WHERE id = ?", (json.dumps(rows), shadow_id))

    def unsettled_shadow(self, profile: str, on_or_before: str) -> list[dict]:
        with self._conn() as c:
            found = c.execute(
                "SELECT * FROM shadow WHERE profile = ? AND settled_at IS NULL AND expiry <= ? "
                "ORDER BY id", (profile, on_or_before)).fetchall()
        return [_shadow_doc(r) for r in found]

    def settle_shadow(self, shadow_id, rows: list[dict]) -> None:
        with self._conn() as c:
            c.execute("UPDATE shadow SET rows = ?, settled_at = ? WHERE id = ?",
                      (json.dumps(rows, default=str), datetime.now().astimezone().isoformat(),
                       shadow_id))

    def settled_shadow(self, profile: str, since: str | None = None) -> list[dict]:
        with self._conn() as c:
            found = c.execute(
                "SELECT * FROM shadow WHERE profile = ? AND settled_at IS NOT NULL "
                "AND ts >= ? ORDER BY id", (profile, since or "")).fetchall()
        return [_shadow_doc(r) for r in found]

    def reduce_spread(self, spread_id, *, qty: int, realized_pnl: float) -> None:
        """A partial close: fewer contracts remain, and some P&L is banked."""
        with self._conn() as c:
            c.execute("UPDATE spreads SET qty = ?, realized_pnl = ? WHERE id = ?",
                      (qty, realized_pnl, spread_id))

    def all_spreads(self, profile: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM spreads WHERE profile = ? ORDER BY id DESC", (profile,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_cycles(self, limit: int = 50, profile: str | None = None) -> list[dict]:
        with self._conn() as c:
            if profile is None:
                rows = c.execute(
                    "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM cycles WHERE profile = ? ORDER BY id DESC LIMIT ?",
                    (profile, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def equity_curve(self, profile: str | None = None) -> list[dict]:
        with self._conn() as c:
            if profile is None:
                rows = c.execute(
                    "SELECT ts, equity FROM marks ORDER BY id ASC"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT ts, equity FROM marks WHERE profile = ? ORDER BY id ASC",
                    (profile,),
                ).fetchall()
        return [dict(r) for r in rows]

    def day_start_equity(self, day: str, profile: str | None = None) -> float | None:
        """First recorded equity on a given YYYY-MM-DD. A fallback only: the
        daily-loss baseline is the broker's prior close (loop.day_start_equity)."""
        with self._conn() as c:
            if profile is None:
                row = c.execute(
                    "SELECT equity FROM marks WHERE ts LIKE ? ORDER BY id ASC LIMIT 1",
                    (f"{day}%",),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT equity FROM marks WHERE ts LIKE ? AND profile = ? "
                    "ORDER BY id ASC LIMIT 1", (f"{day}%", profile),
                ).fetchone()
        return row["equity"] if row else None


def _mark(rows: list[dict], chosen, traded) -> list[dict]:
    for flag, key in (("chosen", chosen), ("traded", traded)):
        if key is None:
            continue
        u, r, ks, kl = key
        for row in rows:
            if (row.get("u"), row.get("r"), float(row.get("ks")), float(row.get("kl"))) == \
                    (u, r, float(ks), float(kl)):
                row[flag] = True
    return rows


def _shadow_doc(r) -> dict:
    d = dict(r)
    d["rows"] = json.loads(d["rows"]) if isinstance(d.get("rows"), str) else (d.get("rows") or [])
    return d


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
