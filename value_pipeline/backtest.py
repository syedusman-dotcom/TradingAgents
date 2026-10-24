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
    adj = raw["Adj Close"] if "Adj Close" in raw else raw["Close"]
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
        tmp = path.with_suffix(".csv.tmp")
        hist.assign(date=hist["date"].dt.strftime("%Y-%m-%d")).to_csv(tmp, index=False)
        tmp.replace(path)
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
             sector: str | None = None) -> tuple[dict, tuple] | None:
    """The screen's row for ``ticker`` as it would have read on ``as_of``, from the facts filed
    and the closes dated on or before it, plus the PM's record verdict (``pm_record_ok``);
    with the ``(years, price, market_cap, shares)`` that ``screener.apply_sector_targets``
    takes. None with fewer than two fiscal years, no close in the ``WITHIN_DAYS`` up to
    ``as_of``, or ``DataUnavailable``."""
    years = years_as_of(store, as_of,
                        split_factor=lambda end: split_factor_after(hist, end))[:SCREEN_YEARS]
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
    reasons = screener.failed_filters(row, cfg["filters"])
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


def rules(cfg: dict) -> dict[str, Rule]:
    """The eight lane rules under test, with their default bars taken from the code they mirror
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


def cohort_dates(cfg: dict, today: date) -> list[str]:
    """One cohort date per year from ``first_year`` to ``last_year`` on ``cohort_day``, keeping
    only those whose one-year hold has ended by ``today``."""
    bt = cfg["backtest"]
    days = (f"{year}-{bt['cohort_day']}" for year in range(bt["first_year"], bt["last_year"] + 1))
    return [d for d in days if date.fromisoformat(_years_after(d, 1)) <= today]


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
                    "hold": h, "start_date": row.get("start_date"),
                    "end_date": row.get(f"end_date_{h}"), "ret": ret, "spy": spy,
                    "universe": b.get("universe"),
                    "excess_spy": None if ret is None or spy is None else ret - spy,
                    "ended_early": row.get(f"ended_early_{h}"),
                })
    return out


# --- summary -------------------------------------------------------------------------------

SENSITIVITY_GRID = {"premium": (1.0, 1.15, 1.25, 1.5), "min_return": (0.08, 0.10, 0.12, 0.15),
                    "mos": (0.20, 0.30, 0.40), "top_n": (10, 20, 30, 50),
                    "incremental_roic": (0.15, 0.20, 0.30)}


def _diff(a, b) -> float | None:
    return None if a is None or b is None else a - b


def _cohort_line(cohort: str, picks: list[dict], bench: dict) -> dict:
    rets = [p["ret"] for p in picks]
    basket, spy, universe = _mean(rets), bench["spy"], bench["universe"]
    return {"cohort": cohort, "n": len(picks), "basket": basket, "median": _median(rets),
            "spy": spy, "universe": universe, "excess_spy": _diff(basket, spy),
            "excess_universe": _diff(basket, universe),
            "hit_rate": sum(p["ret"] > spy for p in picks) / len(picks),
            "ended_early": sum(bool(p.get("ended_early")) for p in picks)}


def _sentence(rule: str, hold: int, lines: list[dict], beat: int, median_excess, picks: int) -> str:
    if not lines:
        return f"{rule}: no picks in any cohort."
    return (f"{rule}: beat SPY in {beat} of {len(lines)} cohorts ({hold}-year hold, equal weight), "
            f"median excess {median_excess * 100:+.1f} points; {picks} picks in all.")


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
    return {
        "cohorts": lines, "beat_spy": beat, "cohorts_with_picks": len(lines),
        "mean_excess_spy": _mean(excess), "median_excess_spy": median_excess,
        "mean_excess_universe": _mean(line["excess_universe"] for line in lines
                                      if line["excess_universe"] is not None),
        "all_picks_median": _median(p["ret"] for p in usable),
        "worst_cohort": worst and worst["cohort"], "picks": len(usable),
        "sentence": _sentence(rule, hold, lines, beat, median_excess, len(usable)),
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
        table = pd.read_html(StringIO((fetch or _wiki_html)(market_data.WIKI["sp500"])))[0]
        tickers = table["Symbol"].astype(str).str.replace(".", "-", regex=False)
        dates = pd.to_datetime(table["Date added"], errors="coerce")
        return {t: d.date().isoformat() for t, d in zip(tickers, dates, strict=True)
                if pd.notna(d)}
    except Exception as exc:  # noqa: BLE001 - the dates are optional; callers fail open
        log.warning("no S&P 500 dates added: %s", exc)
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

def _universe(paths: Paths, cfg: dict, tickers: list[str] | None, universe_fn) -> list[tuple]:
    """``(ticker, sector)`` for today's members less the excluded sectors, narrowed to
    ``tickers`` when given."""
    uni = universe_fn(paths, cfg["universe"]["indexes"])
    uni = uni[~uni["sector"].isin(cfg["universe"]["exclude_sectors"])]
    if tickers:
        wanted = {t.upper() for t in tickers}
        uni = uni[uni["ticker"].isin(wanted)]
    return [(t, s if isinstance(s, str) else None) for t, s in zip(uni["ticker"], uni["sector"],
                                                                   strict=True)]


def _ticker_rows(ticker: str, sector, store: dict, hist, cohorts: list[str], rates: dict,
                 added: dict, cfg: dict) -> dict[str, tuple[dict, tuple]]:
    """The ticker's snapshot per cohort it belonged to (not joined the S&P 500 after it)."""
    out = {}
    for cohort in cohorts:
        if added.get(ticker, "") > cohort:
            continue
        snap = snapshot(ticker, cohort, store, hist, rates[cohort], cfg, sector)
        if snap is not None:
            out[cohort] = snap
    return out


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


def _join_dates(paths: Paths, fetch: bool, added_fn) -> dict[str, str]:
    """The S&P 500 join dates, cached at ``cache/backtest/added_dates.json``: read from the cache
    with ``fetch=False``, else fetched and the cache rewritten (the cache when the fetch comes
    back empty). ``{}`` when neither has any."""
    path = paths.cache / "backtest" / "added_dates.json"
    cached: dict[str, str] = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("unreadable join-date cache %s: %s", path, exc)
    if not fetch:
        return cached
    added = (added_fn or added_dates)()
    if not added:
        return cached
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(added, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        log.warning("could not cache the join dates: %s", exc)
    return added


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


def run(paths: Paths, cfg: dict, today: date | None = None, tickers: list[str] | None = None,
        refresh: bool = False, fetch: bool = True, progress=None,
        universe_fn=market_data.universe, added_fn=None) -> dict:
    """The backtest: every cohort's rows from the statements and closes known on its date, the
    rules' picks and their returns against SPY and the universe, the summary, sensitivity, the
    bar that bound and the survivorship counts. ``fetch=False`` reads the statement and price
    caches only (the join dates too, from their own cache). A ticker whose data is missing or whose snapshot raises is recorded in
    ``skipped`` and the run goes on. Nothing trades; no agent is called."""
    today = today or date.today()
    holds = list(cfg["backtest"]["holds"])
    cohorts = cohort_dates(cfg, today)
    members = _universe(paths, cfg, tickers, universe_fn)
    added = _join_dates(paths, fetch, added_fn)
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
    for i, (ticker, sector) in enumerate(members, start=1):
        if progress:
            progress(i, len(members), ticker)
        try:
            store, hist = _load(ticker, paths, cfg, fetch, refresh, stale)
            if store is None or hist is None:
                skipped[ticker] = "no EDGAR statements" if store is None else "no price history"
                continue
            snaps = _ticker_rows(ticker, sector, store["facts"], hist, cohorts, rates, added, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the run
            log.warning("skip %s: %s", ticker, exc)
            skipped[ticker] = f"snapshot error ({type(exc).__name__})"
            continue
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
    return {
        "generated": today.isoformat(), "cohorts": cohorts, "holds": holds,
        "universe_size": len(members), "with_data": with_data,
        "rows_per_cohort": {c: len(rows[c]) for c in cohorts}, "picks": picks,
        "missing_fields": {c: _missing_fields(inputs[c]) for c in cohorts},
        "summary": summarize(picks, bench, holds, names=list(rule_set)),
        "sensitivity": sensitivity(rule_set, rows, bench),
        "bound": bound_by_price(rule_set, rows, bench, holds),
        "survivorship": _joined_after(added, cohorts), "joins_known": bool(added),
        "defaults": {name: dict(rule.bars) for name, rule in rule_set.items()},
        "skipped": skipped,
        "spy_ends": (None if spy is None or spy.empty
                     else spy["date"].iloc[-1].date().isoformat()),
        "stale_cohorts": _stale_cohorts(cohorts, holds, bench, today),
    }


# --- report --------------------------------------------------------------------------------

PICK_COLUMNS = ["cohort", "ticker", "rule", "price", "value_base", "mos_formula", "market_cap",
                "hold", "start_date", "end_date", "ret", "spy", "universe", "excess_spy",
                "ended_early"]
SURVIVORSHIP = ("today's members only: N of today's S&P 500 names joined after this date and are "
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
}


def _joins_known(result: dict) -> bool:
    return result.get("joins_known", bool(result.get("survivorship")))


def _ret(x) -> str:
    return "–" if x is None else f"{x:.1%}"


def _points(x) -> str:
    return "–" if x is None else f"{x * 100:+.1f}"


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
    return ["## What was tested", "",
            f"The screen's lane rules ({', '.join(result['summary'])}) replayed on "
            f"{len(cohorts)} cohort dates ({span}), each pick held {holds} with dividends "
            "reinvested, equal weight, against SPY and the equal-weight universe. Point in time: "
            "each cohort sees only the statements filed on or before its date (EDGAR) and the "
            "closes dated on or before it; returns start at the first close on or after it. The "
            "metrics see the newest four fiscal years known then, as the live screen does. "
            "Sectors are today's (EDGAR has none), a mild look-ahead. " + survivorship, ""]


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


def markdown(result: dict) -> str:
    """The report: what was tested, the verdict, each rule by cohort, the sensitivity, the bar
    that bound and the coverage."""
    lines = ["# Lane-rule backtest", "", f"Generated {result['generated']}. Nothing trades; "
             "no agent was called.", ""]
    lines += _tested(result) + [verdict(result)] + _by_cohort(result) + _sensitivity(result)
    lines += _bound(result) + _coverage(result)
    return "\n".join(lines)


def _folder(paths: Paths) -> Path:
    return paths.reports / "backtest"


def write(paths: Paths, result: dict, today: str) -> tuple[Path, Path]:
    """The report and the picks CSV (one row per pick and hold) under ``reports/backtest``."""
    folder = _folder(paths)
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / f"backtest_{today}.md"
    csv = folder / f"backtest_{today}_picks.csv"
    md.write_text(markdown(result), encoding="utf-8")
    pd.DataFrame(result["picks"], columns=PICK_COLUMNS).to_csv(csv, index=False)
    return md, csv


def latest_report(paths: Paths) -> tuple[str, str] | None:
    """``(date, text)`` of the newest ``backtest_*.md``, None when there is none."""
    reports = sorted(_folder(paths).glob("backtest_*.md"))
    if not reports:
        return None
    return reports[-1].stem.removeprefix("backtest_"), reports[-1].read_text(encoding="utf-8")
