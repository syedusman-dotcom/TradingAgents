"""The local website: pages, forms, files and jobs (no network, agents faked)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from value_pipeline import operations as ops, portfolio as pfmod
from value_pipeline.config import Paths
from value_pipeline.web import create_app, views


def _client(tmp_path, **kw):
    paths = Paths(tmp_path / "data")
    kw.setdefault("price_fn", {"HRB": 44.0, "ADBE": 240.0}.get)
    kw.setdefault("series_fn", lambda p: [])
    kw.setdefault("benchmark_fn", lambda *a: None)
    app = create_app(paths, config_path=tmp_path / "config.yaml", **kw)
    return TestClient(app), app, paths


@pytest.mark.unit
def test_dashboard_without_a_portfolio_offers_to_start_one(tmp_path):
    client, app, paths = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200 and "Start your portfolio" in r.text
    r = client.post("/start", data={"cash": "1000", "currency": "USD", "token": app.state.token},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "1,000.00" in client.get("/").text


@pytest.mark.unit
def test_dashboard_lists_holdings(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    ops.record_buy(paths, "HRB", 1, 40.0)
    r = client.get("/")
    assert r.status_code == 200
    assert "HRB" in r.text and "44.00" in r.text and "+10.0%" in r.text


@pytest.mark.unit
def test_post_without_the_token_is_refused(tmp_path):
    client, app, paths = _client(tmp_path)
    r = client.post("/start", data={"cash": "1000", "currency": "USD"})
    assert r.status_code == 403 and not paths.portfolio.exists()


@pytest.mark.unit
@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 11), date(2026, 11, 7)),    # after this month's first Saturday
    (date(2026, 11, 2), date(2026, 11, 7)),
    (date(2026, 11, 7), date(2026, 11, 7)),     # on the day itself
    (date(2026, 12, 20), date(2027, 1, 2)),     # across the year end
])
def test_next_review_is_the_first_saturday(today, expected):
    assert views.next_review_date(today) == expected


@pytest.mark.unit
def test_line_chart_is_inline_svg():
    from value_pipeline.web.charts import line_chart

    svg = line_chart(["a", "b", "c"], {"Portfolio": [1.0, 2.0, 3.0], "SPY": [1.0, 1.5, 2.0]})
    assert svg.startswith("<svg") and svg.count("<polyline") == 2
    assert "http" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert line_chart(["only"], {"x": [1.0]}) == ""


def _post(client, app, url, **data):
    return client.post(url, data={**data, "token": app.state.token}, follow_redirects=False)


@pytest.mark.unit
def test_trade_forms_match_the_cli_and_show_errors(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    r = _post(client, app, "/trade/buy", ticker=" hrb", shares="2", price="44", fees="1", date="2026-10-02", note="x")
    assert r.status_code == 303
    pf = pfmod.load(paths)
    assert pf.position("HRB").shares == 2 and pf.cash == pytest.approx(911.0)
    r = _post(client, app, "/trade/sell", ticker="HRB", shares="5", price="44", fees="0", date="", note="")
    assert r.status_code == 400 and "only 2 held" in r.text and 'value="5"' in r.text
    r = _post(client, app, "/trade/buy", ticker="HRB", shares="1,5", price="44", fees="0", date="", note="")
    assert r.status_code == 400 and "is not a number" in r.text
    r = _post(client, app, "/trade/buy", ticker="HRB", shares="1", price="44", fees="0", date="11/10/2026", note="")
    assert r.status_code == 400
    r = _post(client, app, "/trade/deposit", amount="250", date="")
    assert r.status_code == 303 and pfmod.load(paths).cash == pytest.approx(1161.0)


@pytest.mark.unit
def test_trade_page_shows_the_latest_rating_and_price(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    folder = paths.reports / "2026-10-11"
    folder.mkdir(parents=True)
    (folder / "results.json").write_text('[{"ticker": "HRB", "rating": "Overweight", "intrinsic_value": 55.0, '
                                         '"margin_of_safety": 0.2, "error": null, "report": "HRB/complete_report.html", '
                                         '"thesis": "t"}]')
    r = client.get("/trade?ticker=hrb")
    assert r.status_code == 200 and "Overweight" in r.text and "55.00" in r.text and "44.00" in r.text


@pytest.mark.unit
def test_history_lists_trades_with_gains_and_undo(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    ops.record_buy(paths, "HRB", 2, 40.0, on="2026-10-02")
    ops.record_sell(paths, "HRB", 1, 50.0, on="2026-10-03")
    r = client.get("/trades")
    assert r.status_code == 200 and "+10.00" in r.text and "Undo: SELL 1 HRB" in r.text
    assert _post(client, app, "/trades/undo").status_code == 303
    assert pfmod.load(paths).position("HRB").shares == 2
    r = _post(client, app, "/trades/undo")
    assert r.status_code == 303 and "Nothing+to+undo" in r.headers["location"]


@pytest.mark.unit
def test_trade_page_without_a_portfolio_offers_to_start_one(tmp_path):
    client, app, paths = _client(tmp_path)
    assert "Start your portfolio" in client.get("/trade").text
    assert "Start your portfolio" in client.get("/trades").text
