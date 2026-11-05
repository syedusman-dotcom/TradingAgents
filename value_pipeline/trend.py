"""Trend-sleeve backtest engine: Turtle-style breakouts and 12-1 momentum on daily closes.

Long only, fractional shares, one unit per name. A signal is read at the close of day t and
traded at the ticker's next close (none within five rows cancels an entry). Equity compounds and
every size comes from the equity at the signal's close. Costs are charged on every buy and sell;
cash earns nothing. Nothing decided at day t reads a price after t: every rolling level used on
day t is built from closes up to t.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date

import numpy as np
import pandas as pd

from value_pipeline.config import DEFAULTS

SYSTEMS = {"breakout_55_20": (55, 20), "breakout_20_10": (20, 10), "momentum_12_1": None}

FILL_ROWS = 5          # an order not filled within this many rows after its signal is cancelled
PRIORITY_DAYS = 126    # breakout entries competing for slots or cash: the highest return over this many rows goes first
TREND_DAYS = 200       # SPY's (and momentum's) moving average, over the prior closes
MOMENTUM_FROM, MOMENTUM_TO = 252, 21   # momentum return from close[t-252] to close[t-21]


@dataclass(frozen=True)
class Settings:
    start: str
    equity: float
    risk_per_trade: float
    stop_n: float
    n_days: int
    max_positions: int
    max_position_weight: float
    cost_per_side: float
    market_filter: bool


def settings(cfg: dict, **overrides) -> Settings:
    """The ``trend`` section of a config (defaults filling any gap) with ``overrides`` on top."""
    values = {**DEFAULTS["trend"], **(cfg.get("trend") or {}), **overrides}
    return Settings(**{f.name: values[f.name] for f in fields(Settings)})


def _above_prior_average(frame, days: int) -> np.ndarray:
    """Close above the mean of the prior ``days`` closes (all present)."""
    average = frame.shift(1).rolling(days, min_periods=days).mean()
    return (frame > average).to_numpy()


def _market_ok(spy: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    spy = spy.astype(float).sort_index()
    ok = pd.Series(_above_prior_average(spy, TREND_DAYS), index=spy.index)
    return ok.reindex(index, fill_value=False).to_numpy(dtype=bool)


def _first_row_of_month(index: pd.DatetimeIndex, start: int) -> np.ndarray:
    """Rows from ``start`` on that open a calendar month among those rows (``start`` itself does)."""
    months = index.year * 12 + index.month
    out = np.zeros(len(index), dtype=bool)
    out[start:] = np.r_[True, months[start + 1:] != months[start:-1]] if start < len(index) else []
    return out


def simulate(system: str, closes: pd.DataFrame, spy: pd.Series, s: Settings,
             eligible: Callable[[str, date], bool] | None = None) -> dict:
    """Run ``system`` over ``closes`` (warm-up rows before ``s.start`` included).

    Returns ``{"equity", "exposure", "trades", "costs"}``; both series run from ``s.start``.
    Trades are the closed ones in exit order, then the positions still open (reason ``"open"``,
    marked at their last close without a sale cost).
    """
    if system not in SYSTEMS:
        raise ValueError(f"unknown trend system {system!r}; choose from {', '.join(SYSTEMS)}")
    closes = closes.sort_index().astype(float)
    index = pd.DatetimeIndex(closes.index)
    tickers = [str(t) for t in closes.columns]
    days = [ts.date() for ts in index]
    c = closes.to_numpy()
    valid = ~np.isnan(c)
    marked = closes.ffill().to_numpy()
    rows = len(index)
    start = int(index.searchsorted(pd.Timestamp(s.start)))
    market = _market_ok(spy, index) if s.market_filter else np.ones(rows, dtype=bool)
    ok = eligible or (lambda ticker, day: True)

    momentum = SYSTEMS[system] is None
    if momentum:
        score = (closes.shift(MOMENTUM_TO) / closes.shift(MOMENTUM_FROM) - 1).to_numpy()
        candidates = valid & _above_prior_average(closes, TREND_DAYS) & ~np.isnan(score)
        decision = _first_row_of_month(index, start)
    else:
        entry_days, exit_days = SYSTEMS[system]
        prior = closes.shift(1)
        high = prior.rolling(entry_days, min_periods=entry_days).max().to_numpy()
        low = prior.rolling(exit_days, min_periods=exit_days).min().to_numpy()
        n = closes.diff().abs().shift(1).rolling(s.n_days, min_periods=s.n_days).mean().to_numpy()
        priority = (closes / closes.shift(PRIORITY_DAYS) - 1).to_numpy()
        candidates = (c > high) & ~np.isnan(n)

    cost = s.cost_per_side
    cash = float(s.equity)
    costs = 0.0
    held: dict[int, dict] = {}          # column -> position
    buys: list[dict] = []               # pending entries, in fill priority
    sells: dict[int, str] = {}           # column -> reason, sold at its next close
    trades: list[dict] = []
    equity_out = np.empty(rows - start)
    exposure_out = np.empty(rows - start)

    def close_trade(col: int, pos: dict, row: int, price: float, reason: str, sale_cost: float) -> dict:
        return {
            "ticker": tickers[col], "signal_date": days[pos["signal"]], "entry_date": days[pos["entry"]],
            "entry_price": pos["price"], "shares": pos["shares"], "n": pos["n"],
            "exit_date": days[row], "exit_price": price, "reason": reason,
            "pnl": pos["shares"] * (price - pos["price"]) - pos["cost"] - sale_cost,
            "ret": price / pos["price"] - 1, "days": (days[row] - days[pos["entry"]]).days,
        }

    for i in range(start, rows):
        # 1. fills at today's close: sales first, then entries in priority order
        sold_today = set()
        for col in [col for col in sells if valid[i, col]]:
            reason = sells.pop(col)
            pos = held.pop(col)
            price = float(c[i, col])
            value = pos["shares"] * price
            fee = cost * value
            cash += value - fee
            costs += fee
            trades.append(close_trade(col, pos, i, price, reason, fee))
            sold_today.add(col)
        waiting = []
        for order in buys:
            col = order["col"]
            if not valid[i, col]:
                if i - order["signal"] < FILL_ROWS:
                    waiting.append(order)
                continue
            price = float(c[i, col])
            value = min(order["value"], s.max_position_weight * order["equity"], max(cash, 0.0) / (1 + cost))
            if order["shares"] is not None:
                value = min(value, order["shares"] * price)
            if value <= 0:
                continue
            fee = cost * value
            cash -= value + fee
            costs += fee
            held[col] = {"shares": value / price, "price": price, "entry": i, "signal": order["signal"],
                         "n": order["n"], "stop": price - s.stop_n * order["n"] if order["n"] is not None else None,
                         "cost": fee}
        buys = waiting

        # 2. mark to market (a missing close keeps the last one)
        invested = float(sum(pos["shares"] * marked[i, col] for col, pos in held.items()))
        equity = cash + invested
        equity_out[i - start] = equity
        exposure_out[i - start] = invested / equity if equity > 0 else 0.0

        # 3. decisions at today's close, filled from tomorrow
        busy = held.keys() | {order["col"] for order in buys} | sold_today
        if momentum:
            if not decision[i]:
                continue
            ranked = [col for col in np.flatnonzero(candidates[i]) if ok(tickers[col], days[i])]
            ranked.sort(key=lambda col: -score[i, col])
            target = ranked[: s.max_positions]
            for col in held:
                if col not in target and col not in sells:
                    sells[col] = "rebalance"
            if market[i]:
                for col in target:
                    if col not in busy:
                        buys.append({"col": col, "signal": i, "equity": equity, "n": None, "shares": None,
                                     "value": equity / s.max_positions})
            continue

        for col, pos in held.items():
            if col in sells or pos["entry"] == i or not valid[i, col]:   # exits read closes after the fill
                continue
            if c[i, col] <= pos["stop"]:
                sells[col] = "stop"
            elif c[i, col] < low[i, col]:
                sells[col] = "exit_low"
        free = s.max_positions - len(held) - len(buys)
        if free <= 0 or not market[i]:
            continue
        fresh = [col for col in np.flatnonzero(candidates[i]) if col not in busy and ok(tickers[col], days[i])]
        fresh.sort(key=lambda col: -priority[i, col] if not math.isnan(priority[i, col]) else math.inf)
        for col in fresh[:free]:
            risk = s.risk_per_trade * equity
            shares = risk / (s.stop_n * n[i, col]) if n[i, col] > 0 else math.inf
            buys.append({"col": col, "signal": i, "equity": equity, "n": float(n[i, col]), "shares": shares,
                         "value": math.inf})

    for col, pos in held.items():
        last = int(np.flatnonzero(valid[:, col])[-1])
        trades.append(close_trade(col, pos, last, float(c[last, col]), "open", 0.0))
    return {
        "equity": pd.Series(equity_out, index=index[start:], name="equity"),
        "exposure": pd.Series(exposure_out, index=index[start:], name="exposure"),
        "trades": trades,
        "costs": costs,
    }


def _years(series: pd.Series) -> float:
    return (series.index[-1] - series.index[0]).days / 365.25


def max_drawdown(series: pd.Series) -> float:
    """The largest fall from a running peak, as a positive fraction."""
    series = series.dropna()
    if series.empty:
        return 0.0
    return float((1 - series / series.cummax()).max())


def cagr(series: pd.Series) -> float:
    series = series.dropna()
    years = _years(series) if len(series) > 1 else 0.0
    if years <= 0 or series.iloc[0] <= 0:
        return 0.0
    return float((series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1)


def _calendar_years(series: pd.Series) -> dict[int, float]:
    out, base = {}, series.iloc[0]
    for year, part in series.groupby(series.index.year):
        out[int(year)] = float(part.iloc[-1] / base - 1)
        base = part.iloc[-1]
    return out


def _mean(values) -> float | None:
    return float(np.mean(values)) if values else None


def metrics(result: dict, spy: pd.Series) -> dict:
    """The report's measures for one simulation, with SPY over the identical span."""
    equity = result["equity"].astype(float)
    spy = spy.astype(float).sort_index().reindex(equity.index).ffill().bfill()
    years = _years(equity) if len(equity) > 1 else 0.0
    exposure = float(result["exposure"].mean()) if len(result["exposure"]) else 0.0

    closed = [t for t in result["trades"] if t["reason"] != "open"]
    wins = [t["ret"] for t in closed if t["pnl"] > 0]
    losses = [t["ret"] for t in closed if t["pnl"] <= 0]
    avg_win, avg_loss = _mean(wins), _mean(losses)
    gains = sorted((t["pnl"] for t in closed if t["pnl"] > 0), reverse=True)

    daily = equity.pct_change().dropna()
    volatility = float(daily.std() * math.sqrt(252)) if len(daily) > 1 else 0.0
    growth = cagr(equity)
    blend = (1 + exposure * spy.pct_change().fillna(0.0)).cumprod()
    system_years, spy_years = _calendar_years(equity), _calendar_years(spy)

    return {
        "cagr": growth,
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1),
        "max_drawdown": max_drawdown(equity),
        "volatility": volatility,
        "return_over_vol": growth / volatility if volatility > 0 else None,
        "exposure": exposure,
        "trades": len(closed),
        "trades_per_year": len(closed) / years if years > 0 else None,
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": avg_win / abs(avg_loss) if avg_win is not None and avg_loss else None,
        "avg_days": _mean([t["days"] for t in closed]),
        "costs_per_year": result["costs"] / years / equity.iloc[0] if years > 0 else None,
        "top5_share": sum(gains[:5]) / sum(gains) if gains else None,
        "years": {year: {"system": system_years[year], "spy": spy_years[year]} for year in system_years},
        "spy_cagr": cagr(spy),
        "spy_max_drawdown": max_drawdown(spy),
        "spy_at_exposure_cagr": cagr(blend),
        "years_gained": sum(r > 0 for r in system_years.values()) / len(system_years),
    }
