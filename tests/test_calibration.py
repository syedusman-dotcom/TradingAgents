"""Calibration: past decisions scored against later prices (no network; prices faked)."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from value_pipeline import calibration, cli, market_data
from value_pipeline.config import Paths
from value_pipeline.web import create_app


def _scen(bear, base, bull, p=(0.3, 0.5, 0.2)):
    return {"bear": {"value": bear, "probability": p[0], "basis": "b"},
            "base": {"value": base, "probability": p[1], "basis": "b"},
            "bull": {"value": bull, "probability": p[2], "basis": "b"},
            "expected_value": None, "upside_downside": None, "weighted_mos": None,
            "price_basis": "review price"}


def _write(paths, folder, rows):
    (paths.reports / folder).mkdir(parents=True, exist_ok=True)
    (paths.reports / folder / "results.json").write_text(json.dumps(rows))


def _reviews(tmp_path) -> Paths:
    paths = Paths(tmp_path / "data")
    _write(paths, "2025-01-10", [
        {"ticker": "ADBE", "name": "Adobe", "rating": "Overweight", "error": None, "lane": "compounder",
         "price": 100.0, "intrinsic_value": 120.0, "formula_value": 100.0, "entry_price": 95.0,
         "expected_return": 0.15, "scenarios": _scen(80.0, 120.0, 150.0)},
        {"ticker": "CI", "rating": "Sell", "error": None, "lane": "value", "price": 50.0,
         "intrinsic_value": 40.0, "formula_value": 50.0},
        {"ticker": "BAD", "rating": None, "error": "agents failed", "price": 10.0},
    ])
    # An older row, written before lanes, names, prices and scenarios were recorded.
    _write(paths, "2024-12-01", [{"ticker": "G", "rating": "Hold", "error": None, "intrinsic_value": 30.0}])
    _write(paths, "2025-01-10-dry-run", [{"ticker": "FAKE", "rating": "Buy", "error": None, "price": 1.0}])
    return paths


PRICES = {"ADBE": 125.0, "CI": 45.0, "G": 33.0}


def _price_fn(ticker, day):
    return 30.0 if ticker == "G" and day == "2024-12-01" else PRICES.get(ticker)


@pytest.mark.unit
def test_rows_read_every_review_and_skip_dry_runs_and_errors(tmp_path):
    rows = calibration.rows(_reviews(tmp_path), today=date(2026, 10, 24), price_fn=_price_fn)
    assert [(r["date"], r["ticker"]) for r in rows] == [
        ("2025-01-10", "ADBE"), ("2025-01-10", "CI"), ("2024-12-01", "G")]
    adbe = rows[0]
    assert (adbe["name"], adbe["lane"], adbe["rating"], adbe["price"], adbe["value"]) == (
        "Adobe", "compounder", "Overweight", 100.0, 120.0)
    assert (adbe["formula_value"], adbe["bear"], adbe["base"], adbe["bull"]) == (100.0, 80.0, 120.0, 150.0)
    assert (adbe["entry_price"], adbe["expected_return"]) == (95.0, 0.15)
    assert {k: adbe["h180"][k] for k in ("price", "ret", "reached_value", "below_bear",
                                         "below_entry")} == {
        "price": 125.0, "ret": pytest.approx(0.25), "reached_value": True,
        "below_bear": False, "below_entry": False}
    ci = rows[1]
    assert ci["h90"]["ret"] == pytest.approx(-0.1) and ci["h90"]["reached_value"] is True
    assert ci["h90"]["below_bear"] is None and ci["h90"]["below_entry"] is None
    g = rows[2]   # the old row loads; its price then comes from the close on the review day
    assert (g["lane"], g["name"], g["price"], g["bear"]) == (None, None, 30.0, None)
    assert g["h365"]["ret"] == pytest.approx(0.1)


@pytest.mark.unit
def test_horizons_use_the_last_close_on_or_before(tmp_path, monkeypatch):
    calls = []

    def closes(ticker, start, end=None):
        calls.append((ticker, start, end))
        # Friday's close; the horizon day is the Sunday after it.
        return pd.Series([10.0, 11.0], index=[date(2025, 1, 2), date(2025, 1, 3)])

    monkeypatch.setattr(market_data, "closes_between", closes)
    cache = tmp_path / "prices"
    assert market_data.close_on_or_before("HRB", "2025-01-05", cache_dir=cache) == 11.0
    assert calls == [("HRB", "2024-12-26", "2025-01-05")]
    assert json.loads((cache / "HRB.json").read_text()) == {"2025-01-05": 11.0}
    # Cached: no second fetch, and a cache-only lookup finds it.
    assert market_data.close_on_or_before("HRB", "2025-01-05", cache_dir=cache) == 11.0
    assert market_data.close_on_or_before("HRB", "2025-01-05", cache_dir=cache, fetch=False) == 11.0
    assert len(calls) == 1
    # Not cached and not fetched: None, no call.
    assert market_data.close_on_or_before("HRB", "2025-02-05", cache_dir=cache, fetch=False) is None
    assert len(calls) == 1
    # Fail-open: a failing fetch is None and is not cached.
    monkeypatch.setattr(market_data, "closes_between",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert market_data.close_on_or_before("HRB", "2025-03-05", cache_dir=cache) is None
    assert "2025-03-05" not in json.loads((cache / "HRB.json").read_text())


@pytest.mark.unit
def test_same_day_rerun_counts_once_and_the_newest_stands(tmp_path):
    paths = Paths(tmp_path / "data")
    _write(paths, "2025-01-10", [{"ticker": "ADBE", "rating": "Hold", "error": None,
                                  "price": 100.0, "intrinsic_value": 110.0},
                                 {"ticker": "CI", "rating": "Sell", "error": None, "price": 50.0}])
    _write(paths, "2025-01-10-2", [{"ticker": "ADBE", "rating": "Buy", "error": None,
                                    "price": 101.0, "intrinsic_value": 130.0}])
    rows = calibration.rows(paths, today=date(2026, 10, 24), price_fn=_price_fn)
    assert [(r["folder"], r["ticker"]) for r in rows] == [
        ("2025-01-10-2", "ADBE"), ("2025-01-10", "CI")]
    assert (rows[0]["rating"], rows[0]["price"], rows[0]["value"]) == ("Buy", 101.0, 130.0)


@pytest.mark.unit
def test_default_prices_fetch_one_series_per_ticker(tmp_path, monkeypatch):
    calls = []

    def closes(ticker, start, end=None):
        calls.append((ticker, start, end))
        days = pd.date_range("2024-11-20", "2026-01-31", freq="D")
        return pd.Series([PRICES.get(ticker, 30.0)] * len(days), index=[d.date() for d in days])

    monkeypatch.setattr(market_data, "closes_between", closes)
    paths = _reviews(tmp_path)
    rows = calibration.rows(paths, today=date(2026, 1, 20))
    assert sorted(calls) == [("ADBE", "2025-01-30", "2026-01-20"),
                             ("CI", "2025-01-30", "2026-01-20"),
                             ("G", "2024-11-21", "2026-01-20"),
                             ("SPY", "2024-11-21", "2026-01-20")]
    adbe = rows[0]
    assert all(adbe[f"h{n}"]["price"] == 125.0 for n in calibration.HORIZONS)
    assert rows[2]["price"] == 33.0   # G's price then, from the same series
    cached = json.loads((paths.cache / "prices" / "ADBE.json").read_text())
    assert set(cached) == {"2025-02-09", "2025-04-10", "2025-07-09", "2026-01-10"}
    # A second refresh finds everything cached and fetches nothing.
    calls.clear()
    calibration.rows(paths, today=date(2026, 1, 20))
    assert calls == []


@pytest.mark.unit
def test_future_horizons_are_none(tmp_path):
    asked = []

    def price_fn(ticker, day):
        asked.append(day)
        return 110.0

    rows = calibration.rows(_reviews(tmp_path), today=date(2025, 5, 1), price_fn=price_fn)
    adbe = rows[0]
    assert adbe["h30"] is not None and adbe["h90"] is not None   # 2025-02-09, 2025-04-10
    assert adbe["h180"] is None and adbe["h365"] is None          # 2025-07-09, 2026-01-10
    assert "2025-07-09" not in asked and "2026-01-10" not in asked
    g = rows[2]   # 2024-12-01: +30 and +90 days have passed, +180 has not
    assert g["h90"] is not None and g["h180"] is None


def _row(lane, rating, ret180, *, reached=False, value=None, formula=None, bear_p=None,
         bull_p=None, below_bear=False, above_bull=False):
    price = 100.0
    h180 = None if ret180 is None else {
        "price": price * (1 + ret180), "ret": ret180, "reached_value": reached,
        "below_bear": below_bear, "below_entry": None}
    return {"date": "2025-01-10", "ticker": "X", "lane": lane, "rating": rating, "price": price,
            "value": value, "formula_value": formula, "bear": 80.0 if bear_p else None,
            "bull": (100.0 * (1 + ret180) - (1 if above_bull else -1)) if bull_p and ret180 is not None
            else None,
            "bear_probability": bear_p, "bull_probability": bull_p,
            "h30": h180, "h90": h180, "h180": h180,
            "h365": None if ret180 is None else {**h180, "reached_value": reached}}


@pytest.mark.unit
def test_scores_by_rating_and_lane_and_value_gap():
    rows = [
        _row("value", "Buy", 0.20, reached=True, value=120.0, formula=100.0),
        _row("value", "Overweight", 0.10, reached=False, value=110.0, formula=100.0),
        _row("value", "Buy", -0.05, reached=True, value=90.0, formula=100.0),
        _row("value", "Sell", -0.10),
        _row("value", "Sell", 0.04),
        _row("compounder", "Hold", None),
    ]
    s = calibration.score(rows)
    assert s["by_rating"]["Buy"]["n"] == 2
    assert s["by_rating"]["Buy"]["median_ret_180"] == pytest.approx(0.075)
    assert s["by_rating"]["Buy"]["hit_value_365"] == {"k": 2, "n": 2, "share": 1.0}
    assert s["by_rating"]["Sell"]["sells_fell_180"] == {"k": 1, "n": 2, "share": 0.5}
    assert s["by_rating"]["Sell"]["hit_value_365"] is None
    assert s["by_lane"]["value"]["n"] == 5
    assert s["by_lane"]["value"]["hit_value_365"]["k"] == 2 and s["by_lane"]["value"]["hit_value_365"]["n"] == 3
    assert s["by_lane"]["compounder"]["median_ret_180"] is None
    assert s["median_value_gap"] == pytest.approx(0.10)
    assert s["calibrated_probabilities"] is None
    assert s["verdicts"] == [
        "compounder: 1 decision, none past 30 days yet",
        "value: 5 decisions; Buy/Overweight reached value within a year 2/3; Sells fell at 180 days 1/2",
    ]


@pytest.mark.unit
def test_probability_calibration_needs_ten_rows():
    nine = [_row("value", "Buy", 0.1, bear_p=0.3, bull_p=0.2, below_bear=i < 3, above_bull=i < 1)
            for i in range(9)]
    assert calibration.score(nine)["calibrated_probabilities"] is None
    ten = nine + [_row("value", "Buy", 0.1, bear_p=0.3, bull_p=0.2)]
    cp = calibration.score(ten)["calibrated_probabilities"]
    assert cp["n"] == 10
    assert cp["below_bear"] == pytest.approx(0.3) and cp["bear_probability"] == pytest.approx(0.3)
    assert cp["above_bull"] == pytest.approx(0.1) and cp["bull_probability"] == pytest.approx(0.2)


def _brier_row(bear_p, below, bull_p, above, *, key="h365", bear=80.0, bull=130.0):
    price = 75.0 if below else (135.0 if above else 100.0)
    return {"date": "2025-01-10", "ticker": "X", "price": 100.0, "bear": bear, "bull": bull,
            "bear_probability": bear_p, "bull_probability": bull_p,
            key: {"price": price, "ret": price / 100 - 1, "below_bear": below}}


def _brier_rows(n_below=9, n_above=6, bear_p=0.3, bull_p=0.2, n=30):
    return [_brier_row(bear_p, i < n_below, bull_p, i >= n - n_above) for i in range(n)]


@pytest.mark.unit
def test_brier_by_hand():
    b = calibration.brier(_brier_rows(), 365)
    assert b["horizon"] == 365 and b["n"] == 30
    # bear: p 0.3, 9 of 30 outcomes are 1 (realised 0.3): bs = (9*0.49 + 21*0.09) / 30
    assert b["bear"]["brier"] == pytest.approx((9 * 0.49 + 21 * 0.09) / 30, abs=1e-9)
    assert b["bear"]["reference"] == pytest.approx(0.3 * 0.7, abs=1e-9)
    assert b["bear"]["skill"] == pytest.approx(1 - ((9 * 0.49 + 21 * 0.09) / 30) / 0.21, abs=1e-9)
    assert b["bear"]["stated"] == pytest.approx(0.3) and b["bear"]["realised"] == pytest.approx(0.3)
    # bull: p 0.2, 6 of 30 outcomes are 1 (realised 0.2): the same shape
    assert b["bull"]["brier"] == pytest.approx((6 * 0.64 + 24 * 0.04) / 30, abs=1e-9)
    assert b["bull"]["reference"] == pytest.approx(0.2 * 0.8, abs=1e-9)
    assert b["bull"]["skill"] == pytest.approx(1 - ((6 * 0.64 + 24 * 0.04) / 30) / 0.16, abs=1e-9)
    assert "Bear: Brier 0.21 vs 0.21 for the base rate" in b["reading"]
    assert "stated 30% vs realised 30%" in b["reading"]


@pytest.mark.unit
def test_brier_reading_rule():
    # skill above zero for both legs: informative. Probabilities that follow the outcomes.
    good = [_brier_row(0.8 if i < 9 else 0.1, i < 9, 0.8 if i >= 24 else 0.05, i >= 24)
            for i in range(30)]
    b = calibration.brier(good, 365)
    assert b["bear"]["skill"] > 0 and b["bull"]["skill"] > 0
    assert b["reading"].startswith("Informative")
    # stated well above realised (by more than 10 points) on one leg: over-confident
    over = [_brier_row(0.5, i < 9, 0.2, i >= 24) for i in range(30)]
    b = calibration.brier(over, 365)
    assert b["reading"].startswith("Over-confident")
    # same stated as realised and no skill: uninformative
    assert calibration.brier(_brier_rows(), 365)["reading"].startswith("Uninformative")


@pytest.mark.unit
def test_brier_needs_thirty_rows_and_skips_incomplete_ones():
    assert calibration.brier(_brier_rows(n=29, n_below=9, n_above=6), 365) is None
    rs = _brier_rows()
    extra = [_brier_row(0.3, False, 0.2, False, key="h180"),                  # no h365
             _brier_row(None, False, None, False), _brier_row(0.3, False, 0.2, False, bear=None, bull=None)]
    b = calibration.brier(rs + extra, 365)
    assert b["n"] == 30 and b["bear"]["stated"] == pytest.approx(0.3)
    assert calibration.brier(extra, 180) is None
    # the 180 day read uses the h180 rows
    r180 = [_brier_row(0.3, i < 9, 0.2, i >= 24, key="h180") for i in range(30)]
    assert calibration.brier(r180, 180)["horizon"] == 180
    assert calibration.brier(r180, 365) is None


@pytest.mark.unit
def test_brier_with_one_outcome_has_no_skill():
    rs = [_brier_row(0.3, False, 0.2, False) for _ in range(30)]
    b = calibration.brier(rs, 365)
    assert b["bear"]["reference"] == 0 and b["bear"]["skill"] is None
    assert b["bull"]["skill"] is None
    assert "no skill" in b["reading"].lower() or "every outcome" in b["reading"]
    assert b["reading"].startswith("Over-confident")   # stated 30% against 0% realised
    low = calibration.brier([_brier_row(0.05, False, 0.05, False) for _ in range(30)], 365)
    assert low["reading"].startswith("Uninformative") and "every outcome" in low["reading"]


@pytest.mark.unit
def test_score_carries_brier_and_markdown_shows_the_readings():
    rs = _brier_rows()
    s = calibration.score(rs)
    assert set(s["brier"]) == {"365", "180"}
    assert s["brier"]["365"]["n"] == 30 and s["brier"]["180"] is None
    text = calibration.markdown(rs, s)
    assert "Brier scores" in text and "365 days" in text and "Uninformative" in text
    assert "early read" in text and "not yet readable (0 of 30 at 180 days)" in text
    s = calibration.score([_row("value", "Buy", 0.1)])
    text = calibration.markdown([_row("value", "Buy", 0.1)], s)
    assert "not yet readable (0 of 30 at 365 days)" in text


@pytest.mark.unit
def test_page_shows_the_brier_reading(tmp_path):
    paths = _reviews(tmp_path)
    (paths.cache / "prices").mkdir(parents=True)
    (paths.cache / "prices" / "ADBE.json").write_text(json.dumps(
        {"2025-02-09": 105.0, "2025-04-10": 110.0, "2025-07-09": 125.0, "2026-01-10": 130.0}))
    r = _web(tmp_path, paths).get("/calibration")
    assert r.status_code == 200
    assert "Brier" in r.text and "not yet readable" in r.text and "early read" in r.text


def _web(tmp_path, paths):
    app = create_app(paths, config_path=tmp_path / "config.yaml", price_fn=lambda t: None,
                     series_fn=lambda p: [], benchmark_fn=lambda *a: None,
                     allowed_hosts=("127.0.0.1", "localhost", "testserver"))
    return TestClient(app)


@pytest.mark.unit
def test_calibration_page_renders_empty_and_populated(tmp_path, monkeypatch):
    # The page reads the price cache only: Yahoo is never asked.
    live = []
    monkeypatch.setattr(market_data, "closes_between",
                        lambda *a, **k: live.append(a) or pd.Series(dtype=float))
    paths = Paths(tmp_path / "data")
    client = _web(tmp_path, paths)
    r = client.get("/calibration")
    assert r.status_code == 200 and "No decisions old enough yet" in r.text
    assert 'href="/calibration"' in r.text

    paths = _reviews(tmp_path)
    r = client.get("/calibration")   # reviews exist but no prices are cached: still empty
    assert r.status_code == 200 and "No decisions old enough yet" in r.text

    (paths.cache / "prices").mkdir(parents=True)
    (paths.cache / "prices" / "ADBE.json").write_text(json.dumps(
        {"2025-02-09": 105.0, "2025-04-10": 110.0, "2025-07-09": 125.0, "2026-01-10": 130.0}))
    r = client.get("/calibration")
    assert r.status_code == 200 and "No decisions old enough yet" not in r.text
    assert "ADBE" in r.text and "+25%" in r.text and "compounder: 1 decision" in r.text
    assert 'id="calibration"' in r.text
    assert live == []


@pytest.mark.unit
def test_cli_calibration(tmp_path, monkeypatch, capsys):
    paths = _reviews(tmp_path)
    monkeypatch.setattr(market_data, "closes_between",
                        lambda ticker, start, end=None: pd.Series(
                            [PRICES.get(ticker, 30.0)], index=[date.fromisoformat(end)]))
    cli.main(["--data-dir", str(paths.root), "calibration", "--horizon", "90"])
    out = capsys.readouterr().out
    assert "ADBE" in out and "CI" in out and "FAKE" not in out and "BAD" not in out
    assert "+25%" in out and "-10%" in out
    assert "value: 1 decision" in out
    assert (paths.cache / "prices" / "ADBE.json").exists()


# --- paper Buys scored in dollars against SPY -----------------------------------------------

def _paper_review(paths, folder, currency, price=1000.0, ticker="AAA", **over):
    row = {"ticker": ticker, "rating": "Hold", "error": None, "price": price,
           "margin_of_safety": 0.05, "currency": currency,
           "checklist": {"good_business": {"passed": True}, "good_management": {"passed": True},
                         "still_investing": {"passed": True}}, **over}
    _write(paths, folder, [row])


def _fn(table):
    asked = []

    def price_fn(symbol, day):
        asked.append(symbol)
        return table.get((symbol, day))

    price_fn.asked = asked
    return price_fn


@pytest.mark.unit
def test_pair():
    assert calibration.pair(None) is None and calibration.pair("USD") is None
    assert calibration.pair("GBp") == "GBPUSD=X" and calibration.pair("SEK") == "SEKUSD=X"


@pytest.mark.unit
def test_dollar_return_of_a_pence_quote(tmp_path):
    paths = Paths(tmp_path / "data")
    _paper_review(paths, "2026-01-05", "GBp")
    fn = _fn({("AAA", "2026-02-04"): 1100.0, ("GBPUSD=X", "2026-01-05"): 1.25,
              ("GBPUSD=X", "2026-02-04"): 1.30, ("SPY", "2026-01-05"): 500.0,
              ("SPY", "2026-02-04"): 510.0})
    row = calibration.rows(paths, today=date(2026, 2, 10), price_fn=fn)[0]
    dollars = 1.10 * 1.30 / 1.25 - 1
    assert row["paper"] == ["fair_price", "fair_price_investing"] and row["currency"] == "GBp"
    h = row["h30"]
    assert h["ret"] == pytest.approx(0.10) and h["ret_usd"] == pytest.approx(dollars)
    assert h["spy"] == pytest.approx(0.02) and h["excess"] == pytest.approx(dollars - 0.02)
    assert row["h90"] is None


@pytest.mark.unit
@pytest.mark.parametrize("currency", ["USD", None])
def test_dollar_rows_need_no_pair(tmp_path, currency):
    paths = Paths(tmp_path / "data")
    _paper_review(paths, "2026-01-05", currency)
    fn = _fn({("AAA", "2026-02-04"): 1100.0, ("SPY", "2026-01-05"): 500.0,
              ("SPY", "2026-02-04"): 510.0})
    h = calibration.rows(paths, today=date(2026, 2, 10), price_fn=fn)[0]["h30"]
    assert h["ret_usd"] == h["ret"] and set(fn.asked) == {"AAA", "SPY"}


@pytest.mark.unit
def test_missing_rate_or_spy_leaves_no_excess(tmp_path):
    paths = Paths(tmp_path / "data")
    _paper_review(paths, "2026-01-05", "CHF")
    fn = _fn({("AAA", "2026-02-04"): 1100.0, ("SPY", "2026-01-05"): 500.0,
              ("SPY", "2026-02-04"): 510.0})
    h = calibration.rows(paths, today=date(2026, 2, 10), price_fn=fn)[0]["h30"]
    assert h["ret"] == pytest.approx(0.1) and h["ret_usd"] is None and h["excess"] is None
    _paper_review(paths, "2026-01-05", "USD")
    fn = _fn({("AAA", "2026-02-04"): 1100.0})
    h = calibration.rows(paths, today=date(2026, 2, 10), price_fn=fn)[0]["h30"]
    assert h["spy"] is None and h["excess"] is None and h["ret_usd"] == h["ret"]


@pytest.mark.unit
def test_prefetch_asks_for_spy_and_the_pair(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    _paper_review(paths, "2026-01-05", "GBp")
    asked = {}
    monkeypatch.setattr(market_data, "fill_close_cache",
                        lambda ticker, days, cache_dir=None, today=None: asked.update(
                            {ticker: set(days)}))
    monkeypatch.setattr(market_data, "close_on_or_before",
                        lambda *a, **k: None)
    calibration.rows(paths, today=date(2026, 2, 10))
    assert asked["SPY"] == {"2026-01-05", "2026-02-04"}
    assert asked["GBPUSD=X"] == {"2026-01-05", "2026-02-04"}
    assert asked["AAA"] == {"2026-02-04"}


def _p(day, ticker="AAA", paper=("fair_price",), excess=None, h=180):
    hs = {f"h{n}": None for n in calibration.HORIZONS}
    if excess is not None or h:
        hs[f"h{h}"] = {"price": 1.0, "ret": 0.0, "ret_usd": None if excess is None else excess,
                       "spy": 0.0, "excess": excess}
    return {"date": day, "folder": day, "ticker": ticker, "paper": list(paper), **hs}


@pytest.mark.unit
def test_counted_applies_the_gap_per_ticker():
    rs = [_p("2026-04-06"), _p("2026-03-02"), _p("2026-01-01"),
          _p("2026-03-02", ticker="BBB"), _p("2026-02-01", paper=())]
    kept = calibration.counted(rs, "fair_price")
    assert [(r["ticker"], r["date"]) for r in kept] == [
        ("AAA", "2026-01-01"), ("BBB", "2026-03-02"), ("AAA", "2026-04-06")]
    base = calibration.counted(rs)   # the unflagged 2026-02-01 row counts and blocks 03-02
    assert [(r["ticker"], r["date"]) for r in base] == [
        ("AAA", "2026-01-01"), ("BBB", "2026-03-02"), ("AAA", "2026-04-06")]
    assert calibration.counted(rs, "discount_15") == []
    ninety = [_p("2026-01-01"), _p("2026-04-01")]   # exactly 90 days: counts again
    assert len(calibration.counted(ninety, "fair_price")) == 2


@pytest.mark.unit
def test_paper_horizon_counts_and_summaries():
    rs = [_p("2026-01-01", ticker="A", excess=0.10), _p("2026-01-01", ticker="B", excess=-0.02),
          _p("2026-01-01", ticker="C", excess=None)]
    s = calibration.paper_scores(rs)
    h = s["rules"]["fair_price"]["horizons"][180]
    assert h["n"] == 2 and h["no_dollar_result"] == 1
    assert h["mean_excess"] == pytest.approx(0.04) and h["median_excess"] == pytest.approx(0.04)
    assert h["ahead"] == {"k": 1, "n": 2, "share": 0.5}
    assert s["rules"]["fair_price"]["horizons"][30] == {
        "n": 0, "no_dollar_result": 0, "mean_ret_usd": None, "mean_excess": None,
        "median_excess": None, "ahead": None}
    assert s["rules"]["discount_15"]["counted"] == []
    assert calibration.score(rs)["paper"] == s


def _many(n, months, excess, extra=None):
    dates = [f"2026-{1 + i % months:02d}-10" for i in range(n)]
    rs = [_p(d, ticker=f"T{i}", excess=excess[i] if isinstance(excess, list) else excess)
          for i, d in enumerate(dates)]
    return rs + (extra or [])


@pytest.mark.unit
def test_verdict_not_enough():
    v = calibration.paper_scores(_many(19, 4, 0.05))["verdict"]
    assert v["supports"] is None and v["n"] == 19 and v["months"] == 4
    assert v["sentence"] == ("fair_price: not enough paper Buys yet "
                             "(19 of 20 past 180 days, from 4 of 3 months).")
    v = calibration.paper_scores(_many(20, 2, 0.05))["verdict"]
    assert v["supports"] is None
    assert v["sentence"].endswith("(20 of 20 past 180 days, from 2 of 3 months).")


def _base(excess, k=20):
    """Unflagged rows on other tickers so the baseline's mean lands on ``excess`` (the rule's
    rows are in the baseline too)."""
    return [_p("2026-01-10", ticker=f"U{i}", paper=(), excess=excess) for i in range(k)]


@pytest.mark.unit
def test_verdict_supports():
    rs = _many(20, 3, 0.03, extra=_base(-0.01))   # baseline: (20*0.03 - 20*0.01) / 40 = 0.01
    v = calibration.paper_scores(rs)["verdict"]
    assert v["baseline_mean_excess"] == pytest.approx(0.01)
    assert v["supports"] is True and v["n"] == 20 and v["months"] == 3
    assert v["sentence"].startswith("fair_price supports a live test of the rule:")


@pytest.mark.unit
@pytest.mark.parametrize("rs, failed", [
    (_many(20, 3, 0.0299, extra=_base(-0.02)), "under 3%"),
    (_many(20, 3, [0.2] * 10 + [-0.1] * 10, extra=_base(-0.2)), "fewer than 55%"),
    (_many(20, 3, 0.03, extra=_base(-0.008)), "all-stocks baseline"),   # baseline mean 0.011
])
def test_verdict_each_condition_failing(rs, failed):
    v = calibration.paper_scores(rs)["verdict"]
    assert v["supports"] is False
    assert v["sentence"].startswith("fair_price does not support a live test of the rule:")
    assert failed in v["sentence"].split("Failed:")[1]


# --- paper Buys shown: the markdown and the page --------------------------------------------

SENTENCE_START = "fair_price: not enough paper Buys yet"


def _shown(day, ticker, paper, excess, ret_usd=0.0, price=96.2, currency="USD"):
    r = _p(day, ticker=ticker, paper=paper, excess=excess)
    r["h180"] = {**r["h180"], "ret_usd": ret_usd}
    r.update(price=price, currency=currency, rating="Hold", lane="value", value=120.0)
    return r


def _flagged():
    return [_shown("2026-01-05", "AAA", ("fair_price", "discount_15"), 0.10, ret_usd=0.12),
            _shown("2026-02-05", "BBB", ("fair_price",), -0.02, ret_usd=0.01),
            _shown("2026-03-05", "CCC", ("fair_price",), None, ret_usd=None)]


@pytest.mark.unit
def test_markdown_paper_section_sits_between_scores_and_verdicts():
    rs = _flagged()
    md = calibration.markdown(rs, calibration.score(rs))
    assert md.index("## Scores") < md.index("## Paper Buys") < md.index("## Verdicts")
    section = md.split("## Paper Buys")[1].split("## Verdicts")[0]
    assert ("Tracked only: no money follows these. Each rule is applied in code to what a "
            "review recorded.") in section
    for name, text in calibration.paper.RULES.items():
        assert f"- **{name}**: {text}" in section
    assert SENTENCE_START in section
    assert "| Rule | Counted | 30 days | 90 days | 180 days | 365 days |" in section
    assert "| fair_price | 3 | – | – | +4.0% over SPY (1/2 ahead, n=2) | – |" in section
    assert "| every review | 3 | – | – | +4.0% over SPY (1/2 ahead, n=2) | – |" in section
    assert "| discount_15 | 1 | – | – | +10.0% over SPY (1/1 ahead, n=1) | – |" in section
    assert ("| Date | Ticker | Rules | Price then | Latest horizon | Return in dollars "
            "| Against SPY |") in section
    assert "| 2026-01-05 | AAA | fair_price, discount_15 | 96.20 | 180 days | +12.0% | +10.0% |" \
        in section
    assert "| 2026-03-05 | CCC | fair_price | 96.20 | 180 days | – | – |" in section
    assert ("1 counted result has no dollar return (a currency rate or SPY close is "
            "missing); run `calibration` to fetch them.") in section
    assert "No paper Buys yet." not in section


@pytest.mark.unit
def test_markdown_paper_section_without_a_flagged_row_and_decisions_unchanged():
    rs = [_p("2026-01-05", paper=(), excess=0.1)]
    md = calibration.markdown(rs, calibration.score(rs))
    section = md.split("## Paper Buys")[1].split("## Verdicts")[0]
    assert "No paper Buys yet." in section and SENTENCE_START in section
    assert "- **fair_price**:" in section
    assert "| Rule |" not in section and "| Date | Ticker | Rules" not in section
    assert "no dollar return" not in section
    assert md.split("## Scores")[0] == calibration.markdown(
        rs, calibration.score(rs)).split("## Scores")[0]
    head = md.split("## Scores")[0]
    assert "| Reached value | Below bear | Paper |" in head and "| – |" in head


@pytest.mark.unit
def test_calibration_page_shows_paper_buys_and_a_paper_column(tmp_path):
    paths = Paths(tmp_path / "data")
    _paper_review(paths, "2026-01-05", "USD", ticker="AAA", margin_of_safety=0.2,
                  intrinsic_value=120.0)
    _write(paths, "2026-02-05", [
        {"ticker": "BBB", "rating": "Sell", "error": None, "price": 10.0}])
    client = _web(tmp_path, paths)   # no SPY close cached
    r = client.get("/calibration")
    assert r.status_code == 200
    assert "Paper Buys" in r.text and SENTENCE_START in r.text
    for name in calibration.paper.RULES:
        assert name in r.text
    assert "<th>Paper</th>" in r.text
    assert "fair_price, fair_price_investing, discount_15" in r.text
    assert "No paper Buys yet." not in r.text

    r = _web(tmp_path, Paths(tmp_path / "other")).get("/calibration")
    assert r.status_code == 200 and "No paper Buys yet." in r.text


def test_markdown_decisions_table_has_a_paper_column():
    rs = _flagged()
    head = calibration.markdown(rs, calibration.score(rs)).split("## Scores")[0]
    assert "| Reached value | Below bear | Paper |" in head
    assert "|---|---|---|---|--:|--:|--:|---|---|---|" in head
    assert "| fair_price, discount_15 |" in head and "| fair_price |" in head


def test_a_listed_row_shows_only_the_rules_it_is_counted_for():
    rs = [_shown("2026-01-05", "AAA", ("fair_price",), 0.1),
          _shown("2026-02-05", "AAA", ("fair_price", "fair_price_investing"), 0.1)]
    view = calibration.paper_view(calibration.paper_scores(rs))
    assert [(x["date"], x["rules"]) for x in view["listed"]] == [
        ("2026-01-05", "fair_price"), ("2026-02-05", "fair_price_investing")]


def test_the_no_dollar_return_sentence_is_singular_or_plural():
    one = calibration.markdown(*(lambda rs: (rs, calibration.score(rs)))(_flagged()))
    assert "1 counted result has no dollar return" in one
    rs = [_shown("2026-01-05", "AAA", ("fair_price",), None, ret_usd=None),
          _shown("2026-01-05", "BBB", ("fair_price",), None, ret_usd=None)]
    two = calibration.markdown(rs, calibration.score(rs))
    assert "2 counted results have no dollar return" in two


def test_the_paper_text_says_returns_leave_out_dividends():
    sentence = ("Returns are price changes without dividends, for the stock and for SPY alike, "
                "which understates high-yield names a little.")
    rs = _flagged()
    assert sentence in calibration.markdown(rs, calibration.score(rs))
    assert sentence in calibration.paper_view(calibration.score(rs)["paper"])["note"]
    assert sentence in " ".join(Path("value_pipeline/README.md").read_text(encoding="utf-8").split())


def _past(day, ticker, **kw):
    r = _p(day, ticker=ticker, **kw)
    r["reached"] = [30, 90, 180, 365]
    return r


def test_a_counted_row_with_no_later_close_counts_as_no_dollar_result():
    rs = [_past("2025-01-02", "GONE", h=0), _past("2025-01-02", "OK", excess=0.1)]
    rs[0]["reached"] = [30, 90, 180]
    h = calibration.paper_scores(rs)["rules"]["fair_price"]["horizons"][180]
    assert h["n"] == 1 and h["no_dollar_result"] == 1
    h365 = calibration.paper_scores(rs)["rules"]["fair_price"]["horizons"][365]
    assert h365["n"] == 0 and h365["no_dollar_result"] == 1   # only OK reached 365 and has no close
    view = calibration.paper_view(calibration.paper_scores(rs))
    assert view["missing"] >= 1


def test_a_horizon_still_in_the_future_counts_in_neither(tmp_path):
    rs = [_p("2026-01-02", ticker="NEW", h=0)]
    rs[0]["reached"] = [30]
    for n in calibration.HORIZONS:
        h = calibration.paper_scores(rs)["rules"]["fair_price"]["horizons"][n]
        assert h["n"] == 0 and h["no_dollar_result"] == (1 if n == 30 else 0)


def test_rows_carry_the_horizons_whose_day_has_passed(tmp_path):
    paths = Paths(tmp_path)
    _paper_review(paths, "2026-01-05", "USD")
    row = calibration.rows(paths, today=date(2026, 4, 20), price_fn=lambda t, d: 10.0)[0]
    assert row["reached"] == [30, 90]
