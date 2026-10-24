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
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from tradingagents.agents.decision_math import (
    COMPOUNDER_FCF_CAGR,
    COMPOUNDER_MIN_CAGR_YEARS,
    COMPOUNDER_MIN_FCF_YEARS,
    COMPOUNDER_MIN_ROIC_YEARS,
    COMPOUNDER_REVENUE_CAGR,
    COMPOUNDER_ROIC_INCL_GOODWILL,
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
