"""The verified value metrics the fundamentals analyst quotes (no network)."""

import re
from datetime import date

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
            "dividends_paid": -50, "buybacks": -30, "stock_based_comp": 20,
            "receivables": 120, "depreciation": 40, "sga": 150, "acquisitions": 0}
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
                    "## Valuation worksheet", "Reverse DCF", "do not recompute"):
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


@pytest.mark.unit
def test_discount_rate_follows_the_10_year_treasury_with_a_floor():
    assert vm.discount_rate(0.0521) == pytest.approx(0.097)   # 5.21% + 4.5% premium, to 0.1%
    assert vm.discount_rate(0.03) == vm.MIN_DISCOUNT_RATE     # low rates never go below 9%
    assert vm.discount_rate(None) == vm.MIN_DISCOUNT_RATE


@pytest.mark.unit
def test_current_discount_rate_falls_back_to_the_floor_when_fred_fails():
    def down():
        raise RuntimeError("FRED down")

    assert vm.current_discount_rate(fetch=lambda: 0.0521) == {"rate": pytest.approx(0.097), "treasury_10y": 0.0521}
    assert vm.current_discount_rate(fetch=down) == {"rate": vm.MIN_DISCOUNT_RATE, "treasury_10y": None}


@pytest.mark.unit
def test_a_failed_fred_fetch_is_not_cached_for_the_day(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("timeout")
        return 0.05

    monkeypatch.setattr(vm, "_latest_10y", flaky)
    vm._rate_cache.clear()
    assert vm.current_discount_rate() == {"rate": vm.MIN_DISCOUNT_RATE, "treasury_10y": None}
    assert vm.current_discount_rate()["treasury_10y"] == 0.05     # tried again, not pinned at the floor
    assert vm.current_discount_rate()["treasury_10y"] == 0.05 and len(calls) == 2   # now cached
    vm._rate_cache.clear()


@pytest.mark.unit
def test_report_notes_when_yahoo_share_count_is_one_class(monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            self.info = {"currency": "USD", "financialCurrency": "USD", "currentPrice": 20.0,
                         "marketCap": 8800e6, "sharesOutstanding": 220e6}
            idx = pd.to_datetime(["2025-12-31", "2024-12-31"])
            self.income_stmt = pd.DataFrame({idx[0]: [1000, 200, 150], idx[1]: [900, 180, 140]},
                                            index=["Total Revenue", "EBIT", "Net Income"])
            self.balance_sheet = pd.DataFrame({idx[0]: [2000e6, 440e6, 100e6], idx[1]: [1900e6, 440e6, 100e6]},
                                              index=["Total Assets", "Ordinary Shares Number", "Total Debt"])
            self.cashflow = pd.DataFrame({idx[0]: [220, 180, -40], idx[1]: [210, 170, -40]},
                                         index=["Operating Cash Flow", "Free Cash Flow", "Capital Expenditure"])

    monkeypatch.setattr(yahoo_vm.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    report = yahoo_vm.get_value_metrics("FOX")
    assert "Shares 440M" in report
    assert "Yahoo's sharesOutstanding (220M) counts one share class" in report


@pytest.mark.unit
def test_formula_value_and_reverse_dcf_use_the_given_rate():
    years = [_year(), _year(free_cash_flow=140, ebit=180)]
    v = vm.formula_value(years, price=20.0, shares=100, net_debt=400, rate=0.10)
    owner_ps = ((180 - 20) + (140 - 20)) / 2 / 100
    assert v["discount_rate"] == 0.10
    assert v["methods"]["fcf"] == pytest.approx([owner_ps * vm.perpetuity_multiple(g, 0.10) for g in vm.GROWTH_SCENARIOS])
    assert owner_ps * vm.perpetuity_multiple(v["implied_growth"], 0.10) == pytest.approx(20.0)
    assert v["base"] < vm.formula_value(years, price=20.0, shares=100, net_debt=400)["base"]


@pytest.mark.unit
def test_report_states_the_discount_rate_and_where_it_came_from():
    years = [_year(fiscal_year_end="2025-12-31"), _year(fiscal_year_end="2024-12-31")]
    m = vm.company_metrics(years, price=25.0, market_cap=2500.0, shares=100, rate=0.097, treasury_10y=0.0521)
    report = vm.render_report("ABC", "2026-10-15", m)
    assert "(9.7% − g)" in report
    assert "10-year Treasury 5.2% + 4.5% premium" in report
    plain = vm.render_report("ABC", "2026-10-15", vm.company_metrics(years, price=25.0, market_cap=2500.0, shares=100))
    assert "(9.0% − g)" in plain and "Treasury yield unavailable" in plain


@pytest.mark.unit
def test_accruals_ratio_is_net_income_less_cfo_over_average_assets():
    assert vm.accruals_ratio(_year(net_income=150, operating_cash_flow=220, total_assets=2000),
                             _year(total_assets=1800)) == pytest.approx((150 - 220) / 1900)


@pytest.mark.unit
def test_beneish_m_for_identical_years_is_the_intercept_plus_tata():
    y = _year()
    m = vm.beneish_m(y, y)       # every index is 1.0; TATA = (150-220)/2000
    assert m == pytest.approx(-4.84 + 0.920 + 0.528 + 0.404 + 0.892 + 0.115 - 0.172 - 0.327 + 4.679 * (-70 / 2000))


@pytest.mark.unit
def test_beneish_m_is_none_when_receivables_are_missing():
    assert vm.beneish_m(_year(receivables=None), _year()) is None


@pytest.mark.unit
def test_accounting_checks_tolerate_missing_inputs():
    assert vm.accruals_ratio({}, None) is None
    assert vm.cash_conversion([]) is None
    assert vm.receivables_vs_revenue({}, {}) is None
    assert vm.beneish_m({}, None) is None
    assert vm.accounting_checks([{}])["flags"] == []


@pytest.mark.unit
def test_accounting_flags_name_each_breach():
    bad = [_year(net_income=400, operating_cash_flow=100, receivables=300), _year(receivables=100, revenue=1000)]
    flags = vm.accounting_checks(bad)["flags"]
    assert any("accruals" in f for f in flags) and any("receivables" in f for f in flags)
    assert vm.accounting_checks([_year(), _year()])["flags"] == []


@pytest.mark.unit
def test_report_has_an_accounting_section():
    m = vm.company_metrics([_year(), _year()], price=20, market_cap=2000, shares=100)
    assert "## Accounting checks" in vm.render_report("X", "2026-10-23", m)


@pytest.mark.unit
def test_yahoo_rows_map_the_accounting_fields():
    rows = {"Total Revenue": 1000.0, "Gross Profit": 400.0, "Net Income": 150.0, "Total Assets": 2000.0,
            "Operating Cash Flow": 220.0, "Cash And Cash Equivalents": 100.0,
            "Accounts Receivable": 120.0, "Reconciled Depreciation": 40.0,
            "Selling General And Administration": 150.0, "Purchase Of Business": -7.0}
    frame = pd.DataFrame({pd.Timestamp("2025-12-31"): list(rows.values())}, index=list(rows))
    (year,) = yahoo_vm.statement_years(frame)
    assert (year["receivables"], year["depreciation"], year["sga"], year["acquisitions"]) == (120.0, 40.0, 150.0, -7.0)
    assert year["goodwill"] is None

    def with_rows(extra):
        r = {**rows, **extra}
        return pd.DataFrame({pd.Timestamp("2025-12-31"): list(r.values())}, index=list(r))

    both = with_rows({"Goodwill And Other Intangible Assets": 900.0, "Goodwill": 600.0})
    assert yahoo_vm.statement_years(both)[0]["goodwill"] == 900.0        # intangibles included first
    assert yahoo_vm.statement_years(with_rows({"Goodwill": 600.0}))[0]["goodwill"] == 600.0


@pytest.mark.unit
def test_capital_allocation_totals_and_buyback_price():
    y1 = _year(free_cash_flow=180, dividends_paid=-50, buybacks=-30, shares=100, total_debt=500, cash=100,
               acquisitions=-20, fiscal_year_end="2025-12-31")
    y0 = _year(shares=102, total_debt=520, cash=90)
    ca = vm.capital_allocation([y1, y0], avg_prices={"2025": 16.0}, price=20.0)
    row = ca["per_year"][0]
    assert row["shares_retired"] == 2 and row["buyback_price"] == pytest.approx(15.0)
    assert row["avg_price"] == 16.0
    assert row["net_debt_change"] == pytest.approx((500 - 100) - (520 - 90))
    t = ca["totals"]
    assert t["returned"] == 160 and t["generated"] == 360 and t["acquisitions"] == 20
    assert t["unexplained"] == pytest.approx(360 - 160 - 20 + t["net_debt_change"])
    assert t["avg_buyback_price"] == pytest.approx(15.0)


@pytest.mark.unit
def test_capital_allocation_with_no_returns_reports_zero_returned():
    ca = vm.capital_allocation([_year(dividends_paid=None, buybacks=None, free_cash_flow=-10)])
    assert ca["per_year"][0]["buyback_price"] is None
    assert ca["per_year"][0]["avg_price"] is None
    assert ca["totals"]["returned"] == 0


@pytest.mark.unit
def test_capital_allocation_tolerates_empty_input():
    assert vm.capital_allocation([])["totals"]["returned"] == 0


@pytest.mark.unit
def test_fiscal_average_closes_average_the_twelve_months_to_each_year_end():
    hist = pd.DataFrame({"Close": [10, 20, 30, 40, 50]},
                        index=pd.to_datetime(["2024-06-30", "2024-07-01", "2025-01-15", "2025-06-30",
                                              "2025-07-01"]))
    fyes = ["2025-06-30", "2024-06-30", "2020-06-30"]
    assert yahoo_vm.fiscal_average_closes(hist, fyes) == {"2025-06-30": 30.0, "2024-06-30": 10.0}
    assert yahoo_vm.fiscal_average_closes(None, fyes) == {}


@pytest.mark.unit
def test_average_prices_match_the_fiscal_year_end_first_then_the_calendar_year():
    years = [_year(fiscal_year_end="2025-06-30", shares=98), _year(fiscal_year_end="2024-06-30")]
    by_fye = vm.capital_allocation(years, avg_prices={"2025-06-30": 16.0, "2025": 99.0})
    assert by_fye["per_year"][0]["avg_price"] == 16.0
    by_year = vm.capital_allocation(years, avg_prices={"2025": 17.0})
    assert by_year["per_year"][0]["avg_price"] == 17.0
    dated = _dated_years(5)
    keyed = {y["fiscal_year_end"]: p for y, p in zip(dated[:3], (20, 30, 40), strict=True)}
    er_fye = vm.expected_return(dated, 20.0, 2000, 100, avg_prices=keyed)
    er_year = vm.expected_return(dated, 20.0, 2000, 100, avg_prices={"2025": 20, "2024": 30, "2023": 40})
    assert er_fye["ev_ebit_target"] == pytest.approx(er_year["ev_ebit_target"]) != 12.0


@pytest.mark.unit
def test_capital_allocation_says_the_buyback_price_is_net_of_issuance():
    m = vm.company_metrics([_year(shares=98), _year()], price=20, market_cap=1960, shares=98)
    text = vm.render_report("X", "2026-10-23", m)
    assert "| Buyback price (net of issuance) |" in text and "net of shares issued" in text


@pytest.mark.unit
def test_vendor_averages_unadjusted_closes_by_fiscal_year_in_the_quote_unit(monkeypatch):
    hist = pd.DataFrame({"Close": [800.0, 900.0]}, index=pd.to_datetime(["2024-12-31", "2025-03-31"]))
    fake = _gbp_ticker(history=hist)
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: fake())
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    monkeypatch.setattr(yahoo_vm, "usd_rate", lambda c: None)
    report = yahoo_vm.get_value_metrics("DNLM.L", "2026-10-23")
    assert fake.history_kwargs["auto_adjust"] is False
    row = next(line for line in report.splitlines() if line.startswith("| 2025-06-30 | 100 |"))
    assert row.rstrip().endswith("| 850.00 |")      # mean of the year to June 2025, in pence


@pytest.mark.unit
def test_report_has_a_capital_allocation_section_with_the_totals_line():
    m = vm.company_metrics([_year(), _year()], price=20, market_cap=2000, shares=100)
    text = vm.render_report("X", "2026-10-23", m)
    assert "## Capital allocation" in text and "Over 2 years" in text


@pytest.mark.unit
def test_capital_allocation_net_debt_change_is_none_when_a_balance_value_is_missing():
    ca = vm.capital_allocation([_year(cash=None), _year()])
    assert ca["per_year"][0]["net_debt_change"] is None
    assert ca["totals"]["net_debt_change"] == 0
    ca = vm.capital_allocation([_year(), _year(total_debt=None)])
    assert ca["per_year"][0]["net_debt_change"] is None


def test_epv_is_after_tax_normalized_ebit_capitalized_at_the_rate_less_net_debt():
    years = [_year(), _year(ebit=180)]
    v = vm.formula_value(years, price=20.0, shares=100, net_debt=400, rate=0.10)
    assert v["epv"] == pytest.approx((190 * (1 - vm.TAX_RATE) / 0.10 - 400) / 100)
    assert vm.formula_value([_year(ebit=-5), _year(ebit=-5)], 20.0, 100, 0, 0.10)["epv"] is None


def test_sensitivity_grid_moves_with_rate_and_growth():
    v = vm.formula_value([_year(), _year()], price=20.0, shares=100, net_debt=400, rate=0.10)
    assert set(v["grid"]) == {"r-1", "r", "r+1"} and len(v["grid"]["r"]) == 3
    assert v["grid"]["r-1"][1] > v["grid"]["r"][1] > v["grid"]["r+1"][1]
    assert v["grid"]["r"][1] == pytest.approx(v["base"])
    assert v["grid"]["r"][2] > v["grid"]["r"][0]


def test_growth_base_rates_pick_the_cohort_and_cap():
    br = vm.growth_base_rates(8e9)
    assert br["cohort"] == "$5–25B" and 0 < br["share_achieving"][0.10] < br["share_achieving"][0.05]
    assert br["cap"] in br["share_achieving"] and vm.growth_base_rates(None) is None
    assert vm.growth_base_rates(0) is None
    assert vm.growth_base_rates(5e8)["cohort"] == "<$1B" and vm.growth_base_rates(1e11)["cohort"] == ">$25B"


def test_base_rate_note_names_the_level_the_implied_growth_reaches():
    note = vm.base_rate_note(0.079, 8e9)
    assert note == ("Implied growth 7.9%/yr: base rate — about 30% of companies with revenue $5–25B "
                    "sustained 7% or more for 10 years.")
    assert "0% or more" in vm.base_rate_note(-0.02, 8e9)
    assert vm.base_rate_note(None, 8e9) is None and vm.base_rate_note(0.05, None) is None


def test_worksheet_renders_epv_grid_and_base_rate_note():
    m = vm.company_metrics([_year(), _year()], price=20, market_cap=2000, shares=100)
    text = vm.render_report("X", "2026-10-23", m)
    assert "## Valuation worksheet" in text and "Earnings-power value" in text and "base rate" in text
    assert "| r" in text


@pytest.mark.unit
def test_expected_return_sums_yield_growth_and_reversion_and_orders_scenarios():
    years = [_year(dividends_paid=-50, buybacks=-30, free_cash_flow=180)] * 3 + [_year()] * 2
    er = vm.expected_return(years, price=20.0, market_cap=2000, shares=100,
                            avg_prices={"2021": 20, "2022": 20, "2023": 20, "2024": 20, "2025": 20})
    assert er["expected_return"] == pytest.approx(er["yield"] + er["growth"] + er["reversion"])
    assert er["bear"] <= er["base"] <= er["bull"] and er["growth"] <= 0.10
    assert er["yield"] == pytest.approx(80 / 2000)


@pytest.mark.unit
def test_expected_return_caps_growth_at_the_base_rate_and_reversion_at_five_points():
    rich = [_year(ebit=2000, revenue=30e9)] * 5         # ROIC huge, revenue in the >$25B cohort
    er = vm.expected_return(rich, price=1.0, market_cap=100, shares=100)
    assert er["growth"] <= vm.growth_base_rates(30e9)["cap"] and abs(er["reversion"]) <= 0.05


@pytest.mark.unit
def test_expected_return_is_none_without_positive_owner_earnings():
    assert vm.expected_return([_year(free_cash_flow=-50)] * 3, price=20, market_cap=2000, shares=100) is None


@pytest.mark.unit
def test_company_metrics_carries_the_expected_return_line():
    m = vm.company_metrics([_year()] * 5, price=20.0, market_cap=2000, shares=100)
    assert m["expected_return"] is not None
    assert "Expected return (7-year):" in vm.render_report("X", "2026-01-01", m)


def _dated_years(n, **kw):
    return [_year(fiscal_year_end=f"{2025 - i}-12-31", **kw) for i in range(n)]


@pytest.mark.unit
def test_ev_ebit_target_is_the_median_of_priced_years_needs_three_and_can_be_overridden():
    years = _dated_years(5)
    prices = {"2025": 20, "2024": 30, "2023": 40}            # calendar-year keys: the fallback lookup
    er = vm.expected_return(years, price=20.0, market_cap=2000, shares=100, avg_prices=prices)
    by_hand = sorted((p * 100 + 500 - 100) / 200 for p in prices.values())[1]
    assert er["ev_ebit_target"] == pytest.approx(by_hand) and by_hand != 12.0
    two = vm.expected_return(years, price=20.0, market_cap=2000, shares=100,
                             avg_prices={"2025": 20, "2024": 30})
    assert two["ev_ebit_target"] == 12.0
    for ap in (prices, None):
        over = vm.expected_return(years, 20.0, 2000, 100, avg_prices=ap, target_multiple=15.0)
        assert over["ev_ebit_target"] == 15.0


@pytest.mark.unit
def test_reinvestment_uses_the_same_years_for_payouts_and_owner_earnings():
    years = [_year(), _year(), _year(free_cash_flow=None, dividends_paid=-1000)]
    er = vm.expected_return(years, price=20.0, market_cap=2000, shares=100)
    # two computable years: payout 160 of owner earnings 320 -> reinvest 0.5 (not dragged down by year 3)
    assert er["sustainable_growth"] == pytest.approx(0.5 * min(200 / 700, 1.0))


@pytest.mark.unit
def test_report_places_yen_revenue_in_the_dollar_cohort():
    years = [_year(revenue=1.5e12), _year(revenue=1.4e12)]
    m = vm.company_metrics(years, price=20, market_cap=2000, shares=100, revenue_usd=1.5e12 * 0.0064)
    text = vm.render_report("7203.T", "2026-10-23", m, currency="JPY")
    assert "companies with revenue $5–25B" in text  # 1.5e12 yen is $9.6B; raw it would be >$25B


@pytest.mark.unit
def test_report_skips_the_base_rate_note_for_foreign_revenue_without_a_rate():
    years = [_year(revenue=1.5e12), _year(revenue=1.4e12)]
    m = vm.company_metrics(years, price=20, market_cap=2000, shares=100, revenue_usd=None)
    text = vm.render_report("7203.T", "2026-10-23", m, currency="JPY")
    assert "## Valuation worksheet" in text and "companies with revenue" not in text


def _gbp_ticker(history=None):
    col = [pd.Timestamp("2025-06-30"), pd.Timestamp("2024-06-30"), pd.Timestamp("2023-06-30")]

    def frame(rows):
        return pd.DataFrame(dict.fromkeys(col, rows))

    class FakeTicker:
        info = {"currency": "GBp", "financialCurrency": "GBP", "currentPrice": 886.0,
                "marketCap": 886e6, "sharesOutstanding": 1e8}
        income_stmt = frame({"Total Revenue": 1e9, "EBIT": 1.2e8, "Net Income": 9e7, "Gross Profit": 4e8})
        balance_sheet = frame({"Total Assets": 2e9, "Current Assets": 6e8, "Current Liabilities": 3e8,
                               "Stockholders Equity": 9e8, "Total Debt": 2e8,
                               "Cash And Cash Equivalents": 5e7, "Ordinary Shares Number": 1e8})
        cashflow = frame({"Free Cash Flow": 1e8, "Operating Cash Flow": 1.3e8, "Repurchase Of Capital Stock": -2e7})

        def history(self, **kw):
            FakeTicker.history_kwargs = kw
            return history

    return FakeTicker


@pytest.mark.unit
def test_vendor_presents_a_pence_quote_in_pence_against_pound_statements(monkeypatch):
    from tradingagents.agents.decision_math import worksheet_base

    fake = _gbp_ticker()
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: fake())
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    monkeypatch.setattr(yahoo_vm, "usd_rate", lambda c: None)
    report = yahoo_vm.get_value_metrics("DNLM.L", "2026-10-23")

    years = yahoo_vm.statement_years(fake.income_stmt, fake.balance_sheet, fake.cashflow)
    pounds = vm.company_metrics(years, 8.86, 886e6, 1e8, rate=0.09)["valuation"]
    base = worksheet_base(report)
    assert base == pytest.approx(100 * pounds["base"], rel=1e-3)
    mos = (base - 886) / base
    assert f"margin of safety at base {mos:.1%}" in report
    assert "Per-share values in GBp (pence), the quote unit; statements in GBP." in report
    assert "Price 886.00" in report and "left out rather than mixed" not in report


@pytest.mark.unit
def test_to_quote_unit_scales_every_per_share_value_and_leaves_ratios():
    years = [_year(fiscal_year_end="2025-12-31", shares=98), _year(fiscal_year_end="2024-12-31")]
    m = vm.company_metrics(years, price=8.0, market_cap=784.0, shares=98,
                           avg_prices={"2025": 7.0})
    scaled = yahoo_vm._to_quote_unit(m, 100)
    v, s = m["valuation"], scaled["valuation"]
    assert scaled["price"] == 800.0
    for key in ("low", "high", "base", "epv"):
        assert s[key] == pytest.approx(100 * v[key])
    assert s["methods"]["fcf"] == pytest.approx([100 * x for x in v["methods"]["fcf"]])
    assert s["grid"]["r"] == pytest.approx([100 * x for x in v["grid"]["r"]])
    assert s["margin_of_safety"] == v["margin_of_safety"] and s["implied_growth"] == v["implied_growth"]
    ca, sca = m["capital_allocation"], scaled["capital_allocation"]
    assert sca["per_year"][0]["buyback_price"] == pytest.approx(100 * ca["per_year"][0]["buyback_price"])
    assert sca["per_year"][0]["avg_price"] == 700.0
    assert sca["totals"]["avg_buyback_price"] == pytest.approx(100 * ca["totals"]["avg_buyback_price"])
    assert sca["price"] == 800.0
    assert scaled["expected_return"] == m["expected_return"]
    assert m["price"] == 8.0  # the input is not changed


# --- record, inflection and run-rate value ----------------------------------------------

def _quarter(end, revenue, ebit, **kw):
    base = {"fiscal_year_end": end, "revenue": revenue, "ebit": ebit, "current_assets": 600,
            "current_liabilities": 300, "cash": 100, "net_ppe": 500, "free_cash_flow": 50,
            "stock_based_comp": 5, "total_debt": 500, "shares": 100}
    return {**base, **kw}


# Newest first: q0 grows 30% on q4, q1 grew 10% on q5 -> acceleration +20 points; TTM 518.
Q_REVENUE = [143, 118.8, 136.2, 120, 110, 108, 105, 102, 100]
Q_ENDS = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30",
          "2025-03-31", "2024-12-31", "2024-09-30", "2024-06-30"]


RECORD_LABELS = (
    "ROIC years above 20%", "ROIC years counted", "ROIC worst of counted years",
    "ROIC incl. goodwill years above 15%", "ROIC incl. goodwill worst of counted years", "Revenue CAGR",
    "Revenue CAGR years", "Earned growth", "FCF CAGR", "FCF CAGR years", "Reinvestment rate",
    "Sustainable growth",
    "TTM revenue vs 5y average", "TTM EBIT vs 5y average", "Revenue acceleration",
    "EBIT margin change on a year earlier", "Incremental ROIC", "Run-rate value", "Run-rate 3-year growth",
    "Run-rate decay", "Implied 3-year growth",
)


def _quarters():
    out = []
    for i, (end, rev) in enumerate(zip(Q_ENDS, Q_REVENUE, strict=True)):
        margin = 0.25 if i < 4 else 0.20
        out.append(_quarter(end, rev, rev * margin, net_ppe=500 if i < 4 else 400))
    return out


@pytest.mark.unit
def test_record_counts_roic_years_and_cagrs():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.1 ** (10 - i)) for i in range(11)]
    years[2]["ebit"] = 100                          # ROIC 100/700, below 20%
    years[4]["free_cash_flow"] = 90                 # FCF doubled over the four intervals of years[:5]
    years[0]["goodwill"] = 300                      # ROIC incl. goodwill 200/1000 = 20%
    rec = vm.record(years)
    assert rec["years_available"] == 11
    assert rec["roic_years_above_20"] == 4
    assert rec["roic_min_5y"] == pytest.approx(100 / 700)
    assert rec["roic_years_counted"] == 5 and rec["roic_gw_years_above_15"] == 4
    assert rec["roic_gw_min"] == pytest.approx(100 / 700)
    assert rec["revenue_cagr"] == pytest.approx(0.10) and rec["revenue_cagr_years"] == 10
    assert rec["revenue_cagr_10y"] == pytest.approx(0.10)
    assert rec["revenue_cagr_5y"] == pytest.approx(0.10)
    assert rec["fcf_cagr"] == pytest.approx(2 ** (1 / 4) - 1) and rec["fcf_cagr_years"] == 4
    assert "fcf_cagr_5y" not in rec
    assert rec["reinvestment_rate"] == pytest.approx(1 - 240 / 480)  # payout 80 of owner earnings 160 a year
    assert rec["sustainable_growth"] == pytest.approx((4 * 200 / 700 + 100 / 700) / 5 * 0.5)
    er = vm.expected_return(years, price=20, market_cap=2000, shares=100)
    assert er["sustainable_growth"] == pytest.approx(rec["sustainable_growth"])


@pytest.mark.unit
def test_record_with_three_years_is_partial():
    rec = vm.record([_year(fiscal_year_end=f"{2025 - i}-12-31") for i in range(3)])
    assert rec["years_available"] == 3 and rec["roic_years_above_20"] == 3
    assert rec["revenue_cagr"] is None and rec["revenue_cagr_years"] is None   # a 2-year span is too short
    assert rec["revenue_cagr_10y"] is None and rec["revenue_cagr_5y"] is None
    assert rec["fcf_cagr"] is None and rec["fcf_cagr_years"] is None      # two intervals: too short
    assert rec["roic_years_counted"] == 3
    five = vm.record([_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.1 ** (4 - i)) for i in range(5)])
    assert five["revenue_cagr_years"] == 4 and five["revenue_cagr"] == pytest.approx(0.10)
    assert five["revenue_cagr_5y"] is None
    empty = vm.record([])
    assert empty["years_available"] == 0 and empty["roic_min_5y"] is None
    assert empty["roic_years_counted"] == 0 and empty["roic_gw_min"] is None
    assert empty["reinvestment_rate"] is None and empty["sustainable_growth"] is None


@pytest.mark.unit
def test_record_extends_the_revenue_cagr_with_the_sec_history():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.1 ** (10 - i)) for i in range(5)]
    history = {str(2015 + i): 1000 * 1.1 ** i for i in range(7)}       # 2015..2021, overlapping Yahoo's 2021
    rec = vm.record(years, revenue_history=history)
    assert rec["revenue_cagr_years"] == 10 and rec["revenue_cagr"] == pytest.approx(0.10)
    assert rec["revenue_cagr_10y"] == pytest.approx(0.10) and rec["revenue_cagr_5y"] == pytest.approx(0.10)
    assert rec["years_available"] == 5                                   # the history feeds the CAGR only
    assert vm.record(years, revenue_history={"2015": None, "bad": 5})["revenue_cagr_years"] == 4


@pytest.mark.unit
def test_fcf_cagr_runs_between_the_positive_ends_of_five_years():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", free_cash_flow=f) for i, f in
             enumerate([-10, 150, 120, 100, 75, 10])]
    rec = vm.record(years)                     # newest positive FY2024 (150) on the oldest, FY2021 (75)
    assert rec["fcf_cagr"] == pytest.approx(2 ** (1 / 3) - 1) and rec["fcf_cagr_years"] == 3
    years[4]["free_cash_flow"] = -5            # FY2022 (100) is the oldest positive: two intervals
    assert vm.record(years)["fcf_cagr"] is None and vm.record(years)["fcf_cagr_years"] is None


@pytest.mark.unit
def test_roic_including_goodwill_adds_it_to_capital():
    assert vm.roic_incl_goodwill(_year(goodwill=300)) == pytest.approx(200 / 1000)
    assert vm.roic_incl_goodwill(_year()) == pytest.approx(vm.roic(_year()))   # no goodwill row: 0
    assert vm.roic_incl_goodwill(_year(net_ppe=None)) is None
    assert "goodwill" in vm.YEAR_FIELDS


@pytest.mark.unit
def test_earned_growth_is_the_revenue_cagr_held_to_the_cohort_cap():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.12 ** (10 - i)) for i in range(11)]
    small = vm.company_metrics(years, 20, 2000, 100)
    assert small["record"]["earned_growth"] == pytest.approx(0.10)        # under $1B the cap is 10%
    large = vm.company_metrics(years, 20, 2000, 100, revenue_usd=30e9)
    assert large["record"]["earned_growth"] == pytest.approx(0.05)        # over $25B: 5%
    slow = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.04 ** (10 - i)) for i in range(11)]
    assert vm.company_metrics(slow, 20, 2000, 100)["record"]["earned_growth"] == pytest.approx(0.04)
    assert vm.company_metrics([_year(), _year()], 20, 2000, 100)["record"]["earned_growth"] is None
    assert vm.earned_growth(0.12, None) == pytest.approx(vm.ER_MAX_GROWTH)    # unknown cohort
    report = vm.render_report("X", "2026-10-23", large)
    assert "- Earned growth 5.0%" in report


@pytest.mark.unit
def test_inflection_ttm_and_acceleration():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=400, ebit=80) for i in range(5)]
    years[1]["net_ppe"] = 400                     # FY2024, the last one ended a year before the quarter
    years[1]["revenue"] = 320                     # fiscal 2025 grew 25%
    inf = vm.inflection(years, _quarters())
    assert inf["quarters_available"] == 9
    assert inf["quarters_used"] == Q_ENDS[:4]
    assert inf["ttm_revenue"] == pytest.approx(518)
    assert inf["ttm_ebit"] == pytest.approx(129.5)
    assert inf["ttm_vs_5y_revenue"] == pytest.approx(518 / 384)
    assert inf["ttm_vs_5y_ebit"] == pytest.approx(129.5 / 80)
    assert inf["yoy_growth_latest"] == pytest.approx(0.30)
    assert inf["fiscal_year_growth"] == pytest.approx(0.25) and "yoy_growth_previous" not in inf
    assert inf["revenue_acceleration"] == pytest.approx(0.30 - 0.25)
    assert inf["ebit_margin_change_4q"] == pytest.approx(0.05)
    # (TTM EBIT 129.5 - FY2024's 80) after tax / (invested capital 700 now - 600 at FY2024's end)
    assert inf["incremental_roic"] == pytest.approx((129.5 - 80) * (1 - vm.TAX_RATE) / 100)
    assert inf["ttm_revenue_growth"] == pytest.approx(518 / 425 - 1)


@pytest.mark.unit
def test_inflection_uses_last_four_available_quarters():
    years = [_year(revenue=400, ebit=80)]
    quarters = [_quarter("2026-09-30", None, None), *_quarters()]
    inf = vm.inflection(years, quarters)
    assert inf["quarters_used"] == Q_ENDS[:4] and inf["ttm_revenue"] == pytest.approx(518)
    short = vm.inflection(years, _quarters()[:3])
    assert short["ttm_revenue"] is None and short["revenue_acceleration"] is None
    assert short["incremental_roic"] is None and short["quarters_available"] == 3
    assert vm.inflection(years, _quarters()[:6])["revenue_acceleration"] is None   # one fiscal year
    two = [_year(revenue=400), _year(revenue=400)]
    assert vm.inflection(two, _quarters()[:5])["revenue_acceleration"] == pytest.approx(0.30)
    assert vm.inflection([_year(revenue=400), _year(revenue=0)], _quarters())["revenue_acceleration"] is None
    none = vm.inflection([], [])
    assert none["ttm_vs_5y_ebit"] is None and none["quarters_used"] == []
    # Incremental ROIC: FY2025 ended under a year before the latest quarter, so FY2024 is the base.
    dated = [_year(fiscal_year_end="2025-12-31", ebit=10), _year(fiscal_year_end="2024-12-31", net_ppe=400)]
    four = vm.inflection(dated, _quarters()[:4])
    assert four["incremental_roic"] == pytest.approx((129.5 - 200) * (1 - vm.TAX_RATE) / 100)
    assert vm.inflection(years, _quarters())["incremental_roic"] is None     # no dated fiscal year
    flat = vm.inflection([_year(fiscal_year_end="2024-12-31")], _quarters())   # no new capital
    assert flat["incremental_roic"] is None


@pytest.mark.unit
def test_ttm_needs_four_consecutive_quarters():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=400, ebit=80) for i in range(5)]
    qs = _quarters()
    gap = vm.inflection(years, [qs[0], qs[1], *qs[3:]])           # 2025-12-31 missing: no TTM
    assert gap["ttm_revenue"] is None and gap["ttm_ebit"] is None and gap["quarters_used"] == []
    assert gap["ttm_vs_5y_ebit"] is None and gap["incremental_roic"] is None
    m = vm.company_metrics(years, 20, 2000, 100, quarters=[qs[0], qs[1], *qs[3:]])
    assert m["run_rate"]["basis"] == "latest fiscal year"
    old_gap = vm.inflection(years, [*qs[:6], *qs[7:]])            # a year earlier, 2024-12-31 missing
    assert old_gap["ttm_revenue"] == pytest.approx(518) and old_gap["ttm_revenue_growth"] is None
    shifted = vm.inflection(years, [qs[0], *qs[2:]])              # a gap inside the year-earlier four
    assert shifted["ttm_revenue"] is None and shifted["ttm_revenue_growth"] is None


@pytest.mark.unit
def test_quarter_owner_earnings_fall_back_to_cash_flow_less_capex():
    qs = [{**q, "free_cash_flow": None, "operating_cash_flow": 70, "capex": -20} for q in _quarters()[:2]]
    qs += _quarters()[2:]
    inf = vm.inflection([_year(fiscal_year_end="2025-12-31")], qs)
    assert inf["ttm_owner_earnings"] == pytest.approx(2 * (70 - 20 - 5) + 2 * (50 - 5))
    assert inf["ttm_fcf_derived_quarters"] == 2
    m = vm.company_metrics([_year(fiscal_year_end="2025-12-31", ebit=80)], 20, 2000, 100, quarters=qs)
    assert "operating cash flow less capex for 2 of the 4 quarters" in vm.render_report("ABC", "x", m)
    bare = [{**q, "free_cash_flow": None} for q in _quarters()]
    assert vm.inflection([], bare)["ttm_owner_earnings"] is None


@pytest.mark.unit
def test_yahoo_quarterly_frames_fail_one_at_a_time():
    qcols = [pd.Timestamp(e) for e in Q_ENDS]
    income = pd.DataFrame({c: [100.0, 20.0, 15.0, 2000.0, 600.0, 300.0] for c in qcols},
                          index=["Total Revenue", "EBIT", "Net Income", "Total Assets", "Current Assets",
                                 "Current Liabilities"])

    class Partial:
        quarterly_income_stmt = income
        quarterly_cashflow = None

        @property
        def quarterly_balance_sheet(self):
            raise RuntimeError("Yahoo down")

    quarters, complete = yahoo_vm.fetch_quarters(Partial())
    assert len(quarters) == 9 and quarters[0]["revenue"] == 100.0 and complete is False
    assert yahoo_vm.quarterly_statements(Partial())[0]["ebit"] == 20.0


@pytest.mark.unit
def test_run_rate_value_two_stage_and_implied_g3():
    r, d = 0.09, 0.05

    def two_stage(e, g):
        explicit = sum(e * (1 + g) ** t / (1 + r) ** t for t in (1, 2, 3))
        return explicit + e * (1 + g) ** 3 * ((1 + d) / (r - d)) / (1 + r) ** 3

    fcf_ps = two_stage(100, 0.20) / 10
    ebit_ps = (two_stage(200 * (1 - vm.TAX_RATE), 0.20) - 300) / 10
    price = two_stage(100, 0.10) / 10                   # the price a 10% three-year run assumes
    v = vm.run_rate_value(100, 200, 300, 10, price, g3=0.20, rate=r, decay_to=d)
    assert v["methods"]["fcf"] == pytest.approx(fcf_ps)
    assert v["methods"]["ebit"] == pytest.approx(ebit_ps)
    assert v["base"] == pytest.approx((fcf_ps + ebit_ps) / 2)
    assert (v["low"], v["high"]) == (pytest.approx(min(fcf_ps, ebit_ps)), pytest.approx(max(fcf_ps, ebit_ps)))
    assert v["implied_g3"] == pytest.approx(0.10, abs=1e-4)
    assert vm.run_rate_value(100, 200, 300, 10, 1e12, 0.2, r, d)["implied_g3"] is None
    assert vm.run_rate_value(100, 200, 300, 10, None, 0.2, r, d)["implied_g3"] is None
    assert vm.run_rate_value(-5, -1, 0, 10, 20, 0.2, r, d) is None
    assert vm.run_rate_value(100, 200, 0, 0, 20, 0.2, r, d) is None
    assert vm.run_rate_value(None, None, None, None, None, 0.2, r, d) is None


@pytest.mark.unit
def test_report_has_record_and_inflection_lines():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=400, ebit=80, current_assets=300,
                   net_ppe=100) for i in range(5)]
    m = vm.company_metrics(years, price=20, market_cap=2000, shares=100, quarters=_quarters())
    assert m["run_rate"]["g3"] == pytest.approx(518 / 425 - 1)
    assert "TTM a year earlier" in m["run_rate"]["g3_source"]
    assert m["run_rate"]["decay_to"] == 0.03   # the worksheet's base growth, whatever the cohort
    assert m["record"]["roic_years_above_20"] == 5 and m["inflection"]["quarters_available"] == 9
    report = vm.render_report("ABC", "2026-10-23", m)
    section = report.split("## Record and inflection", 1)[1]
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    values = dict(ln[2:].rsplit(" ", 1) for ln in lines)
    assert list(values) == list(RECORD_LABELS)
    for v in values.values():                      # one number per label, no thousands commas
        assert v == "n/a" or re.fullmatch(r"[+-]?\d+(\.\d+)?%?", v), v
    assert values["ROIC years above 20%"] == "5" and values["ROIC years counted"] == "5"
    assert (values["Revenue CAGR"], values["Revenue CAGR years"]) == ("0.0%", "4")
    assert values["TTM EBIT vs 5y average"] == "1.62"
    assert values["ROIC incl. goodwill years above 15%"] == "5"
    assert values["ROIC incl. goodwill worst of counted years"] == "80.0%"     # 80 / (0 + 100)
    assert (values["FCF CAGR"], values["FCF CAGR years"], values["Earned growth"]) == ("0.0%", "4", "0.0%")
    assert values["Revenue acceleration"] == "+30%"
    assert values["Incremental ROIC"] == "7%"        # (129.5 - 80) x 0.79 / (700 - 100)
    assert (values["Run-rate 3-year growth"], values["Run-rate decay"]) == ("22%", "3%")
    assert values["Run-rate value"] == f"{m['run_rate']['base']:.2f}"
    assert values["Implied 3-year growth"] == f"{m['run_rate']['implied_g3']:.0%}"
    assert "Record: ROIC ≥ 20% in 5/5 years, ROIC incl. goodwill ≥ 15% in 5/5 years" in section
    assert ("Inflection: TTM EBIT is 1.62× the 5-year average; revenue growth accelerated from 0% in "
            "fiscal 2025 to 30% y/y in the latest quarter") in section
    assert "Run-rate: TTM earnings growing 22% a year for three years" in section
    bare = vm.render_report("ABC", "2026-10-23", vm.company_metrics([_year(), _year()], 20, 2000, 100))
    assert "- TTM EBIT vs 5y average n/a" in bare and "- Revenue acceleration n/a" in bare
    assert "- Revenue CAGR n/a" in bare and "- Revenue CAGR years n/a" in bare
    assert "- Run-rate value n/a" in bare and "- Incremental ROIC n/a" in bare
    assert ("Run-rate: not shown — no inflection in the numbers (TTM EBIT n/a, acceleration n/a): "
            "value on the worksheet base.") in bare


@pytest.mark.unit
def test_run_rate_is_not_shown_without_an_inflection():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31") for i in range(5)]
    years[1]["revenue"] = 800                       # fiscal 2025 grew 25%; the latest quarter 30%
    m = vm.company_metrics(years, price=20, market_cap=2000, shares=100, quarters=_quarters())
    assert m["run_rate"] is None
    note = ("no inflection in the numbers (TTM EBIT 0.65× the 5-year average, acceleration +5 points): "
            "value on the worksheet base")
    assert m["run_rate_note"] == note
    report = vm.render_report("ABC", "2026-10-23", m)
    for label in ("Run-rate value", "Run-rate 3-year growth", "Run-rate decay", "Implied 3-year growth"):
        assert f"- {label} n/a" in report
    assert f"Run-rate: not shown — {note}." in report
    assert vm.inflection_shows({"ttm_vs_5y_ebit": 1.3, "revenue_acceleration": None})
    assert vm.inflection_shows({"ttm_vs_5y_ebit": None, "revenue_acceleration": 0.10})
    assert not vm.inflection_shows({"ttm_vs_5y_ebit": 1.29, "revenue_acceleration": 0.09})


@pytest.mark.unit
def test_run_rate_growth_uses_ttm_on_the_last_fiscal_year_without_eight_quarters():
    # Six months after the fiscal year: the raw TTM change, 518 / 400 - 1 = 29.5%, not annualised (68%).
    years = [_year(fiscal_year_end="2025-12-31", revenue=400), _year(fiscal_year_end="2024-12-31")]
    m = vm.company_metrics(years, price=20, market_cap=2000, shares=100, quarters=_quarters()[:5])
    assert m["run_rate"]["g3"] == pytest.approx(0.295)
    assert m["run_rate"]["g3_source"] == "TTM revenue on fiscal 2025, not annualised"
    # Ten months after the fiscal year: annualised.
    late = [_year(fiscal_year_end="2025-08-31", revenue=480), _year(fiscal_year_end="2024-08-31")]
    m = vm.company_metrics(late, price=20, market_cap=2000, shares=100, quarters=_quarters()[:5])
    elapsed = (date(2026, 6, 30) - date(2025, 8, 31)).days / 365.25
    assert m["run_rate"]["g3"] == pytest.approx((518 / 480) ** (1 / elapsed) - 1)
    assert m["run_rate"]["g3_source"] == "TTM revenue on fiscal 2025, annualised over 10 months"
    few = vm.company_metrics(years, price=20, market_cap=2000, shares=100,
                             quarters=[_quarter("2026-06-30", 130, 30), _quarter("2025-06-30", 100, 20)])
    assert "latest quarter" in few["run_rate"]["g3_source"] and few["run_rate"]["g3"] == pytest.approx(0.30)
    low_rate = vm.company_metrics(years, 20, 2000, 100, rate=0.045, quarters=_quarters()[:5])
    assert low_rate["run_rate"]["decay_to"] == 0.025          # 2 points under the rate


@pytest.mark.unit
def test_yahoo_vendor_reads_quarterly_statements(monkeypatch):
    annual = [pd.Timestamp(f"{2025 - i}-12-31") for i in range(4)]

    def frame(cols, rows):
        return pd.DataFrame({c: [r[i] for r in rows.values()] for i, c in enumerate(cols)}, index=list(rows))

    qcols = [pd.Timestamp(e) for e in Q_ENDS]
    qs = _quarters()

    class FakeTicker:
        def __init__(self, symbol):
            self.info = {"currency": "USD", "financialCurrency": "USD", "currentPrice": 20.0,
                         "marketCap": 2000e6, "sharesOutstanding": 100e6}
            self.income_stmt = frame(annual, {"Total Revenue": [400e6] * 4, "EBIT": [80e6] * 4,
                                              "Net Income": [60e6] * 4})
            self.balance_sheet = frame(annual, {"Total Assets": [2000e6] * 4, "Current Assets": [300e6] * 4,
                                                "Current Liabilities": [300e6] * 4, "Net PPE": [100e6] * 4})
            self.cashflow = frame(annual, {"Free Cash Flow": [70e6] * 4, "Operating Cash Flow": [90e6] * 4})
            self.quarterly_income_stmt = frame(qcols, {"Total Revenue": [q["revenue"] * 1e6 for q in qs],
                                                       "EBIT": [q["ebit"] * 1e6 for q in qs],
                                                       "Net Income": [q["ebit"] * 7e5 for q in qs]})
            self.quarterly_balance_sheet = frame(qcols, {k: [q[f] * 1e6 for q in qs] for k, f in (
                ("Current Assets", "current_assets"), ("Current Liabilities", "current_liabilities"),
                ("Cash And Cash Equivalents", "cash"), ("Net PPE", "net_ppe"))})
            self.quarterly_cashflow = frame(qcols, {"Free Cash Flow": [50e6] * 9,
                                                    "Stock Based Compensation": [5e6] * 9})

        def history(self, **kw):
            return pd.DataFrame()

    years = yahoo_vm.statement_years(FakeTicker("X").quarterly_income_stmt, FakeTicker("X").quarterly_balance_sheet,
                                     FakeTicker("X").quarterly_cashflow)
    assert [y["fiscal_year_end"] for y in years] == Q_ENDS and years[0]["net_ppe"] == 500e6
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    report = yahoo_vm.get_value_metrics("ABC", "2026-10-23")
    assert "- TTM EBIT vs 5y average 1.62" in report
    assert "- Revenue acceleration +30%" in report                     # fiscal 2025 grew 0%


@pytest.mark.unit
def test_pence_scaling_covers_the_run_rate_value():
    m = {"price": 1.0, "run_rate": {"base": 2.0, "low": 1.5, "high": 2.5, "methods": {"fcf": 1.5, "ebit": 2.5},
                                    "implied_g3": 0.1, "g3": 0.2, "decay_to": 0.05}}
    scaled = yahoo_vm._to_quote_unit(m, 100.0)["run_rate"]
    assert (scaled["base"], scaled["low"], scaled["high"]) == (200.0, 150.0, 250.0)
    assert scaled["methods"] == {"fcf": 150.0, "ebit": 250.0}
    assert (scaled["implied_g3"], scaled["g3"]) == (0.1, 0.2)


@pytest.mark.unit
def test_yahoo_vendor_extends_the_record_with_the_sec_revenue_history(monkeypatch):
    cols = [pd.Timestamp(f"{2025 - i}-12-31") for i in range(4)]
    rows = {"Total Revenue": [1000 * 1.1 ** (10 - i) for i in range(4)], "EBIT": [200] * 4,
            "Net Income": [150] * 4, "Total Assets": [2000] * 4, "Current Assets": [600] * 4,
            "Current Liabilities": [300] * 4, "Net PPE": [500] * 4}

    class FakeTicker:
        def __init__(self, symbol):
            self.info = {"currency": "USD", "financialCurrency": "USD", "currentPrice": 20.0, "marketCap": 2000}
            self.income_stmt = pd.DataFrame({c: [v[i] for v in rows.values()] for i, c in enumerate(cols)},
                                            index=list(rows))
            self.balance_sheet = self.cashflow = None

        def history(self, **kw):
            return pd.DataFrame()

    monkeypatch.setattr(yahoo_vm.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    monkeypatch.setattr(yahoo_vm.sec_edgar, "cik_for", lambda t: "0000000001")
    asked = []
    monkeypatch.setattr(yahoo_vm.sec_filings, "annual_revenue",
                        lambda t: asked.append(t) or {str(2015 + i): 1000 * 1.1 ** i for i in range(7)})
    report = yahoo_vm.get_value_metrics("ABC", "2026-10-23")
    assert asked == ["ABC"] and "- Revenue CAGR 10.0%" in report and "- Revenue CAGR years 10" in report
    monkeypatch.setattr(yahoo_vm.sec_edgar, "cik_for", lambda t: None)
    monkeypatch.setattr(yahoo_vm.sec_filings, "annual_revenue", lambda t: pytest.fail("not a US filer"))
    no_sec = yahoo_vm.get_value_metrics("ABC", "2026-10-23")        # Yahoo's four fiscal years alone
    assert "- Revenue CAGR 10.0%" in no_sec and "- Revenue CAGR years 3" in no_sec

    def down(t):
        raise RuntimeError("EDGAR down")

    monkeypatch.setattr(yahoo_vm.sec_edgar, "cik_for", down)
    assert "- Revenue CAGR years 3" in yahoo_vm.get_value_metrics("ABC", "2026-10-23")   # Yahoo alone


@pytest.mark.unit
def test_sec_annual_revenue_by_fiscal_year(monkeypatch):
    from tradingagents.dataflows.vendors import sec_edgar, sec_filings

    def fact(start, end, val, form="10-K", filed=None):
        return {"start": start, "end": end, "val": val, "form": form, "filed": filed or end[:4] + "-02-20"}

    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [fact("2014-01-01", "2014-12-31", 50), fact("2015-01-01", "2015-12-31", 60)]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            fact("2016-01-01", "2016-12-31", 70), fact("2016-10-01", "2016-12-31", 20, form="10-Q")]}},
    }}}
    monkeypatch.setattr(sec_edgar, "cik_for", lambda t: "0000000001")
    monkeypatch.setattr(sec_edgar, "_cached_json", lambda url, name: facts)
    assert sec_filings.annual_revenue("ABC") == {"2016": 70, "2015": 60, "2014": 50}
    monkeypatch.setattr(sec_edgar, "cik_for", lambda t: None)
    assert sec_filings.annual_revenue("ABC") == {}


@pytest.mark.unit
def test_record_computes_the_cagr_from_four_fiscal_years():
    def grow(n):
        return [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.1 ** (n - 1 - i)) for i in range(n)]
    four = vm.record(grow(4))                        # Yahoo serves four fiscal years: three intervals
    assert four["revenue_cagr_years"] == 3 and four["revenue_cagr"] == pytest.approx(0.10)
    three = vm.record(grow(3))
    assert three["revenue_cagr"] is None and three["revenue_cagr_years"] is None


# --- the eight-quarter trend table --------------------------------------------------

def _trend_quarters():
    """Nine quarters (newest first) with the trend fields; the oldest only feeds a y/y."""
    out = []
    for i, q in enumerate(_quarters()):
        rev = q["revenue"]
        out.append({**q, "gross_profit": rev * (0.42 if i == 0 else 0.40), "net_income": 20.0,
                    "operating_cash_flow": 22.0 if i == 0 else 18.0, "free_cash_flow": 15.0,
                    "receivables": rev * 0.5})
    return out


@pytest.mark.unit
def test_quarter_trend_rows_newest_first_with_date_matched_yoy():
    rows = vm.quarter_trend(_trend_quarters())
    assert len(rows) == 8 and [r["period_end"] for r in rows] == Q_ENDS[:8]
    assert set(rows[0]) == {"period_end", "revenue", "revenue_yoy", "gross_margin", "ebit_margin",
                            "operating_cash_flow", "free_cash_flow", "cash_conversion",
                            "receivables_to_revenue"}
    assert rows[0]["revenue"] == 143 and rows[0]["revenue_yoy"] == pytest.approx(143 / 110 - 1)
    assert rows[4]["revenue_yoy"] == pytest.approx(110 / 100 - 1)    # the ninth quarter, not shown, matches
    assert rows[5]["revenue_yoy"] is None                              # nothing a year before it
    assert rows[0]["gross_margin"] == pytest.approx(0.42) and rows[1]["gross_margin"] == pytest.approx(0.40)
    assert rows[0]["ebit_margin"] == pytest.approx(0.25) and rows[7]["ebit_margin"] == pytest.approx(0.20)
    assert rows[0]["cash_conversion"] == pytest.approx(22 / 20) and rows[1]["cash_conversion"] == pytest.approx(0.9)
    assert rows[0]["receivables_to_revenue"] == pytest.approx(0.5)
    assert rows[0]["operating_cash_flow"] == 22.0 and rows[0]["free_cash_flow"] == 15.0


@pytest.mark.unit
def test_quarter_trend_tolerates_gaps_and_skips_quarters_without_revenue():
    quarters = [{"fiscal_year_end": "2026-09-30", "cash": 5.0}, *_quarters()[:3]]
    quarters[2] = {**quarters[2], "net_income": -5.0, "operating_cash_flow": 10.0}
    rows = vm.quarter_trend(quarters)
    assert [r["period_end"] for r in rows] == Q_ENDS[:3]              # the balance-sheet-only quarter is left out
    assert rows[0]["revenue_yoy"] is None and rows[0]["gross_margin"] is None
    assert rows[0]["cash_conversion"] is None and rows[0]["receivables_to_revenue"] is None
    assert rows[1]["cash_conversion"] is None                          # a loss: no conversion ratio
    assert vm.quarter_trend([]) == [] and vm.quarter_trend(None) == []


@pytest.mark.unit
def test_render_report_shows_the_eight_quarter_table_after_the_annual_table():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31") for i in range(3)]
    m = vm.company_metrics(years, 20, 2000, 100, quarters=_trend_quarters())
    text = vm.render_report("X", "2026-10-23", m)
    assert text.index("## By fiscal year") < text.index("## Last eight quarters") < text.index("## Price today")
    start = text.index("## Last eight quarters")
    section = text[start:text.index("\n## ", start)]
    assert ("| Quarter end | Revenue | Revenue y/y | Gross margin | EBIT margin | Operating cash flow "
            "| FCF | Cash conversion | Receivables / revenue |") in section
    assert section.count("\n| 20") == 8
    assert "| 2026-06-30 | 0 | 30.0% | 42.0% | 25.0% | 0 | 0 | 1.10× | 50.0% |" in section
    # Oldest shown (2024-09-30) to newest: gross margin +2 points, EBIT margin +5, conversion 0.90 -> 1.10.
    assert ("Over the span: gross margin up 2.0 points, EBIT margin up 5.0 points, "
            "cash conversion 0.90× → 1.10×.") in section
    flat = vm.render_report("X", "2026-10-23", vm.company_metrics(years, 20, 2000, 100))
    assert "## Last eight quarters" in flat and "No quarterly statements with revenue." in flat


@pytest.mark.unit
def test_quarter_trend_prose_calls_small_moves_flat_and_drops_missing_parts():
    rows = [{"period_end": "2026-06-30", "gross_margin": 0.403, "ebit_margin": 0.18, "cash_conversion": None},
            {"period_end": "2025-06-30", "gross_margin": 0.400, "ebit_margin": 0.20, "cash_conversion": 1.0}]
    assert vm.quarter_trend_prose(rows) == ("Over the span: gross margin flat (0.3 points), "
                                            "EBIT margin down 2.0 points.")
    assert vm.quarter_trend_prose(rows[:1]) is None


# --- consensus ------------------------------------------------------------------

CONSENSUS = {"revenue_growth_next": 0.062, "revenue_growth_following": 0.051, "eps_growth_next": 0.084,
             "analysts": 12, "target_mean": 61.5, "target_median": 60.0, "target_high": 75.0,
             "target_low": 48.0}


@pytest.mark.unit
def test_render_report_shows_the_consensus_section_after_the_record():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31", revenue=1000 * 1.1 ** (4 - i)) for i in range(5)]
    m = vm.company_metrics(years, 20, 2000, 100, consensus=CONSENSUS)
    assert m["consensus"] == CONSENSUS
    text = vm.render_report("X", "2026-10-23", m)
    assert text.index("## Record and inflection") < text.index("## Consensus (Yahoo estimates)")
    for line in ("- Consensus revenue growth next year 6.2%", "- Consensus revenue growth following year 5.1%",
                 "- Consensus EPS growth next year 8.4%", "- Consensus analysts 12",
                 "- Consensus price target median 60.00"):
        assert line in text
    implied, earned = m["valuation"]["implied_growth"], m["record"]["earned_growth"]
    assert (f"Consensus sees 6.2% revenue growth next year against {implied:.1%} implied by the price "
            f"and {earned:.1%} earned growth.") in text


@pytest.mark.unit
def test_render_report_consensus_partial_and_empty():
    years = [_year(fiscal_year_end=f"{2025 - i}-12-31") for i in range(2)]
    partial = vm.render_report("X", "d", vm.company_metrics(
        years, None, None, 100, consensus={"revenue_growth_next": 0.05, "analysts": 3}))
    assert "- Consensus revenue growth next year 5.0%" in partial
    assert "- Consensus EPS growth next year n/a" in partial and "- Consensus price target median n/a" in partial
    assert "Consensus sees 5.0% revenue growth next year." in partial      # no implied or earned growth
    empty = vm.render_report("X", "d", vm.company_metrics(years, 20, 2000, 100, consensus={}))
    assert "- Consensus revenue growth next year n/a" in empty and "- Consensus analysts n/a" in empty
    assert "Consensus sees" not in empty
    assert "## Consensus" not in vm.render_report("X", "d", vm.company_metrics(years, 20, 2000, 100))


def _estimates(growth_0y=0.062, growth_1y=0.051, analysts=12):
    idx = ["0q", "+1q", "0y", "+1y"]
    return pd.DataFrame({"avg": [1.0] * 4, "low": [0.9] * 4, "high": [1.1] * 4,
                         "numberOfAnalysts": [10, 10, analysts, 9], "growth": [0.01, 0.02, growth_0y, growth_1y]},
                        index=idx)


@pytest.mark.unit
def test_consensus_reads_yahoo_estimates_and_targets(monkeypatch):
    from tradingagents.dataflows.vendors.yahoo import consensus as cons

    class Full:
        revenue_estimate = _estimates()
        earnings_estimate = _estimates(growth_0y=0.084, growth_1y=0.07, analysts=14)
        analyst_price_targets = {"current": 50.0, "high": 75.0, "low": 48.0, "mean": 61.5, "median": 60.0}

    monkeypatch.setattr(cons.yf, "Ticker", lambda *a: Full())
    assert cons.consensus("HRB") == pytest.approx(CONSENSUS)

    class Partial:
        revenue_estimate = pd.DataFrame()
        analyst_price_targets = {"median": 60.0, "mean": float("nan")}

        @property
        def earnings_estimate(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(cons.yf, "Ticker", lambda *a: Partial())
    got = cons.consensus("HRB")
    assert got["target_median"] == 60.0 and got["target_mean"] is None
    assert got["revenue_growth_next"] is None and got["eps_growth_next"] is None and got["analysts"] is None

    class Empty:
        revenue_estimate = earnings_estimate = None
        analyst_price_targets = {}

    monkeypatch.setattr(cons.yf, "Ticker", lambda *a: Empty())
    assert cons.consensus("HRB") == {}
    monkeypatch.setattr(cons.yf, "Ticker", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert cons.consensus("HRB") == {}


@pytest.mark.unit
def test_vendor_puts_consensus_on_the_worksheet_with_targets_in_pence(monkeypatch):
    fake = _gbp_ticker()
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: fake())
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    monkeypatch.setattr(yahoo_vm, "usd_rate", lambda c: None)
    monkeypatch.setattr(yahoo_vm, "consensus", lambda s: {**CONSENSUS, "target_median": 950.0})
    report = yahoo_vm.get_value_metrics("DNLM.L", "2026-10-23")
    assert "- Consensus price target median 950.00" in report       # pence in, pence out
    assert "- Consensus revenue growth next year 6.2%" in report


@pytest.mark.unit
def test_vendor_leaves_out_consensus_targets_when_currencies_differ(monkeypatch):
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
    monkeypatch.setattr(yahoo_vm, "consensus", lambda s: dict(CONSENSUS))
    report = yahoo_vm.get_value_metrics("TSM", "2026-10-11")
    assert "- Consensus price target median n/a" in report
    assert "- Consensus revenue growth next year 6.2%" in report
