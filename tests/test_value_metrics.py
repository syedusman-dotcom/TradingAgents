"""The verified value metrics the fundamentals analyst quotes (no network)."""

import pandas as pd
import pytest

from tradingagents.dataflows import value_metrics as vm
from tradingagents.dataflows.vendors.yahoo import value_metrics as yahoo_vm


def _year(**kw):
    base = {"revenue": 1000, "gross_profit": 400, "ebit": 200, "net_income": 150, "interest_expense": -10,
            "total_assets": 2000, "current_assets": 600, "current_liabilities": 300, "cash": 100,
            "net_ppe": 500, "long_term_debt": 400, "total_debt": 500, "equity": 900,
            "retained_earnings": 700, "total_liabilities": 1100, "shares": 100,
            "operating_cash_flow": 220, "free_cash_flow": 180, "capex": -40,
            "dividends_paid": -50, "buybacks": -30, "stock_based_comp": 20}
    return {**base, **kw}


@pytest.mark.unit
def test_perpetuity_multiples_match_the_growth_scenarios():
    assert [round(vm.perpetuity_multiple(g), 2) for g in vm.GROWTH_SCENARIOS] == [11.11, 17.17, 26.25]


@pytest.mark.unit
def test_formula_value_from_owner_earnings_and_after_tax_ebit():
    years = [_year(), _year(free_cash_flow=140, ebit=180)]
    v = vm.formula_value(years, price=20.0, shares=100, net_debt=400)
    owner_ps = ((180 - 20) + (140 - 20)) / 2 / 100           # FCF less stock-based pay, per share
    nopat = (200 + 180) / 2 * (1 - vm.TAX_RATE)
    m = [vm.perpetuity_multiple(g) for g in vm.GROWTH_SCENARIOS]
    assert v["methods"]["fcf"] == pytest.approx([owner_ps * x for x in m])
    assert v["methods"]["ebit"] == pytest.approx([(nopat * x - 400) / 100 for x in m])
    assert v["base"] == pytest.approx((owner_ps * m[1] + (nopat * m[1] - 400) / 100) / 2)
    assert v["margin_of_safety"] == pytest.approx((v["base"] - 20) / v["base"])
    # Reverse DCF: the growth at which owner earnings x (1+g)/(r-g) equals the price.
    g = v["implied_growth"]
    assert owner_ps * vm.perpetuity_multiple(g) == pytest.approx(20.0)


@pytest.mark.unit
def test_formula_value_needs_a_price_and_positive_cash_flow():
    assert vm.formula_value([_year(), _year()], price=None, shares=100, net_debt=0) is None
    losing = [_year(free_cash_flow=-50, ebit=-20), _year(free_cash_flow=-60, ebit=-30)]
    assert vm.formula_value(losing, price=10, shares=100, net_debt=0) is None
    only_fcf = vm.formula_value([_year(ebit=-20), _year(ebit=-30)], price=10, shares=100, net_debt=0)
    assert set(only_fcf["methods"]) == {"fcf"}


@pytest.mark.unit
def test_company_metrics_and_report():
    years = [_year(fiscal_year_end="2025-12-31", shares=98), _year(fiscal_year_end="2024-12-31", revenue=900)]
    m = vm.company_metrics(years, price=25.0, market_cap=2450.0, shares=98)
    assert m["per_year"][0]["shares_change"] == pytest.approx(-0.02)
    assert m["per_year"][0]["sbc_to_revenue"] == pytest.approx(0.02)
    assert m["pe"] == pytest.approx(2450 / 150)
    assert m["fscore"]["points"]["no_dilution"] is True
    report = vm.render_report("ABC", "2026-10-11", m, currency="USD")
    for section in ("## By fiscal year", "## Price today", "## Piotroski F-Score", "Altman Z-Score",
                    "## Formula valuation", "Reverse DCF", "do not recompute"):
        assert section in report, section


@pytest.mark.unit
def test_report_without_a_price_keeps_the_ratios():
    m = vm.company_metrics([_year(), _year()], price=None, market_cap=None, shares=100)
    report = vm.render_report("ABC", "2026-10-11", m)
    assert "Price: not available" in report and "not computable" in report
    assert "## By fiscal year" in report


@pytest.mark.unit
def test_vendor_withholds_for_a_past_date_without_a_request(monkeypatch):
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: pytest.fail("no request for a past date"))
    assert "Not available for 2020-01-02" in yahoo_vm.get_value_metrics("KO", "2020-01-02")


@pytest.mark.unit
def test_vendor_drops_price_figures_when_currencies_differ(monkeypatch):
    col = [pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31")]

    def frame(rows):
        return pd.DataFrame(dict.fromkeys(col, rows))

    class FakeTicker:
        info = {"currency": "USD", "financialCurrency": "TWD", "currentPrice": 150.0,
                "marketCap": 7e11, "sharesOutstanding": 5e9}
        income_stmt = frame({"Total Revenue": 3e12, "EBIT": 1.4e12, "Net Income": 1.1e12, "Gross Profit": 1.7e12})
        balance_sheet = frame({"Total Assets": 6e12, "Current Assets": 3e12, "Current Liabilities": 1e12,
                               "Stockholders Equity": 4e12})
        cashflow = frame({"Free Cash Flow": 9e11, "Operating Cash Flow": 1.8e12})

    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: FakeTicker())
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    report = yahoo_vm.get_value_metrics("TSM", "2026-10-11")
    assert "quote is in USD but the statements are in TWD" in report
    assert "Price: not available" in report and "## By fiscal year" in report


SAMPLE_10K = """
Table of Contents
Item 1A. Risk Factors 12
Item 1B. Unresolved Staff Comments 30
Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations 35
Item 7A. Quantitative and Qualitative Disclosures About Market Risk 60
For risks, refer to Item 1A. Risk Factors in this report.
Item 1A. Risk Factors
""" + "Demand for our drinks may fall. " * 40 + """
Item 1B. Unresolved Staff Comments
None.
Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations
""" + "Revenue grew on pricing. " * 40 + """
Item 7A. Quantitative and Qualitative Disclosures About Market Risk
Rates.
"""


@pytest.mark.unit
def test_10k_sections_skip_the_table_of_contents_and_cross_references():
    from tradingagents.dataflows.vendors.sec_filings import section_spans

    found = section_spans(SAMPLE_10K)
    risks = found["Risk factors (Item 1A)"]
    mdna = found["Management's discussion and analysis (Item 7)"]
    assert risks.startswith("Demand for our drinks may fall.") and "Unresolved" not in risks
    assert mdna.startswith("Revenue grew on pricing.") and "Rates." not in mdna


@pytest.mark.unit
def test_10k_text_keeps_one_line_per_block():
    from tradingagents.dataflows.vendors.sec_filings import _plain_text

    html = b"<html><body><div>Item 1A.&#160;Risk Factors</div><p>Risk one <b>bold</b>.</p></body></html>"
    assert _plain_text(html).splitlines() == ["Item 1A. Risk Factors", "Risk one bold."]


@pytest.mark.unit
def test_annual_report_picks_the_latest_10k_filed_by_the_date(monkeypatch):
    from tradingagents.dataflows.vendors import sec_filings

    submissions = {"filings": {"recent": {
        "form": ["10-Q", "10-K", "10-K", "8-K"],
        "filingDate": ["2026-05-01", "2026-02-20", "2025-02-21", "2026-03-01"],
        "accessionNumber": ["a-1", "a-2", "a-3", "a-4"],
        "primaryDocument": ["q.htm", "k26.htm", "k25.htm", "e.htm"],
    }}}
    monkeypatch.setattr(sec_filings.sec_edgar, "cik_for", lambda t: "0000021344")
    monkeypatch.setattr(sec_filings.sec_edgar, "_cached_json", lambda url, name: submissions)
    seen = []
    monkeypatch.setattr(sec_filings, "_sections",
                        lambda cik, filing: seen.append(filing) or {"Risk factors (Item 1A)": "x" * 600})
    out = sec_filings.get_annual_report("KO", "2026-01-31")
    assert seen[0]["accession"] == "a-3"                 # the 2026 10-K was not filed yet
    assert "filed 2025-02-21" in out and "## Risk factors (Item 1A)" in out
