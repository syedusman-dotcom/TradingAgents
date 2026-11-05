"""Universe from Nordnet's markets: table finding, ticker codes, cross-listings (no network)."""

from __future__ import annotations

import logging

import pandas as pd
import pytest

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
        "omxh25": ".HE", "obx": ".OL", "tsxcomp": ".TO",
    }
    assert "nikkei225" not in market_data.MARKETS and "kospi" not in market_data.MARKETS
    gics = {k for k, m in market_data.MARKETS.items() if m.gics_column}
    assert gics == {"sp500", "sp400"}


@pytest.mark.unit
def test_default_indexes_are_every_nordnet_market():
    assert set(config.DEFAULTS["universe"]["indexes"]) == set(market_data.MARKETS)


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

    def fundamentals(t, p, days=7):
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
    monkeypatch.setattr(market_data, "fundamentals", lambda t, p, days=7: record())
    monkeypatch.setattr(market_data, "cached_sector", lambda t, p: None)
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"GBp": 1.30, "GBP": 1.30}.get(c))
    monkeypatch.setattr(metrics, "current_discount_rate", lambda: {"rate": 0.10, "treasury_10y": None})
    row = review.offscreen_row("DNLM.L", paths, config.DEFAULTS, {})
    assert row["price"] == pytest.approx(800.0) and row["market_cap"] == pytest.approx(2.6e9)
    assert row["country"] == "UK" and row["market"] == "ftse100" and row["quote_currency"] == "GBp"
    assert row["sector"] == "Consumer Discretionary"
