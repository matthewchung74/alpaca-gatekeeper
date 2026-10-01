"""Firestore journal backend, for Cloud Run.

Same interface as SQLiteJournal. Cloud Run's filesystem is ephemeral and each
scheduled invocation is a separate container, so the journal cannot be a local
file.

Deliberate design choice: every query here is single-field ordered or
single-field filtered, and any further narrowing happens in Python. That avoids
composite indexes entirely -- a hackathon week produces a few hundred documents,
so the cost is nil, and there is no chance of a missing-index error stopping the
agent at the open.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from google.cloud import firestore

LOCKS = "locks"
SHADOW = "shadow"
RULES = "rules"
CYCLES = "cycles"
MARKS = "marks"
SPREADS = "spreads"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _jsonify(v: Any) -> str | None:
    """Store nested structures as JSON strings.

    Keeps parity with the SQLite backend so the dashboard's decode path is
    identical for both, and sidesteps Firestore's nested-array restrictions.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, default=str)


class FirestoreJournal:
    def __init__(self, project: str | None = None):
        self.db = firestore.Client(
            project=project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )

    # --- writes ----------------------------------------------------------

    def record_cycle(
        self, *, profile: str, action: str, snapshot: Any = None,
        reasoning: str | None = None, proposal: Any = None, gates: Any = None,
        regime: str | None = None, equity: float | None = None,
        order_id: str | None = None, error: str | None = None, usage=None,
    ) -> str:
        doc = {
            "ts": _now(), "profile": profile, "regime": regime, "equity": equity,
            "snapshot": _jsonify(snapshot), "reasoning": reasoning,
            "proposal": _jsonify(proposal), "gates": _jsonify(gates),
            "action": action, "order_id": order_id, "error": error,
            "usage": _jsonify(usage),
        }
        ref = self.db.collection(CYCLES).document()
        ref.set(doc)
        return ref.id

    def record_mark(self, *, profile: str, equity: float, cash: float | None,
                    positions: Any) -> None:
        ts = _now()
        self.db.collection(MARKS).document().set({
            "ts": ts, "day": ts[:10], "profile": profile, "equity": equity,
            "cash": cash, "positions": _jsonify(positions),
        })

    def record_spread(self, *, profile: str, proposal, order_id: str | None) -> str:
        ref = self.db.collection(SPREADS).document()
        ref.set({
            "ts_open": _now(), "profile": profile,
            "underlying": proposal.underlying, "expiry": proposal.expiry,
            "right": proposal.right, "short_strike": proposal.short_strike,
            "long_strike": proposal.long_strike, "qty": proposal.qty,
            "entry_credit": proposal.net_price, "sleeve": proposal.sleeve,
            "open_order_id": order_id, "status": "open",
            "ts_close": None, "exit_debit": None, "exit_rule": None,
            "realized_pnl": None, "close_order_id": None,
        })
        return ref.id

    def close_spread(self, spread_id: str, *, exit_debit: float, exit_rule: str,
                     realized_pnl: float, close_order_id: str | None) -> None:
        self.db.collection(SPREADS).document(str(spread_id)).update({
            "status": "closed", "ts_close": _now(), "exit_debit": exit_debit,
            "exit_rule": exit_rule, "realized_pnl": realized_pnl,
            "close_order_id": close_order_id,
        })

    # --- reads -----------------------------------------------------------

    def set_entry_credit(self, spread_id, credit: float) -> None:
        self.db.collection(SPREADS).document(str(spread_id)).update(
            {"entry_credit": credit})

    # --- one job at a time ------------------------------------------------

    def acquire_lock(self, name: str, holder: str, ttl_s: int) -> bool:
        """Take the account's lock unless someone else holds a live one.

        A transaction, so the cycle and the sweep cannot both read "free" and
        both write. The TTL is the backstop for a container that dies holding it.
        """
        import time
        ref = self.db.collection(LOCKS).document(name)
        txn = self.db.transaction()

        @firestore.transactional
        def _take(t) -> bool:
            snap = ref.get(transaction=t)
            cur = snap.to_dict() if snap.exists else None
            now = time.time()
            if cur and cur.get("expires", 0) > now and cur.get("holder") != holder:
                return False
            t.set(ref, {"holder": holder, "expires": now + ttl_s, "ts": _now()})
            return True

        return _take(txn)

    def release_lock(self, name: str, holder: str) -> None:
        """Let go, but only of a lock that is still ours.

        One transaction. A separate read then delete could remove a lock that
        expired and was re-taken by someone else between the two calls.
        """
        ref = self.db.collection(LOCKS).document(name)
        txn = self.db.transaction()

        @firestore.transactional
        def _drop(t) -> None:
            snap = ref.get(transaction=t)
            if snap.exists and (snap.to_dict() or {}).get("holder") == holder:
                t.delete(ref)

        _drop(txn)

    # --- rules versions --------------------------------------------------------

    def get_rules(self, profile: str) -> dict | None:
        snap = self.db.collection(RULES).document(profile).get()
        if not snap.exists:
            return None
        return json.loads((snap.to_dict() or {}).get("doc") or "null")

    def put_rules(self, profile: str, doc: dict) -> None:
        self.db.collection(RULES).document(profile).set(
            {"doc": json.dumps(doc, default=str), "ts": _now()})

    # --- the shadow ledger ---------------------------------------------------

    def record_shadow(self, *, profile: str, ts: str, expiry: str, rules_version: int,
                      rows: list[dict]) -> str:
        ref = self.db.collection(SHADOW).document()
        ref.set({"ts": ts, "profile": profile, "expiry": expiry, "rules_version": rules_version,
                 "rows": json.dumps(rows, default=str), "settled_at": None})
        return ref.id

    def mark_shadow(self, shadow_id, *, chosen=None, traded=None) -> None:
        from .journal import _mark
        ref = self.db.collection(SHADOW).document(str(shadow_id))
        snap = ref.get()
        if not snap.exists:
            return
        rows = _mark(json.loads((snap.to_dict() or {}).get("rows") or "[]"), chosen, traded)
        ref.update({"rows": json.dumps(rows)})

    def unsettled_shadow(self, profile: str, on_or_before: str) -> list[dict]:
        # Single-field filter, narrowed in Python: no composite index needed.
        docs = self.db.collection(SHADOW).where(
            filter=firestore.FieldFilter("profile", "==", profile)).stream()
        out = []
        for d in docs:
            doc = {**d.to_dict(), "id": d.id}
            if doc.get("settled_at") is None and str(doc.get("expiry")) <= on_or_before:
                doc["rows"] = json.loads(doc.get("rows") or "[]")
                out.append(doc)
        return sorted(out, key=lambda x: x.get("ts") or "")

    def settle_shadow(self, shadow_id, rows: list[dict]) -> None:
        self.db.collection(SHADOW).document(str(shadow_id)).update(
            {"rows": json.dumps(rows, default=str), "settled_at": _now()})

    def settled_shadow(self, profile: str, since: str | None = None) -> list[dict]:
        docs = self.db.collection(SHADOW).where(
            filter=firestore.FieldFilter("profile", "==", profile)).stream()
        out = []
        for d in docs:
            doc = {**d.to_dict(), "id": d.id}
            if doc.get("settled_at") and (doc.get("ts") or "") >= (since or ""):
                doc["rows"] = json.loads(doc.get("rows") or "[]")
                out.append(doc)
        return sorted(out, key=lambda x: x.get("ts") or "")

    def reduce_spread(self, spread_id, *, qty: int, realized_pnl: float) -> None:
        """A partial close: fewer contracts remain, and some P&L is banked."""
        self.db.collection(SPREADS).document(str(spread_id)).update(
            {"qty": qty, "realized_pnl": realized_pnl})

    def _spreads(self, profile: str) -> list[dict]:
        docs = self.db.collection(SPREADS).where(
            filter=firestore.FieldFilter("profile", "==", profile)
        ).stream()
        rows = [{**d.to_dict(), "id": d.id} for d in docs]
        rows.sort(key=lambda r: r.get("ts_open") or "")
        return rows

    def open_spreads(self, profile: str) -> list[dict]:
        return [r for r in self._spreads(profile) if r.get("status") == "open"]

    def all_spreads(self, profile: str) -> list[dict]:
        return list(reversed(self._spreads(profile)))

    def recent_cycles(self, limit: int = 50, profile: str | None = None) -> list[dict]:
        """Recent cycles, narrowed to one profile.

        Without the profile filter a second agent writing to the same database
        appears in the dashboard's decision log. Over-fetch and narrow in
        Python rather than adding a where() -- filtering on profile while
        ordering by ts needs a composite index, which this backend avoids by
        design.
        """
        docs = self.db.collection(CYCLES).order_by(
            "ts", direction=firestore.Query.DESCENDING
        ).limit(limit if profile is None else limit * 6).stream()
        rows = [{**d.to_dict(), "id": d.id} for d in docs]
        if profile is not None:
            rows = [r for r in rows if r.get("profile") == profile]
        return rows[:limit]

    def equity_curve(self, profile: str | None = None) -> list[dict]:
        """The equity series for one profile.

        The dashboard derives its headline equity from the last point here, so
        an unfiltered curve does not merely add stray points -- it reports
        another account's balance as this one's.
        """
        docs = self.db.collection(MARKS).order_by("ts").stream()
        return [{"ts": d.get("ts"), "equity": d.get("equity")} for d in docs
                if profile is None or d.get("profile") == profile]

    def day_start_equity(self, day: str, profile: str | None = None) -> float | None:
        docs = self.db.collection(MARKS).where(
            filter=firestore.FieldFilter("day", "==", day)
        ).stream()
        rows = sorted((d.to_dict() for d in docs), key=lambda r: r.get("ts") or "")
        if profile is not None:
            rows = [r for r in rows if r.get("profile") == profile]
        return rows[0]["equity"] if rows else None
