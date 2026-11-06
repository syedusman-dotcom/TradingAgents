"""Universe from Nordnet's markets: table finding, ticker codes, cross-listings (no network)."""

from __future__ import annotations

import json
import logging
from datetime import date

import pandas as pd
import pytest
import requests

from value_pipeline import config, market_data
from value_pipeline.config import Paths


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


def members_table(rows, head=("Ticker", "Company")) -> str:
    body = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows)
    return f"<table><tr><th>{head[0]}</th><th>{head[1]}</th></tr>{body}</table>"


TOC_TABLE = ("<table><tr><th>Foundation</th><th>Operator</th></tr>"
             "<tr><td>1984</td><td>LSE</td></tr></table>")


def many(n, prefix="T"):
    return [(f"{prefix}{i}", f"{prefix} Company {i}") for i in range(n)]


def serve(monkeypatch, pages: dict[str, str]):
    """Answer requests.get by the market's page URL (a substring of the URL)."""
    def get(url, **kwargs):
        for key, html in pages.items():
            if key in url:
                return FakeResponse(html)
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(market_data.requests, "get", get)


@pytest.mark.unit
def test_universe_finds_the_constituents_table_by_column_name(tmp_path, monkeypatch):
    rows = [("AZN", "AstraZeneca"), ("BT.A", "BT Group")] + many(10, "X")
    html = TOC_TABLE + members_table(rows)
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: FakeResponse(html))
    df = market_data.universe(Paths(tmp_path), ["ftse100"])
    assert list(df["ticker"][:2]) == ["AZN.L", "BT-A.L"]
    assert set(df["market"]) == {"ftse100"} and set(df["country"]) == {"UK"}
    assert (df["index"] == df["market"]).all()
    assert df["sector"].isna().all()


@pytest.mark.unit
def test_universe_normalises_ticker_codes():
    uk = market_data.MARKETS["ftse100"]
    dk = market_data.MARKETS["omxc25"]
    us = market_data.MARKETS["sp500"]
    assert market_data.normalise_code("LSE: AZN", uk) == "AZN.L"
    assert market_data.normalise_code("AZN.L", uk) == "AZN.L"
    assert market_data.normalise_code("RR.", uk) == "RR.L"
    assert market_data.normalise_code("BT.A", uk) == "BT-A.L"
    assert market_data.normalise_code("NOVO B", dk) == "NOVO-B.CO"
    assert market_data.normalise_code("BRK.B", us) == "BRK-B"
    assert market_data.normalise_code("Euronext Dublin:\xa0A5G", market_data.MARKETS["iseq20"]) \
        == "A5G.IR"


@pytest.mark.unit
def test_name_key_drops_legal_suffixes_and_punctuation():
    assert market_data.name_key("Unilever PLC") == market_data.name_key("Unilever N.V.")
    assert market_data.name_key("Banca Intesa S.p.A.") == "banca intesa"
    assert market_data.name_key("Carlsberg A/S") == "carlsberg"


@pytest.mark.unit
def test_universe_keeps_gics_sector_for_the_sp_markets(tmp_path, monkeypatch):
    rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td>Industrials</td></tr>"
                   for a, b in [("BRK.B", "Berkshire")] + many(10))
    html = ("<table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>"
            f"{rows}</table>")
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: FakeResponse(html))
    df = market_data.universe(Paths(tmp_path), ["sp500"])
    assert df["ticker"].iloc[0] == "BRK-B"
    assert set(df["sector"]) == {"Industrials"}
    assert set(df["country"]) == {"US"}


@pytest.mark.unit
def test_universe_drops_cross_listings_keeping_the_first_market(tmp_path, monkeypatch, caplog):
    uk = members_table([("ULVR", "Unilever PLC")] + many(10, "U"))
    nl = members_table([("UNA", "Unilever N.V.")] + many(10, "N"))
    serve(monkeypatch, {"FTSE_100": uk, "AEX": nl})
    with caplog.at_level(logging.INFO, logger=market_data.log.name):
        df = market_data.universe(Paths(tmp_path), ["ftse100", "aex"])
    unilever = df[df["name"].str.contains("Unilever")]
    assert list(unilever["market"]) == ["ftse100"]
    assert "ULVR.L" in caplog.text and "UNA.AS" in caplog.text


@pytest.mark.unit
def test_universe_skips_a_market_under_ten_tickers_with_a_warning(tmp_path, monkeypatch, caplog):
    serve(monkeypatch, {"FTSE_100": members_table(many(10, "U")),
                        "AEX": members_table(many(3, "N"))})
    with caplog.at_level(logging.WARNING, logger=market_data.log.name):
        df = market_data.universe(Paths(tmp_path), ["ftse100", "aex"])
    assert set(df["market"]) == {"ftse100"}
    assert "aex" in caplog.text


@pytest.mark.unit
def test_universe_rejects_an_unknown_index_key(tmp_path, monkeypatch):
    def boom(url, **k):
        raise AssertionError("fetched before validating")
    monkeypatch.setattr(market_data.requests, "get", boom)
    with pytest.raises(ValueError, match="unknown universe.indexes entry: nikkei"):
        market_data.universe(Paths(tmp_path), ["ftse100", "nikkei"])


@pytest.mark.unit
def test_universe_csv_carries_market_and_country(tmp_path, monkeypatch):
    html = members_table(many(12))
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: FakeResponse(html))
    paths = Paths(tmp_path)
    market_data.universe(paths, ["dax"])
    saved = pd.read_csv(paths.universe)
    assert {"market", "country", "index"} <= set(saved.columns)
    assert set(saved["country"]) == {"Germany"}


@pytest.mark.unit
def test_universe_falls_back_to_the_cache(tmp_path, monkeypatch):
    html = members_table(many(12))
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: FakeResponse(html))
    paths = Paths(tmp_path)
    first = market_data.universe(paths, ["dax"])
    def down(url, **k):
        raise OSError("offline")
    monkeypatch.setattr(market_data.requests, "get", down)
    again = market_data.universe(paths, ["dax"])
    assert list(again["ticker"]) == list(first["ticker"])


@pytest.mark.unit
def test_markets_table_matches_the_spec():
    assert {k: m.suffix for k, m in market_data.MARKETS.items()} == {
        "sp500": "", "sp400": "", "ftse100": ".L", "ftse250": ".L", "dax": ".DE",
        "mdax": ".DE", "cac40": ".PA", "cacnext20": ".PA", "aex": ".AS", "amx": ".AS",
        "bel20": ".BR", "psi": ".LS", "iseq20": ".IR", "ftsemib": ".MI", "smi": ".SW",
        "smim": ".SW", "atx": ".VI", "ibex35": ".MC", "omxs30": ".ST", "omxc25": ".CO",
        "omxh25": ".HE", "obx": ".OL", "tsxcomp": ".TO", "sto": ".ST", "cph": ".CO",
        "hel": ".HE", "ose": ".OL", "sp600": "",
    }
    assert "nikkei225" not in market_data.MARKETS and "kospi" not in market_data.MARKETS
    gics = {k for k, m in market_data.MARKETS.items() if m.gics_column}
    assert gics == {"sp500", "sp400", "sp600"}


@pytest.mark.unit
def test_default_indexes_are_every_nordnet_market():
    assert set(config.DEFAULTS["universe"]["indexes"]) == set(market_data.MARKETS)


@pytest.mark.unit
def test_default_indexes_end_with_the_five_small_company_markets():
    assert config.DEFAULTS["universe"]["indexes"][-5:] == ["sto", "cph", "hel", "ose", "sp600"]
    assert list(market_data.MARKETS)[-5:] == ["sto", "cph", "hel", "ose", "sp600"]
    assert [market_data.MARKETS[k].api for k in ("sto", "cph", "hel")] == ["STO", "CPH", "HEL"]
    assert market_data.MARKETS["omxs30"].api is None


class FakeApiResponse:
    def __init__(self, payload=None, status=200, text=None):
        self.payload, self.status, self.text = payload, status, text

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        if self.text is not None:
            return json.loads(self.text)
        return self.payload


def api_payload(rows):
    return {"data": {"instrumentListing": {"rows": rows}}}


def api_row(symbol, name=None, turnover="1,000", sector="Industrials"):
    return {"symbol": symbol, "fullName": name or symbol, "sector": sector, "turnover": turnover}


def sto_rows():
    return [
        api_row("ERIC B", "Ericsson B", "5,000,000", "Telecommunications"),
        api_row("ERIC A", "Ericsson A", "40,000", "Telecommunications"),
        api_row("AAK", "AAK", "17,633,939", "Consumer Staples"),
    ] + [api_row(f"S{i}", f"S Company {i}") for i in range(10)]


@pytest.mark.unit
def test_api_market_gives_tickers_and_keeps_the_busier_share_class(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeApiResponse(api_payload(sto_rows()))
    monkeypatch.setattr(market_data.requests, "get", get)
    df = market_data.fetch_market(market_data.MARKETS["sto"])
    assert "ERIC-B.ST" in set(df["ticker"]) and "AAK.ST" in set(df["ticker"])
    assert "ERIC-A.ST" not in set(df["ticker"])
    assert set(df["market"]) == {"sto"} and set(df["country"]) == {"Sweden"}
    eric = df[df["ticker"] == "ERIC-B.ST"].iloc[0]
    assert eric["name"] == "Ericsson B" and eric["sector"] == "Telecommunications"
    url, kwargs = calls[0]
    assert url == market_data.NASDAQ_NORDIC.format(code="STO")
    assert kwargs["headers"] == market_data.NASDAQ_HEADERS


@pytest.mark.unit
def test_blank_or_unparsable_turnover_counts_as_zero(monkeypatch):
    rows = [api_row("ACRI A", turnover=""), api_row("ACRI B", turnover="12"),
            api_row("SAND A", turnover="1,000"), api_row("SAND B", turnover="n/a"),
            api_row("OTHER A", turnover=None), api_row("OTHER B", turnover="")]
    rows += [api_row(f"S{i}") for i in range(10)]
    monkeypatch.setattr(market_data.requests, "get",
                        lambda url, **k: FakeApiResponse(api_payload(rows)))
    tickers = set(market_data.fetch_market(market_data.MARKETS["sto"])["ticker"])
    assert "ACRI-B.ST" in tickers and "ACRI-A.ST" not in tickers
    assert "SAND-A.ST" in tickers and "SAND-B.ST" not in tickers
    assert len({t for t in tickers if t.startswith("OTHER")}) == 1


@pytest.mark.unit
@pytest.mark.parametrize("response", [
    FakeApiResponse(text="<html>blocked</html>"),
    FakeApiResponse({"data": {"instrumentListing": {}}}),
    FakeApiResponse({"data": None}),
    FakeApiResponse({}, status=400),
])
def test_a_bad_api_response_raises(monkeypatch, response):
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: response)
    with pytest.raises(Exception):  # noqa: B017 - any failure must reach universe's cache fallback
        market_data.fetch_market(market_data.MARKETS["hel"])


@pytest.mark.unit
def test_share_class_key():
    assert market_data.share_class_key("ACRI B") == "ACRI"
    assert market_data.share_class_key("ALVO SDB") == "ALVO"
    assert market_data.share_class_key("NOKIA") == "NOKIA"
    assert market_data.share_class_key("NOVO B") == "NOVO"
    assert market_data.share_class_key("ABC Lower") == "ABC Lower"
    assert market_data.share_class_key("ABC TOOLONG") == "ABC TOOLONG"


@pytest.mark.unit
def test_oslo_table_strips_the_exchange_prefix(tmp_path, monkeypatch):
    html = members_table([("OSE: ABG", "ABG Sundal Collier")] + many(10, "O"),
                         head=("Ticker", "Company"))
    serve(monkeypatch, {"Oslo_Stock_Exchange": html})
    df = market_data.fetch_market(market_data.MARKETS["ose"])
    assert df["ticker"].iloc[0] == "ABG.OL"
    assert set(df["country"]) == {"Norway"}


@pytest.mark.unit
def test_sp600_keeps_its_gics_sector(monkeypatch):
    rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td>Health Care</td></tr>"
                   for a, b in many(11))
    html = ("<table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>"
            f"{rows}</table>")
    serve(monkeypatch, {"S%26P_600": html})
    df = market_data.fetch_market(market_data.MARKETS["sp600"])
    assert set(df["sector"]) == {"Health Care"} and set(df["country"]) == {"US"}


@pytest.mark.unit
def test_universe_keeps_the_index_row_when_an_api_market_lists_the_same_company(tmp_path, monkeypatch):
    index = members_table([("ERIC-B", "Ericsson B")] + many(10, "I"))
    rows = [api_row("ERIC B", "Ericsson B")] + [api_row(f"S{i}", f"S Company {i}") for i in range(10)]

    def get(url, **kwargs):
        if "api.nasdaq.com" in url:
            return FakeApiResponse(api_payload(rows))
        return FakeResponse(index)
    monkeypatch.setattr(market_data.requests, "get", get)
    df = market_data.universe(Paths(tmp_path), ["omxs30", "sto"])
    eric = df[df["name"] == "Ericsson B"]
    assert list(eric["market"]) == ["omxs30"]


@pytest.mark.unit
def test_under_ten_market_keeps_its_cached_rows(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    cached = pd.DataFrame({
        "ticker": [f"C{i}.AS" for i in range(12)], "name": [f"Cached {i}" for i in range(12)],
        "sector": None, "index": "aex", "market": "aex", "country": "Netherlands"})
    cached.to_csv(paths.universe, index=False)
    serve(monkeypatch, {"FTSE_100": members_table(many(10, "U")),
                        "AEX": members_table(many(3, "N"))})
    df = market_data.universe(paths, ["ftse100", "aex"])
    assert (df["market"] == "aex").sum() == 12 and (df["market"] == "ftse100").sum() == 10
    assert len(pd.read_csv(paths.universe)) == 22


@pytest.mark.unit
def test_one_failing_market_does_not_discard_the_others(tmp_path, monkeypatch):
    good = members_table(many(10, "U"))

    def get(url, **k):
        if "AEX" in url:
            raise OSError("down")
        return FakeResponse(good)
    monkeypatch.setattr(market_data.requests, "get", get)
    df = market_data.universe(Paths(tmp_path), ["ftse100", "aex"])
    assert set(df["market"]) == {"ftse100"} and len(df) == 10


@pytest.mark.unit
def test_old_cache_without_market_columns_is_filled(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    pd.DataFrame({"ticker": ["A"], "name": ["A Co"], "sector": ["X"], "index": ["sp500"]}) \
        .to_csv(paths.universe, index=False)
    monkeypatch.setattr(market_data.requests, "get",
                        lambda url, **k: (_ for _ in ()).throw(OSError("offline")))
    df = market_data.universe(paths, ["dax"])
    assert {"market", "country"} <= set(df.columns) and list(df["ticker"]) == ["A"]


@pytest.mark.unit
def test_a_short_table_is_skipped_for_the_next_one(tmp_path, monkeypatch):
    html = members_table(many(3, "Y"), head=("Year", "Company")) + members_table(many(12))
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: FakeResponse(html))
    assert len(market_data.fetch_market(market_data.MARKETS["dax"])) == 12


@pytest.mark.unit
def test_atx_is_static_and_needs_no_request(monkeypatch):
    monkeypatch.setattr(market_data.requests, "get",
                        lambda url, **k: (_ for _ in ()).throw(AssertionError("network")))
    df = market_data.fetch_market(market_data.MARKETS["atx"])
    assert len(df) == 20 and df["ticker"].str.endswith(".VI").all()


# --- dollars in, quote unit out ---------------------------------------------------

def _year(**kw):
    from tests.test_value_metrics import _year as base

    return base(fiscal_year_end="2025-12-31", **kw)


def record(**kw):
    base = {"ticker": "DNLM.L", "price": 800.0, "market_cap": 2.0e9, "enterprise_value": 2.5e9,
            "shares": 10.0, "currency": "GBp", "financial_currency": "GBP", "sector": "Consumer Cyclical",
            "years": [{**_year(), "revenue": 100.0 - i, "shares": 10.0,
                       "fiscal_year_end": f"{2025 - i}-12-31"} for i in range(4)],
            "quarters": [{**_year(), "revenue": 25.0}], "closes": []}
    return {**base, **kw}


@pytest.mark.unit
def test_in_usd_converts_a_pence_quote_with_pound_statements(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"GBp": 1.30, "GBP": 1.30}.get(c))
    data = record()
    out = market_data.in_usd(data)
    assert out["price"] == pytest.approx(10.4) and out["fx_quote"] == pytest.approx(0.013)
    assert out["fx_financial"] == pytest.approx(1.30) and out["quote_currency"] == "GBp"
    assert out["market_cap"] == pytest.approx(2.6e9) and out["enterprise_value"] == pytest.approx(3.25e9)
    assert out["years"][0]["revenue"] == pytest.approx(130.0) and out["years"][0]["shares"] == 10.0
    assert out["quarters"][0]["revenue"] == pytest.approx(32.5)
    assert out["years"][0]["fiscal_year_end"] == "2025-12-31"
    assert out["years"][1]["revenue"] == pytest.approx(99.0 * 1.30)
    assert data["price"] == 800.0 and data["years"][0]["revenue"] == 100.0      # the input is untouched
    assert data["quarters"][0]["revenue"] == 25.0 and "fx_quote" not in data


@pytest.mark.unit
def test_in_usd_converts_statements_at_the_financial_currency(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"USD": 1.0, "EUR": 1.10}.get(c))
    out = market_data.in_usd(record(currency="USD", financial_currency="EUR", price=50.0,
                                    market_cap=1e9, enterprise_value=None))
    assert out["price"] == 50.0 and out["market_cap"] == 1e9 and out["enterprise_value"] is None
    assert out["years"][0]["revenue"] == pytest.approx(110.0)
    assert out["fx_quote"] == 1.0 and out["fx_financial"] == pytest.approx(1.10)


@pytest.mark.unit
def test_in_usd_passes_a_dollar_record_through(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"USD": 1.0}.get(c))
    for kw in ({"currency": "USD", "financial_currency": "USD"}, {"currency": None, "financial_currency": None}):
        data = record(**kw)
        out = market_data.in_usd(data)
        assert out["fx_quote"] == out["fx_financial"] == 1.0
        assert out["price"] == 800.0 and out["years"] == data["years"] and out["quarters"] == data["quarters"]


@pytest.mark.unit
def test_in_usd_without_financial_currency_uses_the_quote_major_rate(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"GBp": 1.30}.get(c))
    out = market_data.in_usd(record(financial_currency=None))
    assert out["years"][0]["revenue"] == pytest.approx(130.0)
    assert out["fx_financial"] == pytest.approx(1.30) and out["fx_quote"] == pytest.approx(0.013)


@pytest.mark.unit
def test_in_usd_raises_without_a_rate(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: None)
    with pytest.raises(market_data.DataUnavailable, match="no USD rate for SEK"):
        market_data.in_usd(record(currency="SEK", financial_currency="SEK"))
    with pytest.raises(market_data.DataUnavailable, match="no USD rate for GBp"):
        market_data.in_usd(record(financial_currency=None))


@pytest.mark.unit
def test_to_quote_round_trips_the_per_share_columns():
    from value_pipeline import screener

    row = {"price": 10.4, "value_base": 13.0, "value_low": 9.1, "value_high": None, "market_cap": 2.6e9}
    out = screener.to_quote(row, {"fx_quote": 0.013, "quote_currency": "GBp"})
    assert out["price"] == pytest.approx(800.0) and out["value_base"] == pytest.approx(1000.0)
    assert out["value_low"] == pytest.approx(700.0) and out["value_high"] is None
    assert out["market_cap"] == 2.6e9 and out["quote_currency"] == "GBp" and out["fx_quote"] == 0.013


@pytest.mark.unit
def test_gics_sector_maps_yahoo_names():
    assert market_data.gics_sector("Financial Services") == "Financials"
    assert market_data.gics_sector("Technology") == "Information Technology"
    assert market_data.gics_sector("Consumer Cyclical") == "Consumer Discretionary"
    assert market_data.gics_sector("Consumer Defensive") == "Consumer Staples"
    assert market_data.gics_sector("Basic Materials") == "Materials"
    assert market_data.gics_sector("Healthcare") == "Health Care"
    assert market_data.gics_sector("Industrials") == "Industrials"
    assert market_data.gics_sector("Something Else") == "Something Else"
    assert market_data.gics_sector(None) is None


@pytest.mark.unit
def test_fundamentals_stores_the_financial_currency_under_schema_7(tmp_path, monkeypatch):
    class FakeTicker:
        info = {"marketCap": 2000, "currentPrice": 800, "currency": "GBp", "financialCurrency": "GBP",
                "sector": "Industrials"}
        lines = ["Total Revenue", "EBIT", "Net Income", "Total Assets", "Current Assets",
                 "Current Liabilities", "Net PPE"]
        income_stmt = pd.DataFrame({pd.Timestamp("2025-12-31"): range(7)}, index=lines)
        balance_sheet = cashflow = None
        quarterly_income_stmt = pd.DataFrame({pd.Timestamp("2026-03-31"): range(7)}, index=lines)
        quarterly_balance_sheet = quarterly_cashflow = None

    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: FakeTicker())
    monkeypatch.setattr(market_data, "closes", lambda t: [800.0])
    paths = Paths(tmp_path)
    data = market_data.fundamentals("DNLM.L", paths)
    assert market_data.CACHE_SCHEMA == 7 and data["schema"] == 7
    assert data["currency"] == "GBp" and data["financial_currency"] == "GBP"
    assert market_data.fundamentals("DNLM.L", paths)["financial_currency"] == "GBP"


def _screen_setup(tmp_path, monkeypatch, universe_rows, records, rates=None):
    import copy

    from value_pipeline import metrics, screener, superinvestors

    paths = Paths(tmp_path)
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["screen"]["throttle_seconds"] = 0
    uni = pd.DataFrame(universe_rows, columns=market_data.UNIVERSE_COLUMNS)
    fetched = []

    def fundamentals(t, p, days=7, price_days=None):
        fetched.append(t)
        return dict(records[t])

    monkeypatch.setattr(market_data, "universe", lambda p, idx: uni)
    monkeypatch.setattr(market_data, "fundamentals", fundamentals)
    monkeypatch.setattr(market_data, "usd_rate", lambda c: (rates or {"USD": 1.0}).get(c))
    monkeypatch.setattr(market_data, "throttle", lambda s: None)
    monkeypatch.setattr(metrics, "current_discount_rate", lambda: {"rate": 0.10, "treasury_10y": None})
    monkeypatch.setattr(superinvestors, "refresh_if_stale", lambda p, c: {"holdings": {}})
    monkeypatch.setattr(superinvestors, "load", lambda p: {"holdings": {}})
    return paths, cfg, screener, fetched


@pytest.mark.unit
def test_screen_run_ranks_in_dollars_and_writes_quote_units(tmp_path, monkeypatch):
    usd = record(ticker="AAA", currency="USD", financial_currency="USD", price=20.0, market_cap=2.0e9,
                 enterprise_value=None, sector="Industrials")
    gbp = record()
    paths, cfg, screener, _ = _screen_setup(
        tmp_path, monkeypatch,
        [("AAA", "Aaa Inc", "Industrials", "sp500", "sp500", "US"),
         ("DNLM.L", "Dunelm", None, "ftse100", "ftse100", "UK")],
        {"AAA": usd, "DNLM.L": gbp}, rates={"USD": 1.0, "GBp": 1.30, "GBP": 1.30})
    res = screener.run(paths, cfg, owned=set(), today="2026-11-05")
    rows = {r["ticker"]: r for r in pd.read_csv(res["path"]).to_dict("records")}
    row = rows["DNLM.L"]
    assert row["market_cap"] == pytest.approx(2.0e9 * 1.30) and row["price"] == pytest.approx(800.0)
    assert row["market"] == "ftse100" and row["country"] == "UK" and row["quote_currency"] == "GBp"
    assert row["fx_quote"] == pytest.approx(0.013)
    assert row["value_base"] / row["price"] == pytest.approx(1 / (1 - row["mos_formula"]))   # pence
    assert row["shares"] == pytest.approx(row["market_cap"] / (row["price"] * row["fx_quote"]))
    assert row["sector"] == "Consumer Discretionary"          # from Yahoo's "Consumer Cyclical"
    assert rows["AAA"]["quote_currency"] == "USD" and rows["AAA"]["price"] == 20.0
    assert rows["AAA"]["sector"] == "Industrials" and rows["AAA"]["market"] == "sp500"


@pytest.mark.unit
def test_screen_run_excludes_non_sp_tickers_by_yahoo_sector(tmp_path, monkeypatch):
    barc = record(ticker="BARC.L", sector="Financial Services")
    land = record(ticker="LAND.L", sector="Real Estate")
    ok = record(ticker="OK.L", sector="Industrials")
    paths, cfg, screener, fetched = _screen_setup(
        tmp_path, monkeypatch,
        [("BARC.L", "Barclays", None, "ftse100", "ftse100", "UK"),
         ("LAND.L", "Landsec", None, "ftse100", "ftse100", "UK"),
         ("OK.L", "Okay", None, "ftse100", "ftse100", "UK"),
         ("BANK", "Bank Inc", "Financials", "sp500", "sp500", "US")],
        {"BARC.L": barc, "LAND.L": land, "OK.L": ok}, rates={"GBp": 1.30, "GBP": 1.30})
    paths.cache.mkdir(parents=True)
    (paths.cache / "BARC.L.json").write_text('{"ticker": "BARC.L", "sector": "Financial Services"}')
    res = screener.run(paths, cfg, owned=set(), today="2026-11-05")
    assert "BARC.L" not in fetched and "BANK" not in fetched
    by = {e["ticker"]: e for e in res["excluded"]}
    assert set(by) == {"BARC.L", "LAND.L", "BANK"}
    assert by["LAND.L"]["sector"] == "Real Estate" and by["BARC.L"]["sector"] == "Financials"
    assert by["BANK"]["name"] == "Bank Inc"
    assert list(pd.read_csv(res["path"])["ticker"]) == ["OK.L"]
    assert res["screened"] == 1
    saved = pd.read_csv(paths.screens / "screen_2026-11-05_excluded.csv")
    assert set(saved["ticker"]) == {"BARC.L", "LAND.L", "BANK"}


@pytest.mark.unit
def test_offscreen_row_gives_quote_units_for_a_london_ticker(tmp_path, monkeypatch):
    from value_pipeline import metrics, review

    paths = Paths(tmp_path)
    monkeypatch.setattr(market_data, "fundamentals", lambda t, p, days=7, price_days=None: record())
    monkeypatch.setattr(market_data, "cached_sector", lambda t, p: None)
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"GBp": 1.30, "GBP": 1.30}.get(c))
    monkeypatch.setattr(metrics, "current_discount_rate", lambda: {"rate": 0.10, "treasury_10y": None})
    row = review.offscreen_row("DNLM.L", paths, config.DEFAULTS, {})
    assert row["price"] == pytest.approx(800.0) and row["market_cap"] == pytest.approx(2.6e9)
    assert row["country"] == "UK" and row["market"] == "ftse100" and row["quote_currency"] == "GBp"
    assert row["sector"] == "Consumer Discretionary"


# --- daily prices without refetching statements ---------------------------------------------

def _cached_record(paths, ticker="ABC", **over):
    from datetime import date, datetime, timedelta

    record = {"schema": market_data.CACHE_SCHEMA, "fetched_at": datetime.now().isoformat(timespec="seconds"),
              "ticker": ticker, "market_cap": 1e9, "enterprise_value": 1.2e9, "price": 10.0,
              "currency": "USD", "years": [{"fiscal_year_end": "2025-12-31"}], "quarters": [],
              "closes": [9.0, 10.0], "price_at": (date.today() - timedelta(days=3)).isoformat()}
    record.update(over)
    paths.cache.mkdir(parents=True, exist_ok=True)
    (paths.cache / f"{ticker}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _fake_yahoo(monkeypatch, closes=None):
    class FakeTicker:
        def __init__(self, t):
            pass

        @property
        def info(self):
            raise AssertionError("info must not be read")

    monkeypatch.setattr(market_data.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(market_data, "closes", lambda t: closes if closes is not None else [11.0, 12.0])


@pytest.mark.unit
def test_stale_price_is_refreshed_without_info(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths)
    _fake_yahoo(monkeypatch)
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 12.0 and data["market_cap"] == pytest.approx(1.2e9)
    assert data["enterprise_value"] == pytest.approx(1.4e9)
    assert data["closes"] == [11.0, 12.0] and data["price_at"] == date.today().isoformat()
    assert data["from_cache"] is True and data["price_refreshed"] is True
    assert data["years"] == [{"fiscal_year_end": "2025-12-31"}] and data["quarters"] == []
    stored = json.loads((paths.cache / "ABC.json").read_text())
    assert stored["price"] == 12.0 and "price_refreshed" not in stored and "from_cache" not in stored


@pytest.mark.unit
def test_fresh_price_makes_no_call(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths, price_at=date.today().isoformat())
    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: (_ for _ in ()).throw(AssertionError("no call")))
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 10.0 and data["from_cache"] is True and "price_refreshed" not in data


@pytest.mark.unit
def test_record_without_price_at_is_refreshed(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    record = _cached_record(paths)
    del record["price_at"]
    (paths.cache / "ABC.json").write_text(json.dumps(record), encoding="utf-8")
    _fake_yahoo(monkeypatch)
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 12.0 and data["price_refreshed"] is True


@pytest.mark.unit
def test_old_schema_record_is_fetched_in_full_not_price_refreshed(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths, schema=market_data.CACHE_SCHEMA - 1)
    reads = []

    class FullTicker:
        @property
        def info(self):
            reads.append("info")
            return {"marketCap": 5e9, "currentPrice": 20.0, "currency": "USD", "financialCurrency": "EUR"}

    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: FullTicker())
    monkeypatch.setattr(market_data, "statement_years", lambda *a: [{"fiscal_year_end": "2025-12-31"}])
    monkeypatch.setattr(market_data, "fetch_quarters", lambda t: ([], True))
    monkeypatch.setattr(market_data, "closes", lambda t: [20.0])
    monkeypatch.setattr(FullTicker, "income_stmt", None, raising=False)
    monkeypatch.setattr(FullTicker, "balance_sheet", None, raising=False)
    monkeypatch.setattr(FullTicker, "cashflow", None, raising=False)
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert reads == ["info"] and data["price"] == 20.0 and not data.get("from_cache")
    assert "price_refreshed" not in data
    assert data["price_at"] == date.today().isoformat() and data["financial_currency"] == "EUR"


@pytest.mark.unit
def test_failed_refresh_serves_the_cached_record(tmp_path, monkeypatch, caplog):
    paths = Paths(tmp_path)
    _cached_record(paths)

    class Down:
        def __init__(self, t):
            raise RuntimeError("Yahoo down")

    monkeypatch.setattr(market_data.yf, "Ticker", Down)
    with caplog.at_level(logging.WARNING):
        data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 10.0 and data["from_cache"] is True and "price_refreshed" not in data
    assert "price refresh failed for ABC" in caplog.text


@pytest.mark.unit
def test_empty_closes_count_as_a_failed_refresh(tmp_path, monkeypatch, caplog):
    paths = Paths(tmp_path)
    _cached_record(paths)
    _fake_yahoo(monkeypatch, closes=[])
    with caplog.at_level(logging.WARNING):
        data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 10.0 and data["closes"] == [9.0, 10.0] and "price_refreshed" not in data
    assert "price refresh failed for ABC" in caplog.text


@pytest.mark.unit
def test_market_cap_and_ev_are_left_alone_when_the_old_price_is_missing(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths, price=None)
    _fake_yahoo(monkeypatch)
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 12.0 and data["market_cap"] == 1e9 and data["enterprise_value"] == 1.2e9
    assert data["price_refreshed"] is True


@pytest.mark.unit
def test_refresh_keeps_a_pence_record_in_its_own_units(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths, ticker="DNLM.L", currency="GBp", price=800.0, market_cap=2.0e9,
                   enterprise_value=2.5e9)
    _fake_yahoo(monkeypatch, closes=[790.0, 880.0])
    data = market_data.fundamentals("DNLM.L", paths, cache_days=7, price_days=1)
    assert data["price"] == 880.0 and data["market_cap"] == pytest.approx(2.2e9)
    assert data["enterprise_value"] == pytest.approx(2.7e9)


@pytest.mark.unit
def test_price_days_none_never_refreshes(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths)
    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: (_ for _ in ()).throw(AssertionError("no call")))
    data = market_data.fundamentals("ABC", paths, cache_days=7)
    assert data["price"] == 10.0 and "price_refreshed" not in data


@pytest.mark.unit
def test_screen_config_defaults():
    assert config.DEFAULTS["screen"]["price_days"] == 1 and config.DEFAULTS["screen"]["cache_days"] == 7


@pytest.mark.unit
def test_unparseable_price_at_is_refreshed(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    _cached_record(paths, price_at="garbage")
    _fake_yahoo(monkeypatch)
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 12.0 and data["price_refreshed"] is True


@pytest.mark.unit
def test_unserialisable_refresh_is_a_warned_cache_hit(tmp_path, monkeypatch, caplog):
    paths = Paths(tmp_path)
    _cached_record(paths)
    _fake_yahoo(monkeypatch, closes=[11.0, object()])
    with caplog.at_level(logging.WARNING):
        data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 10.0 and "price_refreshed" not in data
    assert "price refresh failed for ABC" in caplog.text


@pytest.mark.unit
def test_pipeline_note_names_the_market_and_currency():
    from value_pipeline import review

    note = review.pipeline_note({"country": "UK", "market": "ftse100", "quote_currency": "GBp",
                                 "hints": "compounder", "move_3m": -0.12})
    assert note.startswith("UK (ftse100), quoted in GBp; screen figures in dollars; lane hint compounder")
    assert not review.pipeline_note({"country": "US", "quote_currency": "USD", "hints": "compounder"}).startswith("US")
    assert review.pipeline_note({"country": "UK", "market": "ftse100", "quote_currency": "GBp"}) \
        == "UK (ftse100), quoted in GBp; screen figures in dollars."
    assert review.pipeline_note({"hints": "compounder"}) == "lane hint compounder."


@pytest.mark.unit
def test_actions_header_mentions_fx_for_foreign_names():
    from value_pipeline import render

    summary = {"total_value": 1000.0, "cash": 1000.0, "target_weight": 0.1, "cash_after": 1000.0}
    kw = {"reports": {}, "comparison": None, "cost": 0.0, "failures": [], "dry_run": False}
    sentence = "Nordnet charges FX and, in the UK and Ireland, stamp duty."
    assert sentence in render.actions_markdown("2026-11-05", [], summary, foreign=True, **kw)
    assert sentence not in render.actions_markdown("2026-11-05", [], summary, **kw)


@pytest.mark.unit
def test_actions_table_values_in_the_quote_unit():
    from value_pipeline import render
    from value_pipeline.actions import Decision, plan

    uk = Decision("IMB.L", "Buy", 3020.0, intrinsic_value=4376.1, formula_value=5749.0,
                  margin_of_safety=0.31, currency="GBp", fx_to_usd=0.013464)
    us = Decision("DECK", "Buy", 100.0, intrinsic_value=140.0, formula_value=150.0, margin_of_safety=0.3)
    actions, summary = plan([uk, us], cash=50_000.0, sizing=config.DEFAULTS["sizing"])
    md = render.actions_markdown("2026-11-05", actions, summary, reports={}, comparison=None, cost=0.0,
                                 failures=[], dry_run=False)
    assert "| 4,376.10 GBp / 5,749.00 GBp |" in md
    assert "| $140.00 / $150.00 |" in md
    assert "$4,376" not in md


@pytest.mark.unit
def test_backtest_report_says_us_only():
    from tests.test_lane_backtest import _fake_result
    from value_pipeline import backtest
    assert "S&P 500 and 400 only" in backtest.markdown(_fake_result())


@pytest.mark.unit
def test_old_universe_csv_without_market_keeps_its_us_rows(tmp_path):
    paths = Paths(tmp_path)
    paths.universe.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"ticker": "AAA", "name": "Aaa", "sector": "Industrials", "index": "sp500"}]
                 ).to_csv(paths.universe, index=False)
    assert list(market_data._cached_universe(paths)["market"]) == ["sp500"]


@pytest.mark.unit
def test_name_key_keeps_holding_and_merges_ltd_and_inc():
    assert market_data.name_key("Heineken Holding N.V.") != market_data.name_key("Heineken N.V.")
    assert market_data.name_key("ABB Ltd") == market_data.name_key("ABB")
    assert market_data.name_key("Aaa Inc") == market_data.name_key("Aaa Limited") == "aaa"


@pytest.mark.unit
def test_an_off_screen_holding_sector_is_in_gics_names(tmp_path):
    from value_pipeline import review

    paths = Paths(tmp_path)
    _cached_record(paths, ticker="TECH", sector="Technology")
    assert review._sector(paths, "TECH", None) == "Information Technology"


@pytest.mark.unit
def test_an_unreadable_cache_file_is_fetched_again_and_replaced(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.cache.mkdir(parents=True)
    (paths.cache / "ABC.json").write_text("{not json")

    class FullTicker:
        info = {"marketCap": 5e9, "currentPrice": 20.0, "currency": "USD"}
        income_stmt = balance_sheet = cashflow = None

    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: FullTicker())
    monkeypatch.setattr(market_data, "statement_years", lambda *a: [{"fiscal_year_end": "2025-12-31"}])
    monkeypatch.setattr(market_data, "fetch_quarters", lambda t: ([], True))
    monkeypatch.setattr(market_data, "closes", lambda t: [20.0])
    data = market_data.fundamentals("ABC", paths, cache_days=7)
    assert data["price"] == 20.0 and not data.get("from_cache")
    assert json.loads((paths.cache / "ABC.json").read_text())["price"] == 20.0
    assert not list(paths.cache.glob("*.tmp"))
