"""The lane-rule backtest: would the screen's lane rules have picked winners, as of past dates?

For each cohort date the screen is replayed on what was known then: statements filed on or
before the date (``pit_statements``) and closes dated on or before it. A cohort's returns
start at the first close on or after the date, so no row sees its own future.

Prices come from one Yahoo ``history(period="max")`` call per ticker, cached as CSV under
``cache/backtest/prices``. Yahoo's ``Close`` is split-adjusted but not dividend-adjusted (the
screen's price and trend input); ``Adj Close`` is both (the total return). EDGAR's share
counts are on each filing's own basis, so they are brought to Yahoo's split basis with the
split ratios dated after the date. Nothing here trades; a failed fetch is logged and answered
with None.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from tradingagents.agents.decision_math import (
    COMPOUNDER_BUY_PREMIUM,
    COMPOUNDER_BUY_RETURN,
    COMPOUNDER_FCF_CAGR,
    COMPOUNDER_MIN_CAGR_YEARS,
    COMPOUNDER_MIN_FCF_YEARS,
    COMPOUNDER_MIN_ROIC_YEARS,
    COMPOUNDER_OVERWEIGHT_PREMIUM,
    COMPOUNDER_OVERWEIGHT_RETURN,
    COMPOUNDER_REVENUE_CAGR,
    COMPOUNDER_ROIC_INCL_GOODWILL,
    INFLECTION_INCREMENTAL_ROIC,
)
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.value_metrics import EQUITY_PREMIUM, MIN_DISCOUNT_RATE
from value_pipeline import market_data, metrics, screener
from value_pipeline.config import Paths
from value_pipeline.pit_statements import (
    facts_for,
    quarters_as_of,
    revenue_history,
    shares_as_of_dated,
    years_as_of,
)

log = logging.getLogger(__name__)

COLUMNS = ["date", "close", "adj_close", "split"]
WITHIN_DAYS = 10        # how far after a date its "next close" may be
CLOSES = 420            # the screen's trend and dislocation window (market_data.closes)


# --- prices --------------------------------------------------------------------------------

def _price_path(ticker: str, paths: Paths):
    return paths.cache / "backtest" / "prices" / f"{ticker.lstrip('^')}.csv"


def _read_prices(path) -> pd.DataFrame | None:
    try:
        hist = pd.read_csv(path, parse_dates=["date"])
        if list(hist.columns) != COLUMNS or hist.empty:
            raise ValueError(f"unexpected columns {list(hist.columns)}")
        return hist
    except Exception as exc:  # noqa: BLE001 - a damaged cache is refetched or skipped
        log.warning("unreadable price cache %s: %s", path, exc)
        return None


def _fetch_prices(ticker: str) -> pd.DataFrame | None:
    try:
        raw = yf.Ticker(ticker).history(period="max", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001 - Yahoo raises many types
        log.warning("no price history for %s: %s", ticker, exc)
        return None
    if raw is None or raw.empty or "Close" not in raw:
        log.info("no price history for %s", ticker)
        return None
    index = pd.DatetimeIndex(raw.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    if "Adj Close" in raw:
        adj = raw["Adj Close"]
    else:
        log.warning("%s: Yahoo sent no Adj Close; its total return uses the close, without "
                    "dividends", ticker)
        adj = raw["Close"]
    split = raw["Stock Splits"] if "Stock Splits" in raw else pd.Series(0.0, index=raw.index)
    hist = pd.DataFrame({"date": index.normalize(), "close": raw["Close"].to_numpy(),
                         "adj_close": adj.to_numpy(), "split": split.fillna(0.0).to_numpy()})
    hist = hist.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return hist if not hist.empty else None


def _needs_fetch(cached: pd.DataFrame | None, refresh: bool, stale_before: str | None) -> bool:
    """Whether a price cache is to be (re)fetched: none, a refresh, or its last date before
    ``stale_before``."""
    return (cached is None or refresh
            or (stale_before is not None
                and cached["date"].iloc[-1] < pd.Timestamp(stale_before)))


def price_history(ticker: str, paths: Paths, fetch: bool = True, refresh: bool = False,
                  stale_before: str | None = None) -> pd.DataFrame | None:
    """Daily ``date, close, adj_close, split`` for the ticker, oldest first, cached under
    ``cache/backtest/prices/<ticker>.csv`` (``^TNX`` as ``TNX.csv``). Refetched on ``refresh``
    or when the cache's last date is before ``stale_before`` (an ISO date), keeping the cached
    copy if the fetch fails; ``fetch=False`` reads only the cache. None, logged, when there is
    no history."""
    path = _price_path(ticker, paths)
    cached = _read_prices(path) if path.exists() else None
    if not fetch or not _needs_fetch(cached, refresh, stale_before):
        return cached
    hist = _fetch_prices(ticker)
    if hist is None:
        return cached
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        dated = hist.assign(date=hist["date"].dt.strftime("%Y-%m-%d"))
        replace_file(path, lambda temp: dated.to_csv(temp, index=False))
    except OSError as exc:
        log.warning("could not cache the price history for %s: %s", ticker, exc)
    return _read_prices(path) if path.exists() else hist


def _on_or_after(hist, day: str, column: str, within: int) -> tuple[date, float] | None:
    if hist is None or hist.empty:
        return None
    start = pd.Timestamp(day)
    rows = hist[(hist["date"] >= start) & (hist["date"] <= start + timedelta(days=within))]
    rows = rows.dropna(subset=[column])
    if rows.empty:
        return None
    first = rows.iloc[0]
    return first["date"].date(), float(first[column])


def close_on_or_after(hist, day: str, within: int = WITHIN_DAYS) -> tuple[date, float] | None:
    """The first ``close`` dated on or after ``day``, at most ``within`` days later."""
    return _on_or_after(hist, day, "close", within)


def adj_close_on_or_after(hist, day: str,
                          within: int = WITHIN_DAYS) -> tuple[date, float] | None:
    """``close_on_or_after`` on the dividend-adjusted ``adj_close``."""
    return _on_or_after(hist, day, "adj_close", within)


def closes_before(hist, day: str, n: int = CLOSES) -> list[float]:
    """The last ``n`` closes dated on or before ``day``, oldest first."""
    if hist is None or hist.empty:
        return []
    rows = hist[hist["date"] <= pd.Timestamp(day)].dropna(subset=["close"])
    return [float(c) for c in rows["close"].tail(n)]


def split_factor_after(hist, day: str) -> float:
    """The product of the split ratios dated after ``day`` (1.0 when none): what a share count
    reported on ``day`` is multiplied by to reach Yahoo's split-adjusted basis."""
    if hist is None or hist.empty:
        return 1.0
    ratios = hist.loc[hist["date"] > pd.Timestamp(day), "split"].fillna(0.0)
    factor = 1.0
    for ratio in ratios:
        if ratio > 0:
            factor *= float(ratio)
    return factor


def total_return(hist, start: str, end: str) -> dict | None:
    """``{"start_date", "end_date", "ret", "ended_early"}`` on ``adj_close`` from the first close
    on or after ``start`` to the first on or after ``end``; when the series stops before
    ``end`` (a delisting or a data gap) it ends at the last close on or before ``end``, with
    ``ended_early`` true. None when there is no close within ten days of ``start``."""
    first = adj_close_on_or_after(hist, start)
    if first is None:
        return None
    last = adj_close_on_or_after(hist, end)
    ended_early = last is None
    if last is None:
        rows = hist[hist["date"] <= pd.Timestamp(end)].dropna(subset=["adj_close"])
        if rows.empty:
            return None
        row = rows.iloc[-1]
        last = row["date"].date(), float(row["adj_close"])
    if not first[1]:
        return None
    return {"start_date": first[0].isoformat(), "end_date": last[0].isoformat(),
            "ret": last[1] / first[1] - 1, "ended_early": ended_early}


def discount_rate_on(tnx: pd.DataFrame | None, day: str) -> float:
    """The screen's discount rate on ``day``: the 10-year Treasury (``^TNX``, in percent) on the
    last trading day on or before it plus the equity premium, never below the floor."""
    if tnx is None or tnx.empty:
        return MIN_DISCOUNT_RATE
    rows = tnx[tnx["date"] <= pd.Timestamp(day)].dropna(subset=["close"])
    if rows.empty:
        return MIN_DISCOUNT_RATE
    return max(MIN_DISCOUNT_RATE, float(rows["close"].iloc[-1]) / 100 + EQUITY_PREMIUM)


# --- snapshot ------------------------------------------------------------------------------

SCREEN_YEARS = 4        # the live screen sees Yahoo's four fiscal years; its lane bars assume it


def pm_record_ok(rec: dict) -> bool:
    """Whether ``metrics.record``'s figures clear the Portfolio Manager's compounder record
    bars, as ``decision_math._compounder_problems`` tests them."""
    above, counted, worst = (rec.get("roic_gw_years_above_15"), rec.get("roic_years_counted"),
                             rec.get("roic_gw_min"))
    if (above is None or counted is None or counted < COMPOUNDER_MIN_ROIC_YEARS
            or above != counted
            or (worst is not None and worst < COMPOUNDER_ROIC_INCL_GOODWILL - 1e-9)):
        return False
    cagr, years = rec.get("revenue_cagr"), rec.get("revenue_cagr_years")
    if (cagr is None or years is None or years < COMPOUNDER_MIN_CAGR_YEARS
            or cagr < COMPOUNDER_REVENUE_CAGR):
        return False
    fcf, fcf_years = rec.get("fcf_cagr"), rec.get("fcf_cagr_years")
    return not (fcf is None or fcf_years is None or fcf_years < COMPOUNDER_MIN_FCF_YEARS
                or fcf < COMPOUNDER_FCF_CAGR - 1e-9)


def _traded_near(hist, day: str) -> bool:
    """Whether the last close on or before ``day`` is at most ``WITHIN_DAYS`` before it (a
    delisted name is not valued on a stale price)."""
    rows = hist[hist["date"] <= pd.Timestamp(day)].dropna(subset=["close"])
    return (not rows.empty
            and rows["date"].iloc[-1] >= pd.Timestamp(day) - timedelta(days=WITHIN_DAYS))


def snapshot(ticker: str, as_of: str, store: dict, hist: pd.DataFrame, rate: float, cfg: dict,
             sector: str | None = None, market: str | None = None) -> tuple[dict, tuple] | None:
    """The screen's row for ``ticker`` as it would have read on ``as_of``, from the facts filed
    and the closes dated on or before it, plus the PM's record verdict (``pm_record_ok``);
    with the ``(years, price, market_cap, shares)`` that ``screener.apply_sector_targets``
    takes. None with fewer than two fiscal years, no close in the ``WITHIN_DAYS`` up to
    ``as_of``, or ``DataUnavailable``. The row carries ``market``, so a small-company market's size
    floor applies; its trading-value floor does not (there is no volume history)."""
    years = years_as_of(store, as_of,
                        split_factor=lambda filed: split_factor_after(hist, filed))[:SCREEN_YEARS]
    if len(years) < 2:
        log.info("skip %s on %s: fewer than two fiscal years filed", ticker, as_of)
        return None
    closes = closes_before(hist, as_of)
    if not closes or not _traded_near(hist, as_of):
        log.info("skip %s on %s: no close in the %d days to it", ticker, as_of, WITHIN_DAYS)
        return None
    price = closes[-1]
    # The count is on its filing's basis at its own date; Yahoo's close already reflects every
    # split since, so the count is brought forward from that date, not from ``as_of``.
    dated = shares_as_of_dated(store, as_of)
    shares = None if dated is None else dated[1] * split_factor_after(hist, dated[0])
    market_cap = None if shares is None else price * shares
    quarters = quarters_as_of(store, as_of)
    data = {"ticker": ticker, "price": price, "shares": shares, "market_cap": market_cap,
            "years": years, "quarters": quarters, "closes": closes}
    try:
        row = screener.evaluate(data, cfg["screen"]["normalize_years"],
                                cfg["screen"]["roic_cap"], rate)
    except market_data.DataUnavailable as exc:
        log.info("skip %s on %s: %s", ticker, as_of, exc)
        return None
    row["market"] = market
    reasons = screener.failed_filters(row, cfg["filters"], check_liquidity=False)
    row["sector"] = sector
    row["fail_reasons"] = "; ".join(reasons)
    row["passes_filters"] = not reasons
    row["lane_hints"] = screener.lane_hints(row)
    row["lane_hint"] = screener.lane_hint(row)
    row["incremental_roic"] = metrics.inflection(years, quarters)["incremental_roic"]
    row["pm_record_ok"] = pm_record_ok(metrics.record(years, revenue_history(store, as_of)))
    row["as_of"] = as_of
    return row, (years, price, market_cap, shares)


# --- rules ---------------------------------------------------------------------------------

TOLERANCE = 1e-9        # a value exactly at a bar clears it


def _at_least(value, bar) -> bool:
    return value is not None and value >= bar - TOLERANCE


def _at_most(value, bar) -> bool:
    return value is not None and bar is not None and value <= bar + TOLERANCE


@dataclass
class Rule:
    """A lane rule: its default ``bars`` and ``picks(rows, bars)``, the cohort rows it takes."""
    name: str
    bars: dict[str, float]
    picks: Callable[[list[dict], dict], list[dict]]


def _ranked(row: dict, top_n) -> bool:
    return (bool(row.get("passes_filters")) and row.get("trend") != "FREEFALL"
            and _at_most(row.get("mf_position"), top_n))


def _hinted(row: dict, lane: str) -> bool:
    return lane in (row.get("lane_hints") or [])


def _record(row: dict) -> bool:
    return _hinted(row, "compounder") and bool(row.get("pm_record_ok"))


def _priced(row: dict, bars: dict) -> bool:
    """The PM's compounder price bars: price at most ``premium`` × the formula base, expected
    return at least ``min_return``, implied growth at most max(sustainable, earned)."""
    base = row.get("value_base")
    ceiling = max(row.get("sustainable_growth") or 0.0, row.get("earned_growth") or 0.0)
    return (_at_most(row.get("price"), None if base is None else bars["premium"] * base)
            and _at_least(row.get("compounder_return"), bars["min_return"])
            and _at_most(row.get("implied_growth"), ceiling))


def _value(rows, bars):
    return [r for r in rows
            if _ranked(r, bars["top_n"]) and _at_least(r.get("mos_formula"), bars["mos"])]


def _magic_formula(rows, bars):
    return [r for r in rows if _ranked(r, bars["top_n"])]


def _compounder(rows, bars):
    return [r for r in rows if _record(r) and _priced(r, bars)]


def _compounder_record(rows, bars):
    return [r for r in rows if _record(r)]


def _compounder_hint(rows, bars):
    return [r for r in rows if _hinted(r, "compounder")]


def _inflection(rows, bars):
    return [r for r in rows if _hinted(r, "inflection")
            and _at_least(r.get("incremental_roic"), bars["incremental_roic"])]


def _dislocation(rows, bars):
    return [r for r in rows if _hinted(r, "dislocation")]


def _eligible(row: dict) -> bool:
    return bool(row.get("passes_filters")) and row.get("trend") != "FREEFALL"


def _top_by(rows: list[dict], key: str, n) -> list[dict]:
    """The ``n`` rows with the highest ``key``, best first; rows without it are dropped."""
    have = [r for r in rows if r.get(key) is not None]
    return sorted(have, key=lambda r: r[key], reverse=True)[:int(n)]


def _er_hurdle(rows, bars):
    return _top_by([r for r in rows if _eligible(r) and _at_least(r.get("expected_return"),
                                                                 bars["min_er"])],
                   "expected_return", bars["top_n"])


def _er_momentum(rows, bars):
    """``er_hurdle``'s picks at or above the median ``momentum_12_1`` of the cohort's rows that
    pass the filters, are not FREEFALL and have a momentum value; with fewer than two such rows,
    nothing."""
    moments = [r["momentum_12_1"] for r in rows
               if _eligible(r) and r.get("momentum_12_1") is not None]
    if len(moments) < 2:
        return []
    median = statistics.median(moments)
    return [r for r in _er_hurdle(rows, bars) if _at_least(r.get("momentum_12_1"), median)]


def _trending_value(rows, bars):
    return _top_by([r for r in rows if _ranked(r, bars["pool"])], "momentum_12_1", bars["top_n"])


def _value_no_downtrend(rows, bars):
    return [r for r in _value(rows, bars) if r.get("trend") != "DOWNTREND"]


def _compounder_fcf(rows, bars):
    return _top_by([r for r in rows if _eligible(r) and _record(r)
                    and _at_least(r.get("fcf_yield"), bars["min_fcf_yield"])],
                   "expected_return", bars["top_n"])


BAR_CANDIDATES = ("er_hurdle", "er_momentum", "trending_value", "value_graded",
                  "value_no_downtrend", "compounder_fcf")


def rules(cfg: dict) -> dict[str, Rule]:
    """The eight lane rules under test, then the six buy-bar candidates, with their default bars taken from the code they mirror
    (``decision_math`` and the config), in the report's order."""
    top_n = cfg["backtest"]["value_top_n"]
    specs = [
        ("value", {"top_n": top_n, "mos": cfg["watch"]["buy_margin_of_safety"]}, _value),
        ("magic_formula", {"top_n": top_n}, _magic_formula),
        ("compounder", {"premium": COMPOUNDER_BUY_PREMIUM, "min_return": COMPOUNDER_BUY_RETURN},
         _compounder),
        ("compounder_overweight", {"premium": COMPOUNDER_OVERWEIGHT_PREMIUM,
                                   "min_return": COMPOUNDER_OVERWEIGHT_RETURN}, _compounder),
        ("compounder_record", {}, _compounder_record),
        ("compounder_hint", {}, _compounder_hint),
        ("inflection", {"incremental_roic": INFLECTION_INCREMENTAL_ROIC}, _inflection),
        ("dislocation", {}, _dislocation),
        ("er_hurdle", {"min_er": 0.12, "top_n": 20}, _er_hurdle),
        ("er_momentum", {"min_er": 0.12, "top_n": 20}, _er_momentum),
        ("trending_value", {"pool": 60, "top_n": 20}, _trending_value),
        ("value_graded", {"top_n": top_n, "mos": 0.20}, _value),
        ("value_no_downtrend", {"top_n": top_n, "mos": cfg["watch"]["buy_margin_of_safety"]},
         _value_no_downtrend),
        ("compounder_fcf", {"min_fcf_yield": 0.04, "top_n": 20}, _compounder_fcf),
    ]
    return {name: Rule(name, bars, picks) for name, bars, picks in specs}


# --- cohorts and returns -------------------------------------------------------------------

def _years_after(day: str, years: int) -> str:
    """``day`` moved ``years`` years on (29 February lands on the 28th)."""
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year + years).isoformat()
    except ValueError:
        return d.replace(year=d.year + years, day=28).isoformat()


def cohort_days(cfg: dict) -> list[str]:
    """The month-days of the cohort dates: ``backtest.cohort_days``, else the older single
    ``backtest.cohort_day`` as a one-item list."""
    bt = cfg["backtest"]
    return list(bt["cohort_days"]) if "cohort_days" in bt else [bt["cohort_day"]]


def _real_day(day: str) -> bool:
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


def cohort_dates(cfg: dict, today: date) -> list[str]:
    """Every cohort day of every year from ``first_year`` to ``last_year``, in date order and
    once each, keeping only those whose one-year hold has ended by ``today``; 29 February is
    skipped in a common year."""
    bt = cfg["backtest"]
    days = sorted({f"{year}-{day}" for year in range(bt["first_year"], bt["last_year"] + 1)
                   for day in cohort_days(cfg)})
    return [d for d in days if _real_day(d) and date.fromisoformat(_years_after(d, 1)) <= today]


def finish_cohort(rows: list[dict], inputs: dict[str, tuple]) -> None:
    """What the screen does after the rows: each expected return re-run on its sector's median
    EV/EBIT, then ``mf_position`` (1 = best) among the passing, non-FREEFALL rows by the Magic
    Formula rank; None for every other row."""
    screener.apply_sector_targets(rows, inputs)
    eligible = [r for r in rows if r.get("passes_filters") and r.get("trend") != "FREEFALL"]
    ranked = metrics.magic_formula_rank(eligible, screener.EY_KEY, screener.ROIC_KEY)
    position = {r["ticker"]: i for i, r in enumerate(ranked, start=1)}
    eligible_ids = {id(r) for r in eligible}
    for r in rows:
        r["mf_position"] = position.get(r["ticker"]) if id(r) in eligible_ids else None


def attach_returns(rows: list[dict], hist_by_ticker: dict, cohort: str, holds: list[int]) -> None:
    """Each row's total return from the cohort date over each hold: ``ret_{h}``,
    ``end_date_{h}``, ``ended_early_{h}`` and ``start_date`` (all None without a start close)."""
    for row in rows:
        hist = hist_by_ticker.get(row["ticker"])
        row["start_date"] = None
        for h in holds:
            tr = total_return(hist, cohort, _years_after(cohort, h))
            row[f"ret_{h}"] = tr and tr["ret"]
            row[f"end_date_{h}"] = tr and tr["end_date"]
            row[f"ended_early_{h}"] = tr and tr["ended_early"]
            if tr and row["start_date"] is None:
                row["start_date"] = tr["start_date"]


def _mean(values) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _median(values) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def benchmarks(spy_hist, rows_by_cohort: dict[str, list[dict]],
               holds: list[int]) -> dict[tuple[str, int], dict]:
    """``{(cohort, hold): {"spy", "universe"}}``: SPY's total return over the hold (None when
    its series ends before the hold does, i.e. the hold has not ended) and the equal-weight
    mean return of every cohort row that has one."""
    out = {}
    for cohort, rows in rows_by_cohort.items():
        for h in holds:
            tr = total_return(spy_hist, cohort, _years_after(cohort, h))
            spy = None if tr is None or tr["ended_early"] else tr["ret"]
            universe = _mean(r[f"ret_{h}"] for r in rows if r.get(f"ret_{h}") is not None)
            out[(cohort, h)] = {"spy": spy, "universe": universe}
    return out


def picks_for(rule: Rule, rows_by_cohort: dict[str, list[dict]], holds: list[int],
              bench: dict, bars: dict | None = None) -> list[dict]:
    """One pick per row the rule takes (at ``bars``, else its defaults) and hold, with its
    return, the cohort's benchmarks and the excess over SPY (None without either)."""
    bars = rule.bars if bars is None else bars
    out = []
    for cohort, rows in rows_by_cohort.items():
        for row in rule.picks(rows, bars):
            for h in holds:
                b = bench.get((cohort, h)) or {}
                ret, spy = row.get(f"ret_{h}"), b.get("spy")
                out.append({
                    "cohort": cohort, "ticker": row["ticker"], "rule": rule.name,
                    "price": row.get("price"), "value_base": row.get("value_base"),
                    "mos_formula": row.get("mos_formula"), "market_cap": row.get("market_cap"),
                    "expected_return": row.get("expected_return"),
                    "momentum_12_1": row.get("momentum_12_1"), "fcf_yield": row.get("fcf_yield"),
                    "trend": row.get("trend"), "hold": h, "start_date": row.get("start_date"),
                    "end_date": row.get(f"end_date_{h}"), "ret": ret, "spy": spy,
                    "universe": b.get("universe"),
                    "excess_spy": None if ret is None or spy is None else ret - spy,
                    "ended_early": row.get(f"ended_early_{h}"),
                })
    return out


# --- summary -------------------------------------------------------------------------------

SENSITIVITY_GRID = {"premium": (1.0, 1.15, 1.25, 1.5), "min_return": (0.08, 0.10, 0.12, 0.15),
                    "mos": (0.10, 0.20, 0.30, 0.40), "top_n": (10, 20, 30, 50),
                    "incremental_roic": (0.15, 0.20, 0.30), "min_er": (0.10, 0.12, 0.15),
                    "pool": (40, 60, 100), "min_fcf_yield": (0.03, 0.04, 0.05)}


def _diff(a, b) -> float | None:
    return None if a is None or b is None else a - b


MIN_BASKET = 5     # a cohort basket smaller than this is too thin to count toward the verdict


def _cohort_line(cohort: str, picks: list[dict], bench: dict) -> dict:
    rets = [p["ret"] for p in picks]
    basket, spy, universe = _mean(rets), bench["spy"], bench["universe"]
    return {"cohort": cohort, "n": len(picks), "basket": basket, "median": _median(rets),
            "spy": spy, "universe": universe, "excess_spy": _diff(basket, spy),
            "excess_universe": _diff(basket, universe),
            "hit_rate": sum(p["ret"] > spy for p in picks) / len(picks),
            "ended_early": sum(bool(p.get("ended_early")) for p in picks)}


def _sentence(rule: str, hold: int, lines: list[dict], beat: int, median_excess, picks: int,
              dates: int) -> str:
    if not lines:
        return f"{rule}: no picks in any cohort."
    n = len(lines)
    return (f"{rule}: beat SPY in {beat} of {n} cohorts with picks (picked in {n} of {dates} "
            f"cohort dates; {hold}-year hold, equal weight), median excess "
            f"{median_excess * 100:+.1f} points; {picks} pick{'' if picks == 1 else 's'} in all.")


def _measured_dates(bench: dict, hold: int) -> int:
    """The cohort dates with a SPY return over ``hold``: those where a pick could count."""
    return len({c for (c, h), b in bench.items() if h == hold and b.get("spy") is not None})


def _year_stats(full: list[dict]) -> tuple[float | None, float | None]:
    """The mean and standard error (sample standard deviation / sqrt(n); None with fewer than
    two years) of the calendar-year averages of the cohort lines' excess over the universe."""
    by_year: dict[str, list[float]] = {}
    for line in full:
        by_year.setdefault(line["cohort"][:4], []).append(line["excess_universe"])
    years = [sum(v) / len(v) for v in by_year.values()]
    if not years:
        return None, None
    se = statistics.stdev(years) / len(years) ** 0.5 if len(years) >= 2 else None
    return sum(years) / len(years), se


def _summary(rule: str, hold: int, picks: list[dict], bench: dict) -> dict:
    """One rule and hold. A pick counts when it has a return and its cohort a SPY return; a
    cohort is listed when it has such a pick."""
    usable = [p for p in picks if p["ret"] is not None
              and (bench.get((p["cohort"], hold)) or {}).get("spy") is not None]
    by_cohort: dict[str, list[dict]] = {}
    for p in usable:
        by_cohort.setdefault(p["cohort"], []).append(p)
    lines = [_cohort_line(c, ps, bench[(c, hold)]) for c, ps in sorted(by_cohort.items())]
    excess = [line["excess_spy"] for line in lines]
    beat = sum(line["basket"] > line["spy"] for line in lines)
    median_excess = _median(excess)
    worst = min(lines, key=lambda line: line["excess_spy"]) if lines else None
    full = [line for line in lines if line["n"] >= MIN_BASKET and line["universe"] is not None]
    year_mean, year_se = _year_stats(full)
    return {
        "cohorts": lines, "beat_spy": beat, "cohorts_with_picks": len(lines),
        "mean_excess_spy": _mean(excess), "median_excess_spy": median_excess,
        "mean_excess_universe": _mean(line["excess_universe"] for line in lines
                                      if line["excess_universe"] is not None),
        "all_picks_median": _median(p["ret"] for p in usable),
        "cohorts_min5": len(full),
        "years_min5": len({line["cohort"][:4] for line in full}),
        "cohort_excess_universe_min5": {line["cohort"]: line["excess_universe"] for line in full},
        "year_mean_excess_universe": year_mean, "year_se_excess_universe": year_se,
        "beat_universe_min5": sum(line["basket"] > line["universe"] for line in full),
        "mean_excess_universe_min5": _mean(line["excess_universe"] for line in full),
        "mean_excess_spy_min5": _mean(line["excess_spy"] for line in full),
        "worst_cohort": worst and worst["cohort"], "picks": len(usable),
        "sentence": _sentence(rule, hold, lines, beat, median_excess, len(usable),
                              _measured_dates(bench, hold)),
    }


def summarize(picks: list[dict], bench: dict, holds: list[int],
              names: list[str] | None = None) -> dict:
    """``{rule: {hold: summary}}`` for every rule in ``names`` (default: the rules in ``picks``,
    in order of appearance); a rule without a usable pick gets the "no picks" sentence."""
    names = list(names) if names is not None else list(dict.fromkeys(p["rule"] for p in picks))
    return {name: {h: _summary(name, h, [p for p in picks if p["rule"] == name and p["hold"] == h],
                               bench)
                   for h in holds}
            for name in names}


def sensitivity(rules: dict[str, Rule], rows_by_cohort: dict[str, list[dict]], bench: dict,
                hold: int = 1) -> list[dict]:
    """Each rule re-run with one bar moved at a time over ``SENSITIVITY_GRID``, the rest at
    their defaults: ``{rule, bar, value, n, beat_spy, cohorts, mean_excess_spy}`` per point."""
    out = []
    for name, rule in rules.items():
        for bar in rule.bars:
            for value in SENSITIVITY_GRID[bar]:
                picks = picks_for(rule, rows_by_cohort, [hold], bench, {**rule.bars, bar: value})
                s = summarize(picks, bench, [hold], names=[name])[name][hold]
                out.append({"rule": name, "bar": bar, "value": value, "n": s["picks"],
                            "beat_spy": s["beat_spy"], "cohorts": s["cohorts_with_picks"],
                            "mean_excess_spy": s["mean_excess_spy"]})
    return out


def _points_text(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}"


FAILED_TEXT = {
    1: "fewer than 20 cohorts in 7 calendar years",
    2: "beat the universe in under 60%",
    3: "mean excess under +2 points over the universe or under 0 over SPY",
    4: "three-year mean excess under 0",
    5: "not above the plain screen on the same cohorts",
}


def matched_baseline(one_year: dict, baseline: dict | None) -> float | None:
    """``magic_formula``'s mean one-year excess over the universe on the candidate's five-pick
    cohorts (``one_year["cohort_excess_universe_min5"]``); a cohort where the baseline has no
    line (or its line no universe return) is left out. None when no cohort is left."""
    by_cohort = {line["cohort"]: line["excess_universe"]
                 for line in (baseline or {}).get("cohorts", [])}
    return _mean(by_cohort[c] for c in one_year.get("cohort_excess_universe_min5", {})
                 if by_cohort.get(c) is not None)


def _base_conditions(one_year: dict, three_year: dict | None) -> dict:
    """Conditions 1 to 4 of the pass rule, read by ``bar_verdict`` and ``small_cap_verdict``:
    ``held`` maps each condition number to whether it holds, beside the numbers they read."""
    cohorts = one_year["cohorts_min5"]
    years = one_year.get("years_min5", 0)
    beat = one_year["beat_universe_min5"]
    excess_universe = one_year["mean_excess_universe_min5"]
    excess_spy = one_year["mean_excess_spy_min5"]
    three = three_year if three_year and three_year["cohorts_min5"] >= 1 else None
    excess_3y = None if three_year is None else three_year["mean_excess_universe_min5"]
    held = {
        1: cohorts >= 20 and years >= 7,
        2: cohorts > 0 and beat * 5 >= cohorts * 3,       # at least 60%, without float rounding
        3: _at_least(excess_universe, 0.02) and _at_least(excess_spy, 0.0),
        4: three is not None and _at_least(excess_3y, 0.0),
    }
    return {"held": held, "cohorts": cohorts, "years": years, "beat": beat,
            "beat_share": beat / cohorts if cohorts else 0,
            "mean_excess_universe": excess_universe, "mean_excess_spy": excess_spy,
            "mean_excess_universe_3y": excess_3y}


def bar_verdict(one_year: dict, three_year: dict | None, baseline_matched: float | None,
                rule: str = "") -> dict:
    """The pass rule, fixed before any run (amended 2026-11-05, still before any run). Over the
    one-year hold, counting cohorts with at least ``MIN_BASKET`` picks, the rule supports a live
    bar change when (1) it has at least 20 such cohorts in at least 7 calendar years, (2) beat
    the equal-weight universe in at least 60% of them, (3) averaged at least +2 points over the
    universe and at least 0 over SPY, (4) averaged at least 0 over the universe across the
    three-year hold (which needs a cohort), and (5) averaged more over the universe than
    ``magic_formula`` on the same cohorts (``baseline_matched``, from ``matched_baseline``). A
    missing mean fails its condition. ``failed`` lists the conditions that failed; ``rule``
    names the rule in the sentence."""
    base = _base_conditions(one_year, three_year)
    excess_universe = base["mean_excess_universe"]
    held = {**base["held"],
            5: (excess_universe is not None and baseline_matched is not None
                and excess_universe > baseline_matched)}
    cohorts, years, beat = base["cohorts"], base["years"], base["beat"]
    excess_spy, excess_3y = base["mean_excess_spy"], base["mean_excess_universe_3y"]
    beat_share = base["beat_share"]
    failed = [n for n, ok in held.items() if not ok]
    supports = not failed
    sentence = (f"{rule + ': ' if rule else ''}{cohorts} cohorts with five or more picks in "
                f"{years} calendar years; beat the universe in {beat} of {cohorts}; mean one-year "
                f"excess {_points_text(excess_universe)} points over the universe and "
                f"{_points_text(excess_spy)} over SPY; three-year {_points_text(excess_3y)}; the "
                f"plain screen on the same cohorts {_points_text(baseline_matched)}: ")
    if supports:
        sentence += "supports a live bar change."
    else:
        sentence += ("does not support a live bar change. Failed: "
                     + "; ".join(f"({n}) {FAILED_TEXT[n]}" for n in failed) + ".")
    return {"supports": supports, "sentence": sentence, "failed": failed, "cohorts": cohorts,
            "years": years, "beat": beat, "beat_share": beat_share,
            "mean_excess_universe": excess_universe, "mean_excess_spy": excess_spy,
            "mean_excess_universe_3y": excess_3y, "baseline": baseline_matched,
            "year_mean": one_year.get("year_mean_excess_universe"),
            "year_se": one_year.get("year_se_excess_universe")}


def small_cap_verdict(one_year: dict, three_year: dict | None, rule: str) -> dict:
    """The small-company pass rule, fixed before any run: conditions 1 to 4 of ``bar_verdict``
    (condition 5, beating the plain screen, does not apply to the plain screen itself). Supports
    adding ``sp600`` to ``review.candidate_markets`` when all four hold; a missing mean fails its
    condition. ``rule`` names the rule in the sentence."""
    base = _base_conditions(one_year, three_year)
    failed = [n for n, ok in base["held"].items() if not ok]
    sentence = (f"{rule}: {base['cohorts']} cohorts with five or more picks in {base['years']} "
                f"calendar years; beat the universe in {base['beat']} of {base['cohorts']}; mean "
                f"one-year excess {_points_text(base['mean_excess_universe'])} points over the "
                f"universe and {_points_text(base['mean_excess_spy'])} over SPY; three-year "
                f"{_points_text(base['mean_excess_universe_3y'])}: ")
    if failed:
        sentence += ("does not support adding sp600 to review.candidate_markets. Failed: "
                     + "; ".join(f"({n}) {FAILED_TEXT[n]}" for n in failed) + ".")
    else:
        sentence += "supports adding sp600 to review.candidate_markets."
    return {"supports": not failed, "sentence": sentence, "failed": failed,
            **{k: v for k, v in base.items() if k != "held"},
            "year_mean": one_year.get("year_mean_excess_universe"),
            "year_se": one_year.get("year_se_excess_universe")}


BOUND = "compounder_price_bar"


def bound_by_price(rules: dict[str, Rule], rows_by_cohort: dict[str, list[dict]], bench: dict,
                   holds: list[int]) -> dict:
    """The compounders the price bars turned away: ``compounder_record`` picks that
    ``compounder`` did not take, summarised under ``"compounder_price_bar"``."""
    taken = {(p["cohort"], p["ticker"], p["hold"])
             for p in picks_for(rules["compounder"], rows_by_cohort, holds, bench)}
    refused = [{**p, "rule": BOUND}
               for p in picks_for(rules["compounder_record"], rows_by_cohort, holds, bench)
               if (p["cohort"], p["ticker"], p["hold"]) not in taken]
    return summarize(refused, bench, holds, names=[BOUND])


# --- survivorship --------------------------------------------------------------------------

def _wiki_html(url: str) -> str:
    return requests.get(url, headers=market_data.HEADERS, timeout=30).text


def added_dates(fetch: Callable[[str], str] | None = None) -> dict[str, str]:
    """Ticker (Yahoo's spelling, ``BRK.B`` -> ``BRK-B``) -> ISO date it joined the S&P 500, from
    the constituents table's ``Date added`` column; rows without a readable date are left out,
    and ``{}`` (logged) when the page or table cannot be read."""
    try:
        table = pd.read_html(StringIO((fetch or _wiki_html)(market_data.MARKETS["sp500"].pages[0])))[0]
        tickers = table["Symbol"].astype(str).str.replace(".", "-", regex=False)
        # The first ten characters: a footnoted cell ("2001-05-07[12]") still parses.
        dates = pd.to_datetime(table["Date added"].astype(str).str[:10], errors="coerce")
        return {t: d.date().isoformat() for t, d in zip(tickers, dates, strict=True)
                if pd.notna(d)}
    except Exception as exc:  # noqa: BLE001 - the dates are optional; callers fail open
        log.warning("no S&P 500 dates added: %s", exc)
        return {}


# The constituents page dropped its "Selected changes" table in 2026; this revision
# (2025-12-23) still carries it, with every change through 2025-12-22.
CHANGES_URL = "https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1329075976"


def removed_dates(fetch: Callable[[str], str] | None = None) -> dict[str, dict]:
    """Ticker (Yahoo's spelling) -> ``{"left": ISO date, "reason": text}`` for every ticker in
    the ``Removed`` column of the changes table at ``CHANGES_URL`` (a pinned page revision), the
    latest date when a name was removed more than once; rows without a readable date or a
    removed ticker are left out, and ``{}`` (logged) when the page or table cannot be read."""
    try:
        table = pd.read_html(StringIO((fetch or _wiki_html)(CHANGES_URL)))[1]
        top = table.columns.get_level_values(0)
        day = table.loc[:, top.isin(("Date", "Effective Date"))].iloc[:, 0]
        ticker = table["Removed"]["Ticker"]
        reason = table.loc[:, top == "Reason"].iloc[:, 0]
        left = pd.to_datetime(day.astype(str), errors="coerce")
        out: dict[str, dict] = {}
        for t, d, why in zip(ticker, left, reason, strict=True):
            if pd.isna(t) or pd.isna(d) or not str(t).strip():
                continue
            name = str(t).strip().replace(".", "-")
            if name not in out or d.date().isoformat() > out[name]["left"]:
                out[name] = {"left": d.date().isoformat(),
                             "reason": "" if pd.isna(why) else str(why)}
        return out
    except Exception as exc:  # noqa: BLE001 - the leavers are optional; callers fail open
        log.warning("no S&P 500 leave dates: %s", exc)
        return {}


def survivorship(paths: Paths, cohorts: list[str],
                 fetch: Callable[[str], str] | None = None) -> dict[str, int | None]:
    """Per cohort date, how many of today's S&P 500 members joined after it: names the index did
    not hold then, while the names it held and later dropped are missing from today's list.
    ``{}`` when the join dates cannot be read."""
    return _joined_after(added_dates(fetch), cohorts)


def _joined_after(added: dict[str, str], cohorts: list[str]) -> dict[str, int]:
    """``survivorship`` on dates already read (``{}`` without any)."""
    return {c: sum(d > c for d in added.values()) for c in cohorts} if added else {}


# --- run -----------------------------------------------------------------------------------

def _read_universe(paths: Paths, indexes: list[str]) -> pd.DataFrame:
    """The members, leaving the screen's universe cache as it is."""
    return market_data.universe(paths, indexes, write=False)


def _universe(paths: Paths, cfg: dict, tickers: list[str] | None, universe_fn) -> list[tuple]:
    """``(ticker, sector, market)`` for today's members less the excluded sectors, narrowed to
    ``tickers`` when given; the market is None when the frame has no ``market`` column."""
    uni = universe_fn(paths, cfg["universe"]["indexes"])
    uni = uni[~uni["sector"].isin(cfg["universe"]["exclude_sectors"])]
    if tickers:
        wanted = {t.upper() for t in tickers}
        uni = uni[uni["ticker"].isin(wanted)]
    markets = uni["market"] if "market" in uni else [None] * len(uni)
    return [(t, s if isinstance(s, str) else None, m if isinstance(m, str) else None)
            for t, s, m in zip(uni["ticker"], uni["sector"], markets, strict=True)]


def _ticker_rows(ticker: str, sector, market, store: dict, hist, cohorts: list[str], rates: dict,
                 added: dict, cfg: dict, left: dict | None = None,
                 ) -> tuple[dict[str, tuple[dict, tuple]], dict[str, str]]:
    """The ticker's snapshot per cohort it belonged to (not joined the S&P 500 after it and, for
    a ``left`` entry from ``removed_dates``, not gone from it by then), and per cohort whose
    snapshot raised, the error (logged): one odd cohort keeps the others."""
    out, errors = {}, {}
    for cohort in cohorts:
        if added.get(ticker, "") > cohort or (left and left["left"] <= cohort):
            continue
        try:
            extra = {"market": market} if market else {}
            snap = snapshot(ticker, cohort, store, hist, rates[cohort], cfg, sector, **extra)
        except Exception as exc:  # noqa: BLE001 - one bad cohort must not stop the ticker
            log.warning("skip %s at %s: %s", ticker, cohort, exc)
            errors[cohort] = type(exc).__name__
            continue
        if snap is not None:
            out[cohort] = snap
    return out, errors


def _load(ticker: str, paths: Paths, cfg: dict, fetch: bool, refresh: bool,
          stale: tuple[str | None, str | None] = (None, None)):
    """The ticker's fact store and price history (either None when missing); ``stale`` is the
    ``stale_before`` of the facts and of the prices. Throttles after a Yahoo fetch."""
    facts_stale, prices_stale = stale
    store = facts_for(ticker, paths, fetch=fetch, refresh=refresh, stale_before=facts_stale)
    path = _price_path(ticker, paths)
    yahoo = fetch and _needs_fetch(_read_prices(path) if path.exists() else None, refresh,
                                   prices_stale)
    hist = price_history(ticker, paths, fetch=fetch, refresh=refresh, stale_before=prices_stale)
    if yahoo:
        market_data.throttle(cfg["screen"]["throttle_seconds"])
    return store, hist


def _cached_dates(path: Path, fetch: bool, fetch_fn, what: str) -> dict:
    """``what`` (a mapping) cached at ``path``: read from the cache with ``fetch=False``, else
    fetched with ``fetch_fn`` and the cache rewritten (the cache when the fetch comes back
    empty). ``{}`` when neither has any."""
    cached: dict = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("unreadable %s cache %s: %s", what, path, exc)
        if not isinstance(cached, dict):
            log.warning("%s cache %s is not a mapping; ignored", what, path)
            cached = {}
    if not fetch:
        return cached
    fresh = fetch_fn()
    if not fresh:
        return cached
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(fresh, sort_keys=True)
        replace_file(path, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    except OSError as exc:
        log.warning("could not cache the %s: %s", what, exc)
    return fresh


def _join_dates(paths: Paths, fetch: bool, added_fn) -> dict[str, str]:
    """The S&P 500 join dates, cached at ``cache/backtest/added_dates.json``: read from the cache
    with ``fetch=False``, else fetched and the cache rewritten (the cache when the fetch comes
    back empty). ``{}`` when neither has any."""
    return _cached_dates(paths.cache / "backtest" / "added_dates.json", fetch,
                         lambda: (added_fn or added_dates)(), "join-date")


def _leave_dates(paths: Paths, fetch: bool, removed_fn) -> dict[str, dict]:
    """The S&P 500 leave dates and reasons, cached at ``cache/backtest/removed_dates.json`` like
    the join dates."""
    return _cached_dates(paths.cache / "backtest" / "removed_dates.json", fetch,
                         lambda: (removed_fn or removed_dates)(), "leave-date")


PRICE_SLACK_DAYS = 7     # a price cache this close to the date needed is fresh enough


def _stale_before(cohorts: list[str], holds: list[int],
                  today: date) -> tuple[str | None, str | None]:
    """The ``stale_before`` dates for the fact stores (the newest cohort date: its filings must
    be in the store) and the price caches (a week before the latest hold end needed, capped at
    ``today``); ``(None, None)`` without a cohort."""
    if not cohorts:
        return None, None
    ends = [date.fromisoformat(_years_after(c, h)) for c in cohorts for h in holds]
    needed = min(today, max(ends, default=today))
    return max(cohorts), (needed - timedelta(days=PRICE_SLACK_DAYS)).isoformat()


def _stale_cohorts(cohorts: list[str], holds: list[int], bench: dict, today: date) -> list[str]:
    """The cohorts with a hold ended by ``today`` that SPY's history does not reach."""
    return [c for c in cohorts
            if any(_years_after(c, h) <= today.isoformat()
                   and (bench.get((c, h)) or {}).get("spy") is None for h in holds)]


MISSING_FIELDS = ("revenue", "ebit", "free_cash_flow")


def _missing_fields(inputs: dict[str, tuple]) -> dict[str, int]:
    """How many of a cohort's rows have a newest fiscal year without each of
    ``MISSING_FIELDS``: the tag map's gaps, shown in the report's coverage."""
    newest = [tup[0][0] for tup in inputs.values() if tup[0]]
    return {field: sum(year.get(field) is None for year in newest) for field in MISSING_FIELDS}


_NO_SUMMARY = {"cohorts_min5": 0, "years_min5": 0, "beat_universe_min5": 0,
               "mean_excess_universe_min5": None, "mean_excess_spy_min5": None,
               "cohort_excess_universe_min5": {}, "year_mean_excess_universe": None,
               "year_se_excess_universe": None}


def _bar_verdicts(summary: dict) -> dict:
    """``bar_verdict`` for each candidate against ``magic_formula``'s one-year summary on the
    candidate's own cohorts; a hold that was not run counts as no summary."""
    baseline = summary["magic_formula"].get(1)
    out = {}
    for name in BAR_CANDIDATES:
        one = summary[name].get(1) or _NO_SUMMARY
        out[name] = bar_verdict(one, summary[name].get(3), matched_baseline(one, baseline),
                                rule=name)
    return out


def _leavers(paths: Paths, fetch: bool, removed_fn, members: list[tuple], added: dict,
             tickers: list[str] | None) -> dict[str, dict]:
    """The names that left the S&P 500 and are not members today: not in ``members``, and not
    rejoined (a join date after the leave date keeps today's row), narrowed to ``tickers``."""
    held = {t for t, _, _ in members}
    wanted = {t.upper() for t in tickers} if tickers else None
    return {t: v for t, v in _leave_dates(paths, fetch, removed_fn).items()
            if t not in held and added.get(t, "") <= v["left"] and (wanted is None or t in wanted)}


def run(paths: Paths, cfg: dict, today: date | None = None, tickers: list[str] | None = None,
        refresh: bool = False, fetch: bool = True, progress=None,
        universe_fn=None, added_fn=None, keep_rows: bool = False, leavers: bool = False,
        removed_fn=None) -> dict:
    """The backtest: every cohort's rows from the statements and closes known on its date, the
    rules' picks and their returns against SPY and the universe, the summary, sensitivity, the
    bar that bound and the survivorship counts. ``fetch=False`` reads the statement and price
    caches only (the join dates too, from their own cache). A ticker whose data is missing or
    whose snapshot raises is recorded in ``skipped`` (with the cohort, when one cohort raised;
    its other cohorts stay) and the run goes on. ``keep_rows`` adds ``rows`` (each cohort's
    point-in-time screen rows) and ``added`` (the S&P 500 join dates) to the result, for the trend
    backtest's quality universe. ``leavers`` adds the names that left the S&P 500 since (sector
    unknown), members until their leave date, and counts those without statements or prices in
    ``result["leavers"]``. Nothing trades; no agent is called."""
    today = today or date.today()
    holds = list(cfg["backtest"]["holds"])
    cohorts = cohort_dates(cfg, today)
    members = _universe(paths, cfg, tickers, universe_fn or _read_universe)
    # Join dates are the S&P 500's: a run without that index has none.
    added = _join_dates(paths, fetch, added_fn) if "sp500" in cfg["universe"]["indexes"] else {}
    gone = _leavers(paths, fetch, removed_fn, members, added, tickers) \
        if leavers and "sp500" in cfg["universe"]["indexes"] else None
    members += [(t, None, "sp500_left") for t in gone or {}]
    stale = _stale_before(cohorts, holds, today)
    spy = price_history(cfg["backtest"]["benchmark"], paths, fetch=fetch, refresh=refresh,
                        stale_before=stale[1])
    tnx = price_history("^TNX", paths, fetch=fetch, refresh=refresh, stale_before=stale[1])
    rates = {c: discount_rate_on(tnx, c) for c in cohorts}
    rows: dict[str, list[dict]] = {c: [] for c in cohorts}
    inputs: dict[str, dict[str, tuple]] = {c: {} for c in cohorts}
    hists: dict[str, pd.DataFrame] = {}
    skipped: dict[str, str] = {}
    with_data = 0
    counts = {"included": 0, "no_facts": 0, "no_prices": 0}
    gone_reasons: dict[str, int] = {}
    for i, (ticker, sector, market) in enumerate(members, start=1):
        if progress:
            progress(i, len(members), ticker)
        try:
            store, hist = _load(ticker, paths, cfg, fetch, refresh, stale)
            if store is None or hist is None:
                skipped[ticker] = "no EDGAR statements" if store is None else "no price history"
                if gone and ticker in gone:
                    counts["no_facts" if store is None else "no_prices"] += 1
                    reason = gone[ticker]["reason"][:40].lower()
                    gone_reasons[reason] = gone_reasons.get(reason, 0) + 1
                continue
            if gone and ticker in gone:
                counts["included"] += 1
            snaps, errors = _ticker_rows(ticker, sector, market, store["facts"], hist, cohorts, rates,
                                         added, cfg, left=(gone or {}).get(ticker))
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the run
            log.warning("skip %s: %s", ticker, exc)
            skipped[ticker] = f"snapshot error ({type(exc).__name__})"
            continue
        if errors:
            cohort, name = next(iter(errors.items()))
            skipped[ticker] = f"snapshot error ({name}) at {cohort}"
            left = (gone or {}).get(ticker, {}).get("left", "9999")
            attempted = sum(added.get(ticker, "") <= c < left for c in cohorts)
            if len(errors) == attempted:
                continue                  # every cohort raised: skipped, not counted
        with_data += 1
        for cohort, (row, tup) in snaps.items():
            rows[cohort].append(row)
            inputs[cohort][ticker] = tup
        if snaps:
            hists[ticker] = hist
    for cohort in cohorts:
        finish_cohort(rows[cohort], inputs[cohort])
        attach_returns(rows[cohort], hists, cohort, holds)
    bench = benchmarks(spy, rows, holds)
    rule_set = rules(cfg)
    picks = [p for rule in rule_set.values() for p in picks_for(rule, rows, holds, bench)]
    summary = summarize(picks, bench, holds, names=list(rule_set))
    result = {
        "generated": today.isoformat(), "cohorts": cohorts, "holds": holds,
        "universe_size": len(members), "with_data": with_data,
        "indexes": list(cfg["universe"]["indexes"]),
        "small_markets": list(cfg["filters"]["small_markets"]),
        "rows_per_cohort": {c: len(rows[c]) for c in cohorts}, "picks": picks,
        "missing_fields": {c: _missing_fields(inputs[c]) for c in cohorts},
        "summary": summary, "bar_verdicts": _bar_verdicts(summary),
        "sensitivity": sensitivity(rule_set, rows, bench),
        "bound": bound_by_price(rule_set, rows, bench, holds),
        "survivorship": _joined_after(added, cohorts), "joins_known": bool(added),
        "defaults": {name: dict(rule.bars) for name, rule in rule_set.items()},
        "skipped": skipped,
        "leavers": None if gone is None else {**counts, "reasons": gone_reasons},
        "spy_ends": (None if spy is None or spy.empty
                     else spy["date"].iloc[-1].date().isoformat()),
        "stale_cohorts": _stale_cohorts(cohorts, holds, bench, today),
    }
    if keep_rows:
        result.update(rows=rows, added=added)
    return result


# --- report --------------------------------------------------------------------------------

PICK_COLUMNS = ["cohort", "ticker", "rule", "price", "value_base", "mos_formula", "market_cap",
                "expected_return", "momentum_12_1", "fcf_yield", "trend", "hold", "start_date", "end_date", "ret", "spy", "universe", "excess_spy",
                "ended_early"]
SURVIVORSHIP = ("today's members only: N of today's S&P 500 names joined the S&P 500 after this "
                "date (some from the S&P 400, where they were already in the universe) and are "
                "left out of this cohort; names that left either index since are not in the "
                "sample and cannot be counted from free data; the S&P 400's join dates are not "
                "published, so all its members stay")


NO_JOINS = ("today's members only: the S&P 500 join dates could not be read, so names that "
            "joined after a cohort date are still in that cohort — a look-ahead that flatters "
            "the result")
BAR_MEANINGS = {
    "premium": "price at most premium × worksheet base",
    "min_return": "yield + earned growth + negative reversion at least this",
    "mos": "formula margin of safety at least this",
    "top_n": "Magic Formula rank within the top n",
    "incremental_roic": "at least this",
    "min_er": "expected return at least this",
    "pool": "Magic Formula rank within the best pool, before the momentum cut",
    "min_fcf_yield": "free cash flow yield at least this",
}


def _joins_known(result: dict) -> bool:
    return result.get("joins_known", bool(result.get("survivorship")))


def _ret(x) -> str:
    return "–" if x is None else f"{x:.1%}"


def _points(x) -> str:
    return "–" if x is None else f"{x * 100:+.1f}"


OVERLAP = ("Counting cohorts — one-year holds on quarterly dates overlap, so the cohorts are not "
           "independent and amount to about eleven separate years; three-year holds overlap far "
           "more and amount to fewer than four separate periods; six candidates were tried "
           "against one threshold, so one passing by luck is plausible; returns are before "
           "trading costs; a pass is evidence to look closer, not proof.")


DEFAULT_INDEXES = ("sp500", "sp400")   # the backtest command's lists: EDGAR covers US filers


def _indexes(result: dict) -> list[str]:
    return list(result.get("indexes") or DEFAULT_INDEXES)


def _is_default(indexes: list[str]) -> bool:
    return set(indexes) == set(DEFAULT_INDEXES)


def _small_market_limits(result: dict) -> list[str]:
    """The limit line for a run that includes a small-company market (empty otherwise)."""
    if not set(_indexes(result)) & set(result.get("small_markets") or ()):
        return []
    return ["The trading-value floor cannot be applied to past dates (no volume history), so "
            "small companies are filtered on size only."]


def _leavers_sentence(leavers: dict) -> str:
    """The report's line on the names that left the S&P 500 and were added back as members."""
    missing = leavers["no_facts"] + leavers["no_prices"]
    top = sorted(leavers["reasons"].items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    mostly = f", mostly {', '.join(reason.rstrip('.') for reason, _ in top)}" if top else ""
    return (f"Leavers: {leavers['included']} names that left the S&P 500 since 2013 were added "
            "as members until their leave date; "
            f"{missing} could not be (no EDGAR facts: {leavers['no_facts']}; no price history: "
            f"{leavers['no_prices']}){mostly}. Leavers carry no sector, so the sector "
            "exclusions did not apply to them.")


def _tested(result: dict) -> list[str]:
    cohorts, surv = result["cohorts"], result["survivorship"]
    span = f"{cohorts[0]} to {cohorts[-1]}" if cohorts else "none (no hold has ended)"
    holds = " and ".join(str(h) for h in result["holds"])
    holds += " year" if result["holds"] == [1] else " years"
    if _joins_known(result):
        survivorship = (f"Survivorship — {SURVIVORSHIP}. N per cohort: "
                        + "; ".join(f"{c}: {n}" for c, n in surv.items()) + ".")
    else:
        survivorship = f"Survivorship — {NO_JOINS}."
    if result.get("leavers"):
        survivorship += " " + _leavers_sentence(result["leavers"])
    return ["## What was tested", "",
            f"The screen's lane rules ({', '.join(result['summary'])}) replayed on "
            f"{len(cohorts)} cohort dates ({span}), each pick held {holds} with dividends "
            "reinvested, equal weight, against SPY and the equal-weight universe. Point in time: "
            "each cohort sees only the statements filed on or before its date (EDGAR) and the "
            "closes dated on or before it; returns start at the first close on or after it. The "
            "metrics see the newest four fiscal years known then, as the live screen does. "
            "Sectors are today's (EDGAR has none), a mild look-ahead. Index lists: "
            f"{', '.join(_indexes(result))}. " + survivorship, "",
            OVERLAP, "", *_small_market_limits(result)]


def verdict(result: dict) -> str:
    """The ``## Verdict`` section: each rule's sentence, one hold after another."""
    lines = ["## Verdict", ""]
    if not _joins_known(result):
        lines += [f"**Warning:** {NO_JOINS}.", ""]
    for h in result["holds"]:
        lines += [f"**{h}-year hold**", ""]
        lines += [f"- {by_hold[h]['sentence']}" for by_hold in result["summary"].values()]
        lines.append("")
    return "\n".join(lines)


def _cohort_table(lines: list[dict]) -> list[str]:
    out = ["| Cohort | n | Basket | Median | SPY | Universe | Excess (pts) | Hit rate |",
           "|---|--:|--:|--:|--:|--:|--:|--:|"]
    out += [f"| {c['cohort']} | {c['n']} | {_ret(c['basket'])} | {_ret(c['median'])} "
            f"| {_ret(c['spy'])} | {_ret(c['universe'])} | {_points(c['excess_spy'])} "
            f"| {c['hit_rate']:.0%} |" for c in lines]
    return out


SURVIVORSHIP_SHARED = ("The S&P 400 has no join dates, so names that entered it after rising are "
                       "in every cohort; a 20-name basket ranked on momentum can hold several "
                       "while the universe holds each at a small weight; the S&P 500-only run "
                       "(`--indexes sp500`) is the check.")


def _year_line(v: dict) -> str:
    """The information line after a candidate's sentence: the calendar-year averages."""
    mean, se, n = v.get("year_mean"), v.get("year_se"), v.get("years", 0)
    se_text = "n/a" if se is None else f"{se * 100:.2f}"
    return (f"Calendar-year averages: mean {_points_text(mean)} points, standard error "
            f"{se_text}, {n} year{'' if n == 1 else 's'}.")


def _candidates(result: dict) -> list[str]:
    lines = ["## Buy-bar candidates", "",
             "Pass rule, fixed before any run: over the one-year hold, counting cohorts with at "
             "least five picks, a rule supports a live bar change only if (1) it has at least 20 "
             "such cohorts in at least 7 different calendar years, (2) it beat the equal-weight "
             "universe in at least 60% of them, (3) it averaged at least +2 points over the "
             "universe and at least 0 over SPY, (4) it averaged at least 0 over the universe "
             "across the three-year hold, and (5) it averaged more over the universe than "
             "`magic_formula` on the same cohorts. Quarterly cohorts with one-year holds overlap, "
             "so the cohorts are not independent samples. The calendar-year averages are "
             "information, not part of the rule. " + SURVIVORSHIP_SHARED, ""]
    for name in BAR_CANDIDATES:
        v = result["bar_verdicts"][name]
        lines.append(f"- {v['sentence']} {_year_line(v)}")
    return lines + [""]


def candidates(result: dict) -> str:
    """The ``## Buy-bar candidates`` section: the pass rule and one verdict per candidate."""
    return "\n".join(_candidates(result))


SMALL_COMPANY_LIMITS = ("Limits: today's S&P 600 members only, and no join dates: companies that "
                        "failed or were taken over are missing (which flatters the screen and the "
                        "universe alike), and those that grew into the S&P 400 or 500 are missing "
                        "too (the reverse). The liquidity check is skipped.")


def small_company(result: dict) -> str:
    """The ``## Small-company verdict`` section for a run over ``sp600`` alone (empty for any
    other lists): the pass rule, ``magic_formula``'s sentence, then ``value``'s for information."""
    if _indexes(result) != ["sp600"]:
        return ""
    lines = ["## Small-company verdict", "",
             "Pass rule, fixed before any run: over the one-year hold, counting cohorts with at "
             "least five picks, the `magic_formula` rule (the screen's top 30, which is what "
             "becomes a candidate) supports adding `sp600` to `review.candidate_markets` only if "
             "(1) it has at least 20 such cohorts in at least 7 different calendar years, (2) it "
             "beat the equal-weight universe in at least 60% of them, (3) it averaged at least +2 "
             "points over the universe and at least 0 over SPY, and (4) it averaged at least 0 "
             "over the universe across the three-year hold.", ""]
    for name, note in (("magic_formula", ""), ("value", " (for information)")):
        by_hold = result["summary"].get(name) or {}
        v = small_cap_verdict(by_hold.get(1) or _NO_SUMMARY, by_hold.get(3), name)
        lines.append(f"- {v['sentence']}{note}")
    return "\n".join(lines + ["", SMALL_COMPANY_LIMITS, ""])


def _by_cohort(result: dict) -> list[str]:
    lines = ["## By cohort", ""]
    for name, by_hold in result["summary"].items():
        for h in result["holds"]:
            s = by_hold[h]
            lines += [f"### {name}, {h}-year hold", ""]
            lines += (_cohort_table(s["cohorts"]) if s["cohorts"] else ["No picks."]) + [""]
    return lines


def _sensitivity(result: dict) -> list[str]:
    lines = ["## Sensitivity", "",
             "One bar moved at a time, the others at their defaults; one-year hold. "
             "\\* marks the rule's default.", ""]
    lines += [f"- {bar}: {meaning}" for bar, meaning in BAR_MEANINGS.items()] + [""]
    defaults = result.get("defaults", {})
    by_rule: dict[str, list[dict]] = {}
    for point in result["sensitivity"]:
        by_rule.setdefault(point["rule"], []).append(point)
    for name, points in by_rule.items():
        lines += [f"### {name}", "", "| Bar | Value | Picks | Beat SPY | Mean excess (pts) |",
                  "|---|--:|--:|--:|--:|"]
        lines += [f"| {p['bar']} | {_bar_value(p, defaults)} | {p['n']} | {p['beat_spy']} of {p['cohorts']} "
                  f"| {_points(p['mean_excess_spy'])} |" for p in points]
        lines.append("")
    return lines


def _bar_value(point: dict, defaults: dict) -> str:
    default = defaults.get(point["rule"], {}).get(point["bar"])
    is_default = default is not None and abs(point["value"] - default) < TOLERANCE
    return f"{point['value']:g}" + (" \\*" if is_default else "")


def _bound(result: dict) -> list[str]:
    lines = ["## The bar that bound", "",
             "The compounders that cleared the screen's hint and the PM's record bars but not the "
             "price bars (`compounder_record` minus `compounder`): what the price bars cost or "
             "saved.", ""]
    for h in result["holds"]:
        s = result["bound"][BOUND][h]
        lines += [f"- {s['sentence']}"]
    lines.append("")
    for h in result["holds"]:
        s = result["bound"][BOUND][h]
        if s["cohorts"]:
            lines += [f"### {h}-year hold", ""] + _cohort_table(s["cohorts"]) + [""]
    return lines


def _coverage(result: dict) -> list[str]:
    reasons: dict[str, int] = {}
    for reason in result["skipped"].values():
        reasons[reason] = reasons.get(reason, 0) + 1
    lines = ["## Coverage", "",
             f"- Universe (today's members less the excluded sectors): {result['universe_size']}",
             f"- With statements and prices: {result['with_data']}"]
    if result.get("rows_per_cohort"):
        lines.append("- Rows per cohort: " + "; ".join(
            f"{c}: {n}" for c, n in result["rows_per_cohort"].items()))
    names = {"revenue": "revenue", "ebit": "EBIT", "free_cash_flow": "free cash flow"}
    for c, counts in (result.get("missing_fields") or {}).items():
        lines.append(f"- Newest year lacking a field, {c}: " + ", ".join(
            f"{names[f]} {n}" for f, n in counts.items())
            + f" (of {result.get('rows_per_cohort', {}).get(c, 0)} rows)")
    if "spy_ends" in result:
        lines.append(f"- SPY history ends {result['spy_ends'] or 'nowhere (no SPY history)'}")
    if result.get("stale_cohorts"):
        lines.append("- cohorts " + ", ".join(result["stale_cohorts"]) + " need --refresh: a "
                     f"hold has ended by today, but SPY's cached history stops at "
                     f"{result.get('spy_ends')}, before it")
    lines.append(f"- Skipped: {len(result['skipped'])}")
    lines += [f"  - {reason}: {n}" for reason, n in sorted(reasons.items(),
                                                            key=lambda kv: (-kv[1], kv[0]))]
    return lines + [""]


def _small_section(result: dict) -> list[str]:
    section = small_company(result)
    return [section] if section else []


def markdown(result: dict) -> str:
    """The report: what was tested, the verdict, the buy-bar candidates, each rule by cohort, the sensitivity, the bar
    that bound and the coverage."""
    indexes = _indexes(result)
    cover = ("The backtest covers the S&P 500 and 400 only" if _is_default(indexes)
             else f"This run covers {', '.join(indexes)} only")
    lines = ["# Lane-rule backtest", "", f"Generated {result['generated']}. Nothing trades; "
             "no agent was called.", "",
             f"{cover}: non-US companies have no point-in-time filings.", ""]
    lines += _tested(result) + [verdict(result)] + _small_section(result) + _candidates(result) + _by_cohort(result) + _sensitivity(result)
    lines += _bound(result) + _coverage(result)
    return "\n".join(lines)


def _folder(paths: Paths) -> Path:
    return paths.reports / "backtest"


def _suffix(result: dict) -> str:
    """``""`` for the default lists, else ``_`` and the lists joined (``_sp500``); ``_leavers``
    after it for a run that added the names that left the S&P 500."""
    indexes = _indexes(result)
    lists = "" if _is_default(indexes) else "_" + "_".join(indexes)
    return lists + ("_leavers" if result.get("leavers") is not None else "")


def write(paths: Paths, result: dict, today: str) -> tuple[Path, Path]:
    """The report and the picks CSV (one row per pick and hold) under ``reports/backtest``; a
    run on other lists than the default gets their names in the file names
    (``backtest_<date>_sp500.md``) and a run with leavers ``_leavers``."""
    folder = _folder(paths)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"backtest_{today}{_suffix(result)}"
    md = folder / f"{stem}.md"
    csv = folder / f"{stem}_picks.csv"
    text = markdown(result)
    replace_file(md, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    picks = pd.DataFrame(result["picks"], columns=PICK_COLUMNS)
    replace_file(csv, lambda temp: picks.to_csv(temp, index=False))
    return md, csv


DEFAULT_REPORT = re.compile(r"backtest_\d{4}-\d{2}-\d{2}\.md")


def latest_report(paths: Paths) -> tuple[str, str] | None:
    """``(date, text)`` of the newest default-universe ``backtest_<date>.md`` (not a suffixed
    run on other lists), None when there is none."""
    reports = sorted(p for p in _folder(paths).glob("backtest_*.md")
                     if DEFAULT_REPORT.fullmatch(p.name))
    if not reports:
        return None
    return reports[-1].stem.removeprefix("backtest_"), reports[-1].read_text(encoding="utf-8")
