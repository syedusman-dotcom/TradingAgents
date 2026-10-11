"""Compare the portfolio with SPY bought and sold with the same cash flows on the same dates."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from value_pipeline import market_data

log = logging.getLogger(__name__)


def cash_flows(deposits: list[dict], trades: list[dict]) -> list[tuple[str, str, float]]:
    """(date, kind, amount) events: deposits, money spent on buys, money received from sells."""
    events = [(d["date"], "deposit", float(d["amount"])) for d in deposits]
    for t in trades:
        gross = float(t["shares"]) * float(t["price"])
        fees = float(t.get("fees") or 0)
        if t["action"] == "BUY":
            events.append((t["date"], "buy", gross + fees))
        elif t["action"] == "SELL":
            events.append((t["date"], "sell", gross - fees))
    return sorted(events, key=lambda e: e[0])


def replay(events: list[tuple[str, str, float]], price_on: Callable[[str], float], price_now: float) -> dict:
    """Value of the shadow SPY portfolio: every buy buys SPY, every sell sells SPY, same dollars."""
    shares = cash = invested = 0.0
    for day, kind, amount in events:
        if kind == "deposit":
            cash += amount
            invested += amount
        elif kind == "buy":
            shares += amount / price_on(day)
            cash -= amount
        elif kind == "sell":
            shares -= amount / price_on(day)
            cash += amount
    return {"value": shares * price_now + cash, "invested": invested}


def compare(portfolio_value: float, deposits: list[dict], trades: list[dict], ticker: str = "SPY") -> dict | None:
    """Portfolio and SPY returns on the money put in, or None when SPY prices are unavailable."""
    events = cash_flows(deposits, trades)
    if not events:
        return None
    try:
        closes = market_data.closes_between(ticker, events[0][0])
        if closes.empty:
            return None

        def price_on(day: str) -> float:
            return float(closes[closes.index <= date.fromisoformat(day)].iloc[-1])

        shadow = replay(events, price_on, float(closes.iloc[-1]))
    except Exception as exc:  # noqa: BLE001 - the comparison is informative, never fatal
        log.warning("SPY comparison unavailable: %s", exc)
        return None
    invested = shadow["invested"]
    if invested <= 0:
        return None
    return {
        "since": events[0][0],
        "invested": invested,
        "portfolio_value": portfolio_value,
        "portfolio_return": portfolio_value / invested - 1,
        "spy_value": shadow["value"],
        "spy_return": shadow["value"] / invested - 1,
    }
