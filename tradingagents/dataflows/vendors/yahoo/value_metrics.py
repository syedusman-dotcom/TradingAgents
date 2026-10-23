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
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry

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
}
assert set(ROWS) == set(YEAR_FIELDS)

# Quote currencies Yahoo gives in a minor unit.
_MINOR_UNITS = {"GBp", "GBX", "ZAc", "ILA"}


def statement_years(*frames: pd.DataFrame) -> list[dict]:
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


def yearly_average_closes(history: pd.DataFrame) -> dict[str, float]:
    """Mean closing price per calendar year, keyed "YYYY"; {} when there is no usable history."""
    try:
        close = history["Close"].dropna()
        return {str(k): float(v) for k, v in close.groupby(close.index.year).mean().items()}
    except Exception:
        return {}


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
    info = yf_retry(lambda: t.info) or {}
    frames = [yf_retry(lambda attr=attr: getattr(t, attr)) for attr in ("income_stmt", "balance_sheet", "cashflow")]
    years = statement_years(*frames)
    if not years:
        raise_for_empty(ticker, canonical, "annual statements")

    quote, reported = info.get("currency"), info.get("financialCurrency")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    yahoo_shares = info.get("sharesOutstanding")
    shares = share_count(info.get("marketCap"), price, years[0].get("shares"), yahoo_shares)
    note = ""
    if yahoo_shares and shares and abs(yahoo_shares / shares - 1) > 0.10:
        note = (f"\n\nYahoo's sharesOutstanding ({yahoo_shares / 1e6:,.0f}M) counts one share class; the per-share "
                f"figures above use market cap / price ({shares / 1e6:,.0f}M), which covers every class.")
    if quote in _MINOR_UNITS or (quote and reported and quote != reported):
        # Per-share values would mix currencies (an ADR quoted in USD reporting in
        # TWD, a London listing quoted in pence): keep the ratios, drop the price.
        price = None
        note = (f"\n\nThe quote is in {quote} but the statements are in {reported}, so price-based "
                "figures and the formula valuation are left out rather than mixed.")
    rate = current_discount_rate()
    avg_prices = {}  # fail-open: the report renders without average prices
    if price is not None:  # None means the quote currency differs; its averages would mix currencies
        try:
            avg_prices = yearly_average_closes(yf_retry(lambda: t.history(period="10y", auto_adjust=True)))
        except Exception:
            avg_prices = {}
    metrics = company_metrics(years, price, info.get("marketCap") if price else None, shares,
                              rate=rate["rate"], treasury_10y=rate["treasury_10y"], avg_prices=avg_prices)
    return render_report(canonical, as_of_date or "today", metrics, currency=reported or quote) + note
