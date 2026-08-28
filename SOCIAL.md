# Build-in-public drafts

Tag on **X**: `@lablabai` `@AlpacaHQ` · on **LinkedIn**: `lablab.ai` `Alpaca`
Submit up to 5 links. Judged on content quality *and* engagement.

Repo https://github.com/matthewchung74/alpaca-gatekeeper · Dashboard https://alpaca-ai-agent-2026.web.app

The event page asks for "your process, your reasoning, and **your setbacks**." The bugs are the best
material here — they are specific, verifiable, and useful to anyone else building on the same API.

---

## Post 1 — the thesis (post today)

**Screenshot:** the gate log refusing to trade before kickoff.

### X

> Building an options trading agent for the @AlpacaHQ × @lablabai hackathon.
>
> One rule: the LLM never holds the risk limits.
>
> Claude proposes a spread. 14 deterministic Python gates decide whether it's allowed to exist. The model can't reach them, can't argue with them, and never states a dollar risk figure — it proposes strikes, and code derives the max loss.
>
> Here it is refusing to trade its own account 75 minutes before the competition opened:
>
> `[BLOCK] account_guard: Refusing to trade 'comp' before kickoff`
>
> Risk rules in a prompt fail quietly. Risk rules in code fail loudly.

### LinkedIn

> **Why I put the risk limits where the model can't reach them.**
>
> I'm building an autonomous options trading agent for the Alpaca × lablab.ai hackathon this week, and the first design decision was the one that mattered.
>
> The usual approach is to put risk rules in the system prompt. "Never risk more than X." That works most of the time — and the times it doesn't are exactly the times that cost money.
>
> So the architecture splits in two. Claude Opus 5 reads the market and proposes a defined-risk spread. Fourteen deterministic Python gates then decide whether that trade is allowed to exist. The model never states a dollar figure; it proposes strikes and a quantity, and the risk layer derives max loss from the contract specs and silently resizes to fit the budget.
>
> This screenshot is the agent refusing to trade its own competition account 75 minutes before the event opened. Nothing in the prompt did that — a hard exception in code did.
>
> Repo and live dashboard in the comments.
>
> #AlpacaMarkets #lablab #AITrading #Claude

---

## Post 2 — the API bug (post today or Saturday)

**Screenshot:** the three-line experiment table.

### X

> Setback worth sharing from the @AlpacaHQ hackathon.
>
> My credit spreads kept filling *below* my limit price. Asked 0.80, got 0.75.
>
> Turns out `limit_price` on a multi-leg order is a SIGNED net price:
> • negative = credit you require
> • positive = debit you'll pay
>
> Sending +0.80 on a credit spread means "I'll pay up to 0.80" — which any credit satisfies. The limit never binds.
>
> Proved it three ways:
> demand +2.50 on a spread worth 1.23 → filled at 1.22
> demand −2.50 → correctly rested unfilled
> demand −1.21 → filled at 1.23 ✅
>
> The docs' own iron condor example uses a positive limit. Only the live API settled it.
>
> @lablabai

### LinkedIn

> **A bug that only a real fill could find.**
>
> Day one of the Alpaca × lablab.ai hackathon, and my agent's credit spreads kept filling below the limit I set. Asked for 0.80 credit, received 0.75. A limit order shouldn't do that.
>
> `limit_price` on an Alpaca multi-leg order is a **signed net price**. Negative means a credit you require; positive means a debit you'll pay. I was sending +0.80, which reads as "I'll pay up to 0.80 as a debit" — a condition any credit trivially satisfies. The limit was never binding, and every entry was filling at whatever the market gave.
>
> I tested it three ways on a paper account:
> • demand +2.50 on a spread worth 1.23 → filled at 1.22 (limit ignored)
> • demand −2.50 → rested unfilled (limit binds)
> • demand −1.21 when natural was 1.23 → filled at 1.23 (honoured, and better)
>
> The interesting part: the docs page for multi-leg orders shows an iron condor — a credit structure — with a *positive* limit price, which pointed the wrong way. The OpenAPI spec has it right, and so did the live API.
>
> Then fixing it created a second bug. Once the limit actually binds, orders can rest unfilled or fill partially — two code paths that had never executed. A partial fill left my journal claiming 12 contracts against 7 actually held, and closing 12 there would have *opened* a 5-lot opposite position.
>
> Closes are now sized from the broker's position, never from my own records. Trust the exchange over your own bookkeeping.
>
> #AlpacaMarkets #lablab #AITrading #OptionsTrading

---

## Post 3 — the agent surprises you (post Monday/Tuesday)

**Screenshot:** the decision log entry with the condor reasoning.

### X

> My agent built an iron condor and I didn't tell it to.
>
> 11:45 — sold a SPY 767/762 put spread
> 13:45 — sold a SPY 774/778 call spread
>
> Its own reasoning for the second trade:
>
> "I am already short the 767 put... the marginal trade I want is on the call side, not more short puts. Selling the 774 call turns the book into a defined-risk condor around 767–774 and takes delta off the table rather than doubling down."
>
> It sees its open positions each cycle and reasoned about portfolio delta. No condor logic exists in my code.
>
> @AlpacaHQ @lablabai

### LinkedIn

> **The agent did something I didn't program.**
>
> At 11:45 it sold a SPY 767/762 put credit spread. At 13:45, two hours later, it sold a 774/778 call credit spread — building an iron condor across two separate decisions.
>
> There is no condor logic anywhere in my code. Each cycle is independent and the agent has no memory of previous reasoning. What it does see is the current position list, and this is what it wrote:
>
> "I am already short the 767 put (long delta, currently tested with SPY at 769), the marginal trade I want is on the call side, not more short puts. Selling the 774 call turns the book into a defined-risk condor around 767–774 and takes delta off the table rather than doubling down."
>
> It reasoned about portfolio delta, not just the trade in front of it.
>
> Worth being precise about what's happening: it's one model call per cycle with no tool use and no memory. It reconstructed the portfolio logic from a position list. That's a smaller claim than "emergent strategy" — but it's the kind of context-sensitivity a hard-coded delta-target selector would never produce.
>
> Every decision, with full reasoning and all 14 gate verdicts, is public on the dashboard.
>
> #AlpacaMarkets #lablab #AITrading #Claude

---

## Post 4 — the grounding problem (post Tuesday/Wednesday)

**Screenshot:** before/after reasoning, side by side.

### X

> Caught my agent confabulating and it was setting the risk budget.
>
> It kept writing things like "SPY is near the upper end of its recent range" and "clearly grinding higher."
>
> Its input contained no price history at all. Just the current bid/ask. It was pattern-matching from training data.
>
> That ungrounded read was choosing between a 12% and a 4% risk budget.
>
> Fix: hand it 15 sessions of daily bars, and forbid claims the bars don't support.
>
> Same market, new answer — regime flipped from "bull" to "sideways", with every number now checkable.
>
> @AlpacaHQ @lablabai

### LinkedIn

> **My agent was hallucinating market structure, and it was sizing positions on it.**
>
> The agent classifies the tape as bull, bear or sideways, and that call is a binding control — it sets the risk budget and forbids trade directions. So the regime read decides whether $12,000 or $4,200 is at risk.
>
> Reading its decision log, I noticed it kept making claims like "all sitting near the upper end of their recent ranges" and "an index that has clearly been grinding higher."
>
> Its input contained no price history whatsoever. Current bid/ask only. Every one of those statements was reconstructed from training priors.
>
> The fix was to hand it 15 sessions of daily bars and explicitly forbid asserting a trend the bars don't show. Same market, same moment, different answer:
>
> Before: "clearly grinding higher" → regime **bull**
> After: "the bars show chop, not trend. SPY closes over the last eight sessions: 767.45, 769.06, 762.60... a wobble inside a 762.60-777.88 range, last print at 56% of that range" → regime **sideways**
>
> Every number now checks out against data it was actually given. And it wasn't cosmetic: the regime flip changed the budget and the position size.
>
> The general lesson — if a model's qualitative judgement drives a real decision, make sure it can actually see what it claims to be looking at. Confident prose is not evidence.
>
> #AITrading #lablab #AlpacaMarkets #LLM

---

## Post 5 — results (post Thursday evening / Friday)

**Screenshot:** the equity curve and closed-position table.
**Fill in the real numbers before posting. Do not pre-write the outcome.**

### X

> Final week for my @AlpacaHQ × @lablabai hackathon agent.
>
> [RESULT: P&L, win rate, trades taken]
>
> What the architecture did:
> • every position defined-risk, max loss known before entry
> • [N] trades blocked by gates that never reached the broker
> • [gate that fired most] was the one that earned its keep
>
> Every decision and all 14 gate verdicts are public:
> https://alpaca-ai-agent-2026.web.app
>
> Code: https://github.com/matthewchung74/alpaca-gatekeeper

### LinkedIn

> **What a week of letting an LLM trade — under supervision it couldn't override — actually looked like.**
>
> [RESULT]
>
> The design held up in the way that mattered: [worst drawdown] against a [X]% cap, every position defined-risk, and [N] proposals stopped by gates before they reached the broker.
>
> The four bugs that cost me the most time were all things only a live API could surface — signed limit prices, partial fills, fill-vs-limit reconciliation, and position intent. Unit tests and dry runs found none of them.
>
> What I'd do differently: [honest reflection].
>
> Full decision log, code, and write-up in the comments.
>
> #AlpacaMarkets #lablab #AITrading #Claude

---

## Notes

- Posts 2 and 4 are the strongest. Both are genuine setbacks with reproducible evidence, which is
  what the event explicitly asked for and what other builders will actually find useful.
- Attach a screenshot to every one. The gate log and the decision log are unusually legible.
- Put links in a comment rather than the post body on LinkedIn — link posts get throttled.
- Post 5 has deliberate blanks. Fill them with real numbers; do not pre-write the result.
