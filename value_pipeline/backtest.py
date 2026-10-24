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

import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO

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


def price_history(ticker: str, paths: Paths, fetch: bool = True,
                  refresh: bool = False) -> pd.DataFrame | None:
    """Daily ``date, close, adj_close, split`` for the ticker, oldest first, cached for good
    under ``cache/backtest/prices/<ticker>.csv`` (``^TNX`` as ``TNX.csv``). ``refresh``
    refetches (keeping the cached copy if the fetch fails); ``fetch=False`` reads only the
    cache. None, logged, when there is no history."""
    path = _price_path(ticker, paths)
    cached = _read_prices(path) if path.exists() else None
    if cached is not None and not refresh:
        return cached
    if not fetch:
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
    added = added_dates(fetch)
    if not added:
        return {}
    joined = [pd.Timestamp(d) for d in added.values()]
    return {c: sum(d > pd.Timestamp(c) for d in joined) for c in cohorts}
