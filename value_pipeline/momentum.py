"""Momentum sleeve test: the 12-1 momentum system on today's S&P 500 members, each from the day
it joined the index, over a recent period and an earlier hold-out, against SPY and against the
same names held equally.

The pass rule in ``verdict`` (its numbers below) was fixed before the first run and is not tuned
afterwards. The record of real momentum funds is shown beside it. Every number comes from daily
closes; nothing trades and no agent is called.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from tradingagents.dataflows.files import replace_file
from value_pipeline import backtest, render, trend
from value_pipeline.config import DEFAULTS, Paths

SYSTEM = "momentum_12_1"
BEAT_SPY = 0.02          # (1) recent: CAGR at least this far above SPY's
BEAT_UNIVERSE = 0.01     # (2) recent: and above the equal-weight universe's
DRAWDOWN_RATIO = 1.25    # (4) max drawdown at most this multiple of SPY's, in both periods
YEARS_AHEAD = 0.55       # (5) share of calendar years ahead of SPY, both periods pooled
CHECK_BEAT_SPY = 0.01    # (6) top 20, recent: above SPY by this much (hold-out: not below)
EPS = 1e-9               # every bound is inclusive to this tolerance

# The settings the pass rule was fixed for; a run with any other is shown but does not count.
FIXED = {"recent_start": "2015-07-01", "holdout_start": "2005-01-03", "holdout_end": "2015-06-30",
         "positions": 10, "check_positions": 20, "cost_per_side": 0.005, "market_filter": True}

SENSITIVITY = [("positions", [10, 20, 30]), ("cost_per_side", [0.0, 0.0035, 0.005, 0.0075]),
               ("market_filter", [False])]


def universe_equity(closes: pd.DataFrame, added: dict[str, str], start: str) -> pd.Series:
    """The equal-weight universe from the first row on or after ``start`` (at 1.0). Each later
    day's return is the mean over names joined on or before the day before and with a close on
    both days; a day with none returns 0. A name with no join date is never in it."""
    closes = closes.sort_index().astype(float)
    index = pd.DatetimeIndex(closes.index)
    first = int(index.searchsorted(pd.Timestamp(start)))
    previous = pd.Series(index).shift(1)
    returns = (closes / closes.shift(1) - 1).to_numpy()
    joined = np.zeros(returns.shape, dtype=bool)
    for j, ticker in enumerate(closes.columns):
        if ticker in added:
            joined[:, j] = (previous >= pd.Timestamp(added[ticker])).to_numpy()
    usable = joined & ~np.isnan(returns)
    count = usable.sum(axis=1)
    total = np.where(usable, returns, 0.0).sum(axis=1)
    daily = np.where(count > 0, total / np.maximum(count, 1), 0.0)
    daily[: first + 1] = 0.0
    growth = np.cumprod(1 + daily)
    return pd.Series(growth[first:], index=index[first:])


def sleeve(closes: pd.DataFrame, spy: pd.Series, added: dict[str, str], m: dict, start: str,
           end: str | None = None, **overrides) -> dict:
    """One momentum run from ``start`` (to ``end`` when given: the frames are cut there first, so
    open positions are marked at their last close with no sale cost). ``overrides`` may set
    ``positions``, ``cost_per_side`` and ``market_filter``. Returns ``{"metrics", "trades"}``."""
    if end is not None:
        cut = pd.Timestamp(end)
        attrs = dict(closes.attrs)
        closes, spy = closes.loc[closes.index <= cut], spy.loc[spy.index <= cut]
        closes.attrs = attrs
    values = {"positions": m["positions"], "cost_per_side": m["cost_per_side"],
              "market_filter": m["market_filter"], **overrides}
    t = DEFAULTS["trend"]
    s = trend.Settings(start=start, equity=m["equity"], max_positions=values["positions"],
                       max_position_weight=1.0, cost_per_side=values["cost_per_side"],
                       market_filter=values["market_filter"], risk_per_trade=t["risk_per_trade"],
                       stop_n=t["stop_n"], n_days=t["n_days"])
    joined = trend._eligibility(added)

    def eligible(ticker: str, day: date) -> bool:
        return ticker in added and joined(ticker, day)

    out = trend.simulate(SYSTEM, closes, spy, s, eligible)
    result = trend.metrics(out, spy)
    equity = out["equity"]
    universe = universe_equity(closes.loc[equity.index[0]:], added, str(equity.index[0].date()))
    years = [y for y in result["years"].values() if y["spy"] is not None]
    result.update({
        "start": equity.index[0].date().isoformat(), "end": equity.index[-1].date().isoformat(),
        "positions": values["positions"], "cost_per_side": values["cost_per_side"],
        "market_filter": values["market_filter"],
        "universe_cagr": trend.cagr(universe), "universe_max_drawdown": trend.max_drawdown(universe),
        "years_ahead": sum(y["system"] > y["spy"] for y in years), "years_counted": len(years),
    })
    return {"metrics": result, "trades": out["trades"]}


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.1f}%"


def differing(settings: dict) -> dict:
    """The ``FIXED`` keys whose value in ``settings`` is different, with the value used."""
    return {k: settings.get(k) for k, v in FIXED.items() if settings.get(k) != v}


def verdict(recent: dict, holdout: dict, recent_check: dict, holdout_check: dict) -> dict:
    """The fixed pass rule over the four ``metrics`` dicts (top 10 recent, top 10 hold-out, top 20
    recent, top 20 hold-out). All six conditions must hold; every bound is inclusive."""
    ahead = recent["years_ahead"] + holdout["years_ahead"]
    counted = recent["years_counted"] + holdout["years_counted"]
    ge = lambda a, b: a >= b - EPS  # noqa: E731
    checks = {
        "1": ge(recent["cagr"], recent["spy_cagr"] + BEAT_SPY),
        "2": ge(recent["cagr"], recent["universe_cagr"] + BEAT_UNIVERSE),
        "3": ge(holdout["cagr"], holdout["spy_cagr"]) and ge(holdout["cagr"], holdout["universe_cagr"]),
        "4": all(m["max_drawdown"] <= DRAWDOWN_RATIO * m["spy_max_drawdown"] + EPS
                 for m in (recent, holdout)),
        "5": counted > 0 and ge(ahead / counted, YEARS_AHEAD),
        "6": ge(recent_check["cagr"], recent_check["spy_cagr"] + CHECK_BEAT_SPY)
        and ge(holdout_check["cagr"], holdout_check["spy_cagr"]),
    }
    supports = all(checks.values())
    facts = (f"top 10 returned {_pct(recent['cagr'])} a year in the recent period against "
             f"{_pct(recent['spy_cagr'])} for SPY and {_pct(recent['universe_cagr'])} for the same "
             f"names held equally, and {_pct(holdout['cagr'])} in the earlier period against "
             f"{_pct(holdout['spy_cagr'])} and {_pct(holdout['universe_cagr'])}; the worst fall "
             f"was {_pct(recent['max_drawdown'])} against SPY's {_pct(recent['spy_max_drawdown'])} "
             f"recently and {_pct(holdout['max_drawdown'])} against "
             f"{_pct(holdout['spy_max_drawdown'])} earlier; it beat SPY in {ahead} of {counted} "
             f"calendar years; top 20 returned {_pct(recent_check['cagr'])} and "
             f"{_pct(holdout_check['cagr'])}.")
    words = {
        "1": "the recent return was not 2 points above SPY's",
        "2": "the recent return was not 1 point above the equal-weight universe's",
        "3": "the earlier return did not match both SPY's and the universe's",
        "4": "a worst fall was more than 1.25 times SPY's",
        "5": "it was ahead of SPY in fewer than 55% of calendar years",
        "6": "the top 20 did not hold up (1 point above SPY recently, level with it earlier)",
    }
    head = ("The test supports a small live sleeve: " if supports
            else "The test does not support a live sleeve: ")
    sentence = head + facts
    if not supports:
        sentence += " Failed: " + "; ".join(f"({n}) {words[n]}" for n in checks if not checks[n])
    return {"supports": supports, "checks": checks, "sentence": sentence,
            "years_ahead": ahead, "years_counted": counted}


def fund_record(paths: Paths, pairs: list[dict], fetch: bool = True,
                today: date | None = None) -> list[dict]:
    """For each ``{"fund", "parent", "label"}`` pair, the fund's and its parent's adjusted closes
    on their common dates: CAGR, excess and worst fall. A missing history or fewer than two common
    dates leaves the numbers None and names the problem in ``error``."""
    today = today or date.today()
    stale = (today - timedelta(days=backtest.PRICE_SLACK_DAYS)).isoformat()
    rows = []
    for pair in pairs:
        row = {"fund": pair["fund"], "parent": pair["parent"], "label": pair.get("label", ""),
               "start": None, "end": None, "years": None, "fund_cagr": None, "parent_cagr": None,
               "excess": None, "fund_max_drawdown": None, "parent_max_drawdown": None, "error": None}
        series = {}
        for symbol in (pair["fund"], pair["parent"]):
            hist = backtest.price_history(symbol, paths, fetch=fetch, stale_before=stale)
            if hist is None:
                row["error"] = f"no price history for {symbol}"
                break
            series[symbol] = trend._adj_closes(hist).dropna()
        else:
            fund, parent = series[pair["fund"]], series[pair["parent"]]
            common = fund.index.intersection(parent.index)
            if len(common) < 2:
                row["error"] = "no common dates"
            else:
                fund, parent = fund.loc[common], parent.loc[common]
                row.update({
                    "start": common[0].date().isoformat(), "end": common[-1].date().isoformat(),
                    "years": trend._years(fund), "fund_cagr": trend.cagr(fund),
                    "parent_cagr": trend.cagr(parent),
                    "fund_max_drawdown": trend.max_drawdown(fund),
                    "parent_max_drawdown": trend.max_drawdown(parent)})
                row["excess"] = row["fund_cagr"] - row["parent_cagr"]
        rows.append(row)
    return rows


def run(paths: Paths, cfg: dict, today: date | None = None, fetch: bool = True,
        progress=None) -> dict:
    """The primary and check runs over both periods, the sensitivity rows, the fixed verdict and
    the fund record. ``fetch=False`` reads the caches only. Nothing trades."""
    today = today or date.today()
    m = {**DEFAULTS["momentum"], **(cfg.get("momentum") or {})}
    if fetch:
        # trend.load_closes fetches SPY with no date; without this a cached SPY can end weeks back.
        backtest.price_history(trend.BENCHMARK, paths, fetch=True, stale_before=(
            today - timedelta(days=backtest.PRICE_SLACK_DAYS)).isoformat())
    added = backtest._join_dates(paths, fetch, None)
    if not added:
        raise ValueError("no S&P 500 join dates in the cache; run without --no-fetch")
    tickers = sorted(added)
    closes, spy = trend.load_closes(paths, tickers, m["holdout_start"], fetch=fetch,
                                    progress=progress)
    primary, check = m["positions"], m["check_positions"]
    periods = {"recent": (m["recent_start"], None), "holdout": (m["holdout_start"], m["holdout_end"])}

    def once(period: str, **overrides) -> dict:
        start, end = periods[period]
        return sleeve(closes, spy, added, m, start, end=end, **overrides)

    results, trades = {}, {}
    for period in periods:
        for n in (primary, check):
            out = once(period, positions=n)
            results[(period, n)] = out["metrics"]
            if n == primary:
                trades[(period, n)] = out["trades"]
    info = {period: {"start": results[(period, primary)]["start"], "end": results[(period, primary)]["end"],
                     "eligible_at_start": sum(str(t) in added and added[str(t)] <= start
                                              for t in closes.columns)}
            for period, (start, _end) in periods.items()}
    sensitivity = [{"period": period, "setting": name, "value": value,
                    **once(period, **{name: value})["metrics"]}
                   for period in periods for name, values in SENSITIVITY for value in values]
    filled = closes.attrs.get("filled", {})
    return {
        "generated": today.isoformat(), "settings": m, "as_fixed": not differing(m), "members": len(tickers),
        "used": len(closes.columns), "periods": info, "results": results, "trades": trades,
        "sensitivity": sensitivity,
        "verdict": verdict(results[("recent", primary)], results[("holdout", primary)],
                           results[("recent", check)], results[("holdout", check)]),
        "funds": fund_record(paths, m["funds"], fetch=fetch, today=today),
        "missing": list(closes.attrs.get("missing", [])),
        "short": list(closes.attrs.get("short", [])),
        "filled": {"cells": sum(filled.values()), "tickers": len(filled)},
    }


TRADE_COLUMNS = ["period", "positions", "ticker", "signal_date", "entry_date", "entry_price",
                 "shares", "n", "exit_date", "exit_price", "reason", "pnl", "ret", "days"]
PERIOD_NAMES = {"recent": "recent period", "holdout": "hold-out period"}


def _rule_lines(positions: int, check: int, cost: float) -> list[str]:
    at = f"for the top {positions} at {cost:.1%} a side"
    return [
        f"The rule was fixed before any run. It passes {at} when all six hold:", "",
        f"1. Recent period: CAGR at least SPY's plus {BEAT_SPY * 100:g} points.",
        f"2. Recent period: CAGR at least the equal-weight universe's plus "
        f"{BEAT_UNIVERSE * 100:g} point.",
        "3. Hold-out period: CAGR at least SPY's and at least the universe's.",
        f"4. In each period, maximum drawdown at most {DRAWDOWN_RATIO:g} times SPY's over the "
        "same days.",
        f"5. Over both periods, ahead of SPY in at least {YEARS_AHEAD:.0%} of the calendar years "
        "(2015 counts once per period, as two half years).",
        f"6. The top {check} at the same cost: recent CAGR at least SPY's plus "
        f"{CHECK_BEAT_SPY * 100:g} point, and hold-out CAGR at least SPY's.", "",
        "A pass turns nothing on. It would only justify designing a small live sleeve.", ""]


def _unfixed_lines(settings: dict, as_fixed: bool) -> list[str]:
    if as_fixed:
        return []
    diff = ", ".join(f"{k} {v}" for k, v in differing(settings).items())
    return [f"These settings differ from the ones the rule was fixed for ({diff}), so this "
            "verdict does not count.", ""]


def _count(items: list, noun: str) -> str:
    return f"{len(items)} {noun}{'' if len(items) == 1 else 's'}"


def render_report(result: dict) -> str:
    """The report: span and settings, the verdict and its rule, the results, the top 10's calendar
    years, the sensitivity rows, the fund record and the limits."""
    s = result["settings"]
    primary, check = s["positions"], s["check_positions"]
    settings_line = (f"top {primary} (check: top {check}), {s['cost_per_side']:.1%} a side, "
                     f"{s['equity']:,} start equity, market filter {'on' if s['market_filter'] else 'off'}")
    periods = result["periods"]
    spans = "; ".join(f"the {PERIOD_NAMES[p]} runs {periods[p]['start']} to {periods[p]['end']} "
                      f"with {periods[p]['eligible_at_start']} names eligible at the start"
                      for p in ("recent", "holdout"))
    lines = [
        f"# Momentum sleeve backtest, {result['generated']}", "",
        "The 12-1 momentum system on today's S&P 500 members, each from the day it joined the "
        f"index: {result['members']} members, {result['used']} used. {spans[0].upper()}{spans[1:]}. "
        f"Settings: {settings_line}. Nothing trades; this is a record of what the rule would "
        "have done.", "",
        "## Verdict", "", *_rule_lines(primary, check, s["cost_per_side"]),
        result["verdict"]["sentence"], "",
        *_unfixed_lines(s, result.get("as_fixed", True)),
        "## Results", "",
        "| Period | Positions | CAGR | SPY | Equal-weight universe | Max drawdown | SPY drawdown "
        "| Years ahead of SPY | Trades a year | Costs a year |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for (period, n), m in result["results"].items():
        lines.append(f"| {period} | {n} | {_pct(m['cagr'])} | {_pct(m['spy_cagr'])} "
                     f"| {_pct(m['universe_cagr'])} | {_pct(m['max_drawdown'])} "
                     f"| {_pct(m['spy_max_drawdown'])} | {m['years_ahead']} of {m['years_counted']} "
                     f"| {m['trades_per_year']:.1f} | {_pct(m['costs_per_year'])} |")
    lines += ["", f"## Calendar years: top {primary}", ""]
    for period in ("holdout", "recent"):
        lines += [f"### {PERIOD_NAMES[period].capitalize()}", "", "| Year | Sleeve | SPY |", "|---|--:|--:|"]
        for year, y in result["results"][(period, primary)]["years"].items():
            lines.append(f"| {year} | {_pct(y['system'])} | {_pct(y['spy'])} |")
        lines.append("")
    lines += [f"## Sensitivity: top {primary} against one change at a time", "",
              "| Period | Setting | Value | CAGR | SPY | Max drawdown |", "|---|---|---|--:|--:|--:|"]
    for row in result["sensitivity"]:
        value = (f"{row['value']:.2%}" if row["setting"] == "cost_per_side" else str(row["value"]))
        lines.append(f"| {row['period']} | {row['setting']} | {value} | {_pct(row['cagr'])} "
                     f"| {_pct(row['spy_cagr'])} | {_pct(row['max_drawdown'])} |")
    lines += ["", "## Real momentum funds", "",
              "Information only: real funds after their own costs, with no survivorship bias.", "",
              "| Fund | Against | From | To | Fund a year | Parent a year | Difference "
              "| Fund drawdown | Parent drawdown |", "|---|---|---|---|--:|--:|--:|--:|--:|"]
    for f in result["funds"]:
        name = f["label"] or f["fund"]
        if f["error"]:
            lines.append(f"| {name} | {f['parent']} | unavailable: {f['error']} | | | | | | |")
        else:
            lines.append(f"| {name} | {f['parent']} | {f['start']} | {f['end']} "
                         f"| {_pct(f['fund_cagr'])} | {_pct(f['parent_cagr'])} | {_pct(f['excess'])} "
                         f"| {_pct(f['fund_max_drawdown'])} | {_pct(f['parent_max_drawdown'])} |")
    lines += ["", "## Limits", "",
              "- Today's members only: names that left the index are missing. This flatters the "
              "sleeve and the universe, but not SPY.",
              "- Fills are at the next close after the signal.",
              "- Cash earns nothing.",
              "- A Swedish investor's currency moves are not modelled.",
              "- The last day's close can come from an unfinished session if the prices were "
              "cached during US trading hours.",
              f"- {primary} positions is a concentrated book."]
    if result["missing"]:
        lines.append(f"- No price history for {_count(result['missing'], 'ticker')}: "
                     f"{', '.join(result['missing'])}.")
    if result["short"]:
        lines.append(f"- Too short a history to rank for {_count(result['short'], 'ticker')}: "
                     f"{', '.join(result['short'])}.")
    return "\n".join(lines) + "\n"


def _folder(paths: Paths) -> Path:
    return paths.reports / "backtest"


def write(paths: Paths, result: dict) -> Path:
    """``momentum_<date>.md``, the same as ``.html`` and every top-N trade in
    ``momentum_<date>_trades.csv`` under ``reports/backtest``; returns the Markdown's path."""
    folder = _folder(paths)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"momentum_{result['generated']}"
    text = render_report(result)
    page = render.actions_html(text, f"Momentum sleeve backtest {result['generated']}")
    md = folder / f"{stem}.md"
    replace_file(md, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    replace_file(folder / f"{stem}.html", lambda temp: Path(temp).write_text(page, encoding="utf-8"))
    trades = pd.DataFrame([{"period": period, "positions": n, **t}
                           for (period, n), ts in result["trades"].items() for t in ts],
                          columns=TRADE_COLUMNS)
    replace_file(folder / f"{stem}_trades.csv", lambda temp: trades.to_csv(temp, index=False))
    return md


def latest_report(paths: Paths) -> Path | None:
    """The newest ``momentum_*.html``, None when there is none."""
    reports = sorted(_folder(paths).glob("momentum_*.html"))
    return reports[-1] if reports else None
