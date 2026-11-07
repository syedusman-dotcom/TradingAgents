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

TRADE_FIELDS = ["date", "ticker", "action", "shares", "price", "fees", "note", "currency", "fx"]
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
    currency: str | None = None                                # quote currency (None: dollars)
    avg_fx: float = 1.0                                        # dollars paid per quoted unit, averaged

    @property
    def cost_usd(self) -> float:
        """Dollars paid for the position, fees included."""
        return self.shares * self.avg_price * self.avg_fx

    @property
    def foreign(self) -> bool:
        """Quoted outside dollars, or bought at a rate other than par."""
        return self.currency not in (None, "USD") or self.avg_fx != 1.0


def position_rate(position: Position | None, quote: tuple[str | None, float | None]) -> float | None:
    """Dollars per quoted unit for a holding, from ``market_data.quote_fx``'s answer.

    ``(None, 1.0)`` means the currency could not be read: its 1.0 is a placeholder, so a foreign
    holding gets None (valued at cost, sized by hand) and only a dollar holding (or no holding) 1.0.
    """
    currency, rate = quote
    if currency is None and position is not None and position.foreign:
        return None
    return rate


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
                {"ticker": p.ticker, "quantity": p.shares, "average_price": p.avg_price,
                 "kill_criteria": list(p.kill_criteria)}
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
              lane: str = "value", currency: str | None = None, fx: float = 1.0) -> Position:
    """Buy in the quote unit: price is per quoted unit, fx is dollars per unit, fees are dollars."""
    ticker = ticker.upper()
    _finite(shares=shares, price=price, fees=fees, fx=fx)
    if shares <= 0 or price <= 0 or fees < 0 or fx <= 0:
        raise TradeError("shares, price and fx must be positive, fees not negative")
    cost = shares * price * fx + fees            # dollars
    if cost > pf.cash + 1e-6:
        raise TradeError(
            f"buying costs {cost:,.2f} but cash is {pf.cash:,.2f}; record a deposit first"
        )
    pf.cash -= cost
    quote_cost = shares * price + fees / fx      # quote units
    held = pf.position(ticker)
    if held is None:
        held = Position(ticker=ticker, shares=shares, avg_price=quote_cost / shares,
                        first_bought=on or date.today().isoformat(), thesis=thesis,
                        intrinsic_value_estimate=intrinsic_value,
                        kill_criteria=list(kill_criteria or []), entry_price=entry_price,
                        lane=lane or "value", currency=currency, avg_fx=cost / quote_cost)
        pf.positions.append(held)
    else:
        total_quote = held.shares * held.avg_price + quote_cost
        held.avg_fx = (held.cost_usd + cost) / total_quote
        held.avg_price = total_quote / (held.shares + shares)
        held.currency = held.currency or currency
        held.shares += shares
        if thesis and not held.thesis:
            held.thesis = thesis
        if intrinsic_value is not None and held.intrinsic_value_estimate is None:
            held.intrinsic_value_estimate = intrinsic_value
    return held


def apply_sell(pf: Portfolio, ticker: str, shares: float, price: float, fees: float = 0.0,
               fx: float = 1.0) -> float:
    """Sell shares; returns the realised gain in dollars. Average price is unchanged by a sale."""
    ticker = ticker.upper()
    held = pf.position(ticker)
    if held is None:
        raise TradeError(f"no position in {ticker}")
    _finite(shares=shares, price=price, fees=fees, fx=fx)
    if shares <= 0 or price <= 0 or fees < 0 or fx <= 0:
        raise TradeError("shares, price and fx must be positive, fees not negative")
    if shares > held.shares + _DUST:
        raise TradeError(f"selling {shares:g} {ticker} but only {held.shares:g} held")
    proceeds = shares * price * fx - fees
    pf.cash += proceeds
    held.shares -= shares
    if held.shares <= _DUST:
        pf.positions.remove(held)
    return proceeds - shares * held.avg_price * held.avg_fx


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


def row_fx(row: dict) -> float:
    """A trade row's dollars-per-unit rate; rows from before the column (or blank) are dollars."""
    try:
        return float(row.get("fx") or 1.0)
    except ValueError:
        return 1.0


def _upgrade_trades(paths: Paths) -> None:
    """Rewrite a trades.csv with an older header to the current one, whole, then swap it in."""
    with paths.trades.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == TRADE_FIELDS:
            return
        rows = list(reader)
    tmp = paths.trades.with_name(paths.trades.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in TRADE_FIELDS})
    tmp.replace(paths.trades)


def append_trade(paths: Paths, row: dict) -> None:
    new = not paths.trades.exists()
    paths.root.mkdir(parents=True, exist_ok=True)
    if not new:
        _upgrade_trades(paths)
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
