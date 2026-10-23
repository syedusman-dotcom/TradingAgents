"""Facts handed to the agents: earnings date, news for foreign listings, SEC-only answers."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

import tradingagents.dataflows.vendors.yahoo.news as ynews
import tradingagents.dataflows.vendors.yahoo.snapshot as snapshot
from tradingagents.agents.context import build_instrument_context
from tradingagents.dataflows.vendors import sec_edgar, sec_filings
from value_pipeline import render
from value_pipeline.actions import Decision, plan
from value_pipeline.config import DEFAULTS


def _ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2026-04-01", "2026-05-20")
    closes = [100 + i for i in range(len(dates))]
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [1_000_000] * len(dates)})


@pytest.mark.unit
@pytest.mark.parametrize("found,line", [
    ("2026-05-26", "- Next earnings: 2026-05-26 (13 days)"),
    ("2026-05-13", "- Next earnings: 2026-05-13 (0 days)"),
    ("2026-05-01", "- Next earnings: unknown"),
    (None, "- Next earnings: unknown"),
])
def test_snapshot_shows_days_to_next_earnings(monkeypatch, found, line):
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda s, d, fill_gaps=True: _ohlcv())
    monkeypatch.setattr(snapshot, "next_earnings_date", lambda symbol: found)
    assert line in snapshot.build_verified_market_snapshot("COF", "2026-05-13")


@pytest.mark.unit
def test_instrument_context_names_the_earnings_date():
    text = build_instrument_context("AAPL", trade_date="2026-10-23", earnings_date="2026-11-05")
    assert "Next earnings report: 2026-11-05 (13 days away)." in text
    past = build_instrument_context("AAPL", trade_date="2026-10-23", earnings_date="2026-10-01")
    assert "Next earnings" not in past
    assert "Next earnings" not in build_instrument_context("AAPL", trade_date="2026-10-23")


@pytest.mark.unit
def test_news_falls_back_to_company_name_search(monkeypatch):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    queries = []

    class FakeTicker:
        def __init__(self, *a, **k):
            pass

        def get_news(self, count=20):
            return []

    class FakeSearch:
        def __init__(self, query, **k):
            queries.append(query)
            by_name = query == "Kia Corporation"
            self.quotes = [{"symbol": "000270.KS"}]
            self.news = [{"title": "Kia lifts guidance", "publisher": "P", "link": "l",
                          "providerPublishTime": int(datetime.now(UTC).timestamp()),
                          "relatedTickers": []}] if by_name else []

    monkeypatch.setattr(ynews.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    monkeypatch.setattr(ynews, "get_company_profile", lambda t: {"longName": "Kia Corporation"})
    out = ynews.get_news_yfinance("000270.KS", today, today)
    assert queries == ["000270.KS", "Kia Corporation"]
    assert 'Matched by company name "Kia Corporation" (Yahoo tags no articles with this listing): ' \
           "verify each item is about the company." in out
    assert "Kia lifts guidance" in out


@pytest.mark.unit
def test_news_without_a_name_match_stays_unavailable(monkeypatch):
    from tradingagents.dataflows.errors import VendorUnavailableError

    class FakeTicker:
        def __init__(self, *a, **k):
            pass

        def get_news(self, count=20):
            return []

    class FakeSearch:
        def __init__(self, query, **k):
            self.quotes = [{"symbol": "000270.KS"}]
            self.news = []

    monkeypatch.setattr(ynews.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    monkeypatch.setattr(ynews, "get_company_profile", lambda t: {"longName": "Kia Corporation"})
    with pytest.raises(VendorUnavailableError):
        ynews.get_news_yfinance("000270.KS", "2026-10-01", "2026-10-23")


@pytest.mark.unit
def test_sec_tools_say_not_a_us_filer_for_a_foreign_ticker(monkeypatch):
    monkeypatch.setattr(sec_edgar, "cik_for", lambda ticker: None)
    assert sec_filings.get_financial_history("000270.KS", "2026-10-23") == sec_filings.NOT_A_US_FILER
    assert sec_filings.get_annual_report("000270.KS", "2026-10-23") == sec_filings.NOT_A_US_FILER
    assert sec_filings.NOT_A_US_FILER == (
        "Not a US SEC filer: use get_fundamentals, get_income_statement, get_balance_sheet and "
        "get_cashflow (Yahoo) and the annual report on the company's investor site.")


@pytest.mark.unit
def test_actions_table_shows_the_company_name():
    held = Decision("HLD", "Hold", 10.0, held_shares=5.0)
    new = Decision("000270.KS", "Buy", 100.0, margin_of_safety=0.4)
    skip = Decision("SKP", "Hold", 10.0)
    actions, summary = plan([held, new, skip], cash=50_000.0, sizing=DEFAULTS["sizing"])
    md = render.actions_markdown("2026-11-07", actions, summary, reports={}, comparison=None, cost=0.0,
                                 failures=[], dry_run=False,
                                 names={"000270.KS": "Kia Corporation", "HLD": "Holder Inc", "SKP": "Skipper"})
    assert "| Kia Corporation (000270.KS) |" in md
    assert "| Holder Inc (HLD) |" in md
    assert "- Skipper (SKP):" in md


@pytest.mark.unit
def test_run_agents_records_the_company_name(monkeypatch, tmp_path):
    from value_pipeline import review

    class FakeGraph:
        def propagate(self, ticker, day, portfolio=None):
            return {"final_trade_decision": "**Rating**: Hold"}, "Hold"

        def save_reports(self, *a, **k):
            pass

    from tradingagents.agents import context

    monkeypatch.setattr(context, "resolve_instrument_identity",
                        lambda t: {"company_name": "Kia Corporation"})
    assert review.run_agents(FakeGraph(), "000270.KS", "2026-10-23", None, tmp_path).name == "Kia Corporation"

    def boom(t):
        raise RuntimeError("down")

    monkeypatch.setattr(context, "resolve_instrument_identity", boom)
    assert review.run_agents(FakeGraph(), "000270.KS", "2026-10-23", None, tmp_path).name is None
    assert review.fake_run("X", 0, False, 10.0).name is None
