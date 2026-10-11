"""Screen-only metrics: Magic Formula ranking and the trend timing filter.

The per-company metrics live in tradingagents.dataflows.value_metrics, shared
with the agents' get_value_metrics tool so both read the same figures.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from tradingagents.dataflows.value_metrics import (  # noqa: F401 - re-exported for the screen
    FSCORE_POINTS,
    YEAR_FIELDS,
    altman_z,
    debt_to_equity,
    earnings_yield,
    enterprise_value,
    fcf_yield,
    formula_value,
    interest_coverage,
    num,
    piotroski,
    roic,
)

TRENDS = ("UPTREND", "BASING", "DOWNTREND", "FREEFALL")
# A 200-day SMA moving less than this in a month counts as flat.
_FLAT = 0.005


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
