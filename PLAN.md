# Alpaca AI Trading Agents Hackathon — Battle Plan

**Event:** lablab.ai × Alpaca · 28 Aug 2026 08:00 PT → 4 Sep 2026 08:00 PT
**Entrant:** solo · **Prize pool:** $6,000 · **Posture:** barbell · **Stack:** Python + alpaca-py + Alpaca CLI

---

## 1. The constraints (fail any → not judged)

| Requirement | Detail |
|---|---|
| Autonomous agent | Must use Alpaca **Trading API** |
| MCP or CLI | Must use Alpaca's **MCP server** or **CLI** |
| Options | **Every** strategy must incorporate options trading |
| Fresh account | Brand-new paper account, dedicated to this hackathon. Reused = ineligible |
| Balance | Starting balance set to **$100,000** |
| Account ID | Must be submitted — judges pull your real trade history and P&L |
| Write-up | One page: **AI logic, risk gates, Alpaca infrastructure** |

**Submission bundle:** title · short + long description · tech/category tags · cover image · video presentation · slide deck · public GitHub repo · demo platform + **live application URL** · Alpaca paper account ID · up to 5 social post links.

**Judged on:** P&L Performance · Technology Implementation · Creativity & Originality · Presentation & Execution · Social Engagement.

## 2. The trading window is smaller than it looks

5.25 trading days total:

| Day | Notes |
|---|---|
| Fri Aug 28 | Weekly expiry. Kickoff 11:00 ET — market already open 90 min |
| Mon Aug 31 | Full day |
| Tue Sep 1 | Full day |
| Wed Sep 2 | Full day |
| Thu Sep 3 | Full day — last full session |
| Fri Sep 4 | Weekly expiry. **Deadline 11:00 ET** — only ~90 min of trading |

Both bookends are weekly expiries, but **Sep 4 is the wrong expiry to target.** The deadline is 11:00 ET; options expire at 16:00 ET. A Sep-4 spread is still open — and 0DTE, so at peak gamma — at the exact moment judges freeze your P&L.

**Target Thu Sep 3 instead.** It settles at Thursday's close, so Friday morning you are flat, the P&L is final and unarguable, and the last 90 minutes go entirely to the write-up, slides, video and final post. You give up one day of theta for that. Cheap trade.

## 3. How this is actually won

P&L over 5 days of options is mostly variance. With a large field, someone posts a huge number on luck. You cannot out-skill that in a week and chasing it is how you finish at -80%.

P&L is **one of five** criteria. The other four are deterministic and most of the field will neglect them. Two judges are Alpaca's Chief Brokerage Officer and their Trading API team lead — they will open the account and read the fills.

**Strategy: be credibly green with a clean risk story, and be untouchable on the other four.**

Four edges:

1. **Social is a separate prize pool** ($500 × 2 + Algo Trader Plus per member) and most teams post nothing. Highest ROI per hour on the board, and it double-counts into main criterion #5.
2. **"Risk gates" is named verbatim** in the requirements. That is a tell about what the judges want to see.
3. **Real autonomy.** Most entrants will run the MCP server in Claude Desktop and screenshot the chat. A headless scheduled loop driving the CLI is a different tier on Technology Implementation.
4. **One artifact serves four criteria.** A live public dashboard of decisions, reasoning, P&L and risk-gate trips is simultaneously the required demo URL, the video content, and the social posts.

## 4. Architecture

```
alpaca-agent/
  agent/
    loop.py            # scheduler: pre-open, open, midday, pre-close ticks
    brain.py           # Claude via Anthropic API, tool-calling
    tools.py           # thin wrappers -> alpaca CLI subprocess (JSON in, JSON out)
    risk.py            # deterministic gates, run BEFORE any order leaves the process
    regime.py          # Markov Bull/Bear/Sideways detector -> sleeve allocation
    strategies/
      core_premium.py  # defined-risk credit spreads on Sep-4 weeklies
      satellite.py     # agent-directed directional sleeve
  data/journal.db      # every decision: inputs, reasoning, gates, order, fill, mark
  dashboard/app.py     # FastAPI + htmx, publicly hosted
  scripts/backtest.py
  WRITEUP.md
```

Three decisions worth defending in the write-up:

**The CLI is the execution boundary.** Every order exits through `alpaca api POST /v2/orders` or `alpaca order submit`, always with `--client-order-id` for idempotency and a `--dry-run` preview in the gate step. Satisfies the MCP-or-CLI requirement in a genuinely agentic way rather than as a chat transcript.

**Risk gates live in Python, not in the prompt.** The LLM proposes; a deterministic layer disposes. A model cannot be trusted to respect a position limit, so the limit lives in unbypassable code. This is the single strongest line in the submission.

**Everything is journaled.** Each cycle records market snapshot → agent reasoning → proposed order → every gate's verdict → submitted order → fill → mark. That journal *is* the dashboard, the video, the write-up and the social content. Write it once, use it five times.

## 5. Risk gates

| # | Gate | Threshold |
|---|---|---|
| 1 | Daily loss kill switch | -2% of starting equity → flatten, halt for the day |
| 2 | Event drawdown halt | -6% → halt for the event |
| 3 | Per-underlying notional cap | 15% of equity |
| 4 | Max concurrent positions | 8 |
| 5 | Defined risk only | Bounded max loss on every position. No naked short calls |
| 6 | Liquidity filter | OI ≥ 500 and bid-ask ≤ 10% of mid, else reject |
| 7 | Portfolio delta budget | Capped net directional exposure |
| 8 | No-trade windows | First and last 5 minutes of each session |
| 9 | Expiry discipline | Core expiries **≤ Thu Sep 3** so positions settle before the deadline. Never open anything expiring after Sep 4 |
| 10 | Idempotency | One `client_order_id` per intent, retry-safe |

Gate 6 does double duty: it keeps P&L credible. Alpaca paper can fill wide-spread illiquid contracts unrealistically well, and judges from Alpaca's own trading team will spot a P&L built on that instantly.

## 6. Strategy — the barbell

**Core, ~80% of risk budget**, run as a two-tranche ladder. Defined-risk short put spreads and iron condors on liquid index ETFs (SPY, QQQ, IWM), short strike around 0.15–0.20 delta, 5–10 wide. Agent selects underlying and strikes from an IV-rank and regime read; the risk layer sizes the position.

| Tranche | Enter | Expire | DTE | Risk budget |
|---|---|---|---|---|
| Core | Fri Aug 28 | Thu Sep 3 | 6 | ~60% |
| Second decay cycle | Mon Aug 31 / Tue Sep 1 | Thu Sep 3 | 2–3 | ~20% |
| Satellite | opportunistic | ≤ Sep 3 | — | ~20% |

**Satellite, ~20%.** Agent-selected directional debit spreads on catalysts sourced from `alpaca data news` and `alpaca data screener movers`. Defined risk, small size. This is the upside tail.

**Do not sell Aug 28 0DTE.** Tempting for a fast day-one win, but a bad day-one loss trips the -2% kill switch and burns 20% of the window on a gamma coin-flip.

SPY, QQQ and IWM all list Mon–Fri expirations, so Thu Sep 3 is available on all three — verify with `alpaca data option chain --underlying-symbol SPY` before committing. Late August is typically low-IV, so there may be less premium to collect than a backtest on a normal period suggests. That is a sizing input for the regime layer, not a reason to change the structure.

**Regime filter.** A Markov Bull/Bear/Sideways detector gates which sleeve may fire and how big: sideways → core only, sized up; trending → satellite gets a larger allocation. There is an off-the-shelf skill for this (`markov-hedge-fund-method:regime`) covering transition matrices, position sizing and no-lookahead walk-forward backtesting. It maps directly onto both Creativity & Originality and the risk-gate requirement.

If the satellite loses, the core still likely leaves you green. Green plus excellent execution on everything else beats a lottery ticket far more often than it loses to one.

## 7. Timeline

**Wed Aug 26 — today**
- Enroll on lablab.ai and join the lablab Discord
- `brew install alpacahq/tap/cli`, verify with `alpaca version` / `alpaca doctor`
- Create the **dev** paper account, generate keys, confirm the CLI trades
- **Run `alpaca account config get` immediately.** The dev account is the canary on the options-level question — if paper caps at Level 2, multi-leg spreads are off the table and the core sleeve has to be rebuilt from cash-secured puts and covered calls. Find out today, not Thursday night
- Repo skeleton + journal schema

**Thu Aug 27**
- Agent loop, risk layer, journal, CLI tool wrappers
- Backtest the core strategy
- Dashboard v1, deployed publicly
- Social post #1 — the build begins
- **Evening: create the fresh judged account.** Set balance to $100,000, generate keys, run `alpaca account config get` to confirm options level, then validate the full order path with `alpaca order submit ... --dry-run` — it previews without submitting, so the judged account stays pristine. Then stop. No trades

**Fri Aug 28**
- Pre-open: flip env to the judged account's keys. No setup work today — it was done Thursday
- **First real trade at kickoff (11:00 ET).** Rules don't prohibit trading earlier but don't bless it either; a first-fill timestamp at kickoff is unarguable. Costs 90 minutes
- Enter the core tranche: Sep 3 expiry
- Attend kickoff + Discord Q&A (12:00 ET) — ask whether pre-kickoff trading is permitted
- Social post #2 — agent is live

**Mon Aug 31 – Thu Sep 3**
- Agent runs supervised; daily review of gate trips and journal
- Social posts #3 and #4 with real numbers from the dashboard
- Thu evening: record the video

**Fri Sep 4 — deadline 11:00 ET**
- Final flatten decision
- Write-up, slides, cover image
- Submit with account ID and 5 post links
- Social post #5 with results

## 8. Verified on the dev account (Wed Aug 26, ~22:30 PT)

CLI v0.0.13 installed via Homebrew, profile `dev` authenticated against paper.

- ✅ **Options Level 3 confirmed** — `options_approved_level: 3`, `options_trading_level: 3`. Multi-leg credit spreads are viable. The design fork is closed; build the mleg core.
- ✅ **OPRA option data works** — `alpaca data option chain` returns live quotes, implied vol **and greeks** per contract. The risk layer reads delta straight off the API; no Black-Scholes needed.
- ✅ **Sep 3 expiry exists** on SPY (`SPY260903...`). Chain paginates at 100, so always filter with `--expiration-date` and `--strike-price-gte/lte`.
- ✅ Stock quotes work (IEX feed).
- ⚠️ **Dev account is $10,113 with four open equity positions** (AAPL, HD, NVDA, UNH) and `multiplier: 4`. Reset to $100,000 and flatten before using it to calibrate sizing — spread margin and buying-power math behave differently at $10k vs $100k.
- 📉 **IV is low**, ~17–18% on SPY Sep-3 puts near the money. Less premium to collect than a normal-period backtest implies.

### Live market snapshot at verification time

SPY ≈ 766. Sep 3 puts, the 0.15–0.20 delta band:

| Strike | Bid | Ask | Delta |
|---|---|---|---|
| 750 | 1.23 | 1.29 | −0.149 |
| 751 | 1.36 | 1.37 | −0.160 |
| 752 | 1.48 | 1.54 | −0.175 |
| 753 | 1.60 | 1.67 | −0.188 |
| 754 | 1.71 | 1.79 | −0.202 |

Bid-ask is 2–6 cents wide, so fills will be honest and the P&L credible.

**Candidate core trade:** sell 752P / buy 747P, 5-wide. Net credit ≈ 0.47, max risk 4.53, return on risk ≈ 10%, short strike 1.8% below spot, ~82% probability OTM.

### The honest expectancy caveat

A 0.175-delta spread at ~10% return-on-risk is close to fairly priced. The edge in premium selling is the volatility risk premium — implied above realized — which is real but modest, and IV is already low here. **The core sleeve is chosen for high probability of finishing green, not high expected value.** Its job is to make "positive P&L with a sub-1% drawdown" true.

That means the tournament upside has to come from the satellite. Sizing to a 4% max-loss core yields roughly +0.4–0.5% for the week at ~85% win rate — respectable and defensible in front of professional-trader judges, but not a winning P&L headline on its own. Tune the barbell weights with that in mind.

## 9. Account registry — READ BEFORE RUNNING ANYTHING

| Profile | Account | Equity | Role |
|---|---|---|---|
| `dev` **(active default)** | PA3KFKFLZ65X | $100,000 | Practice. All building and testing |
| `scratch` | PA3B6J79WQUN | ~$10,111 | Old account, idle |
| `comp` | **PA3X3X2CYW0J** | $100,000 | **JUDGED.** Created 2026-08-28T00:39Z. Level 3. Zero orders |

**`PA3X3X2CYW0J` is the account ID for the submission form.**

Verified pristine at creation: 0 orders, 0 positions, 0 activities, options level 3,
$100,000 cash and equity.

### Deployed (Google Cloud project `alpaca-ai-agent-2026`)

Public dashboard: **https://alpaca-ai-agent-2026.web.app**

| Component | Role |
|---|---|
| Cloud Run service `dashboard` | Public dashboard, scale-to-zero |
| Cloud Run job `agent-cycle` | Full LLM entry cycle |
| Cloud Run job `agent-sweep` | Exit management only, no model call |
| Cloud Scheduler `entry-cycles` | 09:45 / 11:45 / 13:45 / 15:45 ET, weekdays |
| Cloud Scheduler `exit-sweeps` | Every 10 min, 09:00-16:59 ET, weekdays |
| Firestore | The journal |
| Secret Manager | Alpaca + Anthropic keys, injected at runtime |

All three components run with `ALPACA_PROFILE=comp`. Roughly 21 model calls across
the event; everything else is deterministic.

Safety rules, non-negotiable:

1. `dev` is the active profile. Never change that. A forgotten `-p` flag must land on practice, not on the judged account.
2. **The `comp` profile does not exist until Thursday evening.** This is the strongest possible guard: you cannot accidentally trade an account whose credentials are not on the machine. Do not create it early.
3. The agent must read its target profile from config and **hard-refuse to run against `comp` before the kickoff timestamp**. Put this in `risk.py` as gate zero, not in a prompt.
4. Once `comp` exists, only ever validate against it with `--dry-run`, which previews without creating an order record. First real fill: Fri Aug 28, 11:00 ET.
5. Dress-rehearse the full competition-day sequence on `dev` Thursday, so Friday is muscle memory rather than improvisation.

## 10. Remaining open items
- **Pre-kickoff trading.** Rules do not prohibit trading the judged account before the 11:00 ET kickoff, but they do not bless it either. Default to going live at kickoff; ask in the Discord Q&A whether earlier is permitted. Costs 90 minutes if the answer is no.
- **Two accounts, strictly separated.** Dev account for all building, testing and backtesting. Judged account created Thu Aug 27 evening, verified by dry-run only, first fill at kickoff. Keep the keys in separate env files so a stray script cannot cross the line.
- **Alpaca MCP auth.** The MCP server is registered in this session but returns 401. Needs paper API keys before anything runs.
- **Dashboard hosting.** Submission requires a live application URL. Render / Railway / Fly free tier.
- **Multi-leg orders.** `alpaca order submit` may not expose mleg flags; use `alpaca api POST /v2/orders` with `order_class: "mleg"` and a `legs[]` array.
