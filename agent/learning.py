"""The learning step: change one gate, one rung, on evidence, and check it later.

Runs after each settlement. The protocol:

  1. If a change is in flight, judge it on the rows journaled SINCE it, in the
     band it newly admits. Once that band has learn_min_review_n effective
     observations: a losing band reverts the change and locks the parameter
     for learn_lock_days; a winning one confirms it. Either way the slot frees.
  2. Otherwise look for one proposal. LOOSEN a parameter when the spreads it
     alone refused have learn_min_n effective observations, a positive mean
     return, and beat the admitted set with one-sided 95% confidence on cluster
     means. TIGHTEN when the marginal admitted band (what the next tighter rung
     would refuse) has that much evidence, a negative mean, and trails the rest
     of the admitted set with the same confidence. Apply only the strongest.
  3. Everything is written to the rules document with its evidence, and to the
     journal as a cycle row with action "rules_change".

A lucky win never counts: returns are scored on the claim the spread made,
so a spread whose view failed is a loss on that claim whatever a stop or an
exit did with the money. The bar is deliberately high: with six clusters a
week, the first change cannot come for about four weeks.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import rules, shadow_stats as st
from .config import ET, RiskLimits

FIELD = "ret_hold"          # exact resolution; the managed path is approximate
T_CRIT = 1.65               # one-sided 95%


def _rows(docs: list[dict]) -> list[dict]:
    return [dict(r, expiry=d.get("expiry"), ts=d.get("ts")) for d in docs for r in (d.get("rows") or [])
            if r.get(FIELD) is not None]


def _event(journal, profile, doc, action, param, old, new, evidence, now) -> dict:
    ev = {"ts": now.isoformat(), "action": action, "param": param, "old": old, "new": new,
          "evidence": evidence}
    doc["history"].append(ev)
    doc["version"] = int(doc.get("version") or 0) + 1
    journal.put_rules(profile, doc)
    journal.record_cycle(profile=profile, action="rules_change",
                         reasoning=f"{action} {param}: {old} -> {new}; {evidence}")
    return ev


def step(journal, profile: str, base: RiskLimits, now: datetime) -> list[dict]:
    if not base.learn_enabled:
        return []
    doc = journal.get_rules(profile) or rules.empty_doc()
    doc.setdefault("overrides", {}); doc.setdefault("locks", {}); doc.setdefault("history", [])
    current, _ = rules.limits_for(journal, profile, base)
    docs = journal.settled_shadow(profile)
    events: list[dict] = []

    # 1. a change in flight is judged before anything else is proposed
    flight = doc.get("in_flight")
    if flight:
        param, old, new = flight["param"], flight["old"], flight["new"]
        since = flight.get("since") or ""
        later = _rows([d for d in docs if (d.get("ts") or "") >= since])
        # the band the change newly admits: passes at `new`, would have failed at `old`
        if flight.get("direction") == "loosen":
            band = [r for r in later if rules.passes_at(r, param, new) and not rules.passes_at(r, param, old)
                    and not [c for c in (r.get("fail") or []) if c != rules.CODE_OF[param]]]
        else:
            band = [r for r in later if rules.passes_at(r, param, old) and not rules.passes_at(r, param, new)
                    and not r.get("fail")]
        n_eff = st.effective_n(band)
        if n_eff >= base.learn_min_review_n:
            mean = sum(float(r[FIELD]) for r in band) / len(band)
            evidence = f"newly {'admitted' if flight.get('direction') == 'loosen' else 'refused'} band: n={len(band)} n_eff={n_eff:.1f} mean={mean:+.3f}"
            doc["in_flight"] = None
            if flight.get("direction") == "loosen" and mean < 0:
                doc["overrides"].pop(param, None)
                doc["locks"][param] = (now + timedelta(days=base.learn_lock_days)).isoformat()
                events.append(_event(journal, profile, doc, "revert", param, new, old, evidence, now))
            elif flight.get("direction") == "tighten" and mean > 0:
                doc["overrides"].pop(param, None)
                doc["locks"][param] = (now + timedelta(days=base.learn_lock_days)).isoformat()
                events.append(_event(journal, profile, doc, "revert", param, new, old,
                                     evidence + "; the refused band was making money", now))
            else:
                events.append(_event(journal, profile, doc, "confirm", param, old, new, evidence, now))
        return events

    # 2. look for one proposal
    all_rows = _rows(docs)
    adm = st.admitted(all_rows)
    adm_means = st.cluster_means(adm, FIELD)
    proposals: list[tuple[float, dict]] = []
    for code, (param, ladder) in rules.TUNABLE.items():
        lock = doc["locks"].get(param)
        if lock and datetime.fromisoformat(lock) > now:
            continue
        cur = getattr(current, param)
        # loosen: what this gate alone refused
        looser = rules.step_from(param, cur, "loosen")
        if looser is not None:
            ref = [r for r in st.refused_only(all_rows, code) if rules.passes_at(r, param, looser)]
            n_eff = st.effective_n(ref)
            if n_eff >= base.learn_min_n:
                means = st.cluster_means(ref, FIELD)
                mean = sum(float(r[FIELD]) for r in ref) / len(ref)
                diff, se, t = st.welch(means, adm_means)
                if mean > 0 and t is not None and t > T_CRIT:
                    proposals.append((t, {"action": "loosen", "param": param, "old": cur, "new": looser,
                                          "effect": diff,
                                          "evidence": f"refused-only n={len(ref)} n_eff={n_eff:.1f} mean={mean:+.3f} vs admitted {sum(adm_means)/len(adm_means):+.3f}; t={t:+.2f}"}))
        # tighten: the marginal admitted band the next rung would refuse
        tighter = rules.step_from(param, cur, "tighten")
        if tighter is not None and adm:
            marginal = [r for r in adm if not rules.passes_at(r, param, tighter)]
            rest = [r for r in adm if rules.passes_at(r, param, tighter)]
            n_eff = st.effective_n(marginal)
            if n_eff >= base.learn_min_n and rest:
                mean = sum(float(r[FIELD]) for r in marginal) / len(marginal)
                diff, se, t = st.welch(st.cluster_means(marginal, FIELD), st.cluster_means(rest, FIELD))
                if mean < 0 and t is not None and t < -T_CRIT:
                    proposals.append((-t, {"action": "tighten", "param": param, "old": cur, "new": tighter,
                                           "effect": -diff,
                                           "evidence": f"marginal admitted n={len(marginal)} n_eff={n_eff:.1f} mean={mean:+.3f} vs rest {sum(float(r[FIELD]) for r in rest)/len(rest):+.3f}; t={t:+.2f}"}))
    if not proposals:
        return events
    # Strongest evidence first; among equally certain proposals (two constant
    # samples both give an unbounded t), the larger effect on returns.
    _, best = max(proposals, key=lambda kv: (min(kv[0], 1e6), kv[1]["effect"]))
    doc["overrides"][best["param"]] = best["new"]
    doc["in_flight"] = {"param": best["param"], "old": best["old"], "new": best["new"],
                        "direction": best["action"], "since": now.isoformat()}
    events.append(_event(journal, profile, doc, best["action"], best["param"], best["old"], best["new"],
                         best["evidence"], now))
    return events
