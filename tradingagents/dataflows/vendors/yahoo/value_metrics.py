"""Verified value metrics from Yahoo's annual statements and today's quote.

The fundamentals analyst used to rebuild ROIC, the F-Score and a valuation by
hand from raw tables, guessing where a line was missing. This computes them once
in code (``tradingagents.dataflows.value_metrics``) so the analyst quotes them.
"""

from __future__ import annotations

from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.value_metrics import (
    YEAR_FIELDS,
    company_metrics,
    current_discount_rate,
    render_report,
    share_count,
)
from tradingagents.dataflows.vendors import sec_edgar, sec_filings
from tradingagents.dataflows.vendors.yahoo.common import (
    MINOR_UNITS,
    raise_for_empty,
    usd_rate,
    yf_retry,
)
from tradingagents.dataflows.vendors.yahoo.consensus import consensus

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
    "capex": ("Capital Expenditure",),
    "dividends_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
    "buybacks": ("Repurchase Of Capital Stock", "Common Stock Payments"),
    "stock_based_comp": ("Stock Based Compensation",),
    "receivables": ("Accounts Receivable", "Receivables"),
    "depreciation": ("Reconciled Depreciation", "Depreciation And Amortization",
                     "Depreciation Amortization Depletion"),
    "sga": ("Selling General And Administration",),
    "acquisitions": ("Purchase Of Business",),
    "goodwill": ("Goodwill And Other Intangible Assets", "Goodwill"),
    "rnd": ("Research And Development",),
}
assert set(ROWS) == set(YEAR_FIELDS)


# What each minor quote unit is called, for the worksheet's note.
_MINOR_NAMES = {"GBp": "pence", "GBX": "pence", "ZAc": "cents", "ILA": "agorot"}


def statement_years(*frames: pd.DataFrame) -> list[dict]:
    """Merge Yahoo's statements into per-period dicts, newest first: annual frames give fiscal
    years, quarterly frames (same row labels) give quarters, keyed by the period end."""
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


QUARTERLY_FRAMES = ("quarterly_income_stmt", "quarterly_balance_sheet", "quarterly_cashflow")


def fetch_quarters(t) -> tuple[list[dict], bool]:
    """The quarterly statements as ``statement_years`` dicts (one per quarter end, newest first),
    and whether every frame was fetched. Each frame is asked for on its own, so one failure keeps
    the others; Yahoo answering it has none is not a failure."""
    frames, complete = [], True
    for attr in QUARTERLY_FRAMES:
        try:
            frames.append(yf_retry(lambda attr=attr: getattr(t, attr)))
        except Exception:
            frames.append(None)
            complete = False
    try:
        return statement_years(*frames), complete
    except Exception:
        return [], False


def quarterly_statements(t) -> list[dict]:
    """The quarters ``fetch_quarters`` could get; [] without any: the report renders without TTM."""
    return fetch_quarters(t)[0]


def sec_revenue_history(symbol: str, reported: str | None) -> dict[str, float] | None:
    """Fiscal year -> revenue from SEC EDGAR (about 20 years, cached by that vendor) for a US filer
    reporting in dollars; None otherwise or when EDGAR fails: the CAGR then spans Yahoo's years."""
    if reported not in (None, "USD"):
        return None   # EDGAR's figures are dollars; another reporting currency would mix units
    try:
        if sec_edgar.cik_for(symbol) is None:
            return None
        return sec_filings.annual_revenue(symbol) or None
    except Exception:
        return None


def fiscal_average_closes(history: pd.DataFrame, fiscal_year_ends: list[str]) -> dict[str, float]:
    """Mean closing price over each fiscal year, (year end - 12 months, year end], keyed by the
    year end's ISO date; a year with no closes is left out, and {} when there is no usable history."""
    try:
        close = history["Close"].dropna()
        index = close.index.tz_localize(None) if close.index.tz is not None else close.index
        close = pd.Series(close.to_numpy(), index=index)
        out = {}
        for fye in fiscal_year_ends:
            end = pd.Timestamp(fye) + pd.Timedelta(days=1)          # through the whole year-end day
            start = pd.Timestamp(fye) - pd.DateOffset(months=12) + pd.Timedelta(days=1)
            window = close[(close.index >= start) & (close.index < end)]
            if len(window):
                out[str(fye)] = float(window.mean())
        return out
    except Exception:
        return {}


def fx_history(code: str) -> pd.DataFrame | None:
    """Ten years of daily closes of ``{code}USD=X`` (US dollars per unit); None when it fails."""
    try:
        return yf_retry(lambda: yf.Ticker(f"{code}USD=X").history(period="10y"))
    except Exception:
        return None


def _historic_cross(quote: str, reported: str, fiscal_year_ends: list[str]) -> dict[str, float]:
    """Fiscal year end -> that year's average rate in ``reported`` units per ``quote`` unit (major
    codes), from each currency's ``{code}USD=X`` history; a year either history lacks is left out."""
    averages = []
    for code in (quote, reported):
        if code == "USD":
            averages.append(dict.fromkeys(map(str, fiscal_year_ends), 1.0))
            continue
        history = fx_history(code)
        averages.append(fiscal_average_closes(history, fiscal_year_ends) if history is not None else {})
    q, r = averages
    return {k: q[k] / r[k] for k in q if k in r and r[k]}


# How far market cap / price may sit from the statements' share count before the quote is taken to
# count a different share (an ADR of 5 ordinary shares) and the price is not converted.
SHARE_BASIS_TOLERANCE = 0.10


TARGET_KEYS = ("target_mean", "target_median", "target_high", "target_low")


def _consensus_in(price, factor: float, symbol: str, cross: float = 1.0) -> dict:
    """Yahoo's consensus for ``symbol`` with its price targets (quote unit) in the worksheet's
    unit, as the price is: divided by ``factor`` (a pence quote) and multiplied by ``cross`` (a
    quote currency other than the statements'), both undone by ``_to_quote_unit``; left out when
    there is no price (no exchange rate). ``{}`` when it fails."""
    try:
        c = dict(consensus(symbol) or {})
    except Exception:  # noqa: BLE001 - the worksheet stands without it
        return {}
    for key in TARGET_KEYS:
        if key in c:
            c[key] = c[key] / factor * cross if price is not None and c[key] is not None else None
    return c


def _to_quote_unit(metrics: dict, factor: float, year_factors: dict | None = None) -> dict:
    """``metrics`` with every per-share price multiplied by ``factor``, quote units per statement
    unit (100 for pence, 1 / rate for another currency); ratios, totals in money and share counts
    are left alone. ``year_factors`` (fiscal year end -> factor) scales a year's buyback and average
    price at that year's rate instead. Returns a copy."""
    import copy

    m = copy.deepcopy(metrics)

    def scale(x):
        return None if x is None else x * factor

    m["price"] = scale(m.get("price"))
    v = m.get("valuation")
    if v:
        v["methods"] = {k: [scale(x) for x in vals] for k, vals in v["methods"].items()}
        for key in ("low", "high", "base", "epv"):
            if key in v:
                v[key] = scale(v[key])
        v["grid"] = {k: [scale(x) for x in row] for k, row in (v.get("grid") or {}).items()}
    rr = m.get("run_rate")
    if rr:
        rr["methods"] = {k: scale(x) for k, x in rr["methods"].items()}
        for key in ("base", "low", "high"):
            rr[key] = scale(rr.get(key))
    ca = m.get("capital_allocation")
    if ca:
        for r in ca["per_year"]:
            f = (year_factors or {}).get(str(r.get("fiscal_year_end")), factor)
            r["buyback_price"] = None if r["buyback_price"] is None else r["buyback_price"] * f
            r["avg_price"] = None if r["avg_price"] is None else r["avg_price"] * f
        ca["totals"]["avg_buyback_price"] = scale(ca["totals"].get("avg_buyback_price"))
        ca["price"] = scale(ca.get("price"))
    c = m.get("consensus")
    if c:
        for key in TARGET_KEYS:
            c[key] = scale(c.get(key))
    return m


def _annual(t, ticker: str, canonical: str) -> tuple[dict, list[dict]]:
    """Yahoo's profile and the annual statements as ``statement_years`` dicts, newest first;
    raises when there are no statements."""
    info = yf_retry(lambda: t.info) or {}
    frames = [yf_retry(lambda attr=attr: getattr(t, attr)) for attr in ("income_stmt", "balance_sheet", "cashflow")]
    years = statement_years(*frames)
    if not years:
        raise_for_empty(ticker, canonical, "annual statements")
    return info, years


def _currency_basis(info: dict, years: list[dict]) -> dict:
    """How the quote relates to the statements: ``major`` (the quote's major unit), ``minor`` (a
    minor-unit quote of the statement currency), ``factor`` (100 for a minor-unit quote, else 1),
    ``cross`` (statement units per major quote unit; 1.0 unless ``converted``), ``converted``
    (another currency, converted at today's rate) and ``same_basis`` (the quote and the statements
    count the same share). The price and market cap are read only as their ratio, a share count."""
    quote, reported = info.get("currency"), info.get("financialCurrency")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    # A London listing is quoted in pence and reports in pounds: compute in the major unit,
    # then show per-share values in the quote unit the user sees at the broker.
    # A quote in another currency (Celestica: CAD quote, USD statements) is converted the same
    # way at today's rate: ``cross`` statement units per quote unit (the major unit for pence).
    major = MINOR_UNITS.get(quote, quote)
    minor = quote in MINOR_UNITS and major == reported
    factor = 100.0 if quote in MINOR_UNITS else 1.0
    cross, converted, same_basis = 1.0, False, True
    if quote and reported and major != reported:
        quote_usd, reported_usd = usd_rate(quote), usd_rate(reported)
        quoted_shares = share_count(info.get("marketCap"), price / factor if price else None)
        statement_shares = years[0].get("shares")
        # An ADR's price is per ADR (TSM: 5 ordinary shares), and the history multiples and buyback
        # price set it against the statements' ordinary count: convert only when the bases agree.
        # When the bases cannot be compared, a non-dollar quote (CLS.TO, BP.L) is still an
        # ordinary share; only a dollar quote over foreign statements is the ADR case.
        same_basis = (abs(quoted_shares / statement_shares - 1) <= SHARE_BASIS_TOLERANCE
                      if quoted_shares and statement_shares else quote != "USD")
        if quote_usd and reported_usd and same_basis:
            cross, converted = quote_usd / reported_usd, True
    return {"major": major, "minor": minor, "factor": factor, "cross": cross,
            "converted": converted, "same_basis": same_basis}


def _revenue_usd(years: list[dict], currency: str | None) -> float | None:
    """The latest year's revenue in US dollars at today's rate (the growth base rates are in
    dollars; revenue arrives in the reporting currency); None without a rate or a revenue."""
    fx = usd_rate(currency)
    if fx and years[0].get("revenue") is not None:
        return years[0]["revenue"] * fx
    return None


def blind_inputs(ticker: str, as_of_date: str | None = None) -> dict:
    """What the price-blind valuer may see, from the same fetches as ``get_value_metrics``: the
    company's name and sector, the annual statements (``years``), the statement currency
    (``reported``) and quote code, ``cross`` (statement units per major quote unit: today's rate
    when converted, 1.0 when the quote is the statement currency or its minor unit, None when the
    two differ and cannot be converted), ``factor`` (100 for a minor-unit quote), ``same_basis``,
    the statements' share count, the latest revenue in dollars and today's discount rate.

    The price and market cap never enter the result; they are read only as their ratio, the quoted
    share count ``same_basis`` compares with the statements'. Raises ValueError for a past date:
    the statements are not dated by filing, as in ``get_value_metrics``."""
    canonical = normalize_symbol(ticker)
    if is_historical(as_of_date):
        raise ValueError(f"no price-blind inputs for the past date {as_of_date}: the statements "
                         "are not dated by filing")
    info, years = _annual(yf.Ticker(canonical), ticker, canonical)
    quote, reported = info.get("currency"), info.get("financialCurrency")
    basis = _currency_basis(info, years)
    comparable = not (quote and reported) or basis["major"] == reported or basis["converted"]
    return {
        "name": info.get("longName") or info.get("shortName") or canonical,
        "sector": info.get("sector"),
        "years": years,
        "reported": reported or quote,
        "quote": quote,
        "cross": basis["cross"] if comparable else None,
        "factor": basis["factor"],
        "same_basis": basis["same_basis"],
        "statement_shares": years[0].get("shares"),
        "revenue_usd": _revenue_usd(years, reported or quote),
        "discount_rate": current_discount_rate()["rate"],
    }


def get_value_metrics(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None,
) -> str:
    canonical = normalize_symbol(ticker)
    if is_historical(as_of_date):
        # The statements carry no filing date and the quote is today's, so a past
        # run would read figures from after its date (the #1300 rule).
        return (f"# Verified value metrics for {canonical}\n\nNot available for {as_of_date}: they combine "
                "today's quote with statements Yahoo does not date by filing, so a past-dated run would "
                "see information from after its date. Use the statement tools instead.")

    t = yf.Ticker(canonical)
    info, years = _annual(t, ticker, canonical)
    quarters = quarterly_statements(t)

    quote, reported = info.get("currency"), info.get("financialCurrency")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    basis = _currency_basis(info, years)
    major, minor, factor = basis["major"], basis["minor"], basis["factor"]
    cross, converted = basis["cross"], basis["converted"]
    if (minor or converted) and price is not None:
        price = price / factor * cross
    market_cap = info.get("marketCap")
    if converted and market_cap is not None:
        market_cap = market_cap * cross   # Yahoo's market cap is in the quote's major unit
    yahoo_shares = info.get("sharesOutstanding")
    shares = share_count(market_cap, price, years[0].get("shares"), yahoo_shares)
    note = ""
    if yahoo_shares and shares and abs(yahoo_shares / shares - 1) > 0.10:
        note = (f"\n\nYahoo's sharesOutstanding ({yahoo_shares / 1e6:,.0f}M) counts one share class; the per-share "
                f"figures above use market cap / price ({shares / 1e6:,.0f}M), which covers every class.")
    if minor:
        note += (f"\n\nPer-share values in {quote} ({_MINOR_NAMES.get(quote, 'minor unit')}), the quote unit; "
                 f"statements in {reported}.")
    elif converted:
        note += (f"\n\nPer-share values in {quote}, the quote unit, converted from {reported} statements at "
                 f"{cross:.4f} {reported} per {major} (today's rate); money figures in {reported} millions.")
    elif quote in MINOR_UNITS or (quote and reported and quote != reported):
        # No exchange rate, so per-share values would mix currencies (an ADR quoted in
        # USD reporting in TWD): keep the ratios, drop the price.
        price = None
        note = (f"\n\nThe quote is in {quote} but the statements are in {reported}, so price-based "
                "figures and the formula valuation are left out rather than mixed.")
    rate = current_discount_rate()
    avg_prices = {}  # fail-open: the report renders without average prices
    year_cross = {}  # fiscal year end -> that year's average rate, for a converted quote
    if price is not None:  # None means no rate for the quote currency; its averages would mix currencies
        try:
            # Unadjusted closes: the prices actually paid, as the buyback comparison needs.
            history = yf_retry(lambda: t.history(period="10y", auto_adjust=False))
            fyes = [y["fiscal_year_end"] for y in years]
            closes = fiscal_average_closes(history, fyes)
            if converted:
                # Each year at its own average rate, so the history multiples and buyback prices
                # do not pick up the currency's move since; today's rate where no history is had.
                year_cross = _historic_cross(major, reported, list(closes))
                if any(k not in year_cross for k in closes):
                    note += " (historic averages converted at today's rate)"
            avg_prices = {k: v / factor * year_cross.get(k, cross) for k, v in closes.items()}
        except Exception:
            avg_prices = {}
    revenue_usd = _revenue_usd(years, reported or quote)
    metrics = company_metrics(years, price, market_cap if price else None, shares,
                              rate=rate["rate"], treasury_10y=rate["treasury_10y"], avg_prices=avg_prices,
                              revenue_usd=revenue_usd, quarters=quarters,
                              revenue_history=sec_revenue_history(canonical, reported),
                              consensus=_consensus_in(price, factor, canonical, cross))
    if minor or converted:
        metrics = _to_quote_unit(metrics, factor / cross, {k: factor / c for k, c in year_cross.items()})
    return render_report(canonical, as_of_date or "today", metrics, currency=reported or quote) + note
