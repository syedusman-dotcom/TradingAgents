"""The local website: pages, forms, files and jobs (no network, agents faked)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from value_pipeline import operations as ops
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
