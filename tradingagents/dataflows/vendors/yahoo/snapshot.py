"""Deterministic market-data verification snapshot.

The market analyst is an LLM that can confabulate exact numbers — citing a
Bollinger band or a "historically validated bounce" that the underlying data
doesn't support (#830). This module computes a ground-truth snapshot (latest
OHLCV row on or before the analysis date, common indicators, recent closes)
the analyst is told to treat as the source of truth for any exact numeric
claim. Deterministic, no LLM involved.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol
from tradingagents.dataflows.vendors.yahoo.fundamentals import next_earnings_date
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

# A fixed, common indicator set so the snapshot is the same shape every run.
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _verified_rows(symbol: str, as_of_date: str) -> pd.DataFrame:
    """OHLCV on or before as_of_date, date-sorted. Raises NoMarketDataError if nothing usable.

    ``load_ohlcv`` already normalizes the Date column and filters out
    look-ahead rows, but we re-apply the cutoff defensively — this is a
    verification path, so it must not trust its input to be pre-filtered.
    """
    # As reported: this snapshot is quoted by the agents as exact prices, so a
    # gap-filled cell would put the previous session's number under this date.
    data = load_ohlcv(symbol, as_of_date, fill_gaps=False)
    if data is None or data.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), "no price rows")

    df = data.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(as_of_date)].sort_values("Date")
    if df.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), f"no price rows on or before {as_of_date}")
    return df


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _earnings_line(symbol: str, as_of_date: str) -> str:
    """The next earnings date and days to it, for a stock analysed today.

    Yahoo's calendar holds only today's next date, so a past-dated run (it would
    be a fact from after that day), a crypto pair, and a date before ``as_of_date``
    (stale) are all unknown.
    """
    if is_historical(as_of_date) or crypto_base(symbol):
        return "- Next earnings: unknown"
    try:
        found = next_earnings_date(symbol)
        days = (date.fromisoformat(found) - date.fromisoformat(as_of_date)).days if found else -1
    except Exception:  # noqa: BLE001
        found, days = None, -1
    if found is None or days < 0:
        return "- Next earnings: unknown"
    return f"- Next earnings: {found} ({days} days)"


def _signed_pct(x: float) -> str:
    return f"{x:+.1%}".replace("-", "−")


def _move_lines(df: pd.DataFrame) -> list[str]:
    """The 3-month move (on the last close at least three calendar months before the latest row)
    and the distance from the 52-week closing high, from the whole frame (not the look-back
    window), so a dislocation reads the same in every report."""
    latest_date, price = df["Date"].iloc[-1], float(df["Close"].iloc[-1])
    closes = df.dropna(subset=["Close"])
    before = closes[closes["Date"] <= latest_date - pd.DateOffset(months=3)]
    if before.empty or not float(before["Close"].iloc[-1]) > 0:
        move = "- 3-month move: n/a (under three months of prices)"
    else:
        then = before.iloc[-1]
        move = (f"- 3-month move: {_signed_pct(price / float(then['Close']) - 1)} "
                f"(close {float(then['Close']):.2f} on {_fmt(then['Date'])})")
    start = latest_date - pd.DateOffset(weeks=52)
    if closes["Date"].iloc[0] > start:
        high = "- From 52-week high: n/a (under a year of prices)"
    else:
        year = closes[closes["Date"] > start]
        top = year.loc[year["Close"].idxmax()]
        high = (f"- From 52-week high: {_signed_pct(price / float(top['Close']) - 1)} "
                f"(high {float(top['Close']):.2f} on {_fmt(top['Date'])})")
    return [move, high]


def build_verified_market_snapshot(
    symbol: str,
    as_of_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> str:
    """Render a ground-truth snapshot: latest OHLCV row, indicators, recent closes."""
    # `df` keeps the original capitalized OHLCV columns (Open/High/Low/Close/
    # Volume); stockstats `wrap()` lowercases columns and adds indicator
    # columns, so read raw prices from `df` and indicators from `stock_df`.
    df = _verified_rows(symbol, as_of_date)
    stock_df = wrap(df.copy())

    selected = tuple(indicators or DEFAULT_SNAPSHOT_INDICATORS)
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]  # triggers stockstats calculation
            indicator_values[name] = _fmt(stock_df.iloc[-1][name])
        except Exception as exc:  # noqa: BLE001 — one bad indicator shouldn't sink the snapshot
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {as_of_date}",
        f"- Latest trading row used: {latest_date}",
        _earnings_line(symbol, as_of_date),
        *_move_lines(df),
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        lines.append(f"| {field} | {_fmt(latest.get(field))} |")

    lines += ["", "### Verified technical indicators (latest row)", "",
              "| Indicator | Value |", "|---|---:|"]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += ["", f"### Recent verified closes (last {len(recent)} rows)", "",
              "| Date | Close |", "|---|---:|"]
    for _, row in recent.iterrows():
        lines.append(f"| {_fmt(row['Date'])} | {_fmt(row.get('Close'))} |")

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return "\n".join(lines)
