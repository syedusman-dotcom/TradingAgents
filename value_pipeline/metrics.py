"""Screen-only metrics: Magic Formula ranking and the trend timing filter.

The per-company metrics live in tradingagents.dataflows.value_metrics, shared
with the agents' get_value_metrics tool so both read the same figures.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from tradingagents.dataflows.value_metrics import (  # noqa: F401 - re-exported for the screen
    DISCOUNT_RATE,
    FSCORE_POINTS,
    YEAR_FIELDS,
    accounting_checks,
    altman_z,
    current_discount_rate,
    debt_to_equity,
    earnings_yield,
    enterprise_value,
    fcf_yield,
    formula_value,
    interest_coverage,
    num,
    piotroski,
    roic,
    share_count,
)

TRENDS = ("UPTREND", "BASING", "DOWNTREND", "FREEFALL")
# A 200-day SMA moving less than this in a month counts as flat.
_FLAT = 0.005


def normalized(years: list[dict], n: int = 5) -> dict:
    """Multi-year figures that keep one good (or bad) year from deciding the rank.

    ``ebit_avg`` and ``roic_avg`` average up to ``n`` years; ``roic_min`` is the
    worst of them (a stable business never had a bad year); ``dilution_per_year``
    is the compound annual change in share count over the same years.
    """
    recent = years[:n]
    ebits = [v for v in (num(y.get("ebit")) for y in recent) if v is not None]
    roics = [v for v in (roic(y) for y in recent) if v is not None]
    shares = [(i, num(y.get("shares"))) for i, y in enumerate(recent) if num(y.get("shares"))]
    dilution = None
    if len(shares) >= 2:
        (i_new, new), (i_old, old) = shares[0], shares[-1]
        dilution = (new / old) ** (1 / (i_old - i_new)) - 1
    return {
        "ebit_avg": sum(ebits) / len(ebits) if ebits else None,
        "roic_avg": sum(roics) / len(roics) if roics else None,
        "roic_min": min(roics) if roics else None,
        "roic_years": len(roics),
        "dilution_per_year": dilution,
    }


def magic_formula_rank(rows: list[dict], ey_key: str = "earnings_yield", roic_key: str = "roic") -> list[dict]:
    """Rank by earnings yield and by ROIC (1 = best), sum the ranks, lower is better.

    Ties on the combined rank go to the higher FCF yield. Rows missing either
    metric are left out. Returns new dicts with ey_rank, roic_rank, mf_score.
    """
    eligible = [dict(r) for r in rows if r.get(ey_key) is not None and r.get(roic_key) is not None]
    for key, rank_key in ((ey_key, "ey_rank"), (roic_key, "roic_rank")):
        ordered = sorted(eligible, key=lambda r: -r[key])
        for i, r in enumerate(ordered):
            # Equal values share the better rank.
            r[rank_key] = i + 1 if i == 0 or r[key] != ordered[i - 1][key] else ordered[i - 1][rank_key]
    for r in eligible:
        r["mf_score"] = r["ey_rank"] + r["roic_rank"]
    return sorted(eligible, key=lambda r: (r["mf_score"], -(r.get("fcf_yield") or -math.inf)))


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
    closes = [float(c) for c in closes if num(c) is not None]
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
