Gatekeeper implementation review — September 18, 2026

Reviewed all 15 files changed by `07cf6c3`, `bfe5436`, and `60f396c`, against `af99b1d`. The larger sizing is an accepted design choice: the 12% per-trade and 24% gross book ceilings remain, subject to the existing regime multipliers. This review evaluates implementation correctness at that sizing.

Most operational remedies have been implemented, but several failure paths remain. The strategy experiments from the previous review have not been built. The existing suite passes all 169 tests; isolated mocked scenarios reproduce the findings below. No production code, sizing, orders, or deployment settings were changed.

1. **P1 — Fractional-second quote timestamps bypass the freshness check.** In `agent/risk.py:211`, the fractional-second parser collects digits from both the fraction and the UTC offset. The reconstructed timestamp loses its timezone, and the fallback interprets it as Eastern time. At 14:00 ET, `2026-09-18T17:00:00.123456789Z` is one hour old, but `_quote_age_minutes` returns approximately −180 minutes. Both legs bearing that timestamp pass the liquidity gate. The same error affects microsecond timestamps. Preserve the UTC offset when parsing, and reject timestamps implausibly in the future. Test a recent quote and a 60-minute-old quote with fractional seconds; the existing stale-quote test only uses a timestamp without a fraction.

2. **P1 — A daily-loss liquidation is abandoned after a recovery.** `agent/loop.py:620` loads the persisted halt, but initializes `flatten=None` and only forces closure while the current equity remains below the daily threshold. Reproduction through `_cycle_body`: equity falls to $95,900 from $100,000; the forced close fills 4 of 10 contracts; the next sweep sees $96,100. The halt remains true, but the remaining 6 contracts receive no further close order because their individual exit rules say hold. Persist liquidation-in-progress, or force flattening while a daily halt remains active and exposure remains. Also route a breach observed by the later pre-submission equity refresh through the same halt/flatten path; currently that observation only blocks the proposal through the entry gate.

3. **P1 — Reconciled spreads sharing a short leg still cannot close correctly.** `reconcile` aggregates expected leg quantities (`agent/loop.py:263`) and can declare the book reconciled, but the exit path at `:450` still uses the entire broker short-leg quantity for each individual spread. Reproduction: a 10-lot SPY 770/775 call spread plus a 5-lot 770/780 spread passes reconciliation. Management submits a 15-lot close for each, despite holding only 10 and 5 of their respective protective options. Those orders cannot close as specified. Allocate signed quantities to journal lots, validate both legs, and update available quantities after each fill. Alternatively, reject overlapping contracts at entry until the accounting supports them. The existing `held_qty` still uses `abs`, so reconciliation has not itself corrected the old sign/quantity problem.

4. **P1 — Orphan cancellation is treated as settled when it is not.** `agent/loop.py:182` discards `cancel_order`'s boolean result, appends the order to the canceled list, and never polls it to a terminal state. A mocked refusal returned `False` but still produced “cancelled orphaned order(s)” in the journal. The caller at `:589` ignores the cleanup result and proceeds. An order-list failure also becomes an empty list, allowing entries without knowing what orders remain working. A still-working entry can fill after positions were sampled and after another entry passed the gates. Poll cancellation to completion, reconcile any fills, and block new entries while an earlier order is unresolved. Alpaca explicitly distinguishes `pending_cancel` from `canceled`: [order lifecycle](https://docs.alpaca.markets/us/docs/orders-at-alpaca). The pre-submission intent record is useful, but no recovery path currently consumes it to recover terminal order fills.

5. **P2 — The final refresh does not refresh all inputs to the decision.** `agent/loop.py:727` updates option quotes, selected underlying price, and equity, but `cycle_regime`, `tape`, Greeks, IV, open-position marks, and broker positions still come from the earlier observation. `book_regime` is then calculated from that older tape. A move during model inference can therefore be evaluated with a current price and a stale permitted direction or budget; an existing losing spread can be checked against its earlier mark. Re-read relevant positions and marks, recompute the tape, and evaluate gates against a consistent final observation. If the account refresh fails after quotes succeeded, the current code can still submit using the old equity; quote freshness does not establish account-state freshness.

6. **P2 — Reconciliation records unknown realized P&L as zero.** When both legs disappear from the broker, `agent/loop.py:255` retires the spread with `realized_pnl=sp.realized_so_far`. If there were no recorded partial closes, that is `0.0`, despite the log saying P&L is unknown. Reproduction confirms a closed row with `exit_debit=None` and `realized_pnl=0.0`. This hides gains/losses from manual closure, broker liquidation, or a process crash after an exit fill. Recover execution/activity records before finalizing P&L, or mark the result explicitly incomplete and exclude it from completed-trade performance statistics. The dashboard currently counts these rows among closed trades.

The lock needs an additional defensive change, with a deployment qualification. Firestore releases it using a separate read and unconditional delete (`agent/journal_firestore.py:129`). If ownership changes between those operations, the old owner can delete the new owner's lock; a mocked interleaving reproduces this. Make release transactional. There is also no lease renewal or ownership check before writes. Both deployed Cloud Run jobs currently have 600-second timeouts, matching the 600-second lease; that bounds the normal cloud run and makes a simple “jobs routinely outlive the lease” claim unsupported. Longer local invocations or future timeout changes would need renewal or a stricter execution bound.

Coverage against the earlier review:

| Earlier item | Implementation status |
| --- | --- |
| Larger sizing | Preserved intentionally |
| Prior-close daily baseline and profile isolation | Implemented; normal broker `last_equity` path works |
| Persisted daily halt and forced exits | Implemented, but unfinished liquidation is not persisted; finding 2 |
| Zero bid on protective long option | Fixed when the ask establishes that a quote exists |
| Normal partial-close accounting | Fixed; quantity shrinks and realized P&L accumulates across settled closes |
| Broker/journal reconciliation | Clean orphan verticals are adopted; shared contracts and unknown P&L remain problematic |
| Assigned stock or unmatched option legs | Detected and blocks entries; still requires manual repair and is not automatically flattened |
| Session hours and early-close expiry management | Implemented on successful calendar reads; calendar failures still assume ordinary hours |
| Dividend assignment prevention | Implemented for near/ITM short core calls on the last session before ex-date |
| Separate directional exposure buckets | Implemented; call and put maximum losses no longer cancel |
| Consistent portfolio regime | Uses the most defensive ticker regime; final tape still needs refreshing |
| Quote validity and OI | Crossed markets, absent size, and missing OI reject; actual OI is fetched; timestamp bug remains |
| Account lock and pre-submission intent | Implemented, with release/recovery qualifications above |
| Published payroll/CPI/PCE dates | Implemented; upcoming CPI and PCE dates checked against the published schedules |
| Deterministic macro-event entry restrictions | Not implemented; scheduled events remain model context |
| Volatility-richness filter | Not implemented |
| Deterministic candidate selection versus LLM baseline | Not implemented |
| Historical options replay and exit/DTE/filter experiments | Not implemented |

The scheduled CPI and PCE dates agree with the current [BLS calendar](https://www.bls.gov/schedule/2026/) and [BEA calendar](https://www.bea.gov/news/schedule). Calendar tables improve information quality; they do not themselves establish a strategy edge or enforce an event risk rule.

The payoff and expiry settings are unchanged: 50% credit capture, a 3× credit close-cost stop, and nearest common expiry at least seven days out. Those were proposed experiments, not proven improvements. A successful implementation review cannot substitute for evaluating them on later data.

Verification: `python -m pytest tests/ -q` reports 169 passed. `git diff af99b1d --check` passes. Additional isolated checks exercised a fractional-second stale quote, daily-halt recovery after a partial fill, shared-short-leg quantities, refused orphan cancellation, missing-position P&L, and the Firestore release interleaving. The two Cloud Run timeout values were read without modifying either job. Deployment image parity with the reviewed commit was not verified.
