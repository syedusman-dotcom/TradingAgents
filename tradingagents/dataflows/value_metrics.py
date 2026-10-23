"""Value-investing metrics from annual statements: return on capital, yields,
Piotroski F-Score, Altman Z-Score, balance-sheet safety, and a formula valuation.

Pure functions over plain numbers, shared by the agents' ``get_value_metrics``
tool and the value pipeline's screen, so both read the same figures. A fiscal
year is a dict of ``YEAR_FIELDS`` (None when not reported); years go newest first.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from statistics import mean, median

logger = logging.getLogger(__name__)

YEAR_FIELDS = (
    "revenue", "gross_profit", "ebit", "net_income", "interest_expense",
    "total_assets", "current_assets", "current_liabilities", "cash", "net_ppe",
    "long_term_debt", "total_debt", "equity", "retained_earnings", "total_liabilities",
    "shares", "operating_cash_flow", "free_cash_flow",
    "capex", "dividends_paid", "buybacks", "stock_based_comp",
    "receivables", "depreciation", "sga", "acquisitions",
)

def num(x) -> float | None:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def div(a, b) -> float | None:
    a, b = num(a), num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


# --- Greenblatt -------------------------------------------------------------

def share_count(market_cap, price, statement=None, reported=None) -> float | None:
    """The shares every per-share figure divides by: market cap / price first (it covers every
    share class; Yahoo's sharesOutstanding can count one class only, FOX and GOOGL among them),
    then the statements' count, then the reported one."""
    market_cap, price = num(market_cap), num(price)
    if market_cap and price and market_cap > 0 and price > 0:
        return market_cap / price
    return num(statement) or num(reported) or None


def enterprise_value(market_cap, total_debt, cash) -> float | None:
    market_cap = num(market_cap)
    if market_cap is None:
        return None
    return market_cap + (num(total_debt) or 0.0) - (num(cash) or 0.0)


def earnings_yield(ebit, ev) -> float | None:
    """EBIT / enterprise value. Undefined for a non-positive EV."""
    ev = num(ev)
    if ev is None or ev <= 0:
        return None
    return div(ebit, ev)


def roic(year: dict) -> float | None:
    """Greenblatt's return on capital: EBIT / (net working capital + net fixed assets).

    Working capital excludes cash and is floored at zero, as Greenblatt does: a
    business its suppliers finance (negative working capital) is not credited
    with negative capital.
    """
    ca, cl, ppe = num(year.get("current_assets")), num(year.get("current_liabilities")), num(year.get("net_ppe"))
    if ca is None or cl is None or ppe is None:
        return None
    nwc = max(0.0, ca - (num(year.get("cash")) or 0.0) - cl)
    capital = nwc + ppe
    if capital <= 0:
        return None
    return div(year.get("ebit"), capital)


def fcf_yield(free_cash_flow, market_cap) -> float | None:
    market_cap = num(market_cap)
    if market_cap is None or market_cap <= 0:
        return None
    return div(free_cash_flow, market_cap)

# --- Piotroski ---------------------------------------------------------------

FSCORE_POINTS = (
    "roa_positive", "cfo_positive", "roa_improving", "cfo_exceeds_net_income",
    "leverage_falling", "current_ratio_rising", "no_dilution",
    "gross_margin_rising", "asset_turnover_rising",
)


def _long_term_debt(year: dict) -> float | None:
    """A missing long-term-debt row means zero for a debt-free company and unknown otherwise."""
    ltd = num(year.get("long_term_debt"))
    if ltd is not None:
        return ltd
    return 0.0 if not (num(year.get("total_debt")) or 0.0) else None


def piotroski(cur: dict, prev: dict) -> dict:
    """The nine Piotroski points from two consecutive fiscal years.

    Each point is True, False, or None when the data is missing; ``score``
    counts the True points, ``missing`` the None ones. Ratios use year-end
    total assets.
    """
    def gt(a, b):
        return None if a is None or b is None else a > b

    roa, roa_prev = div(cur.get("net_income"), cur.get("total_assets")), div(prev.get("net_income"), prev.get("total_assets"))
    cfo = num(cur.get("operating_cash_flow"))
    lev, lev_prev = div(_long_term_debt(cur), cur.get("total_assets")), div(_long_term_debt(prev), prev.get("total_assets"))
    cr, cr_prev = div(cur.get("current_assets"), cur.get("current_liabilities")), div(prev.get("current_assets"), prev.get("current_liabilities"))
    shares, shares_prev = num(cur.get("shares")), num(prev.get("shares"))
    gm, gm_prev = div(cur.get("gross_profit"), cur.get("revenue")), div(prev.get("gross_profit"), prev.get("revenue"))
    at, at_prev = div(cur.get("revenue"), cur.get("total_assets")), div(prev.get("revenue"), prev.get("total_assets"))

    points = {
        "roa_positive": gt(roa, 0.0),
        "cfo_positive": gt(cfo, 0.0),
        "roa_improving": gt(roa, roa_prev),
        "cfo_exceeds_net_income": gt(cfo, num(cur.get("net_income"))),
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
    parts = [num(year.get(k)) for k in ("total_assets", "total_liabilities", "current_assets",
                                          "current_liabilities", "retained_earnings", "ebit", "revenue")]
    if any(p is None for p in parts) or num(market_cap) is None:
        return None
    ta, tl, ca, cl, re, ebit, sales = parts
    if ta <= 0 or tl <= 0:
        return None
    return 1.2 * (ca - cl) / ta + 1.4 * re / ta + 3.3 * ebit / ta + 0.6 * num(market_cap) / tl + 1.0 * sales / ta


def debt_to_equity(year: dict) -> float | None:
    equity = num(year.get("equity"))
    if equity is None or equity <= 0:
        return None
    return (num(year.get("total_debt")) or 0.0) / equity


def interest_coverage(year: dict) -> float | None:
    """EBIT / interest expense. No interest row on positive EBIT reads as no interest
    burden (infinite coverage) only when there is no debt either: Yahoo also drops the
    row for some borrowers, and for them the coverage is unknown, not infinite."""
    ebit = num(year.get("ebit"))
    if ebit is None:
        return None
    interest = abs(num(year.get("interest_expense")) or 0.0)
    if interest == 0:
        if ebit <= 0 or (num(year.get("total_debt")) or 0.0) > 0:
            return None
        return math.inf
    return ebit / interest


# --- accounting checks ----------------------------------------------------------------

ACCRUALS_LIMIT = 0.10         # (net income - operating cash flow) / average assets
CASH_CONVERSION_FLOOR = 0.8   # operating cash flow / net income, over three years
RECEIVABLES_GAP_LIMIT = 0.30  # receivables growth less revenue growth
BENEISH_LIMIT = -1.78         # M above this is the "likely manipulator" band
CAPEX_TO_DEPRECIATION_FLOOR = 0.5


def accruals_ratio(cur: dict, prev: dict | None) -> float | None:
    """(Net income - operating cash flow) / average total assets: profit not backed by cash."""
    ni, cfo, ta = (num(cur.get(k)) for k in ("net_income", "operating_cash_flow", "total_assets"))
    if ni is None or cfo is None or ta is None:
        return None
    ta_prev = num((prev or {}).get("total_assets"))
    return div(ni - cfo, (ta + ta_prev) / 2 if ta_prev is not None else ta)


def cash_conversion(years: list[dict], n: int = 3) -> float | None:
    """Operating cash flow / net income summed over up to ``n`` years; None unless profit is positive."""
    rows = [(num(y.get("operating_cash_flow")), num(y.get("net_income"))) for y in years[:n]]
    rows = [r for r in rows if r[0] is not None and r[1] is not None]
    if not rows:
        return None
    ni = sum(r[1] for r in rows)
    return div(sum(r[0] for r in rows), ni) if ni > 0 else None


def receivables_vs_revenue(cur: dict, prev: dict | None) -> float | None:
    """Receivables growth less revenue growth; positive means sales are piling up unpaid."""
    prev = prev or {}
    rec, rev = div(cur.get("receivables"), prev.get("receivables")), div(cur.get("revenue"), prev.get("revenue"))
    return None if rec is None or rev is None else rec - rev


def beneish_m(cur: dict, prev: dict | None) -> float | None:
    """Beneish's eight-variable M-score; None if any input is missing."""
    if not prev:
        return None
    a = {k: num(cur.get(k)) for k in ("revenue", "gross_profit", "receivables", "current_assets", "net_ppe",
                                "total_assets", "long_term_debt", "current_liabilities", "net_income",
                                "operating_cash_flow", "depreciation", "sga")}
    b = {k: num(prev.get(k)) for k in a}
    if any(v is None for v in (*a.values(), *b.values())):
        return None
    a["depreciation"], a["sga"] = abs(a["depreciation"]), abs(a["sga"])
    b["depreciation"], b["sga"] = abs(b["depreciation"]), abs(b["sga"])

    def margin(y):
        return div(y["gross_profit"], y["revenue"])

    def hard(y):
        v = div(y["current_assets"] + y["net_ppe"], y["total_assets"])
        return None if v is None else 1 - v

    def dep_rate(y):
        return div(y["depreciation"], y["depreciation"] + y["net_ppe"])

    def leverage(y):
        return div(y["long_term_debt"] + y["current_liabilities"], y["total_assets"])

    def ratio(f):
        return div(f(a), f(b))

    parts = (
        ratio(lambda y: div(y["receivables"], y["revenue"])),          # DSRI
        div(margin(b), margin(a)),                                      # GMI
        ratio(hard),                                                    # AQI
        div(a["revenue"], b["revenue"]),                                # SGI
        div(dep_rate(b), dep_rate(a)),                                  # DEPI
        ratio(lambda y: div(y["sga"], y["revenue"])),                   # SGAI
        div(a["net_income"] - a["operating_cash_flow"], a["total_assets"]),  # TATA
        ratio(leverage),                                                # LVGI
    )
    if any(p is None for p in parts):
        return None
    dsri, gmi, aqi, sgi, depi, sgai, tata, lvgi = parts
    return (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi
            - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)


def accounting_checks(years: list[dict]) -> dict:
    """The earnings-quality numbers and a plain-language flag for each breach."""
    cur = years[0] if years else {}
    prev = years[1] if len(years) > 1 else None
    out = {
        "accruals_ratio": accruals_ratio(cur, prev),
        "cash_conversion": cash_conversion(years),
        "receivables_vs_revenue": receivables_vs_revenue(cur, prev),
        "beneish_m": beneish_m(cur, prev),
    }
    flags = []
    if out["accruals_ratio"] is not None and out["accruals_ratio"] > ACCRUALS_LIMIT:
        flags.append(f"accruals ratio {out['accruals_ratio']:.0%} above {ACCRUALS_LIMIT:.0%}")
    if out["cash_conversion"] is not None and out["cash_conversion"] < CASH_CONVERSION_FLOOR:
        flags.append(f"cash conversion {out['cash_conversion']:.2f} below {CASH_CONVERSION_FLOOR} over 3 years")
    if out["receivables_vs_revenue"] is not None and out["receivables_vs_revenue"] > RECEIVABLES_GAP_LIMIT:
        flags.append(f"receivables grew {out['receivables_vs_revenue']:+.0%} faster than revenue")
    if out["beneish_m"] is not None and out["beneish_m"] > BENEISH_LIMIT:
        flags.append(f"Beneish M-score {out['beneish_m']:.2f} above {BENEISH_LIMIT}")
    ratios = [div(abs(num(y.get("capex")) or 0.0) if num(y.get("capex")) is not None else None,
                  abs(num(y.get("depreciation")) or 0.0)) for y in years[:2]]
    if len(ratios) == 2 and all(r is not None and r < CAPEX_TO_DEPRECIATION_FLOOR for r in ratios):
        flags.append("capex below half of depreciation for 2 years")
    out["flags"] = flags
    return out


# --- formula valuation -----------------------------------------------------------

# Value = normalized cash flow x (1 + g) / (r - g): a growing perpetuity, the
# multiple a required return r justifies at each growth rate (at 9%: 0% -> 11.1x,
# 3% -> 17.2x, 5% -> 26.3x). The scenarios state the assumption instead of hiding
# it in a multiple; which one fits the business is the analyst's call.
#
# r is the 10-year Treasury yield plus an equity premium, never below 9%: when
# risk-free bonds pay more, a stock has to promise more to be worth owning.
MIN_DISCOUNT_RATE = 0.09
EQUITY_PREMIUM = 0.045
DISCOUNT_RATE = MIN_DISCOUNT_RATE  # used when no rate is given
GROWTH_SCENARIOS = (0.0, 0.03, 0.05)
TAX_RATE = 0.21
NORMALIZE_YEARS = 5


def discount_rate(treasury_10y: float | None) -> float:
    """The required return for a 10-year Treasury yield (a fraction), to 0.1%; the floor when unknown."""
    if treasury_10y is None:
        return MIN_DISCOUNT_RATE
    return max(MIN_DISCOUNT_RATE, round(treasury_10y + EQUITY_PREMIUM, 3))


def _latest_10y() -> float:
    """The latest 10-year Treasury yield from FRED, as a fraction (needs FRED_API_KEY)."""
    from tradingagents.dataflows.vendors import fred

    data = fred._request("series/observations", {"series_id": "DGS10", "sort_order": "desc", "limit": 10})
    for obs in data.get("observations", []):
        if obs.get("value") not in (None, "", "."):
            return float(obs["value"]) / 100
    raise ValueError("no recent 10-year Treasury yield")


_rate_cache: dict[str, dict] = {}


def current_discount_rate(fetch=None) -> dict:
    """{"rate", "treasury_10y"} for today; the 9% floor (treasury_10y None) when FRED is unavailable.
    Fetched once a day per process."""
    key = date.today().isoformat()
    if fetch is None and key in _rate_cache:
        return _rate_cache[key]
    try:
        ten_year = (fetch or _latest_10y)()
    except Exception as exc:  # noqa: BLE001 - no key or FRED down: value at the floor, never fail
        logger.info("10-year Treasury yield unavailable, using the %.0f%% floor: %s", MIN_DISCOUNT_RATE * 100, exc)
        ten_year = None
    out = {"rate": discount_rate(ten_year), "treasury_10y": ten_year}
    if fetch is None and ten_year is not None:    # a failure is retried next time, not pinned for the day
        _rate_cache[key] = out
    return out


def perpetuity_multiple(growth: float, rate: float = DISCOUNT_RATE) -> float:
    return (1 + growth) / (rate - growth)


def owner_earnings(year: dict) -> float | None:
    """Free cash flow less stock-based pay: shares handed to staff are a cost to owners,
    which free cash flow leaves out (it adds the non-cash charge back)."""
    fcf = num(year.get("free_cash_flow"))
    if fcf is None:
        return None
    return fcf - abs(num(year.get("stock_based_comp")) or 0.0)


def formula_value(years: list[dict], price, shares, net_debt, rate: float = DISCOUNT_RATE) -> dict | None:
    """Per-share intrinsic value at 0% / 3% / 5% growth, from normalized cash flow.

    Two measures of normalized (up to five-year average) cash flow: owner earnings
    (free cash flow less stock-based pay) to equity, and after-tax EBIT to the whole firm, less net debt. Averaging damps
    one-off swings and keeps a cyclical peak from passing as normal earnings.
    ``base`` (3% growth) is the mean of the two; ``margin_of_safety`` is
    (base - price) / base. ``implied_growth`` is the perpetual FCF growth today's
    price assumes (a reverse DCF): what the market already expects.
    """
    price, shares, net_debt = num(price), num(shares), num(net_debt) or 0.0
    if not price or not shares or shares <= 0:
        return None
    recent = years[:NORMALIZE_YEARS]
    fcfs = [v for v in (owner_earnings(y) for y in recent) if v is not None]
    ebits = [v for v in (num(y.get("ebit")) for y in recent) if v is not None]
    multiples = [perpetuity_multiple(g, rate) for g in GROWTH_SCENARIOS]
    out = {"years_used": max(len(fcfs), len(ebits)), "discount_rate": rate, "methods": {}}
    if len(fcfs) >= 2 and mean(fcfs) > 0:
        fcf_ps = mean(fcfs) / shares
        out["normalized_fcf"] = mean(fcfs)
        out["methods"]["fcf"] = [fcf_ps * m for m in multiples]
        out["implied_growth"] = (price * rate - fcf_ps) / (price + fcf_ps)
    if len(ebits) >= 2 and mean(ebits) > 0:
        nopat = mean(ebits) * (1 - TAX_RATE)
        out["normalized_ebit"] = mean(ebits)
        out["methods"]["ebit"] = [(nopat * m - net_debt) / shares for m in multiples]
    if not out["methods"]:
        return None
    values = out["methods"].values()
    out["low"] = min(v[0] for v in values)
    out["high"] = max(v[-1] for v in values)
    out["base"] = mean(v[1] for v in values)
    out["margin_of_safety"] = (out["base"] - price) / out["base"] if out["base"] > 0 else None
    out["epv"] = ((mean(ebits) * (1 - TAX_RATE) / rate - net_debt) / shares
                  if len(ebits) >= 2 and mean(ebits) > 0 else None)
    out["grid"] = {}
    for key, r in (("r-1", rate - 0.01), ("r", rate), ("r+1", rate + 0.01)):
        row = []
        for g in GROWTH_SCENARIOS:
            if r <= g:
                row.append(None)
                continue
            m = perpetuity_multiple(g, r)
            vals = []
            if "fcf" in out["methods"]:
                vals.append(mean(fcfs) / shares * m)
            if "ebit" in out["methods"]:
                vals.append((mean(ebits) * (1 - TAX_RATE) * m - net_debt) / shares)
            row.append(mean(vals))
        out["grid"][key] = row
    return out


# Share of companies that sustained at least each ten-year sales growth rate, by starting
# revenue. Approximate: read off the charts in Credit Suisse's "Base Rate Book"
# (Mauboussin, 2016), not taken from a table. (label, upper revenue bound, {growth: share}).
BASE_RATE_COHORTS = (
    ("<$1B", 1e9, {0.0: 0.80, 0.03: 0.60, 0.05: 0.48, 0.07: 0.38, 0.10: 0.27, 0.15: 0.15}),
    ("$1–5B", 5e9, {0.0: 0.85, 0.03: 0.62, 0.05: 0.47, 0.07: 0.35, 0.10: 0.22, 0.15: 0.10}),
    ("$5–25B", 25e9, {0.0: 0.88, 0.03: 0.62, 0.05: 0.45, 0.07: 0.30, 0.10: 0.17, 0.15: 0.06}),
    (">$25B", None, {0.0: 0.90, 0.03: 0.60, 0.05: 0.40, 0.07: 0.24, 0.10: 0.11, 0.15: 0.03}),
)
BASE_RATE_CAP_SHARE = 0.25


def growth_base_rates(revenue: float | None) -> dict | None:
    """The cohort's historical odds of sustaining each growth rate for ten years, and the
    highest rate at least a quarter of that cohort reached (``cap``)."""
    revenue = num(revenue)
    if revenue is None or revenue <= 0:
        return None
    for label, upper, shares in BASE_RATE_COHORTS:
        if upper is None or revenue < upper:
            cap = max((g for g, s in shares.items() if s >= BASE_RATE_CAP_SHARE), default=0.0)
            return {"cohort": label, "share_achieving": dict(shares), "cap": cap}
    return None


def base_rate_note(implied_growth: float | None, revenue: float | None) -> str | None:
    """How common the growth the price implies has been among similar-sized companies."""
    implied_growth = num(implied_growth)
    br = growth_base_rates(revenue)
    if implied_growth is None or br is None:
        return None
    level = max((g for g in br["share_achieving"] if g <= implied_growth), default=0.0)
    return (f"Implied growth {implied_growth:.1%}/yr: base rate — about {br['share_achieving'][level]:.0%} of "
            f"companies with revenue {br['cohort']} sustained {level:.0%} or more for 10 years.")


# --- the analyst's verified report ----------------------------------------------------

def capital_allocation(years: list[dict], avg_prices: dict[str, float] | None = None,
                       price: float | None = None, n: int = NORMALIZE_YEARS) -> dict:
    """Where the free cash went over the last n years, and what buybacks cost per share."""
    avg_prices = avg_prices or {}
    window = years[:n]

    def out(y, key):
        v = num(y.get(key))
        return abs(v) if v is not None else None

    rows = []
    for i, y in enumerate(window):
        prev = years[i + 1] if i + 1 < len(years) else None
        net_debt_change = shares_retired = shares_change = None
        if prev:
            parts = [num(d.get(k)) for d in (y, prev) for k in ("total_debt", "cash")]
            if None not in parts:
                net_debt_change = (parts[0] - parts[1]) - (parts[2] - parts[3])
            s, s_prev = num(y.get("shares")), num(prev.get("shares"))
            if s is not None and s_prev:
                shares_retired = s_prev - s
                shares_change = s / s_prev - 1.0
        buybacks = out(y, "buybacks")
        fye = y.get("fiscal_year_end")
        rows.append({
            "fiscal_year_end": fye,
            "free_cash_flow": num(y.get("free_cash_flow")),
            "dividends": out(y, "dividends_paid"),
            "buybacks": buybacks,
            "acquisitions": out(y, "acquisitions"),
            "net_debt_change": net_debt_change,
            "shares_change": shares_change,
            "shares_retired": shares_retired,
            "buyback_price": div(buybacks, shares_retired)
            if buybacks and shares_retired and buybacks > 0 and shares_retired > 0 else None,
            "avg_price": _avg_price(avg_prices, fye),
        })

    def total(key):
        return sum(r[key] or 0.0 for r in rows)

    priced = [r for r in rows if r["buyback_price"] is not None]
    retired = sum(r["shares_retired"] for r in priced)
    totals = {
        "generated": total("free_cash_flow"),
        "dividends": total("dividends"),
        "buybacks": total("buybacks"),
        "acquisitions": total("acquisitions"),
        "net_debt_change": total("net_debt_change"),
        "avg_buyback_price": sum(r["buybacks"] for r in priced) / retired if retired else None,
    }
    totals["returned"] = totals["dividends"] + totals["buybacks"]
    totals["unexplained"] = (totals["generated"] - totals["returned"] - totals["acquisitions"]
                             + totals["net_debt_change"])
    return {"per_year": rows, "totals": totals, "price": num(price)}


ER_HORIZON = 7
ER_DEFAULT_TARGET = 12.0       # EV/EBIT to revert to when the company has too little price history
ER_MAX_GROWTH, ER_BULL_BONUS, ER_BULL_MAX = 0.10, 0.03, 0.13
ER_MAX_REVERSION = 0.05


def _avg_price(avg_prices: dict | None, fye) -> float | None:
    """The year's average price: keyed by its fiscal year end, else by its calendar year."""
    if not avg_prices or not fye:
        return None
    price = avg_prices.get(str(fye))
    return price if price is not None else avg_prices.get(str(fye)[:4])


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _ev_ebit_target(years: list[dict], avg_prices: dict | None, shares) -> float:
    """Median of the company's own EV/EBIT at each year's average price (needs 3+ years)."""
    multiples = []
    for y in years:
        px = num(_avg_price(avg_prices, y.get("fiscal_year_end")))
        ebit, n = num(y.get("ebit")), num(y.get("shares")) or num(shares)
        if px is None or n is None or ebit is None or ebit <= 0:
            continue
        multiples.append((px * n + (num(y.get("total_debt")) or 0.0) - (num(y.get("cash")) or 0.0)) / ebit)
    return median(multiples) if len(multiples) >= 3 else ER_DEFAULT_TARGET


def expected_return(years: list[dict], price, market_cap, shares, avg_prices: dict | None = None,
                    horizon: int = ER_HORIZON, target_multiple: float | None = None,
                    revenue_usd: float | None = None) -> dict | None:
    """Annual return if held ``horizon`` years: shareholder yield + growth + EV/EBIT reversion
    to the company's own history. None when owner earnings or EV/EBIT cannot be computed."""
    market_cap = num(market_cap)
    if not years or not market_cap or market_cap <= 0:
        return None
    ebit = num(years[0].get("ebit"))
    ev = enterprise_value(market_cap, years[0].get("total_debt"), years[0].get("cash"))
    recent = years[:3]
    priced = [(y, owner_earnings(y)) for y in recent]
    priced = [(y, o) for y, o in priced if o is not None]
    oe = [o for _, o in priced]
    if not oe or sum(oe) <= 0 or ebit is None or ebit <= 0 or ev is None or ev <= 0:
        return None
    def payout(y: dict) -> float:
        return abs(num(y.get("dividends_paid")) or 0.0) + abs(num(y.get("buybacks")) or 0.0)

    shareholder_yield = mean(payout(y) / market_cap for y in recent)   # no issuance field: counted as 0
    reinvestment = _clamp(1 - sum(payout(y) for y, _ in priced) / sum(oe), 0.0, 1.0)
    roics = [min(r, 1.0) for r in (roic(y) for y in years[:5]) if r is not None]
    sustainable = (mean(roics) if roics else 0.0) * reinvestment
    br = growth_base_rates(revenue_usd if revenue_usd is not None else years[0].get("revenue"))
    cap = br["cap"] if br else ER_MAX_GROWTH
    growth = max(0.0, min(sustainable, cap, ER_MAX_GROWTH))
    now = ev / ebit
    target = num(target_multiple) or _ev_ebit_target(years, avg_prices, shares)

    def reversion(t: float) -> float:
        return _clamp((t / now) ** (1 / horizon) - 1, -ER_MAX_REVERSION, ER_MAX_REVERSION)

    rev = reversion(target)
    bull_growth = min(growth + ER_BULL_BONUS, ER_BULL_MAX)
    return {
        "expected_return": shareholder_yield + growth + rev, "yield": shareholder_yield, "growth": growth,
        "reversion": rev, "growth_cap": cap, "sustainable_growth": sustainable,
        "ev_ebit_now": now, "ev_ebit_target": target,
        "bear": shareholder_yield + reversion(target * 0.8),
        "base": shareholder_yield + growth + rev,
        "bull": shareholder_yield + bull_growth + reversion(target * 1.2),
    }


def company_metrics(years: list[dict], price, market_cap, shares,
                    rate: float = DISCOUNT_RATE, treasury_10y: float | None = None,
                    avg_prices: dict | None = None, revenue_usd: float | None = None) -> dict:
    """Everything the fundamentals analyst needs, computed once from the statements."""
    cur = years[0]
    net_debt = (num(cur.get("total_debt")) or 0.0) - (num(cur.get("cash")) or 0.0)
    ev = enterprise_value(market_cap, cur.get("total_debt"), cur.get("cash"))
    per_year = []
    for i, y in enumerate(years):
        prev = years[i + 1] if i + 1 < len(years) else None
        equity = num(y.get("equity"))
        per_year.append({
            "fiscal_year_end": y.get("fiscal_year_end"),
            "revenue": num(y.get("revenue")),
            "gross_margin": div(y.get("gross_profit"), y.get("revenue")),
            "ebit_margin": div(y.get("ebit"), y.get("revenue")),
            "roic": roic(y),
            "roe": div(y.get("net_income"), equity) if equity and equity > 0 else None,
            "free_cash_flow": num(y.get("free_cash_flow")),
            "fcf_to_net_income": div(y.get("free_cash_flow"), y.get("net_income")),
            "shares_change": (div(y.get("shares"), prev.get("shares")) or 1.0) - 1.0
            if prev and num(prev.get("shares")) else None,
            "sbc_to_revenue": div(abs(num(y.get("stock_based_comp")) or 0.0), y.get("revenue"))
            if num(y.get("stock_based_comp")) is not None else None,
            "buybacks": abs(num(y.get("buybacks"))) if num(y.get("buybacks")) is not None else None,
            "dividends": abs(num(y.get("dividends_paid"))) if num(y.get("dividends_paid")) is not None else None,
            "debt_to_equity": debt_to_equity(y),
            "interest_coverage": interest_coverage(y),
        })
    return {
        "price": num(price),
        "market_cap": num(market_cap),
        "enterprise_value": ev,
        "net_debt": net_debt,
        "shares": num(shares),
        "per_year": per_year,
        "earnings_yield": earnings_yield(cur.get("ebit"), ev),
        "fcf_yield": fcf_yield(cur.get("free_cash_flow"), market_cap),
        "pe": div(market_cap, cur.get("net_income")) if (num(cur.get("net_income")) or 0) > 0 else None,
        "ev_to_ebit": div(ev, cur.get("ebit")) if (num(cur.get("ebit")) or 0) > 0 else None,
        "pb": div(market_cap, cur.get("equity")) if (num(cur.get("equity")) or 0) > 0 else None,
        "fscore": piotroski(cur, years[1]) if len(years) > 1 else None,
        "altman_z": altman_z(cur, market_cap),
        "accounting": accounting_checks(years),
        "current_ratio": div(cur.get("current_assets"), cur.get("current_liabilities")),
        "valuation": formula_value(years, price, shares, net_debt, rate),
        "treasury_10y": treasury_10y,
        "capital_allocation": capital_allocation(years, avg_prices, price),
        "expected_return": expected_return(years, price, market_cap, shares, avg_prices,
                                           revenue_usd=revenue_usd),
        "revenue_usd": num(revenue_usd),  # the base-rate cohorts are in dollars
    }


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _x(x) -> str:
    if x is None:
        return "n/a"
    return "no interest" if math.isinf(x) else f"{x:.1f}x"


def _x2(x, suffix: str = "x") -> str:
    return "n/a" if x is None else f"{x:.2f}{suffix}"


def _price(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _money(x, scale: float = 1e6) -> str:
    return "n/a" if x is None else f"{x / scale:,.0f}"


_FSCORE_LABELS = {
    "roa_positive": "Net income / assets > 0",
    "cfo_positive": "Operating cash flow > 0",
    "roa_improving": "Return on assets rising",
    "cfo_exceeds_net_income": "Operating cash flow > net income",
    "leverage_falling": "Long-term debt / assets falling",
    "current_ratio_rising": "Current ratio rising",
    "no_dilution": "No new shares issued",
    "gross_margin_rising": "Gross margin rising",
    "asset_turnover_rising": "Asset turnover rising",
}


def render_report(symbol: str, as_of: str, m: dict, currency: str | None = None) -> str:
    """The metrics as Markdown the analyst can quote. Money in millions."""
    cur = f" {currency}" if currency else ""
    lines = [
        f"# Verified value metrics for {symbol} (as of {as_of})",
        "Computed in code from the annual statements and the current quote. These are the "
        "source of truth for the figures below: quote them, do not recompute them. "
        f"Money in millions{cur}; n/a means the statements do not report it.",
        "",
        f"Price {m['price']:,.2f} | Market cap {_money(m['market_cap'])} | Enterprise value "
        f"{_money(m['enterprise_value'])} | Net debt {_money(m['net_debt'])} | Shares {_money(m['shares'])}M"
        if m["price"] is not None else "Price: not available",
        "",
        "## By fiscal year (newest first)",
        "",
        "| FY end | Revenue | Gross margin | EBIT margin | ROIC (Greenblatt) | ROE | FCF | FCF / NI "
        "| Share count chg | SBC / revenue | Buybacks | Dividends | Debt / equity | Interest cover |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for y in m["per_year"]:
        lines.append(
            f"| {y['fiscal_year_end']} | {_money(y['revenue'])} | {_pct(y['gross_margin'])} | "
            f"{_pct(y['ebit_margin'])} | {_pct(y['roic'])} | {_pct(y['roe'])} | {_money(y['free_cash_flow'])} | "
            f"{_x(y['fcf_to_net_income'])} | {_pct(y['shares_change'])} | {_pct(y['sbc_to_revenue'])} | "
            f"{_money(y['buybacks'])} | {_money(y['dividends'])} | {_x(y['debt_to_equity'])} | "
            f"{_x(y['interest_coverage'])} |"
        )
    ca = m.get("capital_allocation")
    if ca and ca["per_year"]:
        t = ca["totals"]
        lines += [
            "## Capital allocation",
            "",
            "| FY end | FCF | Dividends | Buybacks | Acquisitions | Net debt chg | Shares retired "
            "| Buyback price (net of issuance) | Avg price |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in ca["per_year"]:
            lines.append(
                f"| {r['fiscal_year_end']} | {_money(r['free_cash_flow'])} | {_money(r['dividends'])} | "
                f"{_money(r['buybacks'])} | {_money(r['acquisitions'])} | {_money(r['net_debt_change'])} | "
                f"{_money(r['shares_retired'])} | {_price(r['buyback_price'])} | {_price(r['avg_price'])} |"
            )
        at = ""
        if t["avg_buyback_price"] is not None and ca["price"] is not None:
            at = (f" at an average of {t['avg_buyback_price']:.2f} per share net of shares issued "
                  f"vs {ca['price']:.2f} today")
        lines += [
            "",
            f"Over {len(ca['per_year'])} years: generated {_money(t['generated'])}, returned "
            f"{_money(t['returned'])} (dividends {_money(t['dividends'])}, buybacks "
            f"{_money(t['buybacks'])}{at}), acquisitions {_money(t['acquisitions'])}, "
            f"net debt {t['net_debt_change'] / 1e6:+,.0f}, unexplained {_money(t['unexplained'])}.",
            "",
        ]
    lines += [
        "",
        "## Price today",
        "",
        f"Earnings yield (EBIT/EV) {_pct(m['earnings_yield'])} | FCF yield {_pct(m['fcf_yield'])} | "
        f"P/E {_x(m['pe'])} | EV/EBIT {_x(m['ev_to_ebit'])} | P/B {_x(m['pb'])}",
        "",
    ]
    fs = m["fscore"]
    if fs:
        lines += [f"## Piotroski F-Score: {fs['score']}/9" + (f" ({fs['missing']} not computable)" if fs["missing"] else ""), "",
                  "| Test | Pass |", "|---|---|"]
        lines += [f"| {_FSCORE_LABELS[k]} | {'n/a' if v is None else ('yes' if v else 'no')} |" for k, v in fs["points"].items()]
        lines.append("")
    z = m["altman_z"]
    lines.append(f"Altman Z-Score: {'n/a' if z is None else f'{z:.2f}'} (above 3 safe, 1.8-3 grey, below 1.8 distress) | "
                 f"Current ratio {_x(m['current_ratio'])}")
    lines.append("")
    acc = m["accounting"]
    lines += [
        "## Accounting checks",
        "",
        f"Accruals ratio {_pct(acc['accruals_ratio'])} | Cash conversion (3 years) {_x2(acc['cash_conversion'])} | "
        f"Receivables growth less revenue growth {_pct(acc['receivables_vs_revenue'])} | "
        f"Beneish M-score {_x2(acc['beneish_m'], '')}",
        "Flags: " + ("; ".join(acc["flags"]) if acc["flags"] else "none"),
        "",
    ]
    v = m["valuation"]
    if v:
        lines += [f"## Valuation worksheet (per share; {v['years_used']}-year averages)", ""]
        if m["price"] is not None:
            lines.append(f"- Price today {m['price']:,.2f}")
        growth = " / ".join(f"{g:.0%}" for g in GROWTH_SCENARIOS)
        r = v["discount_rate"]
        ten_year = m.get("treasury_10y")
        source = (f"10-year Treasury {ten_year:.1%} + {EQUITY_PREMIUM:.1%} premium, never below {MIN_DISCOUNT_RATE:.0%}"
                  if ten_year is not None else f"the {MIN_DISCOUNT_RATE:.0%} floor; Treasury yield unavailable")
        lines.append(f"Discount rate {r:.1%} ({source}).")
        lines.append(f"Value = normalized cash flow × (1 + g) / ({r:.1%} − g), at {growth} perpetual growth:")
        if "fcf" in v["methods"]:
            lo, mid, hi = v["methods"]["fcf"]
            lines.append(f"- Owner earnings (average FCF less stock-based pay {_money(v['normalized_fcf'])}): {lo:,.2f} / {mid:,.2f} / {hi:,.2f}")
        if "ebit" in v["methods"]:
            lo, mid, hi = v["methods"]["ebit"]
            lines.append(f"- After-tax EBIT (average EBIT {_money(v['normalized_ebit'])}, {TAX_RATE:.0%} tax, less net debt): "
                         f"{lo:,.2f} / {mid:,.2f} / {hi:,.2f}")
        lines.append(f"- **Range {v['low']:,.2f} to {v['high']:,.2f}, base (3% growth) {v['base']:,.2f}; margin of safety at base "
                     f"{_pct(v['margin_of_safety'])}**")
        if v.get("implied_growth") is not None:
            lines.append(f"- Reverse DCF: today's price assumes {_pct(v['implied_growth'])} perpetual FCF growth at a "
                         f"{r:.1%} discount rate")
        if v.get("epv") is not None:
            lines.append(f"- Earnings-power value (zero growth, after-tax EBIT / rate, less net debt): {v['epv']:,.2f}")
        grid = v.get("grid")
        if grid:
            lines += ["", "Sensitivity (value per share by discount rate and perpetual growth):", "",
                      "| Rate | " + " | ".join(f"{g:.0%}" for g in GROWTH_SCENARIOS) + " |",
                      "|---|" + "---|" * len(GROWTH_SCENARIOS)]
            for key, rr in (("r-1", r - 0.01), ("r", r), ("r+1", r + 0.01)):
                cells = " | ".join("n/a" if x is None else f"{x:,.2f}" for x in grid[key])
                lines.append(f"| {key.replace('-', '−')} ({rr:.1%}) | {cells} |")
        # Cohorts are in USD: with no dollar revenue, only a USD reporter's own figure will do.
        revenue = m.get("revenue_usd")
        if revenue is None and currency in (None, "USD"):
            revenue = (m.get("per_year") or [{}])[0].get("revenue")
        note = base_rate_note(v.get("implied_growth"), revenue)
        if note:
            lines += ["", note]
        er = m.get("expected_return")
        if er:
            lines += ["", f"Expected return ({ER_HORIZON}-year): {er['expected_return']:.1%}/yr = yield "
                      f"{er['yield']:.1%} + growth {er['growth']:.1%} + multiple reversion {er['reversion']:.1%} "
                      f"(bear {er['bear']:.1%} / base {er['base']:.1%} / bull {er['bull']:.1%})"]
        lines += ["", "Start from this range. Judge which growth rate the business can sustain and whether the "
                  "averages need adjusting (one-off cash flows, a cyclical peak or trough), and state each change "
                  "and its effect on the value."]
    else:
        lines.append("Formula valuation: not computable (no price, or averaged FCF and EBIT are not positive).")
    return "\n".join(lines)
