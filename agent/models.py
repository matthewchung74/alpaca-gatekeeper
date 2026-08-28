"""Shared types.

Note what the agent is *not* allowed to state: max loss, margin, position size in
dollars. It proposes strikes and a quantity; risk.py derives the money from the
contract specs. A model cannot be trusted to compute its own risk budget.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Right = Literal["P", "C"]
Sleeve = Literal["core", "satellite"]


def occ_symbol(underlying: str, expiry: str, right: Right, strike: float) -> str:
    """Build an OCC option symbol: SPY + 260903 + P + 00752000."""
    y, m, d = expiry.split("-")
    return f"{underlying}{y[2:]}{m}{d}{right}{int(round(strike * 1000)):08d}"


def parse_strike(symbol: str) -> float:
    return int(symbol[-8:]) / 1000.0


class TradeProposal(BaseModel):
    """A defined-risk vertical spread the agent wants to open.

    Both sleeves sell `short_strike` and buy `long_strike`; which one sits
    nearer the money decides whether the structure is a credit or a debit.

      core      -> CREDIT. Short strike nearer the money. Collect premium,
                   win slowly with high probability, lose the width if wrong.
      satellite -> DEBIT.  Long strike nearer the money. Pay premium, lose the
                   debit if wrong, win the rest of the width if the move runs.
    """
    underlying: str = Field(description="Ticker, one of the configured universe")
    expiry: str = Field(description="YYYY-MM-DD; must be the configured target expiry")
    right: Right = Field(description="P for puts, C for calls")
    short_strike: float = Field(description="Strike sold (collects premium)")
    long_strike: float = Field(description="Strike bought (defines the risk)")
    qty: int = Field(ge=1, description="Number of spreads")
    net_price: float = Field(
        gt=0,
        description=("Net price per spread in dollars per share, always POSITIVE. "
                     "For a core credit spread this is the credit you require; "
                     "for a satellite debit spread it is the debit you will pay."),
    )
    sleeve: Sleeve = Field(description="core for premium selling, satellite for directional")
    rationale: str = Field(description="Why this trade, in plain English, for the journal")

    @property
    def width(self) -> float:
        return abs(self.short_strike - self.long_strike)

    @property
    def is_credit(self) -> bool:
        return self.sleeve == "core"

    @property
    def signed_limit(self) -> float:
        """Alpaca mleg convention: negative = credit required, positive = debit paid."""
        return -self.net_price if self.is_credit else self.net_price

    @property
    def max_loss_per_spread(self) -> float:
        """Derived here, never taken from the model."""
        if self.is_credit:
            return (self.width - self.net_price) * 100.0
        return self.net_price * 100.0          # a debit spread can only lose the debit

    @property
    def max_profit_per_spread(self) -> float:
        if self.is_credit:
            return self.net_price * 100.0
        return (self.width - self.net_price) * 100.0

    @property
    def total_max_loss(self) -> float:
        return self.max_loss_per_spread * self.qty

    @property
    def total_max_profit(self) -> float:
        return self.max_profit_per_spread * self.qty

    def legs(self) -> list[dict]:
        short = occ_symbol(self.underlying, self.expiry, self.right, self.short_strike)
        long_ = occ_symbol(self.underlying, self.expiry, self.right, self.long_strike)
        return [
            {"symbol": short, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": long_, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
        ]

    def has_valid_structure(self) -> bool:
        """The strike order must match the sleeve's structure.

        credit puts : short ABOVE long      credit calls : short BELOW long
        debit  puts : long  ABOVE short     debit  calls : long  BELOW short
        """
        if self.width <= 0:
            return False
        if self.is_credit:
            return (self.short_strike > self.long_strike if self.right == "P"
                    else self.short_strike < self.long_strike)
        return (self.long_strike > self.short_strike if self.right == "P"
                else self.long_strike < self.short_strike)


class OpenSpread(BaseModel):
    """A spread we opened and still hold. Reconstructed from the journal.

    Alpaca reports option positions leg by leg, so pairing them back into spreads
    from the broker alone is guesswork. The journal knows exactly what we sent.
    """
    # SQLite gives an int rowid, Firestore a string document id. Accept both:
    # this type is what stands between a cloud deployment and a spread that can
    # be opened but never closed.
    id: int | str
    underlying: str
    expiry: str
    right: Right
    short_strike: float
    long_strike: float
    qty: int
    entry_credit: float          # net price paid/received at entry, always positive
    sleeve: Sleeve = "core"

    @property
    def is_credit(self) -> bool:
        return self.sleeve == "core"

    @property
    def width(self) -> float:
        return abs(self.short_strike - self.long_strike)

    @property
    def max_loss_per_spread(self) -> float:
        if self.is_credit:
            return (self.width - self.entry_credit) * 100.0
        return self.entry_credit * 100.0

    def short_symbol(self) -> str:
        return occ_symbol(self.underlying, self.expiry, self.right, self.short_strike)

    def long_symbol(self) -> str:
        return occ_symbol(self.underlying, self.expiry, self.right, self.long_strike)

    def closing_legs(self) -> list[dict]:
        """Reverse of the opening order: buy back the short, sell the long."""
        return [
            {"symbol": self.short_symbol(), "ratio_qty": "1", "side": "buy",
             "position_intent": "buy_to_close"},
            {"symbol": self.long_symbol(), "ratio_qty": "1", "side": "sell",
             "position_intent": "sell_to_close"},
        ]

    def realized_pnl(self, exit_price: float) -> float:
        """P&L in dollars.

        Credit spread: we took in `entry_credit` and pay `exit_price` to close.
        Debit spread:  we paid `entry_credit` and receive `exit_price` to close.
        """
        if self.is_credit:
            return (self.entry_credit - exit_price) * 100.0 * self.qty
        return (exit_price - self.entry_credit) * 100.0 * self.qty


class ExitDecision(BaseModel):
    action: Literal["hold", "close"]
    reason: str
    rule: str | None = None


class AgentDecision(BaseModel):
    """What the brain returns each cycle. `proposal` is None when it stands down."""
    regime: Literal["bull", "bear", "sideways"] = Field(
        description="Your read of the current regime for the traded universe"
    )
    reasoning: str = Field(description="Your analysis, for the journal and the demo")
    proposal: TradeProposal | None = Field(
        default=None, description="The trade to open, or null to stand down this cycle"
    )


class GateResult(BaseModel):
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'BLOCK'}] {self.name}: {self.detail}"
