"""The reasoning layer.

Claude reads the market snapshot and proposes a trade. It has no execution
authority: it returns a structured AgentDecision that risk.py must clear before
anything reaches the broker. Everything it says is journaled verbatim.
"""
from __future__ import annotations

import anthropic

from .config import TARGET_EXPIRY, UNIVERSE, RiskLimits
from .models import AgentDecision

SYSTEM = f"""\
You are the reasoning layer of an autonomous options trading agent competing in \
a one-week paper-trading contest on Alpaca. You analyse; you do not execute. \
Every proposal you make passes through a deterministic risk layer that will \
reject anything outside its limits, so propose what you actually believe is \
right and let the gates do their job.

MANDATE
- Universe: {', '.join(UNIVERSE)}. Nothing else.
- Expiry: {TARGET_EXPIRY} only. It settles before the submission deadline, so
  the final P&L is locked rather than left to a 0DTE pin.
- Instrument: defined-risk vertical credit spreads. For puts the short strike is
  ABOVE the long strike; for calls it is BELOW.
- TWO SLEEVES. Pick one per cycle and set `sleeve` accordingly.
  * core (CREDIT spread): sell premium at roughly 0.25-0.30 delta on the short
    strike; short strike NEARER the money than the long. Deliberately
    aggressive: more credit, and a short strike that will be tested. Do not
    drift back to 0.15. This is the workhorse and wins slowly and often.
  * satellite (DEBIT spread): buy a defined-risk directional spread WITH the
    trend; long strike NEARER the money than the short. It loses the debit
    more often than it wins, and pays multiples when a trend actually runs.
    It exists to give the book convexity the core sleeve cannot produce.
    Keep it small and only take it on real conviction.
- `net_price` is always a POSITIVE number: the credit you require for core, or
  the debit you will pay for satellite.
  Satellite sleeve: directional, smaller, only on a clear catalyst.

YOUR REGIME CALL IS A BINDING CONTROL, NOT A COMMENT
Whatever regime you report is applied deterministically before your trade is
placed. It decides how much risk the tranche may carry and which direction of
spread is allowed at all:
  sideways -> core 12.00% (P or C credit). NO satellite: there is no trend to
              buy, so paying a debit for convexity is burning premium.
  bull     -> core 10.20% (P credit only; short calls fight the tape).
              satellite 3.40% (C debit -- buy the uptrend).
  bear     -> core 4.20% (C credit only; short puts into a downtrend is how
              premium sellers blow up). satellite 1.40% (P debit).
The sleeves lean opposite ways ON PURPOSE: core sells premium against the move,
satellite buys exposure with it. Proposing a satellite trade in a sideways tape
is rejected outright.
So call the regime honestly. Saying "sideways" to unlock size you have not
earned is the one thing that will actually lose this account money. If you
propose a direction the regime forbids, the trade is rejected outright; if you
propose a size above the regime budget, it is silently cut to fit.

HOW TO THINK
- Read the regime first. In a sideways or mildly bullish tape, put credit
  spreads are the bread and butter. In a sharp downtrend, either stand down or
  move to call spreads above resistance.
- Prefer strikes with tight bid-ask and real open interest. A theoretical edge
  on an illiquid contract is not an edge.
- Standing down is a valid and often correct decision. Propose null rather than
  forcing a marginal trade. You will be judged on the quality of the decisions,
  not their number.
- Your rationale is shown to judges. Make it specific: name the levels, the
  delta, the regime read, and what would make you wrong.

WHAT YOU MUST NOT DO
- Do not state dollar risk, margin, or position sizing. Propose a quantity; the
  risk layer derives the money from the contract specs and will resize or reject.
- Do not propose naked or undefined-risk positions.
- Do not invent quotes. Use only the chain data given to you.
- A scheduled macro print inside the holding period is gap risk a stop cannot
  protect against. Size down into one, or stand down. Say so explicitly if a
  print is what changed your decision.
- Headlines are context, not a signal. Do not build a thesis on a headline.
- Ground every claim about trend, range or support in the daily bars provided.
  Do not assert "near the highs", "grinding higher", or reference a shelf or a
  prior week unless the bars above actually show it. Your regime call sets the
  risk budget, so an ungrounded read puts real money at risk.
"""


class Brain:
    def __init__(self, model: str = "claude-opus-5", client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    def decide(self, snapshot: str, limits: RiskLimits) -> AgentDecision:
        """Return a structured decision, or a stand-down if the model declines."""
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": snapshot}],
            output_format=AgentDecision,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return AgentDecision(
                regime="sideways",
                reasoning="Model declined to answer this cycle; standing down.",
                proposal=None,
            )
        return response.parsed_output


def build_snapshot(
    *,
    now,
    equity: float,
    day_start_equity: float,
    positions: list[dict],
    quotes: dict[str, dict],
    chains: dict[str, dict],
    limits: RiskLimits,
    bars: dict[str, list] | None = None,
    news: list[dict] | None = None,
) -> str:
    """Render the market state as text for the model.

    Deliberately compact: only what a decision needs. Chain rows are pre-filtered
    to the tradeable delta band so the model is not asked to scan hundreds of
    strikes.
    """
    lines = [
        f"TIME: {now:%Y-%m-%d %H:%M %Z}",
        f"EQUITY: ${equity:,.2f}   DAY START: ${day_start_equity:,.2f}   "
        f"DAY P&L: ${equity - day_start_equity:+,.2f}",
        f"TARGET EXPIRY: {TARGET_EXPIRY}",
        "",
        "OPEN POSITIONS:",
    ]
    if positions:
        for p in positions:
            lines.append(
                f"  {p.get('symbol')}  qty={p.get('qty')}  "
                f"mv=${float(p.get('market_value') or 0):,.2f}  "
                f"upl=${float(p.get('unrealized_pl') or 0):+,.2f}"
            )
    else:
        lines.append("  (none)")

    lines += ["", "UNDERLYING QUOTES:"]
    for sym, q in quotes.items():
        bid, ask = q.get("bp"), q.get("ap")
        mid = (bid + ask) / 2 if bid and ask else None
        lines.append(f"  {sym}: bid {bid}  ask {ask}" + (f"  mid {mid:.2f}" if mid else ""))

    lines += ["", "RECENT DAILY BARS (most recent last) -- this is your only price history:"]
    for sym, series in (bars or {}).items():
        if not series:
            lines.append(f"  {sym}: (unavailable)")
            continue
        closes = [b.get("c") for b in series if b.get("c") is not None]
        lo, hi = (min(closes), max(closes)) if closes else (None, None)
        last = closes[-1] if closes else None
        pos = f"{(last - lo) / (hi - lo):.0%} of range" if lo is not None and hi > lo else "n/a"
        lines.append(f"  {sym}: {len(series)}-session close range {lo:.2f}-{hi:.2f}, "
                     f"last {last:.2f} ({pos})")
        lines.append("    " + "  ".join(
            f"{b['t'][5:10]} o{b['o']:.2f} h{b['h']:.2f} l{b['l']:.2f} c{b['c']:.2f}"
            for b in series[-8:]))

    lines += ["", f"OPTION CHAINS ({TARGET_EXPIRY}), tradeable delta band:"]
    for sym, chain in chains.items():
        lines.append(f"  --- {sym} ---")
        rows = []
        for osym, snap in chain.items():
            greeks = snap.get("greeks") or {}
            delta = greeks.get("delta")
            if delta is None or not (0.05 <= abs(delta) <= 0.35):
                continue
            q = snap.get("latestQuote") or {}
            rows.append((
                osym, q.get("bp"), q.get("ap"), round(delta, 3),
                snap.get("impliedVolatility"), snap.get("openInterest"),
            ))
        rows.sort(key=lambda r: r[0])
        if not rows:
            lines.append("    (no contracts in the tradeable band)")
        for osym, bid, ask, delta, iv, oi in rows[:40]:
            iv_s = f"{iv:.3f}" if isinstance(iv, (int, float)) else "-"
            lines.append(
                f"    {osym}  bid {bid}  ask {ask}  delta {delta}  iv {iv_s}  oi {oi}"
            )

    from .macro import macro_headlines, upcoming
    events = upcoming(within_days=3, today=now.date())
    lines += ["", "SCHEDULED RELEASES (next 3 days) -- short premium is short gamma:"]
    if events:
        for e in events:
            when = "TODAY" if e["days_away"] == 0 else f"in {e['days_away']}d"
            lines.append(f"  {e['date']} ({when})  {e['event']}")
            lines.append(f"       {e['impact']}")
    else:
        lines.append("  (none in the next 3 days)")
    lines.append("  NOTE: only structurally-dated releases are listed (weekly claims,"
                 " first-Friday payrolls). Other prints -- PCE, CPI, ISM, FOMC -- are not"
                 " scheduled here; infer them from the headlines below.")

    macro = macro_headlines(news or [])
    lines += ["", "MACRO HEADLINES (what has actually printed, and Fed tone):"]
    if macro:
        for n in macro:
            lines.append(f"  [{(n.get('created_at') or '')[:16]}] {n.get('headline','')[:180]}")
    else:
        lines.append("  (none)")

    lines += ["", "OTHER HEADLINES:"]
    if news:
        for n in news[:10]:
            lines.append(f"  [{(n.get('created_at') or '')[:16]}] {n.get('headline','')}")
    else:
        lines.append("  (none available)")

    lines += [
        "",
        "RISK LIMITS IN FORCE (the gate layer enforces these regardless of what you propose):",
        f"  max loss per tranche: {limits.max_tranche_risk_pct:.0%} of equity",
        f"  daily loss halt: {limits.max_daily_loss_pct:.0%}",
        f"  event drawdown halt: {limits.max_event_drawdown_pct:.0%}",
        f"  max concurrent positions: {limits.max_concurrent_positions}",
        f"  min open interest: {limits.min_open_interest}",
        "",
        "Decide: propose one defined-risk vertical credit spread, or stand down.",
    ]
    return "\n".join(lines)
