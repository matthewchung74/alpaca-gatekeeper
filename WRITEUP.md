# Gatekeeper — one-page write-up

**Autonomous options trading agent.** Claude Opus 5 proposes; deterministic Python disposes.

Repo: https://github.com/matthewchung74/alpaca-gatekeeper · Dashboard: https://alpaca-ai-agent-2026.web.app · Account: `PA3X3X2CYW0J`

---

## AI logic

Every cycle, one `messages.parse()` call to **Claude Opus 5** returns a validated Pydantic object: a market regime, its full reasoning, and either a defined-risk vertical spread or a decision to stand down.

What it sees: live quotes, **15 sessions of daily bars**, the option chains for SPY/QQQ/IWM filtered to the 0.05–0.35 delta band with IV and greeks, open positions and P&L, structurally-dated macro releases, and news headlines split into macro and general.

What it may return is deliberately narrow. It proposes an underlying, a right, two strikes, a quantity, a net price and a sleeve. **It never emits an option symbol and never states a dollar risk figure.** Python builds the OCC symbol from the structured fields, and derives max loss from the contract specs. A model that writes `SPY260903P00767000` freehand can drop a digit and reference a real but wrong contract; structured fields make that unrepresentable.

**The regime call is a binding control, not a comment.** Whatever regime the model reports mechanically sets its own risk budget and forbids trade directions:

| Regime | Core budget | Core sells | Satellite budget | Satellite buys |
|---|---|---|---|---|
| sideways | 12.00% | puts or calls | — | nothing |
| bull | 10.20% | puts only | 3.40% | calls |
| bear | 4.20% | calls only | 1.40% | puts |

A regime can only ever *reduce* risk — the configured budget is a ceiling no market read can raise, and an unrecognised regime falls back to the most defensive policy. The bear rule forbids selling puts into a downtrend, which is precisely how short-premium accounts die, and the agent's own analysis is what triggers that lockout.

**The strategy is a barbell.** The core sleeve sells credit spreads *against* the direction of the move; the satellite buys defined-risk debit spreads *with* it. Their economics invert — on a 5-wide at 1.50, core risks $350 to make $150, satellite risks $150 to make $350 — so the book gets convexity the core sleeve structurally cannot produce. A range-bound tape permits no satellite at all: with no trend to buy, paying a debit for convexity is burning premium.

Everything targets a **Thursday expiry**, never Friday. The deadline is Friday 11:00 ET and options expire at 16:00 ET, so a Friday spread would still be open — and 0DTE, at peak gamma — at the exact moment the P&L is judged. One day of theta buys an unarguable final number.

## Risk gates

Fourteen gates, each a pure function of the proposal plus observed state. A proposal must clear **all** of them, and no model output can reach them.

`account_guard` (refuse the judged account before kickoff or after the deadline) · `event_halt` · `daily_loss` −4% · `event_drawdown` −15% · `universe` (SPY/QQQ/IWM only) · `expiry` · `defined_risk` (strike order must match the sleeve's structure) · `price_sanity` (net price > 0 and < width) · `regime_direction` · `tranche_risk` (regime-adjusted, per sleeve) · `concentration` 35% per underlying · `position_count` 8 · `trading_window` · `liquidity` (both legs quoted, spread ≤ 10% of mid).

Sizing is not negotiated. The model proposes a quantity; the risk layer computes max loss from the strikes and **silently resizes to fit the budget** or rejects. Downsizing beats blocking — a good trade at smaller size beats a wasted cycle.

Exits are equally deterministic, in priority order: **assignment risk** (expiry day, spot within $0.50 of the short strike), **expiry flatten** (30 min before the close), **stop loss** (3× credit, capped below the width so it stays reachable), **profit target** (50% of credit). Marks use the unfavourable side of both quotes, so a rule never fires on an optimistic price. Losing quote data returns *hold* — except on expiry day, where flatten and assignment rules still fire, so an outage cannot strand the book into assignment.

## Alpaca infrastructure

The **Alpaca CLI is the execution boundary.** Every order leaves through it — structured JSON, exit codes, automatic retry on 429/5xx, `--client-order-id` for idempotency. Multi-leg orders go through `alpaca api POST /v2/orders` with `order_class: mleg`, since `order submit` does not expose the mleg flags. Market data, chains with greeks, daily bars, news and the trading calendar all come from the same CLI.

It runs unattended on Google Cloud, with no laptop in the loop: **Cloud Scheduler** fires two **Cloud Run Jobs** — `agent-cycle` (a full LLM cycle, 4×/day) and `agent-sweep` (deterministic exits only, every 10 minutes) — writing to **Firestore**, with credentials in **Secret Manager** and a public dashboard on **Cloud Run behind Firebase Hosting**. Entry cycles cost about 21 model calls across the whole competition; exit sweeps run 12× more often and never touch the model.

Four bugs surfaced only against the live API, each of which would have cost money:

1. **`limit_price` on an mleg order is a signed net price** — negative for a credit, positive for a debit. Sending `+0.80` on a credit spread means *"I'll pay up to 0.80"*, which any credit satisfies, so the limit never binds. Verified: demanding `+2.50` on a spread worth 1.23 filled at 1.22; `-2.50` correctly rested unfilled.
2. Fixing that made two dead paths live — orders can now **rest unfilled or fill partially**. A partial fill left the journal claiming 12 contracts against 7 held, and closing 12 there can *open* a 5-lot opposite position. Closes are now sized from the broker, never the journal.
3. **The fill price is not the limit price.** Profit target, stop and realized P&L all key off the entry price, so the agent polls to a settled state and reconciles size and price.
4. **Explicit `position_intent` is a safety feature.** Omitting it gets the order accepted with Alpaca inferring `buy_to_open` — opening an opposite position instead of closing. Explicit `*_to_close` fails loudly with a 422 instead.

## Results, and what they say

Nine positions closed, five winners, four losers. **−$1,843 realized, −1.87% on the event.**
The strategy finished a loser, and the record says why.

| Regime at open | Trades | Net |
|---|---|---|
| `sideways` | 4 | **+1,455** |
| `bear` | 5 | **−3,298** |

Selling defined-risk credit spreads risks roughly three dollars to make one, so it needs a
high win rate to clear. On the trades actually taken — stops working, losses cut before
maximum — break-even sat near **65%**. The agent ran **56%**. That gap is the whole result.

Three of the four losses were short call spreads opened under a `bear` label, which permits
calls only. They lost together, on the same move, because they were the same bet in three
tickers. No gate prevents that: `concentration` caps exposure *per underlying*, and nothing
aggregates direction across the book. `config.RiskLimits` did declare a `max_portfolio_delta`
of 0.30 — with units in the comment — and it was read nowhere in the codebase. It was deleted
mid-event as dead config, which was right; the concept it named is the one control that would
have caught this, and building it is the first thing we would do next.

The fourth loss is the counterexample worth keeping. The SPY 767/762 **put** spread lost
−$1,800 from a `sideways` label, which a bear reading would have forbidden outright. The
regime layer is not simply wrong — it was net positive on `sideways` and negative on `bear`,
across nine trades, which is too small a sample to conclude much beyond *this is the thing to
measure next*.

Two losses exited on `assignment_risk` rather than `stop_loss`, at 3.77× and 1.94× the credit
against a 3.0× stop. That is not the stop failing to bind. Sweeps pause inside the no-trade
window before the close and resume after the open, so nothing ran between 15:50 on 09-02 and
09:40 on 09-03; the positions gapped overnight and the first check that could act came after
the damage. On expiry day `assignment_risk` also outranks `stop_loss`, and closes at the same
market price either way. The limitation is real, was documented before it cost anything, and
is the clearest argument for an out-of-hours risk check rather than a faster one.


## Evidence

Every cycle journals the snapshot, the model's verbatim reasoning, the proposal, all fifteen gate verdicts, the order and the fill — public on the dashboard. From the second live trade, unedited:

> *"I am already short the 767 put (long delta, currently tested with SPY at 769), the marginal trade I want is on the call side, not more short puts. Selling the 774 call turns the book into a defined-risk condor around 767–774 and takes delta off the table rather than doubling down."*

Nobody told it to build a condor.

**101 tests.** Honest limitations: the strategy itself is conventional, each cycle is amnesiac, and it is one model call rather than an agent loop. The originality is in the control architecture — the model reasons, and code it cannot reach decides what is allowed to exist.
