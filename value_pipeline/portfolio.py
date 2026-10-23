"""Holdings and trade history. Only the buy/sell/deposit commands change positions or cash.

The pipeline never places trades: you trade at your broker, then record the fill
here. Every write first copies the current files to a timestamped backup.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from value_pipeline.config import Paths

TRADE_FIELDS = ["date", "ticker", "action", "shares", "price", "fees", "note"]
# Fractional shares below this are rounding dust, not a position.
_DUST = 1e-9


@dataclass
class Position:
    ticker: str
    shares: float
    avg_price: float          # cost basis per share, fees included
    first_bought: str
    thesis: str = ""
    intrinsic_value_estimate: float | None = None
    last_rating: str | None = None
    last_review_date: str | None = None
    consecutive_underweight: int = 0
    kill_criteria: list[str] = field(default_factory=list)   # verifiable facts that would void the thesis
    entry_price: float | None = None                          # the agents' buy-below price at the last review
    next_earnings_date: str | None = None                      # ISO date, looked up at the last review
    lane: str = "value"                                        # value / compounder / inflection


@dataclass
class Portfolio:
    cash: float
    currency: str = "USD"
    positions: list[Position] = field(default_factory=list)
    # Money put in, as [{"date", "amount"}]; the SPY comparison replays these.
    deposits: list[dict] = field(default_factory=list)

    def position(self, ticker: str) -> Position | None:
        ticker = ticker.upper()
        return next((p for p in self.positions if p.ticker == ticker), None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Portfolio:
        return cls(
            cash=float(data["cash"]),
            currency=data.get("currency", "USD"),
            positions=[Position(**p) for p in data.get("positions", [])],
            deposits=list(data.get("deposits", [])),
        )

    def to_context(self):
        """The holdings as the agents' PortfolioContext, so every run knows what you own."""
        from tradingagents.portfolio import PortfolioContext

        return PortfolioContext.model_validate({
            "cash": self.cash,
            "currency": self.currency,
            "positions": [
                {"ticker": p.ticker, "quantity": p.shares, "average_price": p.avg_price}
                for p in self.positions
            ],
        })


class TradeError(ValueError):
    """A recorded trade that cannot have happened (more shares sold than held, ...)."""


def _finite(**values: float) -> None:
    """NaN slips through every comparison (nan > x is False), so it would pass the cash checks."""
    for name, v in values.items():
        if not math.isfinite(v):
            raise TradeError(f"{name} must be a finite number, not {v}")


def apply_buy(pf: Portfolio, ticker: str, shares: float, price: float, fees: float = 0.0,
              on: str | None = None, thesis: str = "", intrinsic_value: float | None = None,
              kill_criteria: list[str] | None = None, entry_price: float | None = None,
              lane: str = "value") -> Position:
    ticker = ticker.upper()
    _finite(shares=shares, price=price, fees=fees)
    if shares <= 0 or price <= 0 or fees < 0:
        raise TradeError("shares and price must be positive, fees not negative")
    cost = shares * price + fees
    if cost > pf.cash + 1e-6:
        raise TradeError(
            f"buying costs {cost:,.2f} but cash is {pf.cash:,.2f}; record a deposit first"
        )
    pf.cash -= cost
    held = pf.position(ticker)
    if held is None:
        held = Position(ticker=ticker, shares=shares, avg_price=cost / shares,
                        first_bought=on or date.today().isoformat(), thesis=thesis,
                        intrinsic_value_estimate=intrinsic_value,
                        kill_criteria=list(kill_criteria or []), entry_price=entry_price,
                        lane=lane or "value")
        pf.positions.append(held)
    else:
        held.avg_price = (held.shares * held.avg_price + cost) / (held.shares + shares)
        held.shares += shares
        if thesis and not held.thesis:
            held.thesis = thesis
        if intrinsic_value is not None and held.intrinsic_value_estimate is None:
            held.intrinsic_value_estimate = intrinsic_value
    return held


def apply_sell(pf: Portfolio, ticker: str, shares: float, price: float, fees: float = 0.0) -> float:
    """Sell shares; returns the realised gain. Average price is unchanged by a sale."""
    ticker = ticker.upper()
    held = pf.position(ticker)
    if held is None:
        raise TradeError(f"no position in {ticker}")
    _finite(shares=shares, price=price, fees=fees)
    if shares <= 0 or price <= 0 or fees < 0:
        raise TradeError("shares and price must be positive, fees not negative")
    if shares > held.shares + _DUST:
        raise TradeError(f"selling {shares:g} {ticker} but only {held.shares:g} held")
    proceeds = shares * price - fees
    pf.cash += proceeds
    held.shares -= shares
    if held.shares <= _DUST:
        pf.positions.remove(held)
    return proceeds - shares * held.avg_price


def apply_deposit(pf: Portfolio, amount: float, on: str | None = None) -> None:
    _finite(amount=amount)
    if amount == 0:
        raise TradeError("amount must not be zero")
    if pf.cash + amount < -1e-6:
        raise TradeError(f"withdrawing {-amount:,.2f} but cash is {pf.cash:,.2f}")
    pf.cash += amount
    pf.deposits.append({"date": on or date.today().isoformat(), "amount": amount})


# --- files -----------------------------------------------------------------

def backup(paths: Paths) -> Path | None:
    """Copy portfolio.json and trades.csv into backups/<timestamp>/ before a write."""
    existing = [p for p in (paths.portfolio, paths.trades) if p.exists()]
    if not existing:
        return None
    dest = paths.backups / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest.mkdir(parents=True, exist_ok=True)
    for p in existing:
        shutil.copy2(p, dest / p.name)
    return dest


def load(paths: Paths) -> Portfolio:
    if not paths.portfolio.exists():
        raise FileNotFoundError(
            f"no portfolio at {paths.portfolio}; start one with: python -m value_pipeline init --cash 1000"
        )
    return Portfolio.from_dict(json.loads(paths.portfolio.read_text(encoding="utf-8")))


def save(paths: Paths, pf: Portfolio) -> Path | None:
    """Write portfolio.json; returns the backup of the previous files (None on the first write)."""
    made = backup(paths)
    paths.root.mkdir(parents=True, exist_ok=True)
    tmp = paths.portfolio.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pf.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(paths.portfolio)
    return made


def append_trade(paths: Paths, row: dict) -> None:
    new = not paths.trades.exists()
    paths.root.mkdir(parents=True, exist_ok=True)
    with paths.trades.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def read_trades(paths: Paths) -> list[dict]:
    if not paths.trades.exists():
        return []
    with paths.trades.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
