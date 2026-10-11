"""Value metrics computed from plain numbers, so they can be tested without the network.

A fiscal year is a dict of the fields below (None when not reported). Years are
passed newest first. See market_data.py for how they are read from Yahoo.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

YEAR_FIELDS = (
    "revenue", "gross_profit", "ebit", "net_income", "interest_expense",
    "total_assets", "current_assets", "current_liabilities", "cash", "net_ppe",
    "long_term_debt", "total_debt", "equity", "retained_earnings", "total_liabilities",
    "shares", "operating_cash_flow", "free_cash_flow",
)

TRENDS = ("UPTREND", "BASING", "DOWNTREND", "FREEFALL")
# A 200-day SMA moving less than this in a month counts as flat.
_FLAT = 0.005


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _div(a, b) -> float | None:
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


# --- Greenblatt -------------------------------------------------------------

def enterprise_value(market_cap, total_debt, cash) -> float | None:
    market_cap = _num(market_cap)
    if market_cap is None:
        return None
    return market_cap + (_num(total_debt) or 0.0) - (_num(cash) or 0.0)


def earnings_yield(ebit, ev) -> float | None:
    """EBIT / enterprise value. Undefined for a non-positive EV."""
    ev = _num(ev)
    if ev is None or ev <= 0:
        return None
    return _div(ebit, ev)


def roic(year: dict) -> float | None:
    """Greenblatt's return on capital: EBIT / (net working capital + net fixed assets).

    Working capital excludes cash and is floored at zero, as Greenblatt does: a
    business its suppliers finance (negative working capital) is not credited
    with negative capital.
    """
    ca, cl, ppe = _num(year.get("current_assets")), _num(year.get("current_liabilities")), _num(year.get("net_ppe"))
    if ca is None or cl is None or ppe is None:
        return None
    nwc = max(0.0, ca - (_num(year.get("cash")) or 0.0) - cl)
    capital = nwc + ppe
    if capital <= 0:
        return None
    return _div(year.get("ebit"), capital)


def fcf_yield(free_cash_flow, market_cap) -> float | None:
    market_cap = _num(market_cap)
    if market_cap is None or market_cap <= 0:
        return None
    return _div(free_cash_flow, market_cap)


def magic_formula_rank(rows: list[dict]) -> list[dict]:
    """Rank by earnings yield and by ROIC (1 = best), sum the ranks, lower is better.

    Ties on the combined rank go to the higher FCF yield. Rows missing either
    metric are left out. Returns new dicts with ey_rank, roic_rank, mf_score.
    """
    eligible = [dict(r) for r in rows if r.get("earnings_yield") is not None and r.get("roic") is not None]
    for key, rank_key in (("earnings_yield", "ey_rank"), ("roic", "roic_rank")):
        ordered = sorted(eligible, key=lambda r: -r[key])
        for i, r in enumerate(ordered):
            # Equal values share the better rank.
            r[rank_key] = i + 1 if i == 0 or r[key] != ordered[i - 1][key] else ordered[i - 1][rank_key]
    for r in eligible:
        r["mf_score"] = r["ey_rank"] + r["roic_rank"]
    return sorted(eligible, key=lambda r: (r["mf_score"], -(r.get("fcf_yield") or -math.inf)))


# --- Piotroski ---------------------------------------------------------------

FSCORE_POINTS = (
    "roa_positive", "cfo_positive", "roa_improving", "cfo_exceeds_net_income",
    "leverage_falling", "current_ratio_rising", "no_dilution",
    "gross_margin_rising", "asset_turnover_rising",
)


def piotroski(cur: dict, prev: dict) -> dict:
    """The nine Piotroski points from two consecutive fiscal years.

    Each point is True, False, or None when the data is missing; ``score``
    counts the True points, ``missing`` the None ones. Ratios use year-end
    total assets.
    """
    def gt(a, b):
        return None if a is None or b is None else a > b

    roa, roa_prev = _div(cur.get("net_income"), cur.get("total_assets")), _div(prev.get("net_income"), prev.get("total_assets"))
    cfo = _num(cur.get("operating_cash_flow"))
    lev, lev_prev = _div(cur.get("long_term_debt") or 0.0, cur.get("total_assets")), _div(prev.get("long_term_debt") or 0.0, prev.get("total_assets"))
    cr, cr_prev = _div(cur.get("current_assets"), cur.get("current_liabilities")), _div(prev.get("current_assets"), prev.get("current_liabilities"))
    shares, shares_prev = _num(cur.get("shares")), _num(prev.get("shares"))
    gm, gm_prev = _div(cur.get("gross_profit"), cur.get("revenue")), _div(prev.get("gross_profit"), prev.get("revenue"))
    at, at_prev = _div(cur.get("revenue"), cur.get("total_assets")), _div(prev.get("revenue"), prev.get("total_assets"))

    points = {
        "roa_positive": gt(roa, 0.0),
        "cfo_positive": gt(cfo, 0.0),
        "roa_improving": gt(roa, roa_prev),
        "cfo_exceeds_net_income": gt(cfo, _num(cur.get("net_income"))),
        "leverage_falling": None if lev is None or lev_prev is None else lev <= lev_prev,
        "current_ratio_rising": gt(cr, cr_prev),
        "no_dilution": None if shares is None or shares_prev is None else shares <= shares_prev,
        "gross_margin_rising": gt(gm, gm_prev),
        "asset_turnover_rising": gt(at, at_prev),
    }
    return {
        "points": points,
        "score": sum(1 for v in points.values() if v is True),
        "missing": sum(1 for v in points.values() if v is None),
    }


# --- Altman, balance-sheet safety --------------------------------------------

def altman_z(year: dict, market_cap) -> float | None:
    """Original Altman Z: 1.2 WC/TA + 1.4 RE/TA + 3.3 EBIT/TA + 0.6 MVE/TL + 1.0 Sales/TA."""
    parts = [_num(year.get(k)) for k in ("total_assets", "total_liabilities", "current_assets",
                                          "current_liabilities", "retained_earnings", "ebit", "revenue")]
    if any(p is None for p in parts) or _num(market_cap) is None:
        return None
    ta, tl, ca, cl, re, ebit, sales = parts
    if ta <= 0 or tl <= 0:
        return None
    return 1.2 * (ca - cl) / ta + 1.4 * re / ta + 3.3 * ebit / ta + 0.6 * _num(market_cap) / tl + 1.0 * sales / ta


def debt_to_equity(year: dict) -> float | None:
    equity = _num(year.get("equity"))
    if equity is None or equity <= 0:
        return None
    return (_num(year.get("total_debt")) or 0.0) / equity


def interest_coverage(year: dict) -> float | None:
    """EBIT / interest expense. No reported interest expense on positive EBIT reads as no
    interest burden (infinite coverage); Yahoo reports debt-free companies that way."""
    ebit = _num(year.get("ebit"))
    if ebit is None:
        return None
    interest = abs(_num(year.get("interest_expense")) or 0.0)
    if interest == 0:
        return math.inf if ebit > 0 else None
    return ebit / interest


# --- trend (timing filter only) -----------------------------------------------

def trend(closes: Sequence[float]) -> dict:
    """Classify a daily close series (oldest first, ~400 trading days) as a timing filter.

    UPTREND: above a 200-day SMA that is not falling. FREEFALL: below a falling
    200-day SMA and making new lows (the last 20 days went below the rest of the
    year's low). DOWNTREND: below a falling 200-day SMA without new lows.
    BASING: anything else, i.e. stabilising after a decline or recovering through
    a falling average. The SMA is "falling" only if it lost more than 0.5% in a
    month, so a stock flat at its lows reads as BASING, not FREEFALL.
    """
    closes = [float(c) for c in closes if _num(c) is not None]
    if len(closes) < 220:
        return {"trend": None, "reason": f"only {len(closes)} daily closes; need 220+"}

    def sma(end: int, n: int = 200) -> float:
        window = closes[end - n:end]
        return sum(window) / n

    price = closes[-1]
    sma200, sma200_month_ago = sma(len(closes)), sma(len(closes) - 20)
    sma50 = sum(closes[-50:]) / 50
    year = closes[-252:]
    high, low = max(year), min(year)
    momentum_12_1 = closes[-21] / closes[-252] - 1 if len(closes) >= 252 else None

    above = price > sma200
    falling = sma200 < sma200_month_ago * (1 - _FLAT)
    new_lows = min(closes[-20:]) < min(closes[-252:-20])

    if above and not falling:
        label = "UPTREND"
    elif not above and falling and new_lows:
        label = "FREEFALL"
    elif not above and falling:
        label = "DOWNTREND"
    else:
        label = "BASING"
    return {
        "trend": label,
        "price": price,
        "vs_sma200": price / sma200 - 1,
        "sma50_vs_sma200": sma50 / sma200 - 1,
        "sma200_falling": falling,
        "momentum_12_1": momentum_12_1,
        "from_52w_high": price / high - 1,
        "from_52w_low": price / low - 1,
    }
