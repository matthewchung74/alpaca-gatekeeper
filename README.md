# Gatekeeper

**An autonomous options trading agent where the model proposes and unbypassable code disposes.**

Built for the [lablab.ai × Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon), 28 Aug – 4 Sep 2026.

Live dashboard: **https://alpaca-ai-agent-2026.web.app**

---

## The thesis

Every LLM trading agent faces the same question: how much do you trust the model?

The usual answer is to put the risk rules in the prompt. That fails quietly. A model asked to respect a position limit will respect it most of the time, and the times it doesn't are exactly the times that cost money.

Gatekeeper takes the opposite position. **Claude decides what to trade. Deterministic Python decides whether that trade is allowed to exist.** The two are separate processes with a hard boundary between them:

```
observe ──▶ reason (Claude Opus 5) ──▶ 23 gates (pure Python) ──▶ Alpaca CLI ──▶ journal
             proposes                    disposes                  executes
```

The model never states a dollar risk figure. It proposes strikes and a quantity; the risk layer derives max loss from the contract specs, resizes the position to fit the budget, and rejects anything outside its limits. Nothing the model outputs can raise a limit, skip a gate, or size a position up.

Concretely, from a live run:

```
proposal: SPY 2026-09-03 P 767/762 x12 @ 0.80 cr (core)
[PASS] account_guard: comp cleared to trade at 2026-08-28 11:45 EDT
[PASS] regime_direction: sideways permits P/C for the core sleeve
[PASS] tranche_risk: max loss 5,040 vs core budget 12,000 (12.00% of equity)
[PASS] liquidity: both legs quoted with acceptable spreads
fill reconciled: limit 0.80 -> actual 0.75 ($-60)
SUBMITTED
```

Every cycle is journaled this way — the market snapshot, the model's full reasoning, the proposal, every gate's verdict, the order, and the fill.

---

## The strategy

### A barbell, with the two sleeves leaning opposite ways

| Sleeve | Structure | Behaviour |
|---|---|---|
| **core** | Credit spread — short strike nearer the money | Sells premium. Wins slowly, often. Loses the width when wrong |
| **satellite** | Debit spread — long strike nearer the money | Buys direction. Loses the debit often. Pays multiples when a trend runs |

The core sleeve sells premium **against** the direction of the move. The satellite buys defined-risk exposure **with** it. That is what makes this a barbell rather than the same bet twice, and it is why a range-bound tape permits no satellite at all: with no trend to buy, paying a debit for convexity is burning premium.

**The satellite is switched off as of 2026-09-17.** Its first four live trades were all put debit spreads bought at the bottom of a range because the core had no legal strike that day, and all four stopped out at the next open, −$1,500 together. The code stays; the regime policy grants it no side in any regime.

The economics genuinely invert. On a 5-wide spread at 1.50 net price:

```
core (credit)      max loss $350    max profit $150
satellite (debit)  max loss $150    max profit $350
```

### Why Thursday expiry, not Friday

The competition deadline is Friday 11:00 ET. Options expire at 16:00 ET.

A Friday-expiry spread is still open — and 0DTE, so at peak gamma — at the exact moment judges freeze the P&L. A **Thursday** expiry settles at Thursday's close, so Friday morning the book is flat, the number is final, and the last session goes to the write-up instead of watching a pin.

One day of theta is a cheap price for an unarguable result.

### Universe

SPY, QQQ, IWM only. Penny-to-nickel-wide markets in the strikes we trade.

This is a risk control and a credibility control at once. Alpaca's paper environment will fill wide-spread illiquid contracts unrealistically well, and a P&L built on that would not survive inspection by judges who trade for a living.

---

## Regime is computed, not asked

The hackathon version let the model label the tape and bound the risk budget and the permitted direction to that label. On 2026-09-01 it labelled a 1.2% dip to the bottom of a 15-session band "bear", the bear rule permitted calls only, and the book sold four call spreads at the range low. They lost together on the rally two days later.

Now `regime.classify` reads the last ten completed sessions: a trend if spot has moved more than twice the mean daily range since the session ten back, otherwise sideways, plus where spot sits inside the ten-session high-low.

| Read | Core budget | Core may sell |
|---|---|---|
| sideways, middle of range | 4.00% | puts or calls |
| sideways, bottom quarter | 4.00% | **puts** only |
| sideways, top quarter | 4.00% | **calls** only |
| bull | 3.40% | **puts** only |
| bear | 1.40% | **calls** only |

The model sees the read in its snapshot and cannot change it.

Budgets are per trade and deliberately small. The hackathon ran 12% tranches, where two positions filled the 24% book and nothing else could be entered until one closed. A tranche is now a third of that inside the same book cap, directional cap and daily limit: the same total risk in more, smaller positions, each proposal cut to whatever room the tranche, the book and its side have left.

---

## The 23 gates

Every gate is a pure function of the proposal plus observed account and market state. A proposal must clear **all** of them.

| # | Gate | Rule |
|---|---|---|
| 0 | `account_guard` | Refuse to trade the judged account before kickoff or after the deadline |
| 1 | `event_halt` | Halted for the event |
| 2 | `daily_loss` | −4% of the day's starting equity |
| 3 | `event_drawdown` | −15% from the starting balance |
| 4 | `universe` | SPY / QQQ / IWM only |
| 5 | `expiry` | Target expiry only — must settle before the deadline |
| 6 | `defined_risk` | Strike order must match the sleeve's structure; loss always bounded |
| 7 | `price_sanity` | Net price must be > 0 and < the width |
| 8 | `regime_direction` | The sleeve may only lean the way the regime permits |
| 9 | `tranche_risk` | Max loss within the sleeve's regime-adjusted budget |
| 10 | `concentration` | 35% of equity per underlying |
| 11 | `position_count` | 8 open spreads |
| 12 | `trading_window` | Not in the first or last 5 minutes of a session |
| 13 | `liquidity` | Both legs quoted, spread ≤ 10% of mid, OI ≥ 500 |
| 14 | `delta_band` | Short-leg \|delta\| within 0.10–0.35 |
| 15 | `directional_risk` | Max loss signed by direction across the book ≤ 20% of equity |
| 16 | `range_buffer` | Short strike ≥ 1 expected move from spot; in a sideways tape also outside the 10-session high-low |
| 17 | `credit_floor` | Credit ≥ 10% of width |
| 18 | `book_risk` | Open max loss + proposal ≤ 24% of equity × regime multiplier |
| 19 | `same_direction` | ≤ 5 open core spreads on one right across SPY/QQQ/IWM |
| 20 | `losing_side` | No new spread on a right where an open spread marks ≥ 1.5× its credit |
| 21 | `cadence` | 4 entries per day; 24h cooldown per underlying and right after a close |
| 22 | `leg_overlap` | A new spread may not reuse a contract an open spread already holds |

Gates 16 to 21 come from the post-mortem. Six of nine hackathon short strikes finished in the money; held to expiry the book would have lost about $11,400 against the $1,843 it did lose. The strikes sat inside the prior week's range at one to four days to expiry, the book stacked three same-direction spreads in three tickers that move together, and the model proposed a trade in every cycle it had budget for. Each of those is now a gate.

Gate 14 exists because the prompt had asked for a 0.25–0.30 short delta since day one and nothing enforced it — a 0.304-delta call cleared every gate on 28 Aug because none of them looked. The enforced band is deliberately wider than the instruction: strikes are a point apart and delta moves 0.03–0.05 per strike, so a literal 0.25–0.30 gate leaves one legal strike per wing and, on the live 3 Sep chain, none at all for QQQ puts. It is also the one gate that passes when its input is missing, because it governs strategy conformance rather than solvency — max loss is bounded by `defined_risk` and `tranche_risk` whatever the delta.

Gate 0 is the one that cannot be reached by any prompt. It is a hard exception, not a warning, because the eligibility of the whole submission depends on the judged account having no pre-kickoff fills.

---

## Exit management

Exits are as deterministic as entries — the model has no say in when a position is closed. Four rules, in priority order:

| Rule | Core (credit) | Satellite (debit) |
|---|---|---|
| `assignment_risk` | Expiry day, spot within $0.50 of the short strike | same |
| `dividend_assignment_risk` | Last session before an ex-dividend date, short **call** within $0.50 of spot | n/a |
| `daily_loss_flatten` | Day's loss from the prior close reaches 4%: close everything, halt until tomorrow | same |
| `expiry_flatten` | Expiry day, 30 min before the close | same |
| `stop_loss` | Cost to close ≥ 3× the credit (capped below the width) | Value ≤ 50% of the debit paid |
| `profit_target` | Cost to close ≤ 50% of the credit | Value ≥ 60% of max profit |

Three details that took a live position to get right:

**Marks use the unfavourable side of both quotes** — ask on the short leg, bid on the long — so an exit rule never fires on an optimistic price.

**The stop is capped below the width.** At higher deltas the credit is fat enough that credit × multiple can exceed the spread's own max loss, which would make the stop unreachable and silently turn every loser into a full max-loss ride.

**Degraded data holds, except when it can't.** Losing quotes returns `hold` — but `expiry_flatten` and `assignment_risk` still fire without a mark, so a data outage cannot strand the book into assignment.

---

## The model chooses from what can pass

Before the model is asked anything, `agent/candidates.py` walks every vertical up to five points wide on the permitted sides and runs the same per-trade gate functions the final verdict uses: liquidity including open interest, delta band, range buffer, credit floor, leg overlap. The survivors go into the snapshot with their mid and natural credit, short delta and open interest, and the model is told to choose from that list or stand down. The count of pairs each gate rejected is journaled with every cycle, so whether a threshold is too tight is a number in the log.

The expiry is the nearest **Friday** weekly at least seven days out. "Nearest expiry" kept landing on Monday and Wednesday weeklies listed days earlier: on 2026-09-18 the 09-28 Monday weekly had 1 surviving vertical out of 115 across all three names, and the 10-02 Friday had 26 of 183.

## What the model actually sees

One `messages.parse()` call per cycle, returning a validated Pydantic object. No tool loop, no multi-turn.

```
TIME / EQUITY / DAY P&L / TARGET EXPIRY
OPEN POSITIONS
UNDERLYING QUOTES
RECENT DAILY BARS          15 sessions, OHLC + range position
SCHEDULED RELEASES         only structurally-dated ones
MACRO HEADLINES            claims, Fed speakers, inflation, rates
OTHER HEADLINES
OPTION CHAINS              filtered to the tradeable delta band, with IV and OI
RISK LIMITS IN FORCE
```

The bars are load-bearing. Without them the agent produced confident claims — *"near the upper end of its range"*, *"grinding higher"* — that nothing in its input supported, and that ungrounded read was setting the risk budget. With them it cites specific closes and is explicitly forbidden from asserting a trend the bars don't show.

The scheduled-release list contains **only dates that are structural or published in advance by the agency itself**: weekly jobless claims on Thursdays, and the BLS, BEA and Fed schedules for payrolls, CPI, PCE and FOMC decisions. Payrolls used to be "the first Friday", which was wrong for five of 2026's twelve reports. Everything else arrives through the news feed as it actually prints. An invented date is worse than no date, because the agent treats it as fact and sizes on it, so a year with no table tells the model the calendar is out of date rather than guessing.

---

## Infrastructure

Runs unattended on Google Cloud. No laptop in the loop.

```
Cloud Scheduler ──▶ Cloud Run Job ──▶ Alpaca CLI  +  Anthropic API
                          │
                     Firestore  ──▶  Cloud Run service ──▶ Firebase Hosting
                     (journal)         (dashboard)
```

| Component | Role |
|---|---|
| `agent-cycle` (Cloud Run Job) | Full entry cycle. 09:45 / 11:45 / 13:45 / 15:45 ET |
| `agent-sweep` (Cloud Run Job) | Exit management only, no model call. Every 10 min |
| `dashboard` (Cloud Run service) | Public decision log, scale-to-zero |
| Firestore | The journal |
| Secret Manager | Alpaca and Anthropic credentials, injected at runtime |

Entry cycles cost one model call each — about 21 across the whole competition. Exit sweeps run 12× more often and never touch the model, because exit rules are deterministic.

**The Alpaca CLI is the execution boundary.** Every order leaves through it: structured JSON, exit codes, automatic retry on 429/5xx, and `--client-order-id` for idempotency. Multi-leg orders go through `alpaca api POST /v2/orders` with `order_class: mleg`, since `order submit` does not expose the mleg flags.

---

## Things the live API taught us

Four bugs that only a real fill could surface, each of which would have cost money:

**`limit_price` on a multi-leg order is a signed net price.** Negative = a credit you require, positive = a debit you will pay. Sending `+0.80` on a credit spread means *"I'll pay up to 0.80"*, which any credit satisfies — so the limit never binds and the order fills at whatever the market gives. Verified: demanding `+2.50` on a spread worth 1.23 filled at 1.22; demanding `-2.50` correctly rested unfilled.

**Fixing that made two dead code paths live.** Once the limit binds, orders can rest unfilled or fill partially. A partial fill left the journal claiming 12 contracts while the broker held 7 — and closing 12 against a 7-lot position can open a 5-lot opposite position. Closes are now sized from the broker's position, never the journal's.

**The fill price is not the limit price.** Every downstream number — profit target, stop level, realized P&L — keys off the entry price, so recording what you asked for instead of what you got is silently wrong forever. The agent now polls the order to a settled state and reconciles both size and price.

**Explicit `position_intent` is a safety feature.** Omitting it gets the order *accepted*, with Alpaca inferring `buy_to_open` — which would open an opposite position instead of closing yours. With explicit `*_to_close`, an order against a position you don't hold fails loudly with a 422. Fail-loud beats fail-dangerous.

---

## Layout

```
agent/
  config.py            limits, event timing, the account guard
  models.py            TradeProposal / OpenSpread; derived risk lives here
  regime.py            tape read from the bars; regime -> budget and permitted sides
  risk.py              the 23 gates
  manage.py            exit rules, structure-aware
  brain.py             Claude Opus 5, structured output
  alpaca_cli.py        the execution boundary
  journal.py           SQLite (local) / Firestore (cloud), one interface
  loop.py              one cycle: observe -> manage -> reason -> gate -> execute
dashboard/             FastAPI + a single self-contained page
tests/                 97 tests
```

## Running it

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
brew install alpacahq/tap/cli
alpaca profile login --api-key --name dev      # a paper account
export ANTHROPIC_API_KEY=sk-ant-...

./.venv/bin/python -m pytest tests/ -q
./.venv/bin/python -m agent.loop --profile dev --dry-run      # gate it, don't send it
./.venv/bin/python -m agent.loop --profile dev --manage-only  # exits only
./.venv/bin/uvicorn dashboard.app:app --port 8000
```

`--dry-run` runs the full pipeline including every gate and stops before the broker.

---

## Honest limitations

- **The strategy itself is conventional.** Selling delta-25 put credit spreads is the most common options income trade there is. The originality here is in the control architecture, not the trade.
- **No memory.** Each cycle is amnesiac — five days of decisions accumulate in the journal and the agent reads none of them.
- **One call, not an agent loop.** No tool use, no self-critique, no exploration.
- **The drawdown halt is checked at cycle time**, so an overnight gap can overshoot it.
- **Paper fills are not real fills**, and paper charges fees but not slippage the way a real book would.

## Disclaimer

Educational project for a hackathon. Paper trading only. Nothing here is investment advice.
