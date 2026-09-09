# Post-Mortem Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Gatekeeper from selling strikes inside the recent range at 1 to 4 DTE, from stacking same-direction spreads, and from trading every cycle, then redeploy the agent jobs and verify the Claude API on every wake-up.

**Architecture:** Regime becomes a deterministic read of the daily bars, computed per underlying in `agent/regime.py`; the model no longer returns one. Six new gates in `agent/risk.py` (range buffer, credit floor, book risk, same direction, losing side, cadence) consume the computed tape read, the exit sweep's marks, and recent journal rows. `agent/loop.py` wires those inputs, adds an Anthropic preflight before any market data is fetched, and the laptop monitor runs the same preflight every 15 minutes. The two Cloud Run jobs get a new image; the judged dashboard service is not redeployed.

**Tech Stack:** Python 3.12, pydantic 2, anthropic 1.2.0, pytest, Alpaca CLI, Google Cloud Run Jobs + Cloud Scheduler, launchd.

**Spec:** This document. Design summary follows; the tasks implement it.

## Design (approved 2026-09-08)

1. **Regime is computed, per underlying.** `regime.classify(bars, spot, today)` returns a `TapeRead`: `regime` (bull / bear / sideways), `range_position` (0 at the 10-session low, 1 at the high), `lookback_high`, `lookback_low`, `trend_pct`, `avg_range_pct`. Trend if spot vs the close 10 sessions ago exceeds 2x the mean daily high-low range. The existing `POLICY` table keeps budgets and the bull/bear direction rules. In sideways, range position decides sides: bottom quarter forbids short calls, top quarter forbids short puts.
2. **Expiry:** nearest expiry listed for the whole universe at least 7 days out (`MIN_DAYS_TO_EXPIRY = 7`). SPY, QQQ and IWM list Mon/Wed/Fri expiries, so this lands at 7 to 9 days; no ceiling is needed.
3. **New gates**, all journaled:
   - `range_buffer`: short strike outside the 10-session high-low and at least 1.0x the expected move from spot (`spot * IV * sqrt(DTE/365)`, IV from the short leg's chain snapshot). Missing bars, spot or IV block.
   - `credit_floor`: core credit at least 10% of width (revised from 20% after the first live dry run: credit/width is bounded by the short delta, which range_buffer puts near 0.15). Satellite passes.
   - `book_risk`: open max loss plus the proposal within 24% of equity times the regime multiplier.
   - `same_direction`: at most 2 open core spreads on the same right across the universe.
   - `losing_side`: no new core spread on a right where an open core spread marks at or above 1.5x its credit.
   - `cadence`: at most 1 entry per ET calendar day; no entry in the same underlying and right within 24h of a close.
4. **Consequential edits:** `min_short_delta` 0.20 to 0.10. Prompt loses the aggressive-delta and regime-as-control text; snapshot gains a computed TAPE READ section with permitted sides. `AgentDecision` loses `regime`. `manage_open_spreads` returns marks. `risk.evaluate` gains `tape`, `open_marks`, `recent_spreads`.
5. **Claude API check on wake-up:** `Brain.preflight()` makes a one-token call before `observe()`; failure journals `action="error"` with `anthropic preflight: ...` and the cycle exits. The laptop monitor's `check_anthropic_api()` makes the same call every run, reading the key from Secret Manager, and reports CRIT on failure.
6. **Deploy:** build a new image tag, update `agent-cycle` and `agent-sweep` to it, execute one sweep as a smoke test, resume `entry-cycles`. Do not touch the `dashboard` service.
7. **Out of scope:** satellite sleeve, exit rules, execution, dashboard service.

## Global Constraints

- Every limit lives in `RiskLimits` in `agent/config.py`; gates read limits, never literals.
- Gates are pure functions of the proposal plus observed state. No gate calls the broker or the model.
- Keep the existing SQLite and Firestore journal interfaces unchanged.
- Run the full suite with `./.venv/bin/python -m pytest tests/ -q` before every commit; 101 tests pass at the start.
- Work on branch `post-mortem-gates`. Never push to `main`. Never redeploy the `dashboard` Cloud Run service.
- Commit messages end with the session's Co-Authored-By and Claude-Session trailers.

---

### Task 1: Regime detector

**Files:**
- Modify: `agent/regime.py` (append after `describe`)
- Modify: `agent/config.py` (RiskLimits: add `range_lookback`, `trend_range_multiple`, `range_edge_quantile`)
- Test: `tests/test_tape.py` (create)

**Interfaces:**
- Produces: `regime.TapeRead` dataclass with fields `regime: str`, `range_position: float | None`, `lookback_high: float | None`, `lookback_low: float | None`, `trend_pct: float | None`, `avg_range_pct: float | None`, `detail: str`.
- Produces: `regime.classify(bars: list[dict], spot: float, today: str, *, lookback: int = 10, trend_multiple: float = 2.0) -> TapeRead`. Bars are Alpaca daily bars with keys `t`, `o`, `h`, `l`, `c`; `today` is `YYYY-MM-DD`; only bars dated before `today` count.
- Produces: `regime.core_sides(tape: TapeRead, limits: RiskLimits) -> tuple[str, ...]` returning the permitted rights for the core sleeve, e.g. `("P",)`, `("C",)`, `("P", "C")`, `()`.

- [ ] **Step 1: Add the three limits**

In `agent/config.py`, inside `RiskLimits`, after `max_directional_risk_pct`:

```python
    # --- Tape read (computed regime) ---
    # The 2026-09-01 loss: the model called "bear" after a 1.2% dip to the
    # bottom of a 15-session band, and bear -> calls-only sold four call
    # spreads at the range low. Regime is now computed from the bars, and in
    # a range the position inside it decides which side may be sold.
    range_lookback: int = 10            # completed sessions
    trend_range_multiple: float = 2.0   # trend if |move| > this x mean daily range
    range_edge_quantile: float = 0.25   # sideways: no short calls below this range
                                        # position, no short puts above 1 - this
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_tape.py`:

```python
"""The regime is computed from the bars, not asked of the model."""
from agent import regime
from agent.config import RiskLimits

LIMITS = RiskLimits()


def bars(closes, rng=0.006, start_day=1):
    """Daily bars with a fixed high-low range as a fraction of the close."""
    out = []
    for i, c in enumerate(closes):
        day = start_day + i
        out.append({"t": f"2026-08-{day:02d}T04:00:00Z", "o": c, "h": c * (1 + rng / 2),
                    "l": c * (1 - rng / 2), "c": c})
    return out


def test_needs_ten_completed_sessions():
    read = regime.classify(bars([760.0] * 6), spot=760.0, today="2026-08-20")
    assert read.regime == "sideways"
    assert read.range_position is None and read.lookback_high is None
    assert "6 completed" in read.detail


def test_today_partial_bar_is_ignored():
    """A daily bar dated today is the session in progress; it is not history."""
    b = bars([760.0] * 10 + [900.0], start_day=1)      # the 900 bar is dated 08-11
    read = regime.classify(b, spot=760.0, today="2026-08-11")
    assert read.lookback_high < 900


def test_the_09_01_tape_is_sideways_at_the_bottom_of_the_range():
    """SPY closes 08-18..08-31, spot 762 on 09-01: a 0.7% dip inside a
    13-point band. This must NOT read as bear, and it sits at the range low."""
    closes = [767.45, 769.06, 762.6, 765.72, 763.47, 765.91, 766.08, 771.1, 769.35, 767.05]
    b = bars(closes, rng=0.008, start_day=18)
    b[2]["l"] = 762.04; b[7]["h"] = 772.36; b[8]["h"] = 775.3   # the real extremes
    read = regime.classify(b, spot=762.0, today="2026-09-01")
    assert read.regime == "sideways"
    assert read.range_position < 0.05
    assert regime.core_sides(read, LIMITS) == ("P",)     # no short calls at the low


def test_top_of_range_forbids_short_puts():
    closes = [760.0] * 10
    read = regime.classify(bars(closes), spot=760.0 * 1.003, today="2026-08-20")
    assert read.regime == "sideways"
    assert read.range_position > 0.75
    assert regime.core_sides(read, LIMITS) == ("C",)


def test_middle_of_range_permits_both():
    read = regime.classify(bars([760.0] * 10), spot=760.0, today="2026-08-20")
    assert regime.core_sides(read, LIMITS) == ("P", "C")


def test_a_real_decline_is_bear_and_permits_calls_only():
    closes = [790 - 3 * i for i in range(10)]           # -3.4% over ten sessions
    read = regime.classify(bars(closes, rng=0.006), spot=760.0, today="2026-08-20")
    assert read.regime == "bear"
    assert regime.core_sides(read, LIMITS) == ("C",)


def test_a_real_rally_is_bull_and_permits_puts_only():
    closes = [740 + 3 * i for i in range(10)]
    read = regime.classify(bars(closes, rng=0.006), spot=770.0, today="2026-08-20")
    assert read.regime == "bull"
    assert regime.core_sides(read, LIMITS) == ("P",)


def test_no_range_means_no_sides_are_forbidden_by_position():
    """Insufficient history: regime falls back to sideways with no range rule.
    The range_buffer gate still blocks the entry; this only governs direction."""
    read = regime.classify(bars([760.0] * 3), spot=760.0, today="2026-08-20")
    assert regime.core_sides(read, LIMITS) == ("P", "C")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_tape.py -q`
Expected: FAIL with `AttributeError: module 'agent.regime' has no attribute 'classify'`

- [ ] **Step 4: Implement the detector**

Append to `agent/regime.py`:

```python
# --- the tape read: computed, never asked ---------------------------------

@dataclass(frozen=True)
class TapeRead:
    """What the daily bars say about one underlying.

    Built from completed sessions only. The regime feeds the budget and the
    bull/bear direction rules; the range position decides which side may be
    sold in a sideways tape. `None` fields mean there was not enough history,
    and the gates that need them fail closed.
    """
    regime: str
    range_position: float | None
    lookback_high: float | None
    lookback_low: float | None
    trend_pct: float | None
    avg_range_pct: float | None
    detail: str


def classify(bars: list[dict], spot: float, today: str, *,
             lookback: int = 10, trend_multiple: float = 2.0) -> TapeRead:
    """Regime and range position from the last `lookback` completed sessions.

    Trend is spot against the close `lookback` sessions ago, measured in units
    of the mean daily high-low range. A move smaller than `trend_multiple`
    ranges is noise inside a band, not a trend -- on 2026-09-01 SPY was 0.7%
    off its 10-session-ago close with 0.8% daily ranges, and calling that
    "bear" is what sold calls at the low.
    """
    done = [b for b in bars if str(b.get("t", ""))[:10] < today]
    if len(done) < lookback:
        return TapeRead("sideways", None, None, None, None, None,
                        f"only {len(done)} completed sessions; need {lookback}")
    win = done[-lookback:]
    hi = max(float(b["h"]) for b in win)
    lo = min(float(b["l"]) for b in win)
    ref = float(win[0]["c"])
    trend = (spot - ref) / ref
    avg_range = sum((float(b["h"]) - float(b["l"])) / float(b["c"]) for b in win) / len(win)
    threshold = trend_multiple * avg_range
    if trend > threshold:
        reg = "bull"
    elif trend < -threshold:
        reg = "bear"
    else:
        reg = "sideways"
    pos = (spot - lo) / (hi - lo) if hi > lo else None
    return TapeRead(
        regime=reg, range_position=pos, lookback_high=hi, lookback_low=lo,
        trend_pct=trend, avg_range_pct=avg_range,
        detail=(f"{reg}: spot {spot:.2f} is {trend:+.2%} vs {lookback} sessions ago "
                f"(trend threshold {threshold:.2%}); range {lo:.2f}-{hi:.2f}, "
                f"position {pos:.0%}" if pos is not None else
                f"{reg}: spot {spot:.2f}, flat range {lo:.2f}-{hi:.2f}"),
    )


def core_sides(tape: TapeRead, limits) -> tuple[Right, ...]:
    """Which rights the core sleeve may sell, given the tape.

    Trends keep the policy rule (bull: puts only; bear: calls only). A range
    forbids selling into the mean reversion: no short calls in the bottom
    quarter, no short puts in the top quarter.
    """
    allowed = policy_for(tape.regime).allowed_rights
    if tape.regime != "sideways" or tape.range_position is None:
        return allowed
    q = limits.range_edge_quantile
    if tape.range_position < q:
        return tuple(r for r in allowed if r != "C")
    if tape.range_position > 1 - q:
        return tuple(r for r in allowed if r != "P")
    return allowed
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_tape.py tests/test_regime.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add agent/regime.py agent/config.py tests/test_tape.py
git commit -m "Compute the regime from the bars instead of asking the model"
```

---

### Task 2: Expiry floor and delta band floor

**Files:**
- Modify: `agent/config.py` (`MIN_DAYS_TO_EXPIRY`, `min_short_delta`)
- Test: `tests/test_risk.py` (append)

**Interfaces:**
- Produces: `config.MIN_DAYS_TO_EXPIRY == 7`, `RiskLimits.min_short_delta == 0.10`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_risk.py`:

```python
# --- post-mortem: strikes lived inside the range at 1-4 DTE ---------------

def test_expiry_floor_is_a_week_out():
    """Every hackathon trade was 1-4 DTE, where a 0.25-delta strike sits
    inside one ordinary day's range. Seven days puts it outside."""
    from agent.config import MIN_DAYS_TO_EXPIRY
    assert MIN_DAYS_TO_EXPIRY >= 7


def test_delta_band_floor_admits_one_expected_move():
    """One expected move at 7-14 DTE is roughly 0.16 delta; the old 0.20
    floor would reject every strike the range gate permits."""
    assert LIMITS.min_short_delta <= 0.10
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_risk.py -q -k "expiry_floor or delta_band_floor"`
Expected: 2 FAIL

- [ ] **Step 3: Change the constants**

In `agent/config.py`, replace the `MIN_DAYS_TO_EXPIRY` block with:

```python
# Past the event the expiry has to roll, or the agent simply stops: every
# proposal is checked against one date, and once it passes nothing can trade.
# resolve_expiry() in loop.py picks the nearest listed expiry at least this
# many days out, per cycle, from the broker rather than from a calendar.
#
# Seven, not three. At 1-4 DTE a 0.25-delta strike sits 0.5-1.0% from spot,
# inside one ordinary day's range; six of nine hackathon short strikes were in
# the money at expiry. SPY/QQQ/IWM list Mon/Wed/Fri, so this lands 7-9 days out.
MIN_DAYS_TO_EXPIRY = 7
```

Replace the `min_short_delta`/`max_short_delta` comment block and values with:

```python
    # Short-leg delta band, enforced by the delta_band gate. The range_buffer
    # gate now decides where the strike goes (outside the recent range, one
    # expected move out), which lands near 0.15 delta at 7-14 DTE. The floor
    # is 0.10 so the band cannot reject a strike range_buffer requires; the
    # ceiling matches the chain filter in brain.py so the gate cannot reject a
    # strike the model was never shown.
    min_short_delta: float = 0.10
    max_short_delta: float = 0.35
```

- [ ] **Step 4: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agent/config.py tests/test_risk.py
git commit -m "Target expiries a week out and widen the delta floor to match"
```

---

### Task 3: Range buffer and credit floor gates

**Files:**
- Modify: `agent/risk.py` (imports, `evaluate` signature and body, two new gate functions)
- Modify: `agent/config.py` (RiskLimits: `expected_move_multiple`, `min_credit_pct_of_width`)
- Test: `tests/test_risk.py` (append)

**Interfaces:**
- Consumes: `regime.TapeRead` from Task 1.
- Produces: `risk.evaluate(..., tape: regime_mod.TapeRead | None = None, open_marks: dict | None = None, recent_spreads: list[dict] | None = None)`. When `tape` is given, `regime` is taken from `tape.regime`. Spot is the mid of `quotes[proposal.underlying]`.
- Produces gates named `range_buffer` and `credit_floor`.

- [ ] **Step 1: Add the limits**

In `agent/config.py`, inside `RiskLimits`, after `range_edge_quantile`:

```python
    # --- Strike placement and premium ---
    # The short strike must clear BOTH the recent range and one expected move
    # (spot x IV x sqrt(DTE/365)). Eight of nine hackathon strikes sat inside
    # the prior five sessions' range; this would have rejected all eight.
    expected_move_multiple: float = 1.0
    # Credit as a fraction of width. A 0.15-0.20 delta strike at 7-14 DTE pays
    # 20-25% in normal vol; below this the trade is not worth its width.
    min_credit_pct_of_width: float = 0.20
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_risk.py`:

```python
from agent.regime import TapeRead

WEEK_OUT = "2026-09-04"       # 7 days after MIDDAY


def tape(**kw) -> TapeRead:
    base = dict(regime="sideways", range_position=0.5, lookback_high=770.0,
                lookback_low=750.0, trend_pct=0.0, avg_range_pct=0.008, detail="t")
    base.update(kw)
    return TapeRead(**base)


def chain_with_iv(p: TradeProposal, iv=0.15, **kw) -> dict:
    ch = make_chain(p, **kw)
    for snap in ch.values():
        snap["impliedVolatility"] = iv
    return ch


def gate(gates, name):
    return next(g for g in gates if g.name == name)


def test_range_buffer_blocks_a_strike_inside_the_recent_range():
    # spot 760, 10-session range 750-770: a 752 put is inside it
    p = make_proposal(expiry=WEEK_OUT, short_strike=752.0, long_strike=747.0)
    g = gate(evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                      tape=tape()), "range_buffer")
    assert not g.passed and "inside" in g.detail


def test_range_buffer_blocks_a_strike_inside_one_expected_move():
    # 7 DTE at 15% IV: expected move = 760 * 0.15 * sqrt(7/365) ~ 15.8
    # 748 is outside the 750-770 range but only 12 from spot
    p = make_proposal(expiry=WEEK_OUT, short_strike=748.0, long_strike=743.0)
    g = gate(evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                      tape=tape()), "range_buffer")
    assert not g.passed and "expected move" in g.detail


def test_range_buffer_passes_a_strike_beyond_both():
    p = make_proposal(expiry=WEEK_OUT, short_strike=740.0, long_strike=735.0)
    g = gate(evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                      tape=tape()), "range_buffer")
    assert g.passed, g.detail


def test_range_buffer_fails_closed_without_history_or_iv():
    p = make_proposal(expiry=WEEK_OUT, short_strike=740.0, long_strike=735.0)
    q = {"SPY": {"bp": 759.9, "ap": 760.1}}
    no_hist = gate(evaluate(p, chain=chain_with_iv(p), quotes=q,
                            tape=tape(lookback_high=None, lookback_low=None)), "range_buffer")
    no_iv = gate(evaluate(p, chain=make_chain(p), quotes=q, tape=tape()), "range_buffer")
    no_tape = gate(evaluate(p, chain=chain_with_iv(p), quotes=q), "range_buffer")
    assert not no_hist.passed and not no_iv.passed and not no_tape.passed


def test_credit_floor_rejects_thin_premium():
    p = make_proposal(net_price=0.47)          # 5-wide: 9.4% of width
    assert not gate(evaluate(p), "credit_floor").passed


def test_credit_floor_passes_a_fair_credit():
    p = make_proposal(net_price=1.10)          # 22% of width
    assert gate(evaluate(p), "credit_floor").passed


def test_credit_floor_ignores_the_satellite():
    p = make_proposal(sleeve="satellite", short_strike=747.0, long_strike=752.0, net_price=0.47)
    assert gate(evaluate(p), "credit_floor").passed
```

Also update the existing happy-path test so it clears the new gates:

```python
def test_clean_proposal_passes_every_gate():
    p = make_proposal(expiry=WEEK_OUT, short_strike=740.0, long_strike=735.0, net_price=1.10)
    gates = evaluate(p, chain=chain_with_iv(p), quotes={"SPY": {"bp": 759.9, "ap": 760.1}},
                     tape=tape())
    assert risk.all_passed(gates), [str(g) for g in risk.blockers(gates)]
```

Note that `make_proposal`'s default expiry is `TARGET_EXPIRY` (2026-09-03) and the expiry gate compares against `target_expiry or TARGET_EXPIRY`; pass `target_expiry=WEEK_OUT` in `evaluate` for this test: `evaluate(p, ..., target_expiry=WEEK_OUT)`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_risk.py -q`
Expected: the new tests FAIL with `TypeError: evaluate() got an unexpected keyword argument 'tape'` or `StopIteration`

- [ ] **Step 4: Implement**

In `agent/risk.py`:

Imports: change `from datetime import datetime` to `from datetime import date, datetime, timedelta` and add `import math`.

`evaluate` signature: add after `open_spreads: list[dict] | None = None,`:

```python
    tape: regime_mod.TapeRead | None = None,
    open_marks: dict | None = None,
    recent_spreads: list[dict] | None = None,
```

At the top of `evaluate` body, before Gate 0:

```python
    if tape is not None:
        regime = tape.regime
    spot = _mid((quotes or {}).get(proposal.underlying) or {})
```

After the Gate 15 append, before `return g`:

```python
    # --- Gate 16: strike placement --------------------------------------
    g.append(_range_buffer_gate(proposal, tape, chain, spot, now, limits))

    # --- Gate 17: premium floor -----------------------------------------
    g.append(_credit_floor_gate(proposal, limits))
```

New helpers, after `_directional_risk_gate`:

```python
def _mid(q: dict) -> float | None:
    try:
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    except (TypeError, ValueError):
        return None
    return (bid + ask) / 2 if bid > 0 and ask > 0 else None


def _range_buffer_gate(proposal: TradeProposal, tape, chain: dict,
                       spot: float | None, now: datetime, limits: RiskLimits) -> GateResult:
    """The short strike must sit outside the recent range AND one expected move out.

    Eight of nine hackathon short strikes were inside the prior five sessions'
    high-low, 0.5-1.0% from spot at 1-4 DTE. Six finished in the money. Delta
    alone cannot place a strike outside the noise at short DTE; this can.

    Fails closed. A strike we cannot place relative to the tape is a strike
    we do not sell.
    """
    if tape is None or tape.lookback_high is None or tape.lookback_low is None:
        return GateResult(name="range_buffer", passed=False,
                          detail="no completed-session range available; cannot place the strike")
    if spot is None:
        return GateResult(name="range_buffer", passed=False,
                          detail=f"no quote for {proposal.underlying}; cannot measure distance")
    sym = occ_symbol(proposal.underlying, proposal.expiry, proposal.right, proposal.short_strike)
    iv = (chain.get(sym) or {}).get("impliedVolatility")
    if iv is None:
        return GateResult(name="range_buffer", passed=False,
                          detail=f"no IV published for short leg {sym}; cannot size the expected move")
    dte = max((date.fromisoformat(proposal.expiry) - now.date()).days, 1)
    em = spot * float(iv) * math.sqrt(dte / 365.0)
    need = limits.expected_move_multiple * em
    k = proposal.short_strike
    dist = (k - spot) if proposal.right == "C" else (spot - k)
    problems: list[str] = []
    if dist < need:
        problems.append(f"{dist:.2f} from spot < {limits.expected_move_multiple:g}x "
                        f"expected move {em:.2f} ({dte} DTE, IV {float(iv):.1%})")
    inside = (k <= tape.lookback_high) if proposal.right == "C" else (k >= tape.lookback_low)
    if inside:
        problems.append(f"short {k:g} inside the {limits.range_lookback}-session range "
                        f"{tape.lookback_low:.2f}-{tape.lookback_high:.2f}")
    if problems:
        return GateResult(name="range_buffer", passed=False, detail="; ".join(problems))
    return GateResult(name="range_buffer", passed=True,
                      detail=(f"short {k:g} is {dist:.2f} from spot {spot:.2f} "
                              f"(>= {need:.2f}) and outside {tape.lookback_low:.2f}-"
                              f"{tape.lookback_high:.2f}"))


def _credit_floor_gate(proposal: TradeProposal, limits: RiskLimits) -> GateResult:
    """Premium must be worth the width. Satellite pays a debit; not its concern."""
    if not proposal.is_credit:
        return GateResult(name="credit_floor", passed=True, detail="debit spread; no floor")
    frac = proposal.net_price / proposal.width if proposal.width > 0 else 0.0
    ok = frac >= limits.min_credit_pct_of_width
    return GateResult(
        name="credit_floor", passed=ok,
        detail=(f"credit {proposal.net_price:.2f} is {frac:.0%} of width {proposal.width:g} "
                f"({'>=' if ok else '<'} {limits.min_credit_pct_of_width:.0%})"))
```

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS. If `test_regime.py` gate tests fail on `credit_floor` (their proposal is 0.47 on a 5-wide), they only assert on named gates and should still pass; if any asserts `all_passed`, raise its `net_price` to 1.10.

- [ ] **Step 6: Commit**

```bash
git add agent/risk.py agent/config.py tests/test_risk.py
git commit -m "Gate the short strike against the range and the expected move, and floor the credit"
```

---

### Task 4: Range position in the direction gate

**Files:**
- Modify: `agent/risk.py` (Gate 8 block)
- Test: `tests/test_risk.py` (append)

**Interfaces:**
- Consumes: `regime.core_sides(tape, limits)` from Task 1.
- The `regime_direction` gate, when `tape` is given and the sleeve is core, uses `core_sides` instead of `policy.allowed_rights`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_risk.py`:

```python
def test_direction_gate_forbids_short_calls_at_the_bottom_of_a_range():
    """The 2026-09-01 trade: sideways tape, spot at the range low, sell calls."""
    p = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    g = gate(evaluate(p, tape=tape(range_position=0.02)), "regime_direction")
    assert not g.passed and "range" in g.detail


def test_direction_gate_forbids_short_puts_at_the_top_of_a_range():
    p = make_proposal(right="P")
    g = gate(evaluate(p, tape=tape(range_position=0.95)), "regime_direction")
    assert not g.passed


def test_direction_gate_permits_puts_at_the_bottom_of_a_range():
    p = make_proposal(right="P")
    assert gate(evaluate(p, tape=tape(range_position=0.02)), "regime_direction").passed


def test_direction_gate_keeps_the_bear_rule_when_the_tape_is_a_trend():
    p = make_proposal(right="P")
    g = gate(evaluate(p, tape=tape(regime="bear", range_position=0.02)), "regime_direction")
    assert not g.passed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_risk.py -q -k direction_gate`
Expected: the first two FAIL (gate passes when it should block)

- [ ] **Step 3: Implement**

Replace the Gate 8 block in `agent/risk.py` with:

```python
    # --- Gate 8: regime direction ----------------------------------------
    # Selling puts into a downtrend is how short-premium accounts die. In a
    # range, selling the side the tape just moved away from is how the
    # 2026-09-01 book died: bottom of the range forbids short calls, top
    # forbids short puts. Computed from the bars, never from the model.
    pol = regime_mod.policy_for(regime)
    if tape is not None and proposal.sleeve == "core":
        permitted = regime_mod.core_sides(tape, limits)
        why = (pol.rationale if tape.regime != "sideways" or tape.range_position is None
               else f"range position {tape.range_position:.0%}")
    else:
        permitted = (pol.satellite_rights if proposal.sleeve == "satellite"
                     else pol.allowed_rights)
        why = pol.rationale
    ok = proposal.right in permitted
    g.append(GateResult(
        name="regime_direction", passed=ok,
        detail=(f"{regime} permits {'/'.join(permitted) or 'nothing'} for the "
                f"{proposal.sleeve} sleeve; proposal is {proposal.right} -- {why}"),
    ))
```

- [ ] **Step 4: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agent/risk.py tests/test_risk.py
git commit -m "Forbid selling into the mean reversion at the edges of a range"
```

---

### Task 5: Book risk, same direction, losing side, cadence gates

**Files:**
- Modify: `agent/risk.py` (four gate functions, four appends in `evaluate`)
- Modify: `agent/config.py` (RiskLimits: `max_book_risk_pct`, `max_same_direction`, `losing_side_multiple`, `max_entries_per_day`, `reentry_cooldown_hours`)
- Test: `tests/test_risk.py` (append)

**Interfaces:**
- Consumes: `open_spreads` rows (journal dicts with `id`, `underlying`, `right`, `sleeve`, `short_strike`, `long_strike`, `qty`, `entry_credit`, `ts_open`, `ts_close`, `status`), `open_marks: dict[id, float]`, `recent_spreads: list[dict]` (journal rows from the last 7 days, any status).
- Produces gates named `book_risk`, `same_direction`, `losing_side`, `cadence`.

- [ ] **Step 1: Add the limits**

In `agent/config.py`, inside `RiskLimits`, after `min_credit_pct_of_width`:

```python
    # --- Book shape ---
    # Per-tranche limits let four bear-sized tranches add up to more than one
    # sideways tranche, and three call spreads in three tickers that move
    # together were one bet. These look at the whole book.
    max_book_risk_pct: float = 0.24         # open max loss + proposal, x regime multiplier
    max_same_direction: int = 2             # open core spreads on one right, whole universe
    losing_side_multiple: float = 1.5       # no add-on where a spread marks >= this x credit
    # --- Cadence ---
    # Four cycles a day produced a proposal in 13 of 13 cycles with budget.
    max_entries_per_day: int = 1
    reentry_cooldown_hours: int = 24        # same underlying and right, after any close
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_risk.py`:

```python
def row(**kw) -> dict:
    base = dict(id="r1", underlying="QQQ", right="C", sleeve="core", short_strike=740.0,
                long_strike=743.0, qty=10, entry_credit=0.60, status="open",
                ts_open="2026-08-27T15:46:00+00:00", ts_close=None)
    base.update(kw)
    return base


def test_book_risk_caps_the_whole_book_not_the_tranche():
    # 24% of 100k = 24,000. Held 20,000; a 5,000 proposal tips it over.
    held = [row(id="a", entry_credit=0.5, short_strike=740.0, long_strike=745.0, qty=45)]  # 450 x 45 = 20,250
    p = make_proposal(qty=11)                       # 453 x 11 = 4,983
    g = gate(evaluate(p, open_spreads=held), "book_risk")
    assert not g.passed and "24%" in g.detail


def test_book_risk_scales_with_the_regime_multiplier():
    held = [row(id="a", entry_credit=0.5, short_strike=740.0, long_strike=745.0, qty=15)]  # 6,750
    p = make_proposal(qty=5)                        # 2,265 -> 9,015 total
    assert gate(evaluate(p, open_spreads=held), "book_risk").passed                 # 24,000
    assert not gate(evaluate(p, open_spreads=held, regime="bear"), "book_risk").passed  # 8,400


def test_same_direction_caps_open_spreads_on_one_right_across_the_universe():
    held = [row(id="a", underlying="QQQ", right="C"), row(id="b", underlying="IWM", right="C")]
    p = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    g = gate(evaluate(p, open_spreads=held), "same_direction")
    assert not g.passed and "2 open" in g.detail
    assert gate(evaluate(make_proposal(right="P"), open_spreads=held), "same_direction").passed


def test_losing_side_blocks_adding_to_a_side_already_underwater():
    """09-02 11:46: QQQ calls added while IWM and SPY calls marked ~2x credit."""
    held = [row(id="a", underlying="IWM", right="C", entry_credit=0.39)]
    p = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    losing = gate(evaluate(p, open_spreads=held, open_marks={"a": 0.80}), "losing_side")
    fine = gate(evaluate(p, open_spreads=held, open_marks={"a": 0.40}), "losing_side")
    no_mark = gate(evaluate(p, open_spreads=held, open_marks={}), "losing_side")
    assert not losing.passed and "2.05x" in losing.detail
    assert fine.passed and no_mark.passed


def test_cadence_allows_one_entry_per_day():
    today = [row(id="a", ts_open="2026-08-28T15:46:00+00:00")]      # 11:46 ET on MIDDAY's date
    g = gate(evaluate(make_proposal(), recent_spreads=today), "cadence")
    assert not g.passed and "1 entr" in g.detail
    yesterday = [row(id="a", ts_open="2026-08-27T15:46:00+00:00")]
    assert gate(evaluate(make_proposal(), recent_spreads=yesterday), "cadence").passed


def test_cadence_cools_down_after_a_close_in_the_same_name_and_side():
    closed = [row(id="a", underlying="SPY", right="P", status="closed",
                  ts_open="2026-08-27T15:46:00+00:00", ts_close="2026-08-28T14:00:00+00:00")]
    g = gate(evaluate(make_proposal(right="P"), recent_spreads=closed), "cadence")
    assert not g.passed and "closed" in g.detail
    other_side = make_proposal(right="C", short_strike=780.0, long_strike=785.0)
    assert gate(evaluate(other_side, recent_spreads=closed), "cadence").passed
    old = [row(id="a", underlying="SPY", right="P", status="closed",
               ts_open="2026-08-25T15:46:00+00:00", ts_close="2026-08-26T14:00:00+00:00")]
    assert gate(evaluate(make_proposal(right="P"), recent_spreads=old), "cadence").passed
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_risk.py -q -k "book_risk or same_direction or losing_side or cadence"`
Expected: FAIL with `StopIteration` (gates do not exist yet)

- [ ] **Step 4: Implement**

In `agent/risk.py`, after the Gate 17 append and before `return g`:

```python
    # --- Gates 18-21: the shape of the whole book ------------------------
    g.append(_book_risk_gate(proposal, open_spreads or [], equity, regime, limits))
    g.append(_same_direction_gate(proposal, open_spreads or [], limits))
    g.append(_losing_side_gate(proposal, open_spreads or [], open_marks or {}, limits))
    g.append(_cadence_gate(proposal, recent_spreads or [], now, limits))
```

New helpers, after `_credit_floor_gate`:

```python
def _book_risk_gate(proposal: TradeProposal, open_spreads: list[dict], equity: float,
                    regime: str, limits: RiskLimits) -> GateResult:
    """Open max loss plus this trade, against a book budget the regime scales.

    tranche_risk caps one trade; nothing capped the sum. Under "bear" the
    tranche budget fell to 35% and the book simply opened four tranches.
    """
    held = 0.0
    for row in open_spreads:
        try:
            held += _spread_max_loss(row)
        except (KeyError, TypeError, ValueError):
            continue
    mult = regime_mod.policy_for(regime).size_multiplier
    cap = equity * limits.max_book_risk_pct * mult
    total = held + proposal.total_max_loss
    ok = total <= cap
    return GateResult(
        name="book_risk", passed=ok,
        detail=(f"open max loss {held:,.0f} + this trade {proposal.total_max_loss:,.0f} = "
                f"{total:,.0f} vs book budget {cap:,.0f} "
                f"({limits.max_book_risk_pct:.0%} of equity x {mult:.0%} {regime})"))


def _same_direction_gate(proposal: TradeProposal, open_spreads: list[dict],
                         limits: RiskLimits) -> GateResult:
    """SPY, QQQ and IWM are one bucket. Count open core spreads on this right."""
    same = [r for r in open_spreads
            if r.get("right") == proposal.right and (r.get("sleeve") or "core") == "core"]
    ok = proposal.sleeve != "core" or len(same) < limits.max_same_direction
    names = ", ".join(f"{r.get('underlying')} {r.get('short_strike'):g}/{r.get('long_strike'):g}"
                      for r in same) or "none"
    return GateResult(
        name="same_direction", passed=ok,
        detail=(f"{len(same)} open core {proposal.right} spread(s) across the universe "
                f"({names}) vs max {limits.max_same_direction}"))


def _losing_side_gate(proposal: TradeProposal, open_spreads: list[dict],
                      open_marks: dict, limits: RiskLimits) -> GateResult:
    """No adding to a side that is already being run over.

    On 2026-09-02 at 11:46 ET a third short call spread was opened while the
    first two marked about twice their credit. The snapshot showed the model
    those positions; it added anyway. This is the gate that says no.
    """
    if proposal.sleeve != "core":
        return GateResult(name="losing_side", passed=True, detail="satellite; not applied")
    for r in open_spreads:
        if r.get("right") != proposal.right or (r.get("sleeve") or "core") != "core":
            continue
        mark = open_marks.get(r.get("id"))
        credit = float(r.get("entry_credit") or 0)
        if mark is None or credit <= 0:
            continue
        ratio = float(mark) / credit
        if ratio >= limits.losing_side_multiple:
            return GateResult(
                name="losing_side", passed=False,
                detail=(f"{r.get('underlying')} {r.get('short_strike'):g}/"
                        f"{r.get('long_strike'):g} {proposal.right} marks {float(mark):.2f} = "
                        f"{ratio:.2f}x its {credit:.2f} credit (>= {limits.losing_side_multiple:g}x); "
                        "not adding to a losing side"))
    return GateResult(name="losing_side", passed=True,
                      detail=f"no open {proposal.right} spread at or beyond "
                             f"{limits.losing_side_multiple:g}x its credit")


def _ts(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=ET)


def _cadence_gate(proposal: TradeProposal, recent: list[dict], now: datetime,
                  limits: RiskLimits) -> GateResult:
    """One entry a day, and no re-entry where a spread just closed.

    Four cycles a day produced a proposal in every cycle with budget, and each
    50% target exit was recycled the same day into a closer, shorter-dated
    spread. Frequency was the strategy's variance, not its edge.
    """
    today = now.astimezone(ET).date()
    opened_today = [r for r in recent
                    if (t := _ts(r.get("ts_open"))) and t.astimezone(ET).date() == today]
    if len(opened_today) >= limits.max_entries_per_day:
        return GateResult(
            name="cadence", passed=False,
            detail=(f"{len(opened_today)} entr{'y' if len(opened_today) == 1 else 'ies'} "
                    f"already today vs max {limits.max_entries_per_day}"))
    window = timedelta(hours=limits.reentry_cooldown_hours)
    for r in recent:
        if r.get("underlying") != proposal.underlying or r.get("right") != proposal.right:
            continue
        closed = _ts(r.get("ts_close"))
        if closed and now - closed < window:
            return GateResult(
                name="cadence", passed=False,
                detail=(f"{proposal.underlying} {proposal.right} spread closed "
                        f"{closed.astimezone(ET):%m-%d %H:%M ET}, inside the "
                        f"{limits.reentry_cooldown_hours}h cooldown"))
    return GateResult(name="cadence", passed=True,
                      detail=(f"{len(opened_today)} entries today; no {proposal.underlying} "
                              f"{proposal.right} close in the last {limits.reentry_cooldown_hours}h"))
```

Add `ET` to the `from .config import (...)` list in `agent/risk.py`.

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add agent/risk.py agent/config.py tests/test_risk.py
git commit -m "Cap the book, the side, the add-on and the cadence, not just the tranche"
```

---

### Task 6: The model stops calling the regime; the snapshot shows the tape read

**Files:**
- Modify: `agent/models.py` (`AgentDecision`)
- Modify: `agent/brain.py` (`SYSTEM`, `Brain.decide`, `build_snapshot`)
- Test: `tests/test_regime.py` (the `_rendered` helper and a new test)

**Interfaces:**
- `AgentDecision` has fields `reasoning: str` and `proposal: TradeProposal | None` only.
- `build_snapshot(..., tape: dict[str, TapeRead] | None = None, sides: dict[str, tuple] | None = None)` renders a `TAPE READ` section per underlying.

- [ ] **Step 1: Write the failing test**

In `tests/test_regime.py`, append:

```python
def test_snapshot_shows_the_computed_tape_and_permitted_sides():
    from datetime import datetime
    from agent.brain import build_snapshot
    from agent.regime import TapeRead
    read = TapeRead(regime="sideways", range_position=0.03, lookback_high=775.3,
                    lookback_low=762.04, trend_pct=-0.007, avg_range_pct=0.008, detail="d")
    out = build_snapshot(
        now=datetime(2026, 9, 1, 9, 46, tzinfo=ET), equity=100_000.0,
        day_start_equity=100_000.0, positions=[], quotes={}, chains={}, bars={},
        news=[], limits=LIMITS, tape={"SPY": read}, sides={"SPY": ("P",)},
    )
    assert "TAPE READ" in out and "SPY: sideways" in out
    assert "range 762.04-775.30" in out and "position 3%" in out
    assert "core may sell: P" in out


def test_decision_has_no_regime_field():
    from agent.models import AgentDecision
    assert "regime" not in AgentDecision.model_fields
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_regime.py -q -k "snapshot_shows or no_regime_field"`
Expected: 2 FAIL

- [ ] **Step 3: Implement**

`agent/models.py`, replace `AgentDecision`:

```python
class AgentDecision(BaseModel):
    """What the brain returns each cycle. `proposal` is None when it stands down.

    No regime field. The regime is computed from the bars in regime.classify
    and shown to the model; a label the model could set was a control the
    model could move, and on 2026-09-01 it moved it to the wrong side.
    """
    reasoning: str = Field(description="Your analysis, for the journal and the demo")
    proposal: TradeProposal | None = Field(
        default=None, description="The trade to open, or null to stand down this cycle"
    )
```

`agent/brain.py`:

Add `from .regime import TapeRead` to the imports.

In `SYSTEM`, replace the `core (CREDIT spread)` bullet with:

```
  * core (CREDIT spread): sell premium with the short strike OUTSIDE the
    recent range and at least one expected move from spot -- the snapshot
    prints both numbers per underlying, and the range_buffer gate enforces
    them. That usually lands the short leg near 0.10-0.20 delta. The
    delta_band gate rejects a short leg outside 0.10-0.35.
```

Replace the whole `YOUR REGIME CALL IS A BINDING CONTROL, NOT A COMMENT` section (through the line ending `it is silently cut to fit.`) with:

```
THE TAPE READ IS COMPUTED FOR YOU
The snapshot carries, per underlying, a regime (bull / bear / sideways) read
from the daily bars, the position of spot inside the recent range, the
expected move to expiry, and the sides the core sleeve may sell. You do not
set any of it. The budget and the permitted sides follow from it:
  sideways -> core 12.00%. Bottom quarter of the range: puts only. Top
              quarter: calls only. Middle: either. NO satellite.
  bull     -> core 10.20% (P credit only). satellite 3.40% (C debit).
  bear     -> core 4.20% (C credit only). satellite 1.40% (P debit).
A side the read forbids is rejected outright; a size above the budget is
silently cut to fit. If no side is permitted in the name you like, stand down
or pick another name.
```

In `HOW TO THINK`, replace the first bullet with:

```
- Read the tape section first. Sell the side it permits, at a strike beyond
  the range and the expected move. A range that has just moved to one edge
  is a mean-reversion risk, not a trend to lean on.
```

In `Brain.decide`, replace the refusal return with:

```python
            return AgentDecision(
                reasoning="Model declined to answer this cycle; standing down.",
                proposal=None,
            )
```

Add `preflight` to `Brain` (after `__init__`):

```python
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
```

In `build_snapshot`, add parameters `tape: dict[str, TapeRead] | None = None, sides: dict[str, tuple] | None = None` after `target_expiry`. After the `RECENT DAILY BARS` block and before `OPTION CHAINS`, insert:

```python
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
    else:
        lines.append("  (unavailable)")
```

- [ ] **Step 4: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agent/models.py agent/brain.py tests/test_regime.py
git commit -m "Show the model the computed tape read instead of asking it for a regime"
```

---

### Task 7: Wire the loop: preflight, tape, marks, recent rows

**Files:**
- Modify: `agent/loop.py` (`manage_open_spreads` return, `run_cycle`)
- Test: `tests/test_loop.py` (create)

**Interfaces:**
- Consumes: `Brain.preflight`, `regime.classify`, `regime.core_sides`, `risk.evaluate(tape=, open_marks=, recent_spreads=)`, `build_snapshot(tape=, sides=)`.
- `manage_open_spreads(...) -> dict` mapping spread id to its conservative mark for every spread the broker holds.
- New helper `read_tape(obs, now, limits) -> tuple[dict[str, TapeRead], dict[str, tuple]]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_loop.py`:

```python
"""The loop wires the computed tape into the gates and checks the API first."""
from datetime import datetime

import pytest

from agent import loop
from agent.brain import Brain
from agent.config import ET, RiskLimits


class DeadAPI:
    class messages:
        @staticmethod
        def create(**kw):
            raise RuntimeError("credit balance is too low")


class LiveAPI:
    calls = 0

    class messages:
        @staticmethod
        def create(**kw):
            LiveAPI.calls += 1
            assert kw["max_tokens"] == 1
            return object()


def test_preflight_raises_when_the_api_is_dead():
    with pytest.raises(RuntimeError, match="credit balance"):
        Brain(client=DeadAPI()).preflight()


def test_preflight_makes_one_tiny_call_when_alive():
    Brain(client=LiveAPI()).preflight()
    assert LiveAPI.calls == 1


def test_read_tape_uses_each_underlyings_own_bars():
    bars = [{"t": f"2026-08-{d:02d}T04:00:00Z", "o": 760, "h": 763, "l": 757, "c": 760}
            for d in range(10, 21)]
    obs = {"quotes": {"SPY": {"bp": 759.9, "ap": 760.1}, "QQQ": {}},
           "bars": {"SPY": bars, "QQQ": []}}
    tape, sides = loop.read_tape(obs, datetime(2026, 8, 21, 13, 0, tzinfo=ET), RiskLimits())
    assert tape["SPY"].regime == "sideways" and tape["SPY"].lookback_high == 763
    assert sides["SPY"] == ("P", "C")
    assert tape["QQQ"].lookback_high is None and sides["QQQ"] == ("P", "C")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_loop.py -q`
Expected: FAIL (`preflight` exists from Task 6 so two pass; `read_tape` FAILS with AttributeError)

- [ ] **Step 3: Implement**

In `agent/loop.py`:

Imports: add `from .regime import TapeRead, classify, core_sides` (keep `from . import regime`).

Add after `resolve_expiry`:

```python
def read_tape(obs: dict, now, limits) -> tuple[dict[str, TapeRead], dict[str, tuple]]:
    """Classify every name in the universe from its own bars and quote.

    A name with no usable quote or history gets a defensive sideways read
    with no range, which the range_buffer gate turns into no entry.
    """
    today = now.strftime("%Y-%m-%d")
    tape: dict[str, TapeRead] = {}
    sides: dict[str, tuple] = {}
    for sym in UNIVERSE:
        q = obs["quotes"].get(sym) or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        spot = (bid + ask) / 2 if bid and ask else None
        bars = obs.get("bars", {}).get(sym) or []
        if spot is None:
            tape[sym] = TapeRead("sideways", None, None, None, None, None, "no quote")
        else:
            tape[sym] = classify(bars, spot, today, lookback=limits.range_lookback,
                                 trend_multiple=limits.trend_range_multiple)
        sides[sym] = core_sides(tape[sym], limits)
        print(f"  tape {sym}: {tape[sym].detail}; core may sell {'/'.join(sides[sym]) or 'none'}")
    return tape, sides
```

`manage_open_spreads`: change the signature's return annotation to `-> dict`, initialise `marks: dict = {}` right after `rows = journal.open_spreads(profile)` (return `marks` in the early `if not rows: return marks`), record `marks[sp.id] = mark` right after `mark = mark_to_close(sp, quotes)` when `mark is not None`, and end the function with `return marks`.

In `run_cycle`, after `print(f"  target expiry: {expiry}")` and before `observe`, insert:

```python
    brain = Brain(model=settings.model)
    if not manage_only:
        # The model is the one dependency that fails silently at billing
        # time. Check it before paying for a snapshot it cannot read.
        try:
            brain.preflight()
        except Exception as e:  # noqa: BLE001 - any failure here means no cycle
            journal.record_cycle(profile=profile, action="error",
                                 error=f"anthropic preflight: {e}")
            print(f"  anthropic preflight failed: {e}", file=sys.stderr)
            return 1
```

Change `manage_open_spreads(settings, journal, obs, now, dry_run=dry_run)` to `open_marks = manage_open_spreads(settings, journal, obs, now, dry_run=dry_run)`.

After the `if manage_only:` block, before `snapshot = build_snapshot(`, insert:

```python
    tape, sides = read_tape(obs, now, settings.limits)
```

Add `tape=tape, sides=sides,` to the `build_snapshot(...)` call.

Replace `decision: AgentDecision = Brain(model=settings.model).decide(snapshot, settings.limits)` with `decision: AgentDecision = brain.decide(snapshot, settings.limits)`.

Replace `print(f"  regime: {decision.regime}")` with nothing (delete the line).

Every `journal.record_cycle(... regime=decision.regime ...)` becomes `regime=cycle_regime`, where `cycle_regime` is defined right after `read_tape`:

```python
    cycle_regime = tape["SPY"].regime      # the universe read, for the journal badge
```

and re-assigned once a proposal exists, right after `p = decision.proposal`:

```python
    cycle_regime = tape[p.underlying].regime if p.underlying in tape else cycle_regime
```

Replace `eff_pct = regime.budget_pct_for(decision.regime, p.sleeve, settings.limits)` with `eff_pct = regime.budget_pct_for(cycle_regime, p.sleeve, settings.limits)` and the following print's `[{decision.regime}/...]` with `[{cycle_regime}/...]`.

In the `risk.evaluate(` call, replace `regime=decision.regime,` with `regime=cycle_regime,` and add:

```python
        tape=tape.get(p.underlying), open_marks=open_marks,
        recent_spreads=[r for r in journal.all_spreads(profile)
                        if (r.get("ts_open") or "") >= (now - timedelta(days=7)).strftime("%Y-%m-%d")],
```

- [ ] **Step 4: Run the full suite and a dry run**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS

Run: `./.venv/bin/python -m agent.loop --profile dev --dry-run`
Expected: prints `tape SPY: ...` lines, then either a preflight failure journaled (Anthropic balance is zero as of 2026-09-07) or a full gated cycle ending in `DRY RUN`. Either outcome is correct; a traceback is not.

- [ ] **Step 5: Commit**

```bash
git add agent/loop.py tests/test_loop.py
git commit -m "Check the API first, read the tape, and hand the gates the book"
```

---

### Task 8: Monitor checks the Claude API on every wake-up

**Files:**
- Modify: `scripts/gatekeeper_health.py` (new check + call in `main`)
- Install: copy to `~/.local/share/gatekeeper/`

**Interfaces:**
- Produces: `check_anthropic_api() -> None`, reporting `CRIT anthropic` on an HTTP error, `WARN anthropic` when the key or network is unavailable.

- [ ] **Step 1: Implement**

In `scripts/gatekeeper_health.py`, add after `PROJECT = "alpaca-ai-agent-2026"`:

```python
ANTHROPIC_MODEL = "claude-opus-5"          # the model the agent trades with
```

Add after `check_journal_errors`:

```python
# --- check 6b: the model the agent depends on, called for real -----------

def check_anthropic_api() -> None:
    """One one-token call to the agent's own model, every wake-up.

    On 2026-09-07 the API balance hit zero and the first anyone knew was a
    journaled brain error after the cycle had already fetched the market.
    Reading the key from Secret Manager is a GET; the call itself costs a
    fraction of a cent.
    """
    key = gcloud("secrets", "versions", "access", "latest", "--secret=anthropic-api-key")
    if not key:
        report("WARN", "anthropic", "API key unreadable from Secret Manager; check skipped")
        return
    body = json.dumps({
        "model": ANTHROPIC_MODEL, "max_tokens": 1,
        "thinking": {"type": "disabled"}, "output_config": {"effort": "low"},
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key.strip(), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        report("CRIT", "anthropic", f"{ANTHROPIC_MODEL} call failed HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        report("WARN", "anthropic", f"could not reach the API: {e}")
```

In `main`, after `check_executions(now_utc, now_et, entries_paused=entries_paused)`:

```python
    check_anthropic_api()
```

- [ ] **Step 2: Run it once by hand**

Run: `/opt/homebrew/bin/python3 scripts/gatekeeper_health.py; echo exit=$?`
Expected: a line `[CRIT] anthropic: claude-opus-5 call failed HTTP 400: ... credit balance is too low ...` while the balance is zero, exit 2, and a macOS notification. Once credit is added the line disappears.

- [ ] **Step 3: Install and commit**

```bash
cp scripts/gatekeeper_health.py ~/.local/share/gatekeeper/
git add scripts/gatekeeper_health.py
git commit -m "Have the monitor call the model itself on every wake-up"
```

---

### Task 9: Dashboard note and README

**Files:**
- Modify: `dashboard/index.html:102`
- Modify: `README.md` (regime section, gate table, `min_short_delta` mention, layout)

- [ ] **Step 1: Dashboard note**

Replace the policy `<p class="note">` in `dashboard/index.html` with:

```html
    <p class="note">The regime is computed from the daily bars, never from the model. It sets the budget; in a range, the position of spot inside it decides which side may be sold.</p>
```

- [ ] **Step 2: README**

Replace the `## Regime is a control, not a label` section body (through the blockquote ending `take the direction the chop favours."*`) with:

```markdown
## Regime is computed, not asked

The hackathon version let the model label the tape and bound the risk budget and the permitted direction to that label. On 2026-09-01 it labelled a 1.2% dip to the bottom of a 15-session band "bear", the bear rule permitted calls only, and the book sold four call spreads at the range low. They lost together on the rally two days later.

Now `regime.classify` reads the last ten completed sessions: a trend if spot has moved more than twice the mean daily range since the session ten back, otherwise sideways, plus where spot sits inside the ten-session high-low.

| Read | Core budget | Core may sell |
|---|---|---|
| sideways, middle of range | 12.00% | puts or calls |
| sideways, bottom quarter | 12.00% | **puts** only |
| sideways, top quarter | 12.00% | **calls** only |
| bull | 10.20% | **puts** only |
| bear | 4.20% | **calls** only |

The model sees the read in its snapshot and cannot change it.
```

Replace the gate table rows 14 and 15 and add rows 16 to 21:

```markdown
| 14 | `delta_band` | Short-leg \|delta\| within 0.10–0.35 |
| 15 | `directional_risk` | Max loss signed by direction across the book ≤ 20% of equity |
| 16 | `range_buffer` | Short strike outside the 10-session high-low and ≥ 1 expected move from spot |
| 17 | `credit_floor` | Credit ≥ 20% of width |
| 18 | `book_risk` | Open max loss + proposal ≤ 24% of equity × regime multiplier |
| 19 | `same_direction` | ≤ 2 open core spreads on one right across SPY/QQQ/IWM |
| 20 | `losing_side` | No new spread on a right where an open spread marks ≥ 1.5× its credit |
| 21 | `cadence` | 1 entry per day; 24h cooldown per underlying and right after a close |
```

Change the heading `## The 16 gates` to `## The 22 gates`, and the paragraph beginning `Gate 15 is the answer to how this event was lost` to:

```markdown
Gates 16 to 21 come from the post-mortem. Six of nine hackathon short strikes finished in the money; held to expiry the book would have lost about $11,400 against the $1,843 it did lose. The strikes sat inside the prior week's range at one to four days to expiry, the book stacked three same-direction spreads in three tickers that move together, and the model proposed a trade in every cycle it had budget for. Each of those is now a gate.
```

Replace `  regime.py            regime -> budget and permitted direction, per sleeve` in the layout block with `  regime.py            tape read from the bars; regime -> budget and permitted sides`.

- [ ] **Step 3: Commit**

```bash
git add dashboard/index.html README.md
git commit -m "Document the computed regime and the six post-mortem gates"
```

---

### Task 10: Deploy the agent jobs and resume entries

**Files:** none in the repo. Cloud state only. The `dashboard` Cloud Run service is not touched.

- [ ] **Step 1: Full suite one last time**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 2: Build the image**

```bash
TAG=us-east1-docker.pkg.dev/alpaca-ai-agent-2026/cloud-run-source-deploy/dashboard:gates-$(git rev-parse --short HEAD)
gcloud builds submit --tag "$TAG" --project alpaca-ai-agent-2026 .
```

Expected: `STATUS: SUCCESS` and the image tag printed.

- [ ] **Step 3: Point both jobs at it**

```bash
gcloud run jobs update agent-cycle --image "$TAG" --region us-east1 --project alpaca-ai-agent-2026
gcloud run jobs update agent-sweep --image "$TAG" --region us-east1 --project alpaca-ai-agent-2026
```

Expected: both print the updated job. Confirm with `gcloud run jobs describe agent-cycle --region us-east1 --project alpaca-ai-agent-2026 --format='value(spec.template.spec.template.spec.containers[0].image)'` showing the new tag.

- [ ] **Step 4: Smoke test with a sweep**

```bash
gcloud run jobs execute agent-sweep --region us-east1 --project alpaca-ai-agent-2026 --wait
```

Expected: execution succeeds. With a flat book the log says `manage-only pass complete` or `outside the trading session; sweep is a no-op`.

- [ ] **Step 5: Resume entry cycles**

```bash
gcloud scheduler jobs resume entry-cycles --project alpaca-ai-agent-2026 --location us-east1
gcloud scheduler jobs describe entry-cycles --project alpaca-ai-agent-2026 --location us-east1 --format='value(state)'
```

Expected: `ENABLED`.

- [ ] **Step 6: Record the state**

Update the memory file `gatekeeper-paused-pending-lablab.md` to say entries resumed on 2026-09-08 with the post-mortem gates deployed, image tag noted, and that the Anthropic balance must be topped up before the first cycle can trade; the preflight will journal an error and the monitor will page until it is.
