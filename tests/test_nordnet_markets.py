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
