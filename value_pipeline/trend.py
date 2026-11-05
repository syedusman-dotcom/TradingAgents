"""Trend-sleeve backtest engine: Turtle-style breakouts and 12-1 momentum on daily closes.

Long only, fractional shares, one unit per name. A signal is read at the close of day t and
traded at the ticker's next close (none within five rows cancels an entry). Equity compounds and
every size comes from the equity at the signal's close. Costs are charged on every buy and sell;
cash earns nothing. Nothing decided at day t reads a price after t: every rolling level used on
day t is built from closes up to t.

``run`` loads today's S&P 500 and S&P 400 members' adjusted closes, runs every system on the
``all`` and ``quality`` universes, the sensitivity grid and the fixed verdict; ``write`` saves the
report (Markdown, HTML) and the trades under ``reports/backtest``. Nothing trades.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from tradingagents.dataflows.files import replace_file
from value_pipeline import backtest, market_data, render
from value_pipeline.config import DEFAULTS, Paths

SYSTEMS = {"breakout_55_20": (55, 20), "breakout_20_10": (20, 10), "momentum_12_1": None}

FILL_ROWS = 5          # an order not filled within this many rows after its signal is cancelled
PRIORITY_DAYS = 126    # breakout entries competing for slots or cash: the highest return over this many rows goes first
TREND_DAYS = 200       # SPY's (and momentum's) moving average, over the prior closes
MOMENTUM_FROM, MOMENTUM_TO = 252, 21   # momentum return from close[t-252] to close[t-21]
MIN_ORDER = 0.01       # dollars: an entry the cash cannot fund to a cent is dropped


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
            if value < MIN_ORDER:            # nothing left to fund it (float residue is not a position)
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
    if series.empty:
        return {}
    out, base = {}, series.iloc[0]
    for year, part in series.groupby(series.index.year):
        out[int(year)] = float(part.iloc[-1] / base - 1)
        base = part.iloc[-1]
    return out


def _mean(values) -> float | None:
    return float(np.mean(values)) if values else None


def _on_or_before(spy: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """SPY on every day of ``index``: its last close on or before that day (none before its first)."""
    spy = spy.astype(float).sort_index().dropna()
    spy = spy[~spy.index.duplicated(keep="last")]
    return spy.reindex(spy.index.union(index)).ffill().reindex(index).dropna()


def _net_return(trade: dict) -> float:
    """The trade's profit after both costs over its entry value."""
    value = trade["shares"] * trade["entry_price"]
    return trade["pnl"] / value if value else 0.0


def metrics(result: dict, spy: pd.Series) -> dict:
    """The report's measures for one simulation, with SPY over the identical span. Win rate,
    average win and loss and the payoff ratio use each trade's return after costs."""
    equity = result["equity"].astype(float)
    spy = _on_or_before(spy, pd.DatetimeIndex(equity.index))
    years = _years(equity) if len(equity) > 1 else 0.0
    exposure = float(result["exposure"].mean()) if len(result["exposure"]) else 0.0

    closed = [t for t in result["trades"] if t["reason"] != "open"]
    returns = [_net_return(t) for t in closed]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
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
        "years": {year: {"system": system_years[year], "spy": spy_years.get(year)} for year in system_years},
        "spy_cagr": cagr(spy),
        "spy_max_drawdown": max_drawdown(spy),
        "spy_at_exposure_cagr": cagr(blend),
        "years_gained": sum(r > 0 for r in system_years.values()) / len(system_years),
    }


# --- data ------------------------------------------------------------------------------------

BENCHMARK = "SPY"
INDEXES = ["sp500", "sp400"]   # today's members of these two only
UNIVERSES = ("all", "quality")
WARM_ROWS = 300                # trading days loaded before the start, so every signal exists on day one
GAP_ROWS = 5                   # missing closes inside a history, at most this many in a row, carry the last close


def _adj_closes(hist: pd.DataFrame) -> pd.Series:
    series = pd.Series(hist["adj_close"].to_numpy(dtype=float),
                       index=pd.DatetimeIndex(hist["date"]).normalize())
    return series[~series.index.duplicated(keep="last")]


def _fill_gaps(closes: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Each run of at most ``GAP_ROWS`` missing closes between a ticker's first and last close
    takes the close before it; longer runs, and the rows before the first or after the last
    close, stay empty. Returns the frame and the cells filled per ticker (those with any)."""
    out, filled = {}, {}
    for ticker in closes.columns:
        series = closes[ticker]
        present = series.notna()
        inside = present.cummax() & present[::-1].cummax()[::-1]
        gap = ~present & inside
        run_length = gap.groupby(present.cumsum()).transform("sum")
        fill = gap & (run_length <= GAP_ROWS)
        if fill.any():
            series = series.where(~fill, series.ffill())
            filled[str(ticker)] = int(fill.sum())
        out[ticker] = series
    return pd.DataFrame(out, index=closes.index, columns=closes.columns), filled


def load_closes(paths: Paths, tickers: list[str], start: str, fetch: bool = True,
                progress=None) -> tuple[pd.DataFrame, pd.Series]:
    """Adjusted closes (splits and dividends in) of ``tickers`` on SPY's trading days from
    ``WARM_ROWS`` days before ``start``, from the backtest price cache (``fetch`` fills a missing
    cache from Yahoo and refetches one that ends more than ``PRICE_SLACK_DAYS`` before SPY's last
    close), and SPY's adjusted closes on the same days. Short gaps inside a history are filled
    (see ``_fill_gaps``). ``attrs["filled"]`` holds the cells filled per ticker,
    ``attrs["missing"]`` the tickers with no close in the span and ``attrs["short"]`` those whose
    closes still end more than ``PRICE_SLACK_DAYS`` before SPY's; none is a column."""
    spy_hist = backtest.price_history(BENCHMARK, paths, fetch=fetch)
    if spy_hist is None:
        raise ValueError(f"no {BENCHMARK} price history in the backtest cache; run without --no-fetch")
    spy = _adj_closes(spy_hist).dropna()
    first = max(0, int(spy.index.searchsorted(pd.Timestamp(start))) - WARM_ROWS)
    spy = spy.iloc[first:]
    stale = (spy.index[-1] - timedelta(days=backtest.PRICE_SLACK_DAYS)).date().isoformat()
    columns, missing, short = {}, [], []
    for i, ticker in enumerate(tickers, start=1):
        if progress:
            progress(i, len(tickers), ticker)
        path = backtest._price_path(ticker, paths)
        fetched = fetch and backtest._needs_fetch(backtest._read_prices(path) if path.exists() else None,
                                                  False, stale)
        hist = backtest.price_history(ticker, paths, fetch=fetch, stale_before=stale)
        if fetched:
            market_data.throttle(DEFAULTS["screen"]["throttle_seconds"])
        series = None if hist is None else _adj_closes(hist).reindex(spy.index)
        if series is None or series.isna().all():
            missing.append(ticker)
            continue
        columns[ticker] = series
        if series.last_valid_index() < pd.Timestamp(stale):
            short.append(ticker)
    closes, filled = _fill_gaps(pd.DataFrame(columns, index=spy.index, dtype=float))
    closes.attrs = {"filled": filled, "missing": missing, "short": short}
    return closes, spy


# --- universes -------------------------------------------------------------------------------

def quality_windows(rows_by_cohort: dict[str, list[dict]], end: str) -> dict[str, list[tuple[date, date]]]:
    """Per ticker, the half-open spans ``[cohort, next cohort)`` in which its cohort row passed
    the screen's filters (the last cohort's span runs to ``end``); back-to-back spans are joined."""
    cohorts = sorted(rows_by_cohort)
    bounds = [date.fromisoformat(c) for c in cohorts] + [date.fromisoformat(end)]
    out: dict[str, list[tuple[date, date]]] = {}
    for i, cohort in enumerate(cohorts):
        lo, hi = bounds[i], bounds[i + 1]
        for row in rows_by_cohort[cohort]:
            if not row.get("passes_filters"):
                continue
            spans = out.setdefault(str(row["ticker"]), [])
            if spans and spans[-1][1] == lo:
                spans[-1] = (spans[-1][0], hi)
            else:
                spans.append((lo, hi))
    return out


def _eligibility(added: dict[str, str], windows: dict | None = None) -> Callable[[str, date], bool]:
    """On or after the ticker's S&P 500 join date when one is known, and with ``windows``, inside
    one of its quality spans."""
    def ok(ticker: str, day: date) -> bool:
        joined = added.get(ticker)
        if joined and day.isoformat() < joined:
            return False
        return windows is None or any(lo <= day < hi for lo, hi in windows.get(ticker, ()))
    return ok


# --- verdict ---------------------------------------------------------------------------------

PRIMARY = "breakout_55_20"
ALTERNATIVE = "momentum_12_1"
HIGH_COST = 0.006           # the verdict's (b): the same test at this cost a side
BEAT_BY = 0.01              # (a) CAGR at least SPY's plus this...
TRAIL_BY = 0.02             # ...or at least SPY's minus this
DRAWDOWN_SHARE = 2 / 3      # ...with a maximum drawdown no more than this share of SPY's
YEARS_GAINED = 0.6          # (c) a gain in at least this share of the calendar years
EPS = 1e-9                  # the bounds are inclusive; this only absorbs float rounding

SENSITIVITY = [("max_positions", [5, 10, 20]), ("cost_per_side", [0.0, 0.0035, 0.006]),
               ("market_filter", [True, False]), ("risk_per_trade", [0.005, 0.01, 0.02])]


def _return_or_drawdown(m: dict) -> bool:
    spy = m["spy_cagr"]
    return (m["cagr"] >= spy + BEAT_BY - EPS
            or (m["cagr"] >= spy - TRAIL_BY - EPS
                and m["max_drawdown"] <= DRAWDOWN_SHARE * m["spy_max_drawdown"] + EPS))


def verdict(primary: dict, high_cost: dict) -> dict:
    """The spec's fixed rule: (a) CAGR at least SPY's plus 1 point, or at least SPY's minus 2
    points with a maximum drawdown no more than two thirds of SPY's; (b) the same at
    ``cost_per_side`` 0.006 (``high_cost``); (c) a gain in at least 60% of the calendar years."""
    checks = {"a": _return_or_drawdown(primary), "b": _return_or_drawdown(high_cost),
              "c": primary["years_gained"] >= YEARS_GAINED - EPS}
    supports = all(checks.values())
    numbers = (f"CAGR {primary['cagr']:.1%} against SPY's {primary['spy_cagr']:.1%}, maximum drawdown "
               f"{primary['max_drawdown']:.1%} against SPY's {primary['spy_max_drawdown']:.1%}, a gain in "
               f"{primary['years_gained']:.0%} of the calendar years")
    high = f"at {HIGH_COST:.1%} a side CAGR {high_cost['cagr']:.1%}"
    if supports:
        sentence = f"The result supports a live sleeve: {numbers}; {high}, still passing."
    else:
        failed = {"a": "(a) neither 1 point above SPY's CAGR nor within 2 points of it with at most "
                       "two thirds of its drawdown",
                  "b": f"(b) the same test fails {high}",
                  "c": f"(c) fewer than {YEARS_GAINED:.0%} of the calendar years gained"}
        sentence = (f"The result does not support a live sleeve: {numbers}; {high}. Failed: "
                    + "; ".join(failed[k] for k, ok in checks.items() if not ok) + ".")
    return {"supports": supports, "sentence": sentence, "checks": checks,
            "cagr": primary["cagr"], "spy_cagr": primary["spy_cagr"],
            "max_drawdown": primary["max_drawdown"], "spy_max_drawdown": primary["spy_max_drawdown"],
            "years_gained": primary["years_gained"], "high_cost_cagr": high_cost["cagr"]}


# --- the run ---------------------------------------------------------------------------------

def run(paths: Paths, cfg: dict, today: date | None = None, fetch: bool = True, progress=None) -> dict:
    """Every system on both universes at the default settings, the sensitivity grid on the
    primary (quality universe) and the verdicts. The cohort rows and S&P 500 join dates come from
    one lane-backtest run over the S&P 500 and S&P 400; ``fetch=False`` reads the caches only.
    Nothing trades; no agent is called."""
    today = today or date.today()
    s = settings(cfg)
    members = market_data.universe(paths, list(INDEXES))
    tickers = sorted({str(t) for t in members["ticker"]})
    cfg_us = copy.deepcopy(cfg)
    cfg_us.setdefault("universe", {})["indexes"] = list(INDEXES)
    lane = backtest.run(paths, cfg_us, today=today, fetch=fetch, progress=progress, keep_rows=True,
                        universe_fn=lambda _paths, _indexes: members)
    closes, spy = load_closes(paths, tickers, s.start, fetch=fetch, progress=progress)
    end = closes.index[-1].date()
    added = lane.get("added") or {}
    windows = quality_windows(lane["rows"], (end + timedelta(days=1)).isoformat())
    eligible = {"all": _eligibility(added), "quality": _eligibility(added, windows)}

    def once(system: str, universe: str, **overrides) -> tuple[dict, dict]:
        out = simulate(system, closes, spy, settings(cfg, **overrides), eligible[universe])
        return out, metrics(out, spy)

    results, trades = {}, {}
    for system in SYSTEMS:
        for universe in UNIVERSES:
            out, results[(system, universe)] = once(system, universe)
            trades[(system, universe)] = out["trades"]
    sensitivity = [{"setting": name, "value": value, **once(PRIMARY, "quality", **{name: value})[1]}
                   for name, values in SENSITIVITY for value in values]
    verdicts = {system: verdict(results[(system, "quality")],
                                once(system, "quality", cost_per_side=HIGH_COST)[1])
                for system in (PRIMARY, ALTERNATIVE)}
    filled = closes.attrs.get("filled", {})
    return {
        "generated": today.isoformat(), "start": s.start, "end": end.isoformat(), "settings": s,
        "universe_sizes": {"all": len(closes.columns),
                           "quality": sum(str(t) in windows for t in closes.columns)},
        "results": results, "trades": trades, "sensitivity": sensitivity, "verdicts": verdicts,
        "filled": {"cells": sum(filled.values()), "tickers": len(filled)},
        "missing": list(closes.attrs.get("missing", [])),
        "short": list(closes.attrs.get("short", [])),
        "quality_from": min(lane["rows"], default=None), "joins_known": bool(added),
    }


# --- report ----------------------------------------------------------------------------------

TRADE_COLUMNS = ["system", "universe", "ticker", "signal_date", "entry_date", "entry_price", "shares", "n",
                 "exit_date", "exit_price", "reason", "pnl", "ret", "days"]


def _pct(x, digits: int = 1) -> str:
    return "–" if x is None else f"{x:.{digits}%}"


def _num(x, digits: int = 1) -> str:
    return "–" if x is None else f"{x:,.{digits}f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _value(name: str, value) -> str:
    if name == "market_filter":
        return "on" if value else "off"
    if name in ("cost_per_side", "risk_per_trade"):
        return _pct(value, 2)
    return str(value)


def _limits(result: dict) -> list[str]:
    s, sizes = result["settings"], result["universe_sizes"]
    joins = ("An S&P 500 name counts from its join date; the S&P 400 publishes no join dates, so its "
             "members count from the start" if result.get("joins_known", True) else
             "The S&P 500 join dates could not be read, so every name counts from the start")
    missing = result.get("missing") or []
    filled = result.get("filled") or {"cells": 0, "tickers": 0}
    lines = [
        f"- **Survivorship.** The universe is today's S&P 500 and S&P 400 members only ({sizes['all']} with "
        "prices). Names that left either index, were bought out or failed are not in the sample, so "
        f"survivorship bias favours the result. {joins}.",
        f"- **Quality universe** ({sizes['quality']} names): those whose 30 June cohort row of the lane "
        "backtest passed the screen's filters, from that cohort date to the next one (the last to the end "
        f"of the data); the first cohort is {result.get('quality_from') or 'none'}. A name without a cohort "
        "row (no EDGAR statements, an excluded sector) is outside it.",
        f"- **N is close-only**: the mean absolute change between daily closes over {s.n_days} days (no "
        "highs and lows); the stop is read on closes.",
        "- **Fills are at the next close** after the signal's close; an entry not filled within "
        f"{FILL_ROWS} trading days is cancelled. Nothing decided at a close reads a later price.",
        "- **Shares are fractional**, one unit per name, no adds. Nothing is shorted, nothing is "
        "leveraged; cash earns nothing.",
        f"- **Costs**: {_pct(s.cost_per_side, 2)} of the trade value on every buy and every sell.",
        "- **Prices**: adjusted closes (splits and dividends in) from the backtest price cache; SPY "
        "bought and held on its adjusted closes over the identical span is the benchmark.",
        f"- **Gaps**: {_plural(filled['cells'], 'missing close')} in {_plural(filled['tickers'], 'ticker')} "
        f"took the close before them (runs of at most {GAP_ROWS} trading days inside a ticker's history); "
        "longer gaps stay empty and block that name's signals. A filled day carries the prior close, so "
        "an order due that day fills at that carried price.",
        f"- **Cash runs out before the slots**: with each position capped at {_pct(s.max_position_weight, 0)} "
        "of equity, about five full positions use the cash, so later entries are small leftovers that still "
        "take a slot; the `max_positions` 5 row of the sensitivity table shows the system without them.",
    ]
    short = result.get("short") or []
    if short:
        shown = ", ".join(short[:30]) + (" …" if len(short) > 30 else "")
        lines.append(f"- **Stale prices**: {_plural(len(short), 'ticker')} end more than "
                     f"{backtest.PRICE_SLACK_DAYS} days before SPY's last close ({shown}); a position "
                     "held at their last close cannot be sold and keeps its slot. Run without --no-fetch.")
    if missing:
        shown = ", ".join(missing[:30]) + (" …" if len(missing) > 30 else "")
        lines.append(f"- **No prices** in the span for {_plural(len(missing), 'ticker')}: {shown}.")
    return lines


SUMMARY_HEADER = ("| System | Universe | CAGR | SPY | SPY at exposure | Total return | Max drawdown | SPY drawdown "
                  "| Volatility | Return/vol | Invested | Trades | Trades/yr | Win rate | Avg win | Avg loss "
                  "| Payoff | Avg days | Costs/yr | Top-5 share | Years gained |")


def _summary_row(system: str, universe: str, m: dict) -> str:
    cells = [system, universe, _pct(m["cagr"]), _pct(m["spy_cagr"]), _pct(m["spy_at_exposure_cagr"]),
             _pct(m["total_return"], 0), _pct(m["max_drawdown"]), _pct(m["spy_max_drawdown"]),
             _pct(m["volatility"]), _num(m["return_over_vol"], 2), _pct(m["exposure"], 0), str(m["trades"]),
             _num(m["trades_per_year"]), _pct(m["win_rate"], 0), _pct(m["avg_win"]), _pct(m["avg_loss"]),
             _num(m["payoff"], 2), _num(m["avg_days"], 0), _pct(m["costs_per_year"], 2),
             _pct(m["top5_share"], 0), _pct(m["years_gained"], 0)]
    return "| " + " | ".join(cells) + " |"


def render_report(result: dict) -> str:
    """The report: span, settings and limits; the verdicts; the summary; the primary's calendar
    years against SPY; the sensitivity of the primary on the quality universe."""
    s = result["settings"]
    settings_line = ", ".join(f"{k} {_value(k, v)}" for k, v in asdict(s).items() if k != "start")
    lines = [f"# Trend-sleeve backtest, {result['generated']}", "",
             f"Daily closes from {result['start']} to {result['end']}; the systems {', '.join(SYSTEMS)} on the "
             f"`all` and `quality` universes. Settings: {settings_line}. Nothing trades; this is a record of "
             "what the rules would have done.", "", "## Limits", "", *_limits(result), "", "## Verdict", "",
             "The rule was fixed before the run: (a) CAGR at least SPY's plus 1 point, or at least SPY's "
             "minus 2 points with a maximum drawdown no more than two thirds of SPY's; (b) the same test at "
             f"{HIGH_COST:.1%} a side; (c) a gain in at least {YEARS_GAINED:.0%} of the calendar years "
             "(part-years count). A supporting verdict starts a design for live signals; it turns nothing on.",
             ""]
    for system, v in result["verdicts"].items():
        label = "primary" if system == PRIMARY else "alternative"
        lines += [f"- **{system}** ({label}, quality universe): {v['sentence']}"]
    lines += ["", "## Summary", "", SUMMARY_HEADER, "|" + "---|" * SUMMARY_HEADER.count(" | ") + "---|"]
    lines += [_summary_row(system, universe, m) for (system, universe), m in result["results"].items()]
    lines += ["", "SPY at exposure: SPY held at the system's average share invested, the rest in cash. "
              "Win rate, average win and loss and payoff use each trade's return after costs; open "
              "positions at the end are left out of the trade counts.", "",
              f"## Calendar years: {PRIMARY} on the quality universe", "",
              "| Year | System | SPY |", "|---|--:|--:|"]
    for year, y in result["results"][(PRIMARY, "quality")]["years"].items():
        lines.append(f"| {year} | {_pct(y['system'])} | {_pct(y['spy'])} |")
    lines += ["", f"## Sensitivity: {PRIMARY} on the quality universe, one setting at a time", "",
              "| Setting | Value | CAGR | SPY | Max drawdown | Trades | Years gained | Test (a) |",
              "|---|---|--:|--:|--:|--:|--:|---|"]
    for row in result["sensitivity"]:
        lines.append(f"| {row['setting']} | {_value(row['setting'], row['value'])} | {_pct(row['cagr'])} "
                     f"| {_pct(row['spy_cagr'])} | {_pct(row['max_drawdown'])} | {row['trades']} "
                     f"| {_pct(row['years_gained'], 0)} | {'passes' if _return_or_drawdown(row) else 'fails'} |")
    return "\n".join(lines) + "\n"


def _folder(paths: Paths) -> Path:
    return paths.reports / "backtest"


def write(paths: Paths, result: dict) -> Path:
    """``trend_<date>.md``, the same as ``.html`` and every trade in ``trend_<date>_trades.csv``
    under ``reports/backtest``; returns the Markdown's path."""
    folder = _folder(paths)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"trend_{result['generated']}"
    text = render_report(result)
    page = render.actions_html(text, f"Trend backtest {result['generated']}")
    md = folder / f"{stem}.md"
    replace_file(md, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    replace_file(folder / f"{stem}.html", lambda temp: Path(temp).write_text(page, encoding="utf-8"))
    trades = pd.DataFrame([{"system": system, "universe": universe, **t}
                           for (system, universe), ts in result["trades"].items() for t in ts],
                          columns=TRADE_COLUMNS)
    replace_file(folder / f"{stem}_trades.csv", lambda temp: trades.to_csv(temp, index=False))
    return md


def latest_report(paths: Paths) -> Path | None:
    """The newest ``trend_*.html``, None when there is none."""
    reports = sorted(_folder(paths).glob("trend_*.html"))
    return reports[-1] if reports else None
