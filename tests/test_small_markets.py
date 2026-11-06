"""Size and trading-value floors for the small-company markets (no network)."""

from __future__ import annotations

import copy
from datetime import date

import pandas as pd
import pytest

from tests.test_lane_backtest import AS_OF, _cfg, _hist, _snap_store
from tests.test_nordnet_markets import (
    FakeApiResponse,
    FakeResponse,
    _screen_setup,
    api_payload,
    api_row,
    many,
    members_table,
    record,
)
from value_pipeline import backtest, config, market_data, screener
from value_pipeline.config import DEFAULTS, Paths, validate_config

GOOD = {"ticker": "GOOD", "market_cap": 5e9, "fscore": 7, "altman_z": 3.0, "free_cash_flow": 1e8,
        "interest_coverage": 10, "earnings_yield_avg": 0.1, "roic_ranked": 0.3, "roic_min": 0.2,
        "dilution_per_year": 0.0, "fcf_yield": 0.05, "trend": "UPTREND"}
F = DEFAULTS["filters"]


def small(**kw):
    return {**GOOD, "market": "sto", "market_cap": 3e8, "daily_value": 5e5, **kw}


@pytest.mark.unit
def test_defaults_name_the_floors():
    assert F["small_markets"] == dict.fromkeys(("sto", "cph", "hel", "ose", "sp600"), 2.5e8)
    assert F["min_daily_value"] == 250000


@pytest.mark.unit
def test_a_small_market_uses_its_own_size_floor():
    assert screener.failed_filters(small(), F) == []
    assert screener.failed_filters(small(market_cap=2e8), F) == ["market cap"]
    assert screener.failed_filters(small(market_cap=2.5e8), F) == ["market cap"]   # as today's check: at the floor fails
    assert screener.failed_filters(dict(GOOD, market="sp500", market_cap=1e9), F) == ["market cap"]
    assert screener.failed_filters(dict(GOOD, market="sp500", market_cap=1e9, daily_value=1.0), F) == ["market cap"]


@pytest.mark.unit
def test_liquidity_applies_to_small_markets_only():
    assert screener.failed_filters(small(daily_value=None), F) == ["liquidity"]
    assert screener.failed_filters(small(daily_value=249999), F) == ["liquidity"]
    assert screener.failed_filters(small(daily_value=250000), F) == []
    assert screener.failed_filters(dict(GOOD, market="sp500"), F) == []          # no daily_value, large cap
    assert screener.failed_filters(small(daily_value=None), F, check_liquidity=False) == []
    assert screener.failed_filters(small(daily_value=1.0), F, check_liquidity=False) == []


@pytest.mark.unit
def test_fundamentals_stores_average_volume(tmp_path, monkeypatch):
    lines = ["Total Revenue", "EBIT", "Net Income", "Total Assets", "Current Assets",
             "Current Liabilities", "Net PPE"]

    class FakeTicker:
        info = {"marketCap": 2000, "currentPrice": 20, "currency": "SEK", "sector": "Industrials",
                "averageVolume": 123456}
        income_stmt = pd.DataFrame({pd.Timestamp("2025-12-31"): range(7)}, index=lines)
        balance_sheet = cashflow = None
        quarterly_income_stmt = pd.DataFrame({pd.Timestamp("2026-03-31"): range(7)}, index=lines)
        quarterly_balance_sheet = quarterly_cashflow = None

    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: FakeTicker())
    monkeypatch.setattr(market_data, "closes", lambda t: [20.0])
    data = market_data.fundamentals("ABC-B.ST", config.Paths(tmp_path))
    assert data["average_volume"] == 123456
    assert market_data.in_usd.__doc__ and "average_volume" not in market_data.in_usd.__code__.co_names


@pytest.mark.unit
def test_screen_row_carries_daily_value_in_dollars(tmp_path, monkeypatch):
    sek = record(ticker="ABC.ST", currency="SEK", financial_currency="SEK", price=100.0, market_cap=3.0e9,
                 average_volume=10000, sector="Industrials")
    old = record(ticker="OLD", currency="USD", financial_currency="USD", price=20.0, market_cap=5.0e9,
                 sector="Industrials")                    # a cache record from before average_volume
    paths, cfg, screener_mod, _ = _screen_setup(
        tmp_path, monkeypatch,
        [("ABC.ST", "Abc", None, "sto", "sto", "Sweden"), ("OLD", "Old", "Industrials", "sp500", "sp500", "US")],
        {"ABC.ST": sek, "OLD": old}, rates={"USD": 1.0, "SEK": 0.1})
    res = screener_mod.run(paths, cfg, owned=set(), today="2026-11-06")
    rows = {r["ticker"]: r for r in pd.read_csv(res["path"]).to_dict("records")}
    assert rows["ABC.ST"]["daily_value"] == pytest.approx(10000 * 10.0)
    assert pd.isna(rows["OLD"]["daily_value"])
    assert "liquidity" not in str(rows["OLD"]["fail_reasons"])
    assert "liquidity" in rows["ABC.ST"]["fail_reasons"]             # $100,000 a day is under $250,000
    assert "market cap" not in rows["ABC.ST"]["fail_reasons"]        # $300M clears the $250M floor


def _snap(market, small_floor=None, big_floor=None):
    cfg = _cfg()
    cap = backtest.snapshot("SYN", AS_OF, _snap_store(), _hist(), 0.09, cfg)[0]["market_cap"]
    cfg["filters"] = dict(cfg["filters"], min_market_cap=cap * 2, small_markets={"sp600": cap / 2})
    return backtest.snapshot("SYN", AS_OF, _snap_store(), _hist(), 0.09, cfg, "Industrials", market=market)[0]


@pytest.mark.unit
def test_backtest_snapshot_carries_market_and_skips_liquidity():
    row = _snap("sp600")
    assert row["market"] == "sp600"
    assert "market cap" not in row["fail_reasons"] and "liquidity" not in row["fail_reasons"]
    other = _snap("sp500")
    assert other["market"] == "sp500" and "market cap" in other["fail_reasons"]
    assert "market cap" in _snap(None)["fail_reasons"]


@pytest.mark.unit
def test_backtest_universe_returns_the_market_and_still_takes_a_frame_without_one():
    cfg = copy.deepcopy(DEFAULTS)
    frame = pd.DataFrame({"ticker": ["AAA", "BBB"], "sector": ["Industrials", None],
                          "market": ["sp600", "sp500"]})
    assert backtest._universe(None, cfg, None, lambda p, i: frame) == [
        ("AAA", "Industrials", "sp600"), ("BBB", None, "sp500")]
    bare = frame.drop(columns="market")
    assert backtest._universe(None, cfg, None, lambda p, i: bare)[0] == ("AAA", "Industrials", None)


@pytest.mark.unit
def test_report_limit_names_the_missing_volume_history():
    run = {"indexes": ["sp600"], "small_markets": ["sp600"]}
    assert "trading-value floor cannot be applied to past dates" in " ".join(backtest._small_market_limits(run))
    assert backtest._small_market_limits({"indexes": ["sp500"], "small_markets": ["sp600"]}) == []
    assert backtest._small_market_limits({"indexes": ["sp600"], "small_markets": ["sto"]}) == []   # the run's map, not the defaults


@pytest.mark.unit
def test_offscreen_rows_carry_daily_value(tmp_path, monkeypatch):
    from value_pipeline import metrics, review
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"USD": 1.0, "SEK": 0.1}.get(c))
    monkeypatch.setattr(metrics, "current_discount_rate", lambda: {"rate": 0.10, "treasury_10y": None})
    cfg = copy.deepcopy(DEFAULTS)
    data = record(ticker="ABC.ST", currency="SEK", financial_currency="SEK", price=100.0, market_cap=3.0e10,
                  average_volume=10000, sector="Industrials")
    monkeypatch.setattr(market_data, "fundamentals", lambda *a, **k: dict(data))
    row = review.offscreen_row("ABC.ST", config.Paths(tmp_path), cfg, {})
    assert row["daily_value"] == pytest.approx(100000.0)        # 10,000 shares x $10
    assert "liquidity" not in row["fail_reasons"]               # .ST maps to omxs30: not a small market
    cfg["filters"]["small_markets"] = {"omxs30": 2.5e8}
    row = review.offscreen_row("ABC.ST", config.Paths(tmp_path), cfg, {})
    assert "liquidity" in row["fail_reasons"]                   # under $250,000 a day
    data["average_volume"] = 1_000_000
    assert "liquidity" not in review.offscreen_row("ABC.ST", config.Paths(tmp_path), cfg, {})["fail_reasons"]
    del data["average_volume"]
    assert "liquidity" in review.offscreen_row("ABC.ST", config.Paths(tmp_path), cfg, {})["fail_reasons"]


@pytest.mark.unit
def test_validate_config_checks_the_small_markets():
    cfg = copy.deepcopy(DEFAULTS)
    validate_config(cfg)
    for bad in ({"nope": 1e8}, {"sto": 0}, {"sto": -1.0}, {"sto": float("inf")}, {"sto": "big"}, {"sto": True}):
        cfg["filters"]["small_markets"] = bad
        with pytest.raises(ValueError, match="small_markets"):
            validate_config(cfg)
    cfg["filters"]["small_markets"] = "sto"
    with pytest.raises(ValueError, match="small_markets"):
        validate_config(cfg)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["filters"]["min_daily_value"] = -1
    with pytest.raises(ValueError, match="min_daily_value"):
        validate_config(cfg)


@pytest.mark.unit
def test_the_shipped_yaml_loads_and_validates():
    cfg = config.load_config()
    validate_config(cfg)
    assert cfg["filters"]["small_markets"] == dict.fromkeys(("sto", "cph", "hel", "ose", "sp600"), 2.5e8)
    assert cfg["filters"]["min_daily_value"] == 250000


# --- final review fixes -------------------------------------------------------------------

def _hel_rows():
    return [api_row(f"H{i}", f"H Company {i}") for i in range(10)]


@pytest.mark.unit
def test_finnish_share_classes_without_a_space_give_one_row(monkeypatch):
    rows = [api_row("ORNAV", "Orion Oyj A", "1,000"), api_row("ORNBV", "Orion Oyj B", "9,000"),
            api_row("METSA", "Metsä Board Oyj A", "50"), api_row("METSB", "Metsä Board Oyj B", "700"),
            api_row("X1", "Kone Series A", "5"), api_row("X2", "Kone Ser. B", "8")] + _hel_rows()
    monkeypatch.setattr(market_data.requests, "get", lambda url, **kw: FakeApiResponse(api_payload(rows)))
    df = market_data.fetch_market(market_data.MARKETS["hel"])
    tickers = set(df["ticker"])
    assert "ORNBV.HE" in tickers and "ORNAV.HE" not in tickers
    assert "METSB.HE" in tickers and "METSA.HE" not in tickers
    assert "X2.HE" in tickers and "X1.HE" not in tickers


def _universe_run(tmp_path, monkeypatch, index_html, api_by_code):
    def get(url, **kwargs):
        for code, rows in api_by_code.items():
            if f"market={code}" in url:
                return FakeApiResponse(api_payload(rows + _hel_rows()))
        return FakeResponse(index_html)
    monkeypatch.setattr(market_data.requests, "get", get)


@pytest.mark.unit
def test_a_small_market_row_matching_an_earlier_company_in_the_class_stripped_name_is_dropped(
        tmp_path, monkeypatch):
    index = members_table([("ORNBV", "Orion Oyj B")] + many(10, "I"))
    _universe_run(tmp_path, monkeypatch, index,
                  {"HEL": [api_row("ORNAV", "Orion Oyj A"), api_row("KESKOA", "Kesko Oyj A")]})
    df = market_data.universe(Paths(tmp_path), ["omxh25", "hel"])
    names = list(df["name"])
    assert "Orion Oyj B" in names and "Orion Oyj A" not in names
    assert "Kesko Oyj A" in names                                   # nothing earlier to match


@pytest.mark.unit
def test_ssab_and_autoliv_dedupe_across_markets(tmp_path, monkeypatch):
    sto = [api_row("SSAB B", "SSAB B"), api_row("ALIV SDB", "Autoliv Inc. SDB")]
    hel = [api_row("SSABAH", "SSAB A")]
    index = members_table([("ALV", "Autoliv")] + many(10, "I"))

    def get(url, **kwargs):
        if "market=STO" in url:
            return FakeApiResponse(api_payload(sto + _hel_rows()))
        if "market=HEL" in url:
            return FakeApiResponse(api_payload(hel + [api_row(f"K{i}", f"K Company {i}") for i in range(10)]))
        return FakeResponse(index)
    monkeypatch.setattr(market_data.requests, "get", get)
    df = market_data.universe(Paths(tmp_path), ["sp500", "sto", "hel"])
    names = set(df["name"])
    assert "Autoliv" in names and "Autoliv Inc. SDB" not in names
    assert "SSAB B" in names and "SSAB A" not in names


@pytest.mark.unit
def test_ncc_b_in_sweden_does_not_collide_with_ncc_group_in_the_uk(tmp_path, monkeypatch):
    index = members_table([("NCC", "NCC Group")] + many(10, "I"))
    _universe_run(tmp_path, monkeypatch, index, {"STO": [api_row("NCC B", "NCC B")]})
    df = market_data.universe(Paths(tmp_path), ["ftse250", "sto"])
    assert {"NCC Group", "NCC B"} <= set(df["name"])


@pytest.mark.unit
def test_icb_sector_names_map_to_gics():
    assert market_data.gics_sector("Telecommunications") == "Communication Services"
    assert market_data.gics_sector("Technology") == "Information Technology"
    assert market_data.gics_sector("Basic Materials") == "Materials"
    for name in ("Consumer Discretionary", "Consumer Staples", "Health Care", "Industrials", "Energy",
                 "Financials", "Real Estate", "Utilities"):
        assert market_data.gics_sector(name) == name


@pytest.mark.unit
def test_screen_run_maps_a_universe_icb_sector_and_still_excludes(tmp_path, monkeypatch):
    tel = record(ticker="TEL.ST", sector="Telecommunications", currency="USD", financial_currency="USD")
    bank = record(ticker="BNK.ST", sector="Financials", currency="USD", financial_currency="USD")
    paths, cfg, screener_mod, _ = _screen_setup(
        tmp_path, monkeypatch,
        [("TEL.ST", "Tel", "Telecommunications", "sto", "sto", "Sweden"),
         ("BNK.ST", "Bank", "Financials", "sto", "sto", "Sweden")],
        {"TEL.ST": tel, "BNK.ST": bank}, rates={"USD": 1.0, "SEK": 0.1})
    res = screener_mod.run(paths, cfg, owned=set(), today="2026-11-06")
    rows = {r["ticker"]: r for r in pd.read_csv(res["path"]).to_dict("records")}
    assert rows["TEL.ST"]["sector"] == "Communication Services"
    assert "BNK.ST" not in rows and any(e["ticker"] == "BNK.ST" for e in res["excluded"])


@pytest.mark.unit
def test_a_backtest_run_leaves_the_screens_universe_csv_alone(tmp_path, monkeypatch):
    from tests.test_lane_backtest import _e2e_setup
    paths, cfg = _e2e_setup(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    original = "ticker,name,sector,index,market,country\nOLD,Old,Industrials,sto,sto,Sweden\n"
    paths.universe.write_text(original, encoding="utf-8")
    cfg["universe"]["indexes"] = ["sp600"]
    index = members_table(many(12, "S"))
    monkeypatch.setattr(market_data.requests, "get", lambda url, **kw: FakeResponse(index))
    monkeypatch.setattr(backtest, "added_dates", lambda *a, **k: {})
    monkeypatch.setattr(backtest.market_data, "throttle", lambda s: None)
    backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False)
    assert paths.universe.read_text(encoding="utf-8") == original
    market_data.universe(paths, ["sp600"])                       # the screen's own call still writes
    assert paths.universe.read_text(encoding="utf-8") != original


@pytest.mark.unit
def test_small_markets_in_the_yaml_replace_the_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("filters:\n  small_markets: {sto: 3.0e8}\n", encoding="utf-8")
    assert config.load_config(path)["filters"]["small_markets"] == {"sto": 3e8}
    path.write_text("filters:\n  small_markets: {}\n", encoding="utf-8")
    assert config.load_config(path)["filters"]["small_markets"] == {}
    assert DEFAULTS["filters"]["small_markets"]["hel"] == 2.5e8


@pytest.mark.unit
def test_an_sp600_only_backtest_has_no_join_dates(tmp_path, monkeypatch):
    from tests.test_lane_backtest import _e2e_setup, _e2e_universe
    paths, cfg = _e2e_setup(tmp_path)
    cfg["universe"]["indexes"] = ["sp600"]
    monkeypatch.setattr(backtest, "added_dates", lambda *a, **k: pytest.fail("S&P 500 dates"))
    monkeypatch.setattr(backtest.market_data, "throttle", lambda s: None)
    result = backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False,
                          universe_fn=_e2e_universe)
    assert result["joins_known"] is False and result["survivorship"] == {}
    text = backtest.markdown(result)
    assert backtest.NO_JOINS in text and backtest.SURVIVORSHIP not in text
