# Improved Gatekeeper: measure the edge, then learn from it

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Gatekeeper a way to tell whether its gates and its strategy have an edge, and a disciplined, automatic way to change its own gates on that evidence, running on the `improved_gate_keeper` paper account (PA3RXV2BV1X4, $50,000).

**Architecture:** Every entry cycle already enumerates every vertical the rules could permit and knows which gate refused each one. That enumeration becomes a *shadow ledger*: each candidate, traded or not, is journaled with a claim registered before the outcome is known. A daily settle job resolves expired claims from real prices. Reports turn the ledger into gate regret, calibration, and a view-versus-structure grid, all carried with an honest effective sample size. A learning step, run after settlement, moves one tunable gate parameter one pre-defined step when the evidence clears a bar, journals it as a new rules version, and reverts it if later data disagrees.

**Tech stack:** Python 3.12, pydantic 2, pytest, Alpaca CLI, Firestore / SQLite journals, Cloud Run Jobs + Cloud Scheduler.

**Spec:** this document.

## Decisions (Matt, 2026-09-21)

- Runs on the new account, profile `igk`. The `dev` account no longer exists (its keys return 401 since 2026-09-21 ~13:00 ET), so the existing two cloud jobs are repointed rather than duplicated. `comp` stays untouched.
- Gate changes apply **automatically**, inside the bounds below.
- The pasted paper keys are used as given; they live in Secret Manager (`alpaca-igk-api-key`, `alpaca-igk-secret-key`) and the local CLI profile `igk`, never in the repo.

## Global constraints

- The ledger and reports are measurement. Nothing in them may influence a trade except through the learning step, and the learning step may only move the parameters listed under "Tunable", one ladder step at a time, one change in flight at a time.
- Never tunable: tranche, book, directional and daily-loss limits; position count; leg overlap; quote validity (crossed, stale, unsized, untimestamped); regime direction; cadence.
- Every statistic is reported with its effective sample size. No conclusion, and no rule change, below the configured minimum.
- A lucky win (view failed, trade profited) is never counted as evidence for anything.
- Hypothetical outcomes are labelled hypothetical. The managed-exit simulation is labelled approximate.
- `./.venv/bin/python -m pytest tests/ -q` passes before every commit (192 at the start).
- Branch `improved-gatekeeper`. `main` and the `dashboard` Cloud Run service stay frozen until lablab announces winners.

## Definitions

- **Claim** (the view): "at expiry the underlying closes beyond the short strike" -- below it for a short call, above it for a short put. The market's own probability of that is `1 - |short delta|`.
- **Cluster:** `(underlying, right, expiry)`. Every candidate in a cluster shares one terminal price, so they are close to one bet, however many strikes and cycles produced them.
- **Effective n:** inverse-Herfindahl over cluster sizes, `(sum n_c)^2 / sum n_c^2`. Equals the number of clusters when they are equal-sized, and shrinks when one cluster dominates.
- **Return:** P&L per dollar of max loss, `pnl / (width - credit)`, so spreads of different widths compare.
- **Refused-only set for a parameter:** settled candidates whose *only* failure codes map to that parameter.
- **Admitted set:** settled candidates that failed nothing.

## Tunable parameters and their ladders

| Failure code | Parameter | Ladder (current value in bold) |
|---|---|---|
| `liquidity:oi` | `min_open_interest` | 1000, **500**, 300, 200, 100 |
| `liquidity:spread` | `max_spread_pct_of_mid` | 0.07, **0.10**, 0.15, 0.20 |
| `range_buffer:em` | `expected_move_multiple` | 1.30, 1.15, **1.00**, 0.85, 0.70 |
| `credit_floor` | `min_credit_pct_of_width` | 0.15, 0.12, **0.10**, 0.08, 0.06 |
| `delta_band:low` | `min_short_delta` | 0.15, **0.10**, 0.07, 0.05 |
| `delta_band:high` | `max_short_delta` | 0.25, 0.30, **0.35**, 0.40 |

Left of bold is tighter, right is looser (for `delta_band:high`, right admits nearer-the-money strikes).

## Learning protocol

Runs after each settle. At most one change in flight.

1. **Review the change in flight, if any.** Take candidates journaled since the change that pass under the new value and would have failed under the old (the newly admitted band). Once its effective n reaches `learn_min_review_n` (8): if its mean return is below zero, revert one step and lock the parameter for `learn_lock_days` (56); otherwise confirm the change. Either way the slot frees.
2. **Otherwise look for one proposal.** For each tunable parameter not locked:
   - *Loosen* if the refused-only set has effective n >= `learn_min_n` (20), its mean return is above zero, and it beats the admitted set's mean return with one-sided 95% confidence on cluster means (Welch).
   - *Tighten* if the marginal admitted set (admitted candidates that would fail at the next tighter step) has effective n >= `learn_min_n`, its mean return is below zero, and it trails the rest of the admitted set with the same confidence.
   Apply the single strongest proposal (largest t statistic).
3. Every apply, confirm and revert is written to the rules document with its evidence, and journaled as a cycle row with action `rules_change`.

With six clusters forming per week, the first automatic change cannot happen for roughly four weeks. That is the honest cost of not tuning on noise.

---

### Task 1: Per-account starting equity
**Files:** `agent/config.py`, `agent/risk.py`, `dashboard/app.py`, `tests/test_risk.py`
- `config.starting_equity_for(profile) -> float`: `{"comp": 100_000, "dev": 100_000, "igk": 50_000}`, default 100,000. `STARTING_EQUITY` stays as the comp value for the frozen dashboard.
- The `event_drawdown` gate uses `starting_equity_for(profile)`.
- Tests: igk at 42,400 equity trips the 15% event drawdown; comp at 90,000 does not.

### Task 2: Machine-readable failure codes and metrics
**Files:** `agent/models.py`, `agent/risk.py`, `tests/test_risk.py`
- `GateResult` gains `codes: list[str] = []`. Set by `liquidity` (`oi`, `spread`, `stale`, `future`, `notime`, `size`, `crossed`, `unquoted`, `missing`), `range_buffer` (`em`, `range`, `nodata`), `delta_band` (`low`, `high`), as `"<gate>:<code>"`. Gates with one cause use the gate name alone.
- `risk.failure_codes(gates) -> list[str]`.
- Tests: an OI-only failure yields exactly `["liquidity:oi"]`; a strike inside the range and inside the expected move yields both range codes.

### Task 3: The ledger rows
**Files:** `agent/candidates.py`, `tests/test_candidates.py`
- `enumerate_candidates` returns `(found, funnel, rows)`. It now walks **both** rights and short deltas 0.05-0.45, so the direction gate and the delta band have a measurable refused set; `found` still requires every shape gate.
- Row: `u, r, ks, kl, w, cr, nat, d, iv, oi` (min of both legs), `spr` (max leg bid-ask % of mid), `emr` (distance / expected move), `spot`, `fail` (codes), `chosen`, `traded` (both false at creation).
- Tests: a forbidden side appears in rows with `regime_direction` in `fail` and never in `found`; metrics match the fixture; survivors have empty `fail`.

### Task 4: Journal the ledger
**Files:** `agent/journal.py`, `agent/journal_firestore.py`, `agent/loop.py`, `tests/test_loop.py`
- Both backends: `record_shadow(profile, ts, expiry, rules_version, rows) -> id`, `mark_shadow(id, chosen=None, traded=None)` (each a `(u, r, ks, kl)` key), `unsettled_shadow(profile, on_or_before)`, `settle_shadow(id, rows)`, `settled_shadow(profile, since=None)`. One document per cycle; rows as a JSON string.
- The entry cycle records the ledger before asking the model, marks the model's pick after it answers, and marks it traded after a fill. Dry runs record nothing.
- Tests: a recorded cycle round-trips; mark sets the flags on the right row only; settled documents drop out of `unsettled_shadow`.

### Task 5: Settlement
**Files:** `agent/shadow.py` (new), `agent/alpaca_cli.py`, `tests/test_shadow.py` (new)
- `cli.option_bars(symbols, start, end, profile)` (daily, batches of 100, paged); `cli.daily_close(symbol, day, profile)`.
- `settle_row(row, close, bars) -> row + {held, v_exp, ret_hold, ret_mgd, mgd_rule}`. Held-to-expiry is exact. The managed path walks daily option bars from entry: a day whose worst case `short_high - long_low` reaches the stop books the stop; else a day whose best case `short_low - long_high` reaches the target books the target; a day that could be both books the stop. Spread values are clamped to `[0, width]` (paper option bars contain absurd prints). Missing bars leave `ret_mgd` null.
- `python -m agent.shadow settle` resolves every unsettled document whose expiry has closed, then runs the learning step.
- Tests: call and put, held and breached, capped at width; stop-before-target on a two-sided day; sparse bars give null; an outlier bar cannot produce a value beyond the width.

### Task 6: Statistics and reports
**Files:** `agent/shadow_stats.py` (new), `tests/test_shadow_stats.py` (new)
- `effective_n(rows)`, `cluster_means(rows, field)`, `welch(a, b) -> (diff, se, t)`.
- `gate_regret(rows, limits)`: per failure code -- refused-only n, effective n, hold rate, mean return, difference versus admitted, t.
- `calibration(rows)`: by short-delta bucket -- implied hold rate, realized hold rate, edge, effective n. This is the edge test.
- `model_vs_field(docs)`: the chosen candidate's return against the mean admitted return in the same cycle.
- `view_structure(spreads, closes)`: the 2x2 on real closed trades whose expiry has passed -- view held x made money -- with the lucky-win cell named as such.
- `python -m agent.shadow report` prints all four, each with its effective n and a plain "not enough data" below the minimum.
- Tests: 300 rows in one cluster have effective n 1; hand-built sets produce the expected regret sign; calibration buckets; the 2x2 classification.

### Task 7: Rules versions and the learning step
**Files:** `agent/rules.py` (new), `agent/learning.py` (new), `agent/journal*.py`, `agent/config.py`, `agent/loop.py`, tests
- Journal backends: `get_rules(profile) -> dict`, `put_rules(profile, doc)`. Document: `version`, `overrides`, `in_flight`, `locks`, `history`.
- `rules.limits_for(journal, profile, base) -> (RiskLimits, version)`: `dataclasses.replace` with overrides, each validated to be a value on its ladder; anything else is ignored and logged.
- `learning.step(journal, profile, base_limits, now) -> list[event]` implements the protocol above.
- The loop loads limits through `rules.limits_for` at the start of every cycle and sweep, stamps the rules version on the ledger and the cycle row.
- Limits: `learn_min_n`, `learn_min_review_n`, `learn_lock_days`, `learn_enabled`.
- Tests: no proposal below the minimum n; a clearly profitable refused-only set loosens exactly one step; two qualifying parameters change only the stronger; nothing is proposed while a change is in flight; a losing newly admitted band reverts and locks; an override off its ladder is ignored; non-tunable limits cannot be overridden.

### Task 8: Base rates in the snapshot, and the size ladder
**Files:** `agent/brain.py`, `agent/regime.py` or `agent/sizing.py`, `agent/loop.py`, tests
- The snapshot gains a MEASURED BASE RATES section from `calibration`, shown only for buckets above the minimum effective n. It is information; no gate reads it.
- Size ladder: tranche multiplier 0.5 until 10 attributable closed trades, 0.75 from 10 with positive mean return, 1.0 from 25 with positive mean return. A trade is attributable once its expiry has passed; view-failed-but-profited trades are excluded from both the count and the mean. A 5% drawdown from peak equity drops one tier, 10% drops to 0.5. Applied inside `room_for_trade` as a further cap, so it can only reduce size.
- Tests: lucky wins never promote; drawdown demotes; the multiplier never exceeds 1.

### Task 9: Repoint the cloud jobs and add the settle job
- Image built from the branch tip. `agent-cycle` and `agent-sweep`: `ALPACA_PROFILE=igk`, secrets `alpaca-igk-api-key` / `alpaca-igk-secret-key`. New job `agent-settle` (`python -m agent.shadow settle`), new scheduler `shadow-settle` at 16:30 ET on weekdays using the existing `scheduler-invoker` service account.
- Verify with a read-only dry run against igk, one manual sweep, one manual settle (a no-op on an empty ledger), then resume `entry-cycles` and `exit-sweeps`.

### Task 10: Documentation and memory
- README: a "How it measures itself" section, the learning protocol, the new account. Memory: the dev account is gone, igk is live, gate changes are automatic within ladders, the first change cannot come for about four weeks.
