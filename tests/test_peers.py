import json

import pandas as pd
import pytest

from tradingagents.dataflows import value_metrics as vm
from tradingagents.dataflows.vendors.yahoo import common, peers, value_metrics as yahoo_vm

COLS = [pd.Timestamp(f"{y}-12-31") for y in (2025, 2024, 2023, 2022, 2021)]


def _statements(revenue_new=1000.0, revenue_old=500.0, shares_new=90.0, shares_old=100.0):
    revs = [revenue_new, 900.0, 800.0, 700.0, revenue_old]
    shs = [shares_new, 92.0, 95.0, 98.0, shares_old]

    def frame(rows):
        return pd.DataFrame({c: {k: v[i] for k, v in rows.items()} for i, c in enumerate(COLS)})

    income = frame({"Total Revenue": revs, "Gross Profit": [r * 0.5 for r in revs],
                    "EBIT": [r * 0.2 for r in revs], "Net Income": [r * 0.1 for r in revs]})
    balance = frame({"Total Assets": [2000.0] * 5, "Current Assets": [600.0] * 5,
                     "Current Liabilities": [300.0] * 5, "Net PPE": [400.0] * 5,
                     "Cash And Cash Equivalents": [100.0] * 5, "Total Debt": [300.0] * 5,
                     "Stockholders Equity": [900.0] * 5, "Ordinary Shares Number": shs})
    cash = frame({"Free Cash Flow": [r * 0.1 for r in revs], "Operating Cash Flow": [r * 0.15 for r in revs]})
    return income, balance, cash


class FakeTicker:
    infos: dict = {}
    options: dict = {}
    seen: list = []

    def __init__(self, symbol):
        FakeTicker.seen.append(symbol)
        self.symbol = symbol
        if not symbol.endswith("=X"):
            self.info = FakeTicker.infos[symbol]
            self.income_stmt, self.balance_sheet, self.cashflow = _statements(**FakeTicker.options.get(symbol, {}))

    def history(self, period="5d"):
        rate = {"GBPUSD=X": 1.34}[self.symbol]
        return pd.DataFrame({"Close": [rate]})


class FakeIndustry:
    top_companies = None

    def __init__(self, key):
        self.key = key


def _info(name, cap, price, currency="USD", **extra):
    return {"shortName": name, "marketCap": cap, "currentPrice": price, "currency": currency,
            "financialCurrency": currency if currency != "GBp" else "GBP", "industryKey": "personal-services",
            **extra}


@pytest.fixture
def world(monkeypatch, tmp_path):
    FakeTicker.infos = {
        "HRB": _info("H&R Block", 5e9, 50.0),
        "ROL": _info("Rollins", 2.5e10, 50.0),
        "SCI": _info("Service Corp", 1.1e10, 70.0),
        "RTO.L": _info("Rentokil", 39.8e9, 1789.5, currency="GBp"),
    }
    FakeTicker.options = {}
    FakeTicker.seen = []
    FakeIndustry.top_companies = pd.DataFrame(
        {"name": ["Rollins", "Service Corp", "H&R Block", "Rentokil"], "rating": ["Buy"] * 4,
         "market weight": [0.4, 0.3, 0.2, 0.1]}, index=["ROL", "SCI", "HRB", "RTO.L"])
    for mod in (peers, common):
        monkeypatch.setattr(mod.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(peers.yf, "Industry", FakeIndustry)
    monkeypatch.setattr(common, "_RATES", {})
    monkeypatch.setattr(peers, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    return tmp_path


@pytest.mark.unit
def test_peer_table_lists_subject_first_then_peers_then_median(world):
    out = peers.peer_comparison("HRB", as_of="2026-10-23")
    header = "| Ticker | Name | Mkt cap (USD) | EV/EBIT | Gross margin | EBIT margin | ROIC | FCF yield |"
    assert header + " Revenue CAGR 5y | Share count 5y |" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Ticker" not in ln]
    tickers = [r.split("|")[1].strip() for r in rows]
    assert tickers == ["HRB", "ROL", "SCI", "RTO.L", "Median (peers)"]
    subject = rows[0]
    assert "$5.0B" in subject and "50%" in subject and "20%" in subject
    assert "19%" in subject  # revenue 500 -> 1000 over four intervals
    assert "-10%" in subject  # share count 100 -> 90
    assert "×" in subject.split("|")[4]


@pytest.mark.unit
def test_peer_row_divides_pence_quotes_by_100(world):
    out = peers.peer_comparison("HRB", as_of="2026-10-23")
    rental = next(ln for ln in out.splitlines() if ln.startswith("| RTO.L"))
    assert "$53.3B" in rental  # 39.8e9 GBP x 1.34 (marketCap is already in pounds)
    assert "GBPUSD=X" in FakeTicker.seen and "GBpUSD=X" not in FakeTicker.seen


@pytest.mark.unit
def test_peer_table_says_why_when_no_industry_is_known(world):
    FakeTicker.infos["HRB"] = {k: v for k, v in FakeTicker.infos["HRB"].items() if k != "industryKey"}
    out = peers.peer_comparison("HRB", as_of="2026-10-23")
    assert "no industry" in out.lower() and "|" not in out


@pytest.mark.unit
def test_peer_table_notes_skipped_peers(world):
    del FakeTicker.infos["SCI"]
    out = peers.peer_comparison("HRB", as_of="2026-10-23")
    assert "Peers skipped (no data): SCI" in out


@pytest.mark.unit
def test_peer_table_is_cached_for_the_day(world, monkeypatch):
    first = peers.peer_comparison("HRB", as_of="2026-10-23")
    cached = json.loads((world / "peers" / "HRB_2026-10-23.json").read_text())
    assert cached["report"] == first
    monkeypatch.setattr(peers.yf, "Ticker", lambda *a: pytest.fail("no request for a cached table"))
    monkeypatch.setattr(peers.yf, "Industry", lambda *a: pytest.fail("no request for a cached table"))
    assert peers.peer_comparison("HRB", as_of="2026-10-23") == first


@pytest.mark.unit
def test_usd_rate_handles_usd_minor_units_and_failure(monkeypatch):
    monkeypatch.setattr(common, "_RATES", {})
    monkeypatch.setattr(common.yf, "Ticker", FakeTicker)
    FakeTicker.seen = []
    assert common.usd_rate("USD") == 1.0
    assert common.usd_rate("GBp") == pytest.approx(1.34)
    assert common.usd_rate("GBX") == pytest.approx(1.34)
    assert FakeTicker.seen == ["GBPUSD=X"]  # cached after the first fetch
    assert common.usd_rate("XYZ") is None
    assert common.usd_rate(None) is None


@pytest.mark.unit
def test_tool_returns_not_available_for_a_past_date(monkeypatch):
    from tradingagents.agents.tools import get_peer_comparison
    monkeypatch.setattr(peers, "is_historical", lambda d: True)
    monkeypatch.setattr(peers.yf, "Ticker", lambda *a: pytest.fail("no request for a past date"))
    out = get_peer_comparison.invoke({"ticker": "HRB", "trade_date": "2020-01-02"})
    assert "Not available for 2020-01-02" in out


@pytest.mark.unit
def test_base_rate_cohort_uses_revenue_in_usd():
    years = [{"fiscal_year_end": "2025-03-31", "revenue": 1.5e12, "ebit": 1e11}]
    m = vm.company_metrics(years, None, None, None, revenue_usd=1.5e12 * 0.0064)
    assert m["revenue_usd"] == pytest.approx(9.6e9)
    assert vm.growth_base_rates(m["revenue_usd"])["cohort"] == "$5–25B"
    assert vm.growth_base_rates(1.5e12)["cohort"] == ">$25B"


@pytest.mark.unit
def test_value_report_converts_reporting_currency_revenue_for_the_base_rate(monkeypatch):
    seen = {}
    monkeypatch.setattr(yahoo_vm, "usd_rate", lambda cur: 0.0064 if cur == "JPY" else None)
    real = yahoo_vm.company_metrics
    monkeypatch.setattr(yahoo_vm, "company_metrics", lambda *a, **k: seen.update(k) or real(*a, **k))
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})

    class T(FakeTicker):
        def __init__(self, s):
            self.info = {"currency": "JPY", "financialCurrency": "JPY", "currentPrice": 100.0, "marketCap": 1e12}
            self.income_stmt, self.balance_sheet, self.cashflow = _statements(revenue_new=1.5e12)

        def history(self, period="5d", **k):
            return pd.DataFrame()

    monkeypatch.setattr(yahoo_vm.yf, "Ticker", T)
    yahoo_vm.get_value_metrics("7203.T", "2026-10-23")
    assert seen["revenue_usd"] == pytest.approx(1.5e12 * 0.0064)
