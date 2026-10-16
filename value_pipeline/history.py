"""Portfolio value over time against SPY bought with the same cash flows, for the dashboard chart."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from value_pipeline import market_data, portfolio as pfmod
from value_pipeline.config import Paths

log = logging.getLogger(__name__)


def _asof(series: pd.Series | None, day: date) -> float | None:
    if series is None or series.empty:
        return None
    before = series[series.index <= day]
    return float(before.iloc[-1]) if not before.empty else None


def value_series(deposits: list[dict], trades: list[dict], closes: dict[str, pd.Series],
                 spy: pd.Series, days: list[date]) -> list[dict]:
    """Value of the portfolio and of the SPY shadow on each day in ``days``."""
    events = sorted(
        [(d["date"], "deposit", d) for d in deposits] + [(t["date"], "trade", t) for t in trades],
        key=lambda e: e[0],
    )
    cash = spy_cash = spy_shares = 0.0
    shares: dict[str, float] = {}
    last_price: dict[str, float] = {}
    out, i = [], 0
    for day in days:
        while i < len(events) and date.fromisoformat(events[i][0]) <= day:
            when, kind, e = events[i]
            if kind == "deposit":
                cash += float(e["amount"])
                spy_cash += float(e["amount"])
            else:
                n, price, fees = float(e["shares"]), float(e["price"]), float(e.get("fees") or 0)
                sign = 1 if e["action"] == "BUY" else -1
                amount = n * price + fees if sign == 1 else n * price - fees
                shares[e["ticker"]] = shares.get(e["ticker"], 0.0) + sign * n
                last_price[e["ticker"]] = price
                cash -= sign * amount
                spy_price = _asof(spy, date.fromisoformat(when))
                if spy_price:
                    spy_shares += sign * amount / spy_price
                    spy_cash -= sign * amount
            i += 1
        held = sum(n * (_asof(closes.get(t), day) or last_price.get(t, 0.0)) for t, n in shares.items())
        out.append({"date": day.isoformat(), "portfolio": cash + held,
                    "spy": spy_cash + spy_shares * (_asof(spy, day) or 0.0)})
    return out


def fetch_value_series(paths: Paths, step_days: int = 7) -> list[dict]:
    """The series from the first deposit to today, every ``step_days``; [] when prices are unavailable."""
    try:
        pf = pfmod.load(paths)
        trades = pfmod.read_trades(paths)
        dates = [d["date"] for d in pf.deposits] + [t["date"] for t in trades]
        if not dates:
            return []
        start = min(dates)      # a stock owned before the first deposit (recorded with its real date) counts
        closes = {t: market_data.closes_between(t, start) for t in sorted({t["ticker"] for t in trades})}
        spy = market_data.closes_between("SPY", start)
        first, today = date.fromisoformat(start), date.today()
        days = [first + timedelta(days=k) for k in range(0, (today - first).days, step_days)] + [today]
        return value_series(pf.deposits, trades, closes, spy, days)
    except Exception as exc:  # noqa: BLE001 - the chart is optional
        log.warning("value history unavailable: %s", exc)
        return []
