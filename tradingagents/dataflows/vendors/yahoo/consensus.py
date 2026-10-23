"""The analysts' consensus from Yahoo: next and following fiscal year revenue growth, next year's
EPS growth, how many analysts, and their price targets.

Each figure is fetched and read on its own and fails open, so one missing frame leaves the rest;
``{}`` when Yahoo has nothing at all. Today's estimates only: callers never use it for a past date.
"""

from __future__ import annotations

import yfinance as yf

from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.value_metrics import num
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

KEYS = ("revenue_growth_next", "revenue_growth_following", "eps_growth_next", "analysts",
        "target_mean", "target_median", "target_high", "target_low")


def _fetch(symbol: str, attr: str):
    """``yf.Ticker(symbol).<attr>``; None when the request or Yahoo fails."""
    try:
        return yf_retry(lambda: getattr(yf.Ticker(symbol), attr))
    except Exception:  # noqa: BLE001 - fail open per figure
        return None


def _cell(frame, period: str, column: str) -> float | None:
    """One estimate (``0y`` / ``+1y`` row, ``growth`` / ``numberOfAnalysts`` column); None when absent."""
    try:
        if frame is None or frame.empty:
            return None
        return num(frame.at[period, column])
    except Exception:  # noqa: BLE001 - a missing row or column is no estimate
        return None


def consensus(symbol: str) -> dict:
    """``KEYS`` as fractions (growth), a count (analysts) and quote-currency prices (targets), None
    where Yahoo has none; ``{}`` when it has none of them."""
    try:
        canonical = normalize_symbol(symbol)
    except Exception:  # noqa: BLE001
        return {}
    revenue = _fetch(canonical, "revenue_estimate")
    earnings = _fetch(canonical, "earnings_estimate")
    targets = _fetch(canonical, "analyst_price_targets")
    targets = targets if isinstance(targets, dict) else {}
    analysts = _cell(revenue, "0y", "numberOfAnalysts")
    out = {
        "revenue_growth_next": _cell(revenue, "0y", "growth"),
        "revenue_growth_following": _cell(revenue, "+1y", "growth"),
        "eps_growth_next": _cell(earnings, "0y", "growth"),
        "analysts": int(analysts) if analysts is not None else None,
        **{f"target_{k}": num(targets.get(k)) for k in ("mean", "median", "high", "low")},
    }
    return out if any(v is not None for v in out.values()) else {}
