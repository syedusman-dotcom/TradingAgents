"""The pipeline's only door to Yahoo Finance and Wikipedia; everything else gets plain data.

Fundamentals are cached per ticker as JSON for ``cache_days``, so a monthly screen
of ~900 tickers costs one slow pass a week at most.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from tradingagents.dataflows.vendors.yahoo.common import MINOR_UNITS, usd_rate
from tradingagents.dataflows.vendors.yahoo.value_metrics import (
    fetch_quarters,
    statement_years,
)
from value_pipeline.config import Paths

log = logging.getLogger(__name__)


HEADERS = {"User-Agent": "value-pipeline/0.1 (personal research tool)"}


# Bump when the cached fields change, so older cache files are fetched again.
CACHE_SCHEMA = 6


class DataUnavailable(Exception):
    """A ticker the screen must skip; the message says why."""


# --- universe ----------------------------------------------------------------

@dataclass(frozen=True)
class Market:
    key: str
    country: str
    pages: tuple[str, ...]          # Wikipedia pages, tried in order; the first usable table wins
    suffix: str = ""                # Yahoo exchange suffix, e.g. ".L"
    gics_column: str | None = None  # only the S&P lists carry a GICS sector worth keeping
    tickers: tuple[tuple[str, str], ...] = ()   # (code, name) when no Wikipedia page has them


_WIKI = "https://en.wikipedia.org/wiki/"


def _market(key: str, country: str, page: str, suffix: str = "", *extra: str,
            gics: str | None = None) -> Market:
    return Market(key, country, tuple(_WIKI + p for p in (page, *extra)), suffix, gics)


# ATX members 2026-11-05 (Wikipedia has names only); check yearly.
ATX_MEMBERS = (
    ("EBS", "Erste Group Bank"), ("VER", "Verbund"), ("OMV", "OMV"), ("BG", "BAWAG Group"),
    ("ANDR", "Andritz"), ("WIE", "Wienerberger"), ("VOE", "voestalpine"),
    ("RBI", "Raiffeisen Bank International"), ("POS", "Porr"), ("CAI", "CA Immo"),
    ("EVN", "EVN"), ("ATS", "AT&S"), ("POST", "Österreichische Post"),
    ("VIG", "Vienna Insurance Group"), ("LNZ", "Lenzing"), ("UQA", "Uniqa Insurance Group"),
    ("SBO", "Schoeller-Bleckmann Oilfield Equipment"), ("IIA", "Immofinanz"),
    ("STR", "Strabag"), ("DOC", "DO & CO"),
)

MARKETS: dict[str, Market] = {m.key: m for m in (
    _market("sp500", "US", "List_of_S%26P_500_companies", gics="GICS Sector"),
    _market("sp400", "US", "List_of_S%26P_400_companies", gics="GICS Sector"),
    _market("ftse100", "UK", "FTSE_100_Index", ".L"),
    _market("ftse250", "UK", "FTSE_250_Index", ".L"),
    _market("dax", "Germany", "DAX", ".DE"),
    _market("mdax", "Germany", "MDAX", ".DE"),
    _market("cac40", "France", "CAC_40", ".PA"),
    _market("cacnext20", "France", "CAC_Next_20", ".PA"),
    _market("aex", "Netherlands", "AEX_index", ".AS"),
    _market("amx", "Netherlands", "AMX_index", ".AS"),
    _market("bel20", "Belgium", "BEL_20", ".BR"),
    _market("psi", "Portugal", "PSI-20", ".LS"),
    _market("iseq20", "Ireland", "ISEQ_20", ".IR"),
    _market("ftsemib", "Italy", "FTSE_MIB", ".MI"),
    _market("smi", "Switzerland", "Swiss_Market_Index", ".SW"),
    _market("smim", "Switzerland", "SMI_MID", ".SW"),
    Market("atx", "Austria", (), ".VI", None, ATX_MEMBERS),
    _market("ibex35", "Spain", "IBEX_35", ".MC"),
    _market("omxs30", "Sweden", "OMX_Stockholm_30", ".ST"),
    _market("omxc25", "Denmark", "OMX_Copenhagen_25", ".CO"),
    _market("omxh25", "Finland", "OMX_Helsinki_25", ".HE"),
    _market("obx", "Norway", "OBX_Index", ".OL"),
    _market("tsxcomp", "Canada", "S%26P/TSX_Composite_Index", ".TO",
            "List_of_companies_listed_on_the_S%26P/TSX_Composite_Index"),
)}

TICKER_COLUMNS = ("ticker", "symbol", "code", "ticker symbol", "mnem code")
NAME_COLUMNS = ("company", "name", "security", "constituent")
MIN_TICKERS = 10
_DROPPED_WORDS = {"plc", "nv", "sa", "ag", "se", "ab", "asa", "as", "oyj", "spa", "group",
                  "holding", "holdings"}


def normalise_code(code: str, market: Market) -> str:
    """A Wikipedia ticker cell as Yahoo writes it: exchange prefix/suffix off, dashes, suffix on."""
    code = re.sub(r"^[^:]+:\s*", "", str(code).strip())
    if market.suffix and code.endswith(market.suffix):
        code = code[: -len(market.suffix)]
    code = code.removesuffix(".")
    return re.sub(r"[.\s]+", "-", code) + market.suffix


def name_key(name: str) -> str:
    """A company name reduced so one firm's listings in two markets compare equal."""
    words = re.sub(r"[^\w\s]", "", str(name).lower()).split()
    return " ".join(w for w in words if w not in _DROPPED_WORDS)


def _column(table: pd.DataFrame, wanted: tuple[str, ...]):
    for col in table.columns:
        label = " ".join(map(str, col)) if isinstance(col, tuple) else str(col)
        if label.strip().lower() in wanted:
            return col
    return None


def fetch_market(market: Market) -> pd.DataFrame:
    """One market's members as ticker, name, sector, market, country; empty when no page has a table.

    A table is usable with a ticker column, a name column and at least MIN_TICKERS rows.
    A failing request raises.
    """
    cols = ["ticker", "name", "sector", "market", "country"]
    if market.tickers:
        frame = pd.DataFrame(market.tickers, columns=["ticker", "name"])
        frame["ticker"] = [normalise_code(t, market) for t in frame["ticker"]]
        frame["sector"], frame["market"], frame["country"] = None, market.key, market.country
        return frame[cols]
    for page in market.pages:
        response = requests.get(page, headers=HEADERS, timeout=30)
        response.raise_for_status()
        for table in pd.read_html(StringIO(response.text)):
            tick, name = _column(table, TICKER_COLUMNS), _column(table, NAME_COLUMNS)
            if tick is None or name is None or len(table) < MIN_TICKERS:
                continue
            gics = _column(table, (market.gics_column.lower(),)) if market.gics_column else None
            frame = pd.DataFrame({
                "ticker": table[tick],
                "name": table[name],
                "sector": table[gics] if gics is not None else None,
            }).dropna(subset=["ticker", "name"])
            frame["ticker"] = [normalise_code(t, market) for t in frame["ticker"]]
            frame["market"], frame["country"] = market.key, market.country
            return frame.reset_index(drop=True)
    return pd.DataFrame(columns=cols)


UNIVERSE_COLUMNS = ["ticker", "name", "sector", "index", "market", "country"]


def _cached_universe(paths: Paths) -> pd.DataFrame | None:
    if not paths.universe.exists():
        return None
    cached = pd.read_csv(paths.universe)
    for col in UNIVERSE_COLUMNS:
        if col not in cached.columns:
            cached[col] = None
    return cached


def universe(paths: Paths, indexes: list[str]) -> pd.DataFrame:
    """Members of every listed market as ticker, name, sector, index, market, country.

    ``index`` equals ``market``. A market that cannot be fetched or gives under MIN_TICKERS
    tickers keeps its rows from the cached CSV; with nothing fetched at all the cache is returned.
    """
    for key in indexes:
        if key not in MARKETS:
            raise ValueError(f"unknown universe.indexes entry: {key}")
    cached = _cached_universe(paths)
    seen_names: dict[str, str] = {}
    seen_tickers: set[str] = set()
    rows = []
    for key in indexes:
        try:
            members = fetch_market(MARKETS[key])
        except Exception as exc:  # noqa: BLE001 - a market that fails is treated as empty
            log.warning("%s: could not fetch (%s)", key, exc)
            members = pd.DataFrame()
        if len(members) < MIN_TICKERS:
            kept = cached[cached["market"] == key] if cached is not None else pd.DataFrame()
            log.warning("%s: %d tickers from Wikipedia; keeping %d cached rows",
                        key, len(members), len(kept))
            members = kept
        for rec in members.to_dict("records"):
            nk = name_key(rec["name"])
            if nk and nk in seen_names:
                log.info("cross-listing dropped: %s (%s) duplicates %s",
                         rec["ticker"], key, seen_names[nk])
                continue
            if rec["ticker"] in seen_tickers:
                continue
            seen_tickers.add(rec["ticker"])
            if nk:
                seen_names[nk] = rec["ticker"]
            rows.append({**rec, "index": key})
    if not rows:
        if cached is not None:
            log.warning("could not fetch the index lists; using cached %s", paths.universe)
            return cached
        raise DataUnavailable("could not fetch the index lists and no cache exists")
    df = pd.DataFrame(rows, columns=UNIVERSE_COLUMNS)
    paths.root.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.universe, index=False)
    return df


# --- fundamentals --------------------------------------------------------------

def closes(ticker: str, days: int = 420) -> list[float]:
    hist = yf.Ticker(ticker).history(period=f"{days}d", auto_adjust=True)
    return [float(x) for x in hist["Close"].dropna()] if not hist.empty else []


def fundamentals(ticker: str, paths: Paths, cache_days: int = 7) -> dict:
    """Annual and quarterly statements, market data and ~400 days of closes for one ticker, cached."""
    cache = paths.cache / f"{ticker}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        fresh = datetime.fromisoformat(data["fetched_at"]) > datetime.now() - timedelta(days=cache_days)
        # A record cached after a failed quarterly fetch has no "quarters": fetched again.
        if fresh and data.get("schema") == CACHE_SCHEMA and "quarters" in data:
            return {**data, "from_cache": True}
    t = yf.Ticker(ticker)
    try:
        info = t.info or {}
        years = statement_years(t.income_stmt, t.balance_sheet, t.cashflow)
        series = closes(ticker)
    except Exception as exc:  # noqa: BLE001 - Yahoo raises many types
        raise DataUnavailable(f"Yahoo request failed: {exc}") from exc
    if not years:
        raise DataUnavailable("no annual statements")
    quarters, complete = fetch_quarters(t)   # fail-open: the lane columns stay blank without them
    data = {
        "schema": CACHE_SCHEMA,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "market_cap": info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice") or (series[-1] if series else None),
        "shares": info.get("sharesOutstanding"),
        "currency": info.get("currency"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "years": years,
        "quarters": quarters,
        "closes": series,
    }
    paths.cache.mkdir(parents=True, exist_ok=True)
    stored = data if complete else {k: v for k, v in data.items() if k != "quarters"}
    cache.write_text(json.dumps(stored), encoding="utf-8")
    return data


def cached_sector(ticker: str, paths: Paths) -> str | None:
    """The sector from the screen's fundamentals cache, whatever its age; None when never fetched."""
    cache = paths.cache / f"{ticker}.json"
    if not cache.exists():
        return None
    try:
        sector = json.loads(cache.read_text(encoding="utf-8")).get("sector")
    except (OSError, ValueError):
        return None
    return sector if isinstance(sector, str) else None


# --- prices ---------------------------------------------------------------------

def latest_price(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        return float(hist["Close"].dropna().iloc[-1]) if not hist.empty else None
    except Exception as exc:  # noqa: BLE001
        log.warning("no price for %s: %s", ticker, exc)
        return None


def quote_fx(ticker: str) -> tuple[str | None, float | None]:
    """The quote currency and US dollars per quoted unit (a pence quote's rate is the pound's / 100).

    ``(None, 1.0)`` when the currency cannot be read (taken as dollars, as before);
    ``(currency, None)`` when its rate is unknown, so nothing is sized in the wrong unit.
    """
    try:
        currency = yf.Ticker(ticker).fast_info["currency"]
    except Exception as exc:  # noqa: BLE001
        log.warning("no quote currency for %s: %s", ticker, exc)
        return None, 1.0
    if not currency:
        return None, 1.0
    if currency == "USD":
        return currency, 1.0
    rate = usd_rate(currency)
    if rate is None:
        return currency, None
    return currency, rate / 100 if currency in MINOR_UNITS else rate


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


def close_on_or_before(ticker: str, day: str, cache_dir: Path | None = None,
                       fetch: bool = True) -> float | None:
    """The last unadjusted close on or before ``day``, cached as JSON per ticker, keyed by day.

    The cache is ``<cache_dir>/<ticker>.json`` (by default ``data/cache/prices/``). With
    ``fetch=False`` only the cache is read, so a page can call it on every request. Fail-open:
    None when Yahoo has no close in the ten days up to ``day`` or the request fails; a miss is
    not cached, and neither is a close for today or later (the day is not over).
    """
    cache = (cache_dir or Paths().cache / "prices") / f"{ticker}.json"
    try:
        known = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    except (OSError, ValueError):
        known = {}
    if day in known:
        return known[day]
    if not fetch:
        return None
    start = (date.fromisoformat(day) - timedelta(days=10)).isoformat()
    try:
        s = closes_between(ticker, start, day)
        s = s[[d <= date.fromisoformat(day) for d in s.index]] if len(s) else s
        price = float(s.iloc[-1]) if len(s) else None
    except Exception as exc:  # noqa: BLE001 - Yahoo raises many types
        log.warning("no close for %s on %s: %s", ticker, day, exc)
        return None
    if price is not None and date.fromisoformat(day) < date.today():
        known[day] = price
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            log.warning("could not cache the close for %s: %s", ticker, exc)
    return price



def fill_close_cache(ticker: str, days, cache_dir: Path | None = None,
                     today: date | None = None) -> None:
    """Cache the close on or before each past day in ``days`` from one Yahoo request.

    One ``closes_between`` call from ten days before the earliest uncached day to ``today``
    replaces a call per day; ``close_on_or_before`` then finds them in the cache. Days already
    cached, today or later, or with no close in the ten days up to them are left alone.
    Fail-open: a failed request caches nothing.
    """
    cache = (cache_dir or Paths().cache / "prices") / f"{ticker}.json"
    try:
        known = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    except (OSError, ValueError):
        known = {}
    stop = min(today or date.today(), date.today())
    wanted = sorted({d for d in days if d not in known and date.fromisoformat(d) < stop})
    if not wanted:
        return
    start = (date.fromisoformat(wanted[0]) - timedelta(days=10)).isoformat()
    try:
        s = closes_between(ticker, start, (today or date.today()).isoformat())
    except Exception as exc:  # noqa: BLE001 - Yahoo raises many types
        log.warning("no closes for %s from %s: %s", ticker, start, exc)
        return
    added = False
    for d in wanted:
        day = date.fromisoformat(d)
        window = [float(v) for k, v in s.items() if day - timedelta(days=10) <= k <= day]
        if window:
            known[d], added = window[-1], True
    if added:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            log.warning("could not cache the closes for %s: %s", ticker, exc)

def throttle(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
