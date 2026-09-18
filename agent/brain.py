"""The reasoning layer.

Claude reads the market snapshot and proposes a trade. It has no execution
authority: it returns a structured AgentDecision that risk.py must clear before
anything reaches the broker. Everything it says is journaled verbatim.
"""
from __future__ import annotations

import anthropic

from .config import ET, TARGET_EXPIRY, UNIVERSE, RiskLimits
from .models import AgentDecision, parse_strike
from .regime import TapeRead

PER_WING_CAP = 30   # per underlying, per side

SYSTEM = f"""\
You are the reasoning layer of an autonomous options trading agent competing in \
a one-week paper-trading contest on Alpaca. You analyse; you do not execute. \
Every proposal you make passes through a deterministic risk layer that will \
reject anything outside its limits, so propose what you actually believe is \
right and let the gates do their job.

MANDATE
- Universe: {', '.join(UNIVERSE)}. Nothing else.
- Expiry: exactly the TARGET EXPIRY given in the snapshot, and nothing else.
  It is chosen to sit at least a week out, so a strike one expected move away
  clears the recent range. A proposal for any other date is rejected before
  it reaches the broker.
- Instrument: defined-risk vertical credit spreads. For puts the short strike is
  ABOVE the long strike; for calls it is BELOW.
- ONE SLEEVE: `sleeve` is always "core", a CREDIT vertical spread. The
  satellite (debit) sleeve is switched off in every regime; a satellite
  proposal is rejected outright, so never propose one and never use it as a
  fallback when the core has no legal strike -- stand down instead.
  * core: sell premium with the short strike at least one expected move from
    spot, and in a sideways tape also outside the recent range. The TAPE
    READ section prints, per underlying, the range and the first strike on
    each side that clears the range_buffer gate, judged with that strike's
    own IV. Use that boundary; do not recompute the move from ATM vol,
    because put skew makes the gate's number larger than yours. The short
    leg usually lands near 0.10-0.20 delta. The delta_band gate rejects a
    short leg outside 0.10-0.35. Keep the width tight (2-5 points): credit
    as a fraction of width falls as the width grows, and the credit_floor
    gate rejects anything under 10% of width.
- `net_price` is always a POSITIVE number: the credit you require.

THE TAPE READ IS COMPUTED FOR YOU
The snapshot carries, per underlying, a regime (bull / bear / sideways) read
from the daily bars, the position of spot inside the recent range, the
expected move to expiry, and the sides the core sleeve may sell. You do not
set any of it. The budget and the permitted sides follow from it:
  sideways -> core 12.00%. Bottom quarter of the range: puts only. Top
              quarter: calls only. Middle: either.
  bull     -> core 10.20% (P credit only).
  bear     -> core 4.20% (C credit only).
A side the read forbids is rejected outright; a size above the budget is
silently cut to fit. If no side is permitted in the name you like, stand down
or pick another name.

HOW TO THINK
- Read the tape section first. Sell the side it permits, at a strike beyond
  the range and the expected move. A range that has just moved to one edge
  is a mean-reversion risk, not a trend to lean on.
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


def _boundary_line(sym: str, read: TapeRead, quote: dict, chain: dict, now,
                   target_expiry: str, limits: RiskLimits) -> str:
    """Where the range_buffer gate starts passing, per side, from the chain itself.

    Each strike is judged with its own IV, exactly as the gate will judge it,
    so put skew is already in the number the model reads.
    """
    from datetime import date
    from .risk import range_clearance
    bid, ask = quote.get("bp"), quote.get("ap")
    if not (bid and ask):
        return "range_buffer boundary: no quote"
    spot = (float(bid) + float(ask)) / 2
    dte = max((date.fromisoformat(target_expiry) - now.date()).days, 1)
    best: dict[str, tuple[float, float | None]] = {}
    for osym, snap in chain.items():
        iv = snap.get("impliedVolatility")
        if iv is None:
            continue
        right = osym[len(sym) + 6]
        strike = parse_strike(osym)
        if not range_clearance(right, strike, spot, float(iv), dte, read, limits).ok:
            continue
        cur = best.get(right)
        # puts: the highest clearing strike; calls: the lowest
        if cur is None or (strike > cur[0] if right == "P" else strike < cur[0]):
            best[right] = (strike, (snap.get("greeks") or {}).get("delta"))
    parts = []
    for right, word, op in (("P", "puts", "<="), ("C", "calls", ">=")):
        if right in best:
            k, d = best[right]
            dd = f", delta {abs(float(d)):.2f}" if d is not None else ""
            parts.append(f"{word} clear at {op} {k:g}{dd}")
        else:
            parts.append(f"no {word[:-1]} strike in the chain clears")
    return f"range_buffer boundary ({dte} DTE, each strike at its own IV): " + "; ".join(parts)


class Brain:
    def __init__(self, model: str = "claude-opus-5", client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    def preflight(self) -> None:
        """One cheap call so a dead key or an empty balance fails loudly and early.

        On 2026-09-07 the entry cycle fetched every quote, chain and headline,
        then died on 'credit balance is too low'. Check the API before paying
        for any of that. Raises the SDK's own exception on failure.
        """
        self.client.messages.create(
            model=self.model, max_tokens=1,
            thinking={"type": "disabled"}, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "ping"}],
        )

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
    target_expiry: str = TARGET_EXPIRY,
    tape: dict[str, TapeRead] | None = None,
    sides: dict[str, tuple] | None = None,
    recent_spreads: list[dict] | None = None,
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
        f"TARGET EXPIRY: {target_expiry}",
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

    lines += ["", "TAPE READ (computed from the bars; binding):"]
    if tape:
        for sym, read in tape.items():
            allowed = "/".join((sides or {}).get(sym, ())) or "none"
            if read.range_position is None:
                lines.append(f"  {sym}: {read.regime} -- {read.detail}; core may sell: {allowed}")
                continue
            lines.append(
                f"  {sym}: {read.regime}, range {read.lookback_low:.2f}-{read.lookback_high:.2f}, "
                f"position {read.range_position:.0%}, {read.trend_pct:+.2%} vs "
                f"{limits.range_lookback} sessions ago; core may sell: {allowed}")
            lines.append("    " + _boundary_line(sym, read, quotes.get(sym) or {},
                                                 chains.get(sym) or {}, now, target_expiry, limits))
    else:
        lines.append("  (unavailable)")

    from .risk import cadence_state
    opened_today, cooling = cadence_state(recent_spreads or [], now, limits)
    lines += ["", "CADENCE (binding):",
              f"  entries today: {opened_today} of max {limits.max_entries_per_day}"
              + (" -- NO further entries today; stand down" if opened_today >= limits.max_entries_per_day else "")]
    if cooling:
        lines.append("  cooling down (do not propose these; the cadence gate rejects them):")
        for u, r, until in sorted(cooling, key=lambda x: x[2]):
            lines.append(f"    {u} {r} until {until.astimezone(ET):%m-%d %H:%M ET}")
    else:
        lines.append("  no cooldowns in force")

    lines += ["", f"OPTION CHAINS ({target_expiry}), tradeable delta band:"]
    for sym, chain in chains.items():
        lines.append(f"  --- {sym} ---")
        puts, calls = [], []
        for osym, snap in chain.items():
            greeks = snap.get("greeks") or {}
            delta = greeks.get("delta")
            if delta is None or not (0.05 <= abs(delta) <= 0.35):
                continue
            q = snap.get("latestQuote") or {}
            row = (osym, parse_strike(osym), q.get("bp"), q.get("ap"), round(delta, 3),
                   snap.get("impliedVolatility"), snap.get("openInterest"))
            (puts if osym[len(sym) + 6] == "P" else calls).append(row)

        # Cap each wing independently. Sorting by symbol and taking the first N
        # would put every call ahead of every put alphabetically, so a busy
        # chain would silently delete the entire put wing -- and the band widens
        # exactly when volatility rises. Trim the far-OTM tail instead, which is
        # the least tradeable end, and keep both sides represented.
        dropped = 0
        for side in (puts, calls):
            side.sort(key=lambda r: -abs(r[4]))       # nearest the money first
            if len(side) > PER_WING_CAP:
                dropped += len(side) - PER_WING_CAP
                del side[PER_WING_CAP:]
            side.sort(key=lambda r: r[1])             # display by strike

        if not puts and not calls:
            lines.append("    (no contracts in the tradeable band)")
        for osym, strike, bid, ask, delta, iv, oi in puts + calls:
            iv_s = f"{iv:.3f}" if isinstance(iv, (int, float)) else "-"
            lines.append(
                f"    {osym}  bid {bid}  ask {ask}  delta {delta}  iv {iv_s}  oi {oi}"
            )
        if dropped:
            lines.append(f"    ({dropped} far-OTM contracts omitted; both wings shown)")

    from .macro import macro_headlines, upcoming
    from datetime import date as _date
    # Look through the whole holding period, not a fixed three days: with a
    # week-out expiry a three-day window hid the 2026-09-16 FOMC decision.
    try:
        horizon = max(3, (_date.fromisoformat(target_expiry) - now.date()).days)
    except ValueError:
        horizon = 3
    events = upcoming(within_days=horizon, today=now.date())
    lines += ["", f"SCHEDULED RELEASES (next {horizon} days, through expiry) -- short premium is short gamma:"]
    if events:
        for e in events:
            when = "TODAY" if e["days_away"] == 0 else f"in {e['days_away']}d"
            lines.append(f"  {e['date']} ({when})  {e['event']}")
            lines.append(f"       {e['impact']}")
    else:
        lines.append("  (none in the next 3 days)")
    lines.append("  NOTE: listed from published schedules: weekly claims, payrolls, CPI,"
                 " PCE, FOMC decision days. Other prints -- ISM, retail sales, GDP -- are"
                 " not scheduled here; infer them from the headlines below.")

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
