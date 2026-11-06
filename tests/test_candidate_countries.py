import copy

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from value_pipeline import market_data, operations as ops, portfolio as pfmod, review
from value_pipeline.config import DEFAULTS, Paths, load_config, validate_config
from value_pipeline.screener import missing_countries, run_rate_skips, top_candidates
from value_pipeline.web.app import create_app

pytestmark = pytest.mark.unit

# (ticker, country, mf_position, hints, ttm_vs_5y_ebit)
ROWS = [
    ("US1", "US", 1, "", 1.0), ("SE1", "Sweden", 2, "", 1.0), ("US2", "US", 3, "", 1.0),
    ("DE1", "Germany", 4, "", 1.0), ("UK1", "UK", 5, "", 1.0), ("FR1", "France", 6, "", 1.0),
    ("USC", "US", None, "compounder", 1.0), ("USX", "US", None, "dislocation", 1.0),
    ("SED", "Sweden", None, "dislocation", 1.0), ("USB", "US", None, "compounder;dislocation", 1.0),
]


def _screen(paths, rows=ROWS, column=True):
    df = pd.DataFrame([{"ticker": t, "country": c, "mf_position": pos, "trend": "BASING",
                        "lane_hint": h.split(";")[0], "hints": h, "ttm_vs_5y_ebit": r,
                        "expected_return": 0.1, "compounder_return": 0.1}
                       for t, c, pos, h, r in rows])
    if not column:
        df = df.drop(columns="country")
    paths.screens.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.screens / "screen_2026-11-05.csv", index=False)


def _t(rows):
    return [r["ticker"] for r in rows]


def test_only_listed_countries_keep_the_rank_order(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert _t(top_candidates(paths, set(), 3, countries=["Sweden", "Germany", "UK"])) == ["SE1", "DE1", "UK1"]


def test_country_names_ignore_case_and_spaces(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert _t(top_candidates(paths, set(), 2, countries=["sweden", " Germany "])) == ["SE1", "DE1"]


def test_no_list_means_every_country(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert _t(top_candidates(paths, set(), 3, countries=None)) == ["US1", "SE1", "US2"]
    assert _t(top_candidates(paths, set(), 3, countries=[])) == ["US1", "SE1", "US2"]


def test_compounders_ignore_the_list_and_dislocations_obey_it(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    got = _t(top_candidates(paths, set(), 1, compounders=2, dislocations=2, countries=["Sweden"]))
    assert got[0] == "SE1"
    assert sorted(got[1:3]) == ["USB", "USC"]
    assert got[3:] == ["SED"]


def test_a_list_without_matches_gives_no_formula_names(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert top_candidates(paths, set(), 3, countries=["Norway"]) == []
    assert _t(top_candidates(paths, set(), 3, compounders=1, countries=["Norway"])) in (["USC"], ["USB"])


def test_a_screen_without_a_country_column_is_not_filtered(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths, column=False)
    assert _t(top_candidates(paths, set(), 3, countries=["Sweden"])) == ["US1", "SE1", "US2"]
    assert missing_countries(paths, ["Sweden"]) == []


def test_run_rate_skips_read_the_filtered_rows(tmp_path):
    paths = Paths(tmp_path)
    rows = [("US1", "US", 1, "", 0.5), ("SE1", "Sweden", 2, "", 0.5), ("DE1", "Germany", 3, "", 0.5),
            ("SE2", "Sweden", 4, "", 1.0), ("DE2", "Germany", 5, "", 1.0)]
    _screen(paths, rows=rows)
    skips = run_rate_skips(paths, set(), 2, 0.8, countries=["Sweden", "Germany"])
    assert [s["ticker"] for s in skips] == ["SE1", "DE1"]


def test_missing_countries(tmp_path):
    paths = Paths(tmp_path)
    assert missing_countries(paths, ["Sweden"]) == []
    _screen(paths)
    assert missing_countries(paths, ["Sweden", "Norge", "UK"]) == ["Norge"]
    assert missing_countries(paths, None) == []
    assert missing_countries(paths, []) == []


def _cfg(countries):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["review"]["candidates"] = 3
    cfg["review"]["candidate_countries"] = countries
    return cfg


def _portfolio(paths):
    ops.init_portfolio(paths, 20_000.0, on="2026-09-01")
    pf = pfmod.load(paths)
    pfmod.apply_buy(pf, "HRB", 10, 50.0, on="2026-09-02", intrinsic_value=55.0)
    pfmod.save(paths, pf)


def test_preview_carries_the_list_and_the_missing_names(tmp_path):
    paths = Paths(tmp_path)
    _portfolio(paths)
    _screen(paths)
    plan = review.preview(paths, _cfg(["Sweden", "Norge"]))
    assert plan["candidate_countries"] == ["Sweden", "Norge"]
    assert plan["missing_countries"] == ["Norge"]
    new = [t for t in plan["tickers"] if t != "HRB"]
    assert new[0] == "SE1" and not {"US1", "US2", "DE1", "USX"} & set(new)
    assert {"USC", "SED"} <= set(new)      # the hinted lanes still run
    plan = review.preview(paths, _cfg(["Sweden", "Norge"]), only=["AAA"])
    assert plan["candidate_countries"] == [] and plan["missing_countries"] == []
    plan = review.preview(paths, _cfg([]))
    assert plan["candidate_countries"] == [] and plan["missing_countries"] == []


@pytest.mark.parametrize("countries, shown", [(["Sweden", "Norge"], True), ([], False)])
def test_dry_run_prints_the_country_lines(tmp_path, capsys, monkeypatch, countries, shown):
    paths = Paths(tmp_path)
    _portfolio(paths)
    _screen(paths)
    monkeypatch.setattr(market_data, "latest_price", lambda t: 50.0)
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("USD", 1.0))
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    review.run(paths, _cfg(countries), yes=True, dry_run=True, today="2026-11-05")
    out = capsys.readouterr().out
    first = ("Value candidates from: Sweden, Norge (review.candidate_countries); "
             "compounder candidates from every market.")
    second = "No screen rows from: Norge (check the spelling in review.candidate_countries)."
    assert (first in out and second in out) is shown
    assert ("Value candidates from" in out or "No screen rows from" in out) is shown


def test_validate_config_checks_the_list():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["review"]["candidate_countries"] = "Sweden"
    with pytest.raises(ValueError, match=r"review\.candidate_countries"):
        validate_config(cfg)
    for bad in (["Sweden", 3], ["Sweden", " "]):
        cfg["review"]["candidate_countries"] = bad
        with pytest.raises(ValueError, match="review.candidate_countries entries must be country names"):
            validate_config(cfg)
    for good in ([], ["Sweden"]):
        cfg["review"]["candidate_countries"] = good
        validate_config(cfg)


def test_defaults_are_empty_and_the_shipped_config_is_europe():
    assert DEFAULTS["review"]["candidate_countries"] == []
    cfg = load_config()
    assert cfg["review"]["candidate_countries"] == [
        "Sweden", "Norway", "Denmark", "Finland", "Germany", "France", "Netherlands", "Switzerland",
        "Italy", "Spain", "Ireland", "Portugal", "Austria", "Belgium", "UK"]
    validate_config(cfg)


def test_the_preview_page_shows_the_list(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"review": {"candidate_countries": ["Sweden", "Norge"]}}))
    paths = Paths(tmp_path / "data")
    app = create_app(paths, config_path=config_path, price_fn={}.get, series_fn=lambda p: [],
                     benchmark_fn=lambda *a: None, allowed_hosts=("testserver",))
    client = TestClient(app)
    ops.init_portfolio(paths, 1000.0)
    r = client.post("/run/preview", data={"kind": "review", "tickers": "", "token": app.state.token})
    assert r.status_code == 200 and "Value candidates from: Sweden, Norge" in r.text


def test_an_all_owned_screen_gives_no_candidates_with_a_list(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    owned = {row[0] for row in ROWS}
    assert top_candidates(paths, owned, 3, countries=["Sweden"]) == []
    assert run_rate_skips(paths, owned, 3, 0.8, countries=["Sweden"]) == []


def test_a_header_only_screen_gives_no_candidates_with_a_list(tmp_path):
    paths = Paths(tmp_path)
    paths.screens.mkdir(parents=True, exist_ok=True)
    (paths.screens / "screen_2026-11-05.csv").write_text(
        "ticker,country,mf_position,trend,lane_hint,hints,ttm_vs_5y_ebit\n")
    assert top_candidates(paths, set(), 3, countries=["Sweden"]) == []
    assert run_rate_skips(paths, set(), 3, 0.8, countries=["Sweden"]) == []


def test_a_bare_string_list_is_one_country(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert _t(top_candidates(paths, set(), 2, countries="Sweden")) == ["SE1"]
    assert missing_countries(paths, "Norge") == ["Norge"]
    _portfolio(paths)
    plan = review.preview(paths, _cfg("Sweden"))
    assert plan["candidate_countries"] == ["Sweden"]


def test_non_string_and_blank_entries_are_dropped(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    odd = ["Sweden", 3, " "]
    assert _t(top_candidates(paths, set(), 2, countries=odd)) == ["SE1"]
    assert missing_countries(paths, odd + ["Norge"]) == ["Norge"]
    _portfolio(paths)
    plan = review.preview(paths, _cfg(odd))
    assert plan["candidate_countries"] == ["Sweden"]


def test_validate_config_still_rejects_a_bare_string_and_odd_entries():
    for bad in ("Sweden", ["Sweden", 3], ["Sweden", " "]):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["review"]["candidate_countries"] = bad
        with pytest.raises(ValueError, match=r"review\.candidate_countries"):
            validate_config(cfg)


# --- candidate_markets -----------------------------------------------------------------------

# (ticker, country, market, mf_position, hints)
MARKET_ROWS = [
    ("US1", "US", "sp500", 1, ""), ("SMC", "US", "sp600", 2, ""), ("SE1", "Sweden", "sto", 3, ""),
    ("SMD", "US", "sp600", None, "dislocation"), ("USD", "US", "sp500", None, "dislocation"),
]


def _market_screen(paths):
    df = pd.DataFrame([{"ticker": t, "country": c, "market": m, "mf_position": pos, "trend": "BASING",
                        "lane_hint": h, "hints": h, "ttm_vs_5y_ebit": 1.0, "expected_return": 0.1}
                       for t, c, m, pos, h in MARKET_ROWS])
    paths.screens.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.screens / "screen_2026-11-06.csv", index=False)


def test_a_listed_market_admits_its_rows_whatever_the_country(tmp_path):
    paths = Paths(tmp_path)
    _market_screen(paths)
    assert _t(top_candidates(paths, set(), 5, countries=["Sweden"])) == ["SE1"]
    assert _t(top_candidates(paths, set(), 5, countries=["Sweden"], markets=["sp600"])) == ["SMC", "SE1"]
    assert _t(top_candidates(paths, set(), 5, countries=["Sweden"], markets=[])) == ["SE1"]


def test_an_unlisted_us_market_stays_out(tmp_path):
    paths = Paths(tmp_path)
    _market_screen(paths)
    assert "US1" not in _t(top_candidates(paths, set(), 5, countries=["Sweden"], markets=["sp600"]))


def test_empty_countries_with_markets_still_allows_every_country(tmp_path):
    paths = Paths(tmp_path)
    _market_screen(paths)
    assert _t(top_candidates(paths, set(), 3, countries=[], markets=["sp600"])) == ["US1", "SMC", "SE1"]


def test_dislocation_candidates_obey_the_markets_and_compounders_ignore_them(tmp_path):
    paths = Paths(tmp_path)
    _market_screen(paths)
    got = _t(top_candidates(paths, set(), 0, dislocations=5, countries=["Sweden"], markets=["sp600"]))
    assert got == ["SMD"]
    assert _t(top_candidates(paths, set(), 0, dislocations=5, countries=["Sweden"])) == []


def test_run_rate_skips_agree_on_the_markets(tmp_path):
    paths = Paths(tmp_path)
    df = pd.DataFrame([
        {"ticker": "A", "country": "US", "market": "sp600", "mf_position": 1, "trend": "BASING",
         "ttm_vs_5y_ebit": 0.3},
        {"ticker": "B", "country": "US", "market": "sp500", "mf_position": 2, "trend": "BASING",
         "ttm_vs_5y_ebit": 0.3},
        {"ticker": "C", "country": "Sweden", "market": "sto", "mf_position": 3, "trend": "BASING",
         "ttm_vs_5y_ebit": 1.0}])
    paths.screens.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.screens / "screen_2026-11-06.csv", index=False)
    assert _t(run_rate_skips(paths, set(), 1, 0.8, ["Sweden"])) == []
    assert _t(run_rate_skips(paths, set(), 1, 0.8, ["Sweden"], markets=["sp600"])) == ["A"]


def test_validate_config_checks_the_markets():
    cfg = copy.deepcopy(DEFAULTS)
    assert cfg["review"]["candidate_markets"] == []
    for good in ([], ["sp600"], ["sp600", "sto"]):
        cfg["review"]["candidate_markets"] = good
        validate_config(cfg)
    for bad in (["sp601"], "sp600", [3], ["sp600", ""]):
        cfg["review"]["candidate_markets"] = bad
        with pytest.raises(ValueError, match=r"review\.candidate_markets"):
            validate_config(cfg)
    assert load_config()["review"]["candidate_markets"] == []


def test_preview_carries_the_markets(tmp_path):
    paths = Paths(tmp_path)
    _portfolio(paths)
    _market_screen(paths)
    cfg = _cfg(["Sweden"])
    cfg["review"]["candidate_markets"] = ["sp600"]
    plan = review.preview(paths, cfg)
    assert plan["candidate_markets"] == ["sp600"]
    assert {"SMC", "SE1"} <= set(plan["tickers"]) and "US1" not in plan["tickers"]
    assert review.preview(paths, _cfg(["Sweden"]))["candidate_markets"] == []
    assert review.preview(paths, cfg, only=["AAA"])["candidate_markets"] == []


@pytest.mark.parametrize("markets, line", [
    (["sp600"], "Value candidates from: Sweden; markets: sp600 (review.candidate_countries, "
                "review.candidate_markets); compounder candidates from every market."),
    ([], "Value candidates from: Sweden (review.candidate_countries); "
         "compounder candidates from every market.")])
def test_dry_run_prints_the_markets(tmp_path, capsys, monkeypatch, markets, line):
    paths = Paths(tmp_path)
    _portfolio(paths)
    _market_screen(paths)
    monkeypatch.setattr(market_data, "latest_price", lambda t: 50.0)
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("USD", 1.0))
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    cfg = _cfg(["Sweden"])
    cfg["review"]["candidate_markets"] = markets
    review.run(paths, cfg, yes=True, dry_run=True, today="2026-11-06")
    assert line in capsys.readouterr().out
