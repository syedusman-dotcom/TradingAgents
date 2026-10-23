import json

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import common, peers

COLS = [pd.Timestamp(f"{y}-12-31") for y in (2025, 2024, 2023, 2022, 2021)]


def _statements(revenue_new=1e9, revenue_old=5e8, shares_new=90.0, shares_old=100.0):
    # EBIT is 20% of revenue and net debt is 20% of the newest revenue: EBIT 2e8, net debt 2e8.
    revs = [revenue_new, 9e8, 8e8, 7e8, revenue_old]
    shs = [shares_new, 92.0, 95.0, 98.0, shares_old]

    def frame(rows):
        return pd.DataFrame({c: {k: v[i] for k, v in rows.items()} for i, c in enumerate(COLS)})

    income = frame({"Total Revenue": revs, "Gross Profit": [r * 0.5 for r in revs],
                    "EBIT": [r * 0.2 for r in revs], "Net Income": [r * 0.1 for r in revs]})
    balance = frame({"Total Assets": [2e9] * 5, "Current Assets": [6e8] * 5,
                     "Current Liabilities": [3e8] * 5, "Net PPE": [4e8] * 5,
                     "Cash And Cash Equivalents": [1e8] * 5, "Total Debt": [3e8] * 5,
                     "Stockholders Equity": [9e8] * 5, "Ordinary Shares Number": shs})
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
        "HRB": _info("H&R Block", 4.8e9, 50.0),
        "ROL": _info("Rollins", 2.5e10, 50.0),
        "SCI": _info("Service Corp", 1.1e10, 70.0),
        "RTO.L": _info("Rentokil", 39.8e9, 1789.5, currency="GBp"),
        # Quoted in pence, reports in dollars, market cap in pounds.
        "DGE.L": _info("Diageo", 40e9, 1700.0, currency="GBp", financialCurrency="USD"),
    }
    FakeTicker.options = {}
    FakeTicker.seen = []
    FakeIndustry.top_companies = pd.DataFrame(
        {"name": ["Diageo", "Service Corp", "Rentokil", "H&R Block", "Rollins"], "rating": ["Buy"] * 5,
         "market weight": [0.05, 0.3, 0.1, 0.2, 0.4]},  # not in weight order: the code sorts
        index=["DGE.L", "SCI", "RTO.L", "HRB", "ROL"])
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
    assert tickers == ["HRB", "ROL", "SCI", "RTO.L", "DGE.L", "Median (peers)"]
    subject = rows[0]
    assert "$4.8B" in subject and "50%" in subject and "20%" in subject
    assert "25.0×" in subject  # (4.8e9 + 2e8 net debt) / 2e8 EBIT
    assert "2%" in subject  # FCF 1e8 / 4.8e9
    rollins = next(r for r in rows if r.startswith("| ROL"))
    assert "$25.0B" in rollins and "126.0×" in rollins  # (2.5e10 + 2e8) / 2e8
    assert "19%" in subject  # revenue 500 -> 1000 over four intervals
    assert "-10%" in subject  # share count 100 -> 90
    assert "×" in subject.split("|")[4]


@pytest.mark.unit
def test_peer_row_divides_pence_quotes_by_100(world):
    out = peers.peer_comparison("HRB", as_of="2026-10-23")
    rental = next(ln for ln in out.splitlines() if ln.startswith("| RTO.L"))
    assert "$53.3B" in rental  # 39.8e9 GBP x 1.34 (marketCap is already in pounds)
    assert "GBPUSD=X" in FakeTicker.seen and "GBpUSD=X" not in FakeTicker.seen
    assert "200.0×" in rental  # (53.332e9 + 2e8 x 1.34) / (2e8 x 1.34): statements in pounds


@pytest.mark.unit
def test_peer_with_pence_quote_and_dollar_statements_converts_only_the_market_cap(world):
    out = peers.peer_comparison("HRB", as_of="2026-10-23")
    diageo = next(ln for ln in out.splitlines() if ln.startswith("| DGE.L"))
    assert "$53.6B" in diageo  # 40e9 GBP x 1.34
    assert "269.0×" in diageo  # (53.6e9 + 2e8 net debt in dollars, unconverted) / 2e8 EBIT


@pytest.mark.unit
def test_subject_with_no_data_returns_text_through_the_tool(world, monkeypatch):
    from tradingagents.agents.tools import get_peer_comparison
    FakeTicker.infos["HRB"] = {}
    monkeypatch.setattr(common, "vendor_reachable", lambda host: True)
    monkeypatch.setattr(peers, "is_historical", lambda d: False)
    out = get_peer_comparison.invoke({"ticker": "HRB", "trade_date": ""})
    assert "NO_DATA_AVAILABLE" in out and "HRB" in out


@pytest.mark.unit
def test_peers_are_taken_in_market_weight_order(world):
    out = peers.peer_comparison("HRB", limit=2, as_of="2026-10-23")
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Ticker" not in ln]
    assert [r.split("|")[1].strip() for r in rows] == ["HRB", "ROL", "SCI", "Median (peers)"]


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
def test_usd_rate_does_not_cache_a_failure(monkeypatch):
    monkeypatch.setattr(common, "_RATES", {})
    calls = []

    class Flaky:
        def __init__(self, symbol):
            calls.append(symbol)

        def history(self, period="5d"):
            if len(calls) == 1:
                raise RuntimeError("blip")
            return pd.DataFrame({"Close": [1.34]})

    monkeypatch.setattr(common.yf, "Ticker", Flaky)
    assert common.usd_rate("GBP") is None
    assert common.usd_rate("GBP") == pytest.approx(1.34)


@pytest.mark.unit
def test_tool_returns_not_available_for_a_past_date(monkeypatch):
    from tradingagents.agents.tools import get_peer_comparison
    monkeypatch.setattr(peers, "is_historical", lambda d: True)
    monkeypatch.setattr(peers.yf, "Ticker", lambda *a: pytest.fail("no request for a past date"))
    out = get_peer_comparison.invoke({"ticker": "HRB", "trade_date": "2020-01-02"})
    assert "Not available for 2020-01-02" in out
