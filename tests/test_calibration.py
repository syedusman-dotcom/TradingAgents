"""Calibration: past decisions scored against later prices (no network; prices faked)."""

import json
from datetime import date

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
    assert adbe["h180"] == {"price": 125.0, "ret": pytest.approx(0.25), "reached_value": True,
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


def _web(tmp_path, paths):
    app = create_app(paths, config_path=tmp_path / "config.yaml", price_fn=lambda t: None,
                     series_fn=lambda p: [], benchmark_fn=lambda *a: None,
                     allowed_hosts=("127.0.0.1", "localhost", "testserver"))
    return TestClient(app)


@pytest.mark.unit
def test_calibration_page_renders_empty_and_populated(tmp_path, monkeypatch):
    # The page reads the price cache only; a live fetch would fail this test.
    monkeypatch.setattr(market_data, "closes_between",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("live call")))
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
