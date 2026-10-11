"""The pipeline's only door to Yahoo Finance and Wikipedia; everything else gets plain data.

Fundamentals are cached per ticker as JSON for ``cache_days``, so a monthly screen
of ~900 tickers costs one slow pass a week at most.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

from value_pipeline.config import Paths
from value_pipeline.metrics import YEAR_FIELDS

log = logging.getLogger(__name__)

# Each field's Yahoo row label, best first.
ROWS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "gross_profit": ("Gross Profit",),
    "ebit": ("EBIT", "Operating Income"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "interest_expense": ("Interest Expense", "Interest Expense Non Operating"),
    "total_assets": ("Total Assets",),
    "current_assets": ("Current Assets",),
    "current_liabilities": ("Current Liabilities",),
    "cash": ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
    "net_ppe": ("Net PPE",),
    "long_term_debt": ("Long Term Debt",),
    "total_debt": ("Total Debt",),
    "equity": ("Stockholders Equity", "Common Stock Equity"),
    "retained_earnings": ("Retained Earnings",),
    "total_liabilities": ("Total Liabilities Net Minority Interest",),
    "shares": ("Ordinary Shares Number", "Share Issued", "Diluted Average Shares"),
    "operating_cash_flow": ("Operating Cash Flow",),
    "free_cash_flow": ("Free Cash Flow",),
}
assert set(ROWS) == set(YEAR_FIELDS)

WIKI = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}
HEADERS = {"User-Agent": "value-pipeline/0.1 (personal research tool)"}


class DataUnavailable(Exception):
    """A ticker the screen must skip; the message says why."""


# --- universe ----------------------------------------------------------------

def universe(paths: Paths, indexes: list[str]) -> pd.DataFrame:
    """Index members as columns ticker, name, sector, index. Falls back to the cached CSV."""
    try:
        frames = []
        for idx in indexes:
            html = requests.get(WIKI[idx], headers=HEADERS, timeout=30).text
            table = pd.read_html(StringIO(html))[0]
            frames.append(pd.DataFrame({
                # Yahoo writes class shares with a dash: BRK.B -> BRK-B.
                "ticker": table["Symbol"].astype(str).str.replace(".", "-", regex=False),
                "name": table["Security"],
                "sector": table["GICS Sector"],
                "index": idx,
            }))
        df = pd.concat(frames).drop_duplicates("ticker").reset_index(drop=True)
        paths.root.mkdir(parents=True, exist_ok=True)
        df.to_csv(paths.universe, index=False)
        return df
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the cache
        if paths.universe.exists():
            log.warning("could not fetch the index lists (%s); using cached %s", exc, paths.universe)
            return pd.read_csv(paths.universe)
        raise DataUnavailable(f"could not fetch the index lists and no cache exists: {exc}") from exc


# --- fundamentals --------------------------------------------------------------

def _years(*frames: pd.DataFrame) -> list[dict]:
    """Merge Yahoo's annual statements into per-year dicts, newest first."""
    columns = sorted({c for f in frames if f is not None for c in f.columns}, reverse=True)
    years = []
    for col in columns:
        year = {"fiscal_year_end": pd.Timestamp(col).date().isoformat()}
        for field, labels in ROWS.items():
            value = None
            for f in frames:
                if f is None or col not in f.columns:
                    continue
                for label in labels:
                    if label in f.index and pd.notna(f.at[label, col]):
                        value = float(f.at[label, col])
                        break
                if value is not None:
                    break
            year[field] = value
        # Yahoo pads with a column for a year it has almost nothing for.
        if sum(v is not None for k, v in year.items() if k != "fiscal_year_end") >= 6:
            years.append(year)
    return years


def closes(ticker: str, days: int = 420) -> list[float]:
    hist = yf.Ticker(ticker).history(period=f"{days}d", auto_adjust=True)
    return [float(x) for x in hist["Close"].dropna()] if not hist.empty else []


def fundamentals(ticker: str, paths: Paths, cache_days: int = 7) -> dict:
    """Annual statements, market data and ~400 days of closes for one ticker, cached."""
    cache = paths.cache / f"{ticker}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if datetime.fromisoformat(data["fetched_at"]) > datetime.now() - timedelta(days=cache_days):
            return {**data, "from_cache": True}
    t = yf.Ticker(ticker)
    try:
        info = t.info or {}
        years = _years(t.income_stmt, t.balance_sheet, t.cashflow)
        series = closes(ticker)
    except Exception as exc:  # noqa: BLE001 - Yahoo raises many types
        raise DataUnavailable(f"Yahoo request failed: {exc}") from exc
    if not years:
        raise DataUnavailable("no annual statements")
    data = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "market_cap": info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice") or (series[-1] if series else None),
        "currency": info.get("currency"),
        "sector": info.get("sector"),
        "years": years,
        "closes": series,
    }
    paths.cache.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


# --- prices ---------------------------------------------------------------------

def latest_price(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        return float(hist["Close"].dropna().iloc[-1]) if not hist.empty else None
    except Exception as exc:  # noqa: BLE001
        log.warning("no price for %s: %s", ticker, exc)
        return None


def closes_between(ticker: str, start: str, end: str | None = None) -> pd.Series:
    """Daily closes (not dividend-adjusted) indexed by date, from a few days before start."""
    begin = (date.fromisoformat(start) - timedelta(days=7)).isoformat()
    stop = (date.fromisoformat(end) if end else date.today()) + timedelta(days=1)
    hist = yf.Ticker(ticker).history(start=begin, end=stop.isoformat(), auto_adjust=False)
    if hist.empty:
        return pd.Series(dtype=float)
    s = hist["Close"].dropna()
    s.index = [d.date() for d in s.index]
    return s


def throttle(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
