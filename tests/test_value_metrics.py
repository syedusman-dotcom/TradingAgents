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
