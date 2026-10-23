"""The local website: pages, forms, files and jobs (no network, agents faked)."""

import json
import threading
import time
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
    kw.setdefault("allowed_hosts", ("127.0.0.1", "localhost", "testserver"))
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


def _review(paths, folder, rows, actions="# Value review\n\n- Cost of this review: ~$4.00\n"):
    d = paths.reports / folder
    (d / "HRB").mkdir(parents=True)
    (d / "HRB" / "complete_report.html").write_text("<p>HRB report</p>")
    (d / "ACTIONS.html").write_text("<p>actions</p>")
    (d / "ACTIONS.md").write_text(actions)
    (d / "results.json").write_text(json.dumps(rows))


ROW = {"ticker": "HRB", "rating": "Overweight", "intrinsic_value": 55.0, "margin_of_safety": 0.2,
       "error": None, "report": "HRB/complete_report.html", "thesis": "t"}


@pytest.mark.unit
def test_reviews_list_and_report_files(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    _review(paths, "2026-10-11", [ROW])
    _review(paths, "2026-10-11-2", [dict(ROW, rating="Hold")])
    r = client.get("/reviews")
    assert r.status_code == 200 and "$4.00" in r.text
    assert r.text.index("/reports/2026-10-11-2/ACTIONS.html") < r.text.index("/reports/2026-10-11/ACTIONS.html")
    assert client.get("/reports/2026-10-11/HRB/complete_report.html").text == "<p>HRB report</p>"


@pytest.mark.unit
@pytest.mark.parametrize("path", ["../portfolio.json", "2026-10-11/../../portfolio.json", "%2e%2e/portfolio.json",
                                  "2026-10-11/missing.html"])
def test_report_files_stay_inside_the_reports_folder(tmp_path, path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    _review(paths, "2026-10-11", [ROW])
    r = client.get(f"/reports/{path}")
    assert r.status_code == 404 and '"cash"' not in r.text


@pytest.mark.unit
def test_stock_page_shows_rating_history_and_trades(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    ops.record_buy(paths, "HRB", 1, 44.0, on="2026-10-12")
    _review(paths, "2026-10-11", [ROW])
    _review(paths, "2026-11-07", [dict(ROW, rating="Hold", intrinsic_value=58.0)])
    r = client.get("/stock/hrb")
    assert r.status_code == 200
    ratings = r.text[r.text.index("<h2>Ratings</h2>"):]
    assert ratings.index("2026-11-07") < ratings.index("2026-10-11")    # newest first
    assert "<svg" in r.text                                              # value-over-time chart
    assert "Overweight" in r.text and "58.00" in r.text and "2026-10-12" in r.text


@pytest.mark.unit
def test_screen_page_shows_the_latest_screen(tmp_path):
    client, app, paths = _client(tmp_path)
    assert "No screen yet" in client.get("/screen").text
    paths.screens.mkdir(parents=True)
    (paths.screens / "screen_2026-10-11.csv").write_text(
        "ticker,name,sector,mf_position,earnings_yield_avg,roic_avg,fcf_yield,fscore,altman_z,mos_formula,trend,"
        "passes_filters,fail_reasons,accounting_flags\n"
        "HRB,H&R Block,Consumer Discretionary,1,0.129,1.34,0.14,7,3.3,0.50,UPTREND,True,,accruals 12%\n"
        "XYZ,Xyz,Industrials,,0.01,0.05,0.01,3,1.0,,DOWNTREND,False,F-Score,\n")
    (paths.screens / "screen_2026-10-11_excluded.csv").write_text("ticker,name,sector\nJPM,JPMorgan,Financials\n")
    (paths.screens / "screen_2026-10-11_skipped.csv").write_text("ticker,reason\nBAD,no annual statements\n")
    r = client.get("/screen")
    assert r.status_code == 200
    for text in ("2026-10-11", "HRB", "50%", "XYZ", "F-Score", "JPM", "no annual statements", "Industrials", "accruals 12%"):
        assert text in r.text, text
    assert r.text.index("HRB") < r.text.index("XYZ")          # ranked first, unranked last


def _wait(runner):
    for _ in range(200):
        if runner.current() is None:
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.mark.unit
def test_job_runner_runs_one_job_at_a_time_and_logs_output(tmp_path):
    from value_pipeline.web.jobs import JobBusy, JobRunner

    runner = JobRunner(tmp_path / "logs")
    release = threading.Event()

    def slow():
        print("working on it")
        release.wait(5)

    job = runner.start("screen", slow)
    with pytest.raises(JobBusy):
        runner.start("watch", lambda: None)
    release.set()
    _wait(runner)
    done = runner.history()[0]
    assert done["name"] == job["name"] and done["status"] == "done"
    assert "working on it" in runner.log_text(done["name"])
    assert runner.log_text("../jobs") is None


@pytest.mark.unit
def test_failed_job_keeps_its_message(tmp_path):
    from value_pipeline.web.jobs import JobRunner

    runner = JobRunner(tmp_path / "logs")

    def boom():
        raise SystemExit("Estimated cost $50.00 exceeds max_cost_per_review $40.00")

    runner.start("review", boom)
    _wait(runner)
    assert runner.history()[0]["status"] == "failed"
    assert "exceeds" in runner.history()[0]["message"]


@pytest.mark.unit
def test_review_needs_a_confirmed_preview(tmp_path):
    calls = []
    client, app, paths = _client(tmp_path, job_funcs={"review": lambda paths, cfg, **kw: calls.append(kw)})
    ops.init_portfolio(paths, 1000.0)
    r = client.post("/run/preview", data={"kind": "review", "tickers": "hog, ingr", "token": app.state.token})
    assert r.status_code == 200 and "2 runs" in r.text and "$4.00" in r.text and "HOG, INGR" in r.text
    assert calls == []
    r = _post(client, app, "/run/start", kind="review", tickers="HOG,INGR")          # not confirmed
    assert r.status_code == 303 and calls == []
    r = _post(client, app, "/run/start", kind="review", tickers="HOG,INGR", confirm="yes")
    assert r.status_code == 303
    _wait(app.state.runner)
    assert calls == [{"only": ["HOG", "INGR"], "dry_run": False}]
    assert client.get("/run").status_code == 200


@pytest.mark.unit
def test_review_over_the_cost_cap_cannot_be_started(tmp_path):
    calls = []
    client, app, paths = _client(tmp_path, job_funcs={"review": lambda paths, cfg, **kw: calls.append(kw)})
    ops.init_portfolio(paths, 1000.0)
    many = ",".join(f"T{i}" for i in range(30))       # 30 x $2 > $40
    r = client.post("/run/preview", data={"kind": "review", "tickers": many, "token": app.state.token})
    assert "exceeds" in r.text and 'name="confirm"' not in r.text
    r = _post(client, app, "/run/start", kind="review", tickers=many, confirm="yes")
    assert r.status_code == 303 and "exceeds" in r.headers["location"].replace("+", " ") and calls == []


@pytest.mark.unit
def test_settings_validate_and_back_up(tmp_path):
    client, app, paths = _client(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("review:\n  cost_per_run: 2.0\n")
    assert "cost_per_run: 2.0" in client.get("/settings").text
    r = _post(client, app, "/settings", text="review: [unclosed")
    assert r.status_code == 400 and "not valid" in r.text and cfg.read_text() == "review:\n  cost_per_run: 2.0\n"
    assert _post(client, app, "/settings", text="- just a list\n").status_code == 400
    assert _post(client, app, "/settings", text="review:\n  cost_per_run: lots\n").status_code == 400
    r = _post(client, app, "/settings", text="review:\n  cost_per_run: 3.0\n")
    assert r.status_code == 303 and "3.0" in cfg.read_text()
    assert list(paths.backups.glob("config_*.yaml"))[0].read_text() == "review:\n  cost_per_run: 2.0\n"


@pytest.mark.unit
def test_forms_refuse_nan_and_inf(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    for field in ("shares", "fees", "price"):
        data = {"ticker": "HRB", "shares": "1", "price": "44", "fees": "0", "date": "", "note": "", field: "nan"}
        assert _post(client, app, "/trade/buy", **data).status_code == 400
    assert _post(client, app, "/trade/deposit", amount="inf", date="").status_code == 400
    assert pfmod.load(paths).cash == 1000.0


@pytest.mark.unit
def test_requests_for_another_host_name_are_refused(tmp_path):
    """DNS rebinding: a hostile page whose name now points at 127.0.0.1 must not reach the site."""
    client, app, paths = _client(tmp_path)
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


@pytest.mark.unit
def test_settings_refuse_a_config_that_would_break_reviews(tmp_path):
    client, app, paths = _client(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("review:\n  cost_per_run: 2.0\n")
    r = _post(client, app, "/settings", text="review: 5\n")
    assert r.status_code == 400 and cfg.read_text() == "review:\n  cost_per_run: 2.0\n"


@pytest.mark.unit
def test_jobs_left_running_by_a_stopped_server_show_as_interrupted(tmp_path):
    from value_pipeline.web.jobs import JobRunner

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "jobs.json").write_text(json.dumps([{"name": "job_1_review", "kind": "review", "started": "x",
                                                 "ended": None, "status": "running", "message": "", "log": "x.log"}]))
    job = JobRunner(logs).history()[0]
    assert job["status"] == "interrupted" and "stopped" in job["message"]


@pytest.mark.unit
def test_stock_and_screen_pages_show_tracked_investors(tmp_path):
    client, app, paths = _client(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "superinvestors.json").write_text(json.dumps({
        "updated": "2026-10-12T09:00:00",
        "investors": [{"name": "Berkshire", "period": "2026-06-30", "filed": "2026-08-14", "holdings": 40}],
        "holdings": {"HRB": [{"investor": "Berkshire", "shares": 1200, "value": 52800, "period": "2026-06-30"}]}}))
    r = client.get("/stock/hrb")
    assert "Berkshire" in r.text and "1,200" in r.text and "2026-06-30" in r.text
    paths.screens.mkdir(parents=True)
    (paths.screens / "screen_2026-10-11.csv").write_text(
        "ticker,sector,mf_position,trend,passes_filters,superinvestors,superinvestor_names\n"
        "HRB,Consumer Discretionary,1,UPTREND,True,1,Berkshire\n")
    r = client.get("/screen")
    assert "Superinvestors" in r.text and "1: Berkshire" in r.text


@pytest.mark.unit
def test_a_corrupt_job_index_neither_blocks_startup_nor_wedges_the_runner(tmp_path):
    from value_pipeline.web.jobs import JobRunner

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "jobs.json").write_text("{not json")
    runner = JobRunner(tmp_path / "logs")
    runner.start("watch", lambda: None)
    _wait(runner)
    assert runner.current() is None
    runner.start("watch", lambda: None)          # not JobBusy
    _wait(runner)
    assert runner.history()[0]["status"] == "done"


@pytest.mark.unit
def test_run_page_shows_a_review_running_in_another_process(tmp_path):
    from value_pipeline import lock

    calls = []
    client, app, paths = _client(tmp_path, job_funcs={"review": lambda paths, cfg, **kw: calls.append(kw)})
    ops.init_portfolio(paths, 1000.0)
    with lock.exclusive(paths, "review"):
        assert "another process" in client.get("/run").text
        r = _post(client, app, "/run/start", kind="review", tickers="HRB", confirm="yes")
        assert r.status_code == 303 and "another" in r.headers["location"]
    _wait(app.state.runner)
    assert calls == []


@pytest.mark.unit
def test_start_date_and_run_tickers_are_validated(tmp_path):
    client, app, paths = _client(tmp_path)
    r = client.post("/start", data={"cash": "1000", "currency": "USD", "date": "10/01/2026", "token": app.state.token})
    assert r.status_code == 400 and not paths.portfolio.exists()
    ops.init_portfolio(paths, 1000.0)
    r = client.post("/run/preview", data={"kind": "review", "tickers": "../x, HRB", "token": app.state.token})
    assert r.status_code == 400 and "ticker" in r.text.lower()
    r = _post(client, app, "/run/start", kind="review", tickers="BRK/B", confirm="yes")
    assert r.status_code == 400
    assert "init --force" in str(pytest.raises(ops.PortfolioExists, ops.init_portfolio, paths, 1.0).value)


@pytest.mark.unit
def test_settings_refuse_numbers_that_disable_the_cost_cap(tmp_path):
    client, app, paths = _client(tmp_path)
    for text in ("review:\n  cost_per_run: 0\n", "review:\n  max_cost_per_review: .nan\n",
                 "review:\n  max_cost_per_review: -5\n", "sizing:\n  max_position_weight: .inf\n"):
        assert _post(client, app, "/settings", text=text).status_code == 400, text
    assert _post(client, app, "/settings", text="review:\n  max_cost_per_review: 60\n").status_code == 303


@pytest.mark.unit
def test_history_lists_same_day_entries_newest_first(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    ops.record_buy(paths, "AAA", 1, 10.0, on="2026-10-02")
    ops.record_buy(paths, "BBB", 1, 10.0, on="2026-10-02")
    import re

    rows = re.findall(r'<a href="/stock/(AAA|BBB)">', client.get("/trades").text)
    assert rows == ["BBB", "AAA"]


@pytest.mark.unit
def test_screen_page_lists_the_latest_other_markets_screen(tmp_path):
    client, app, paths = _client(tmp_path)
    assert "Other markets" not in client.get("/screen").text          # nothing to show without a file
    intl = paths.screens / "intl"
    intl.mkdir(parents=True)
    (intl / "intl_screen_2026-10-20.csv").write_text("ticker,name,region\nOLD.L,Old plc,UK\n")
    (intl / "intl_screen_2026-10-23.csv").write_text(
        "ticker,name,region,sector,market_cap,mf_position,earnings_yield_avg,roic_avg,fscore,altman_z,"
        "mos_formula,trend,passes_filters,fail_reasons\n"
        "000270.KS,Kia Corporation,Korea,Consumer Cyclical,31900000000,14,0.53,0.64,6,3.1,0.68,BASING,True,\n"
        "DGE.L,Diageo plc,UK,Consumer Defensive,53300000000,,0.064,0.43,5,2.1,-0.22,UPTREND,False,F-Score\n")
    r = client.get("/screen")
    assert r.status_code == 200
    for text in ("Other markets", "2026-10-23", "Kia Corporation", "Korea", "68%", "$31.9B", "DGE.L", "F-Score"):
        assert text in r.text, text
    assert "OLD.L" not in r.text                                        # only the newest file
    assert r.text.index("000270.KS") < r.text.index("DGE.L")           # ranked first, unranked last


@pytest.mark.unit
def test_stock_page_shows_checklist_and_kill_criteria(tmp_path):
    client, app, paths = _client(tmp_path)
    row = dict(ROW, entry_price=46.0, kill_criteria=["Revenue falls two years running", "Dividend cut"],
               checklist={"good_business": {"passed": True, "note": "ROIC above 20%"},
                          "good_management": {"passed": False, "note": "serial acquirer"},
                          "cheap": {"passed": True, "note": "Margin of safety: 18%"}},
               scenarios={"bear": {"value": 42.0, "probability": 0.25, "basis": "b"},
                          "base": {"value": 56.0, "probability": 0.5, "basis": "m"},
                          "bull": {"value": 70.0, "probability": 0.25, "basis": "u"},
                          "expected_value": 56.0, "upside_downside": 2.4615384615})
    _review(paths, "2026-10-11", [dict(ROW, rating="Hold")])
    _review(paths, "2026-11-07", [row])
    text = client.get("/stock/HRB").text
    for want in ("Checklist", "Good business", "PASS", "ROIC above 20%", "Good management", "FAIL",
                 "serial acquirer", "Kill criteria", "Dividend cut", "Entry price", "46.00", "Scenarios",
                 "Bear", "25%", "42.00", "Expected value", "56.00", "upside/downside 2.5x"):
        assert want in text, want
    # the newest row lacks nothing; an old row without the keys renders none of the sections
    old = _client(tmp_path / "other")
    _review(old[2], "2026-10-11", [ROW])
    page = old[0].get("/stock/HRB").text
    assert "Kill criteria" not in page and "Checklist" not in page and "Scenarios" not in page


@pytest.mark.unit
def test_reviews_page_shows_the_committee_summary(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    _review(paths, "2026-10-11", [ROW])
    _review(paths, "2026-11-07", [ROW])
    (paths.reports / "2026-11-07" / "committee.json").write_text(json.dumps(
        {"ranking": [], "swaps": [], "concentration": [], "summary": "Trim HRB, add MSFT.", "notes": []}))
    r = client.get("/reviews")
    assert r.status_code == 200 and r.text.count("Trim HRB, add MSFT.") == 1


def _seed_portfolio(client, app):
    client.post("/start", data={"cash": "1000", "currency": "USD", "token": app.state.token},
                follow_redirects=False)


@pytest.mark.unit
def test_dashboard_shows_the_newer_of_watch_and_daily(tmp_path):
    client, app, paths = _client(tmp_path)
    _seed_portfolio(client, app)
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "watch_2026-10-18.md").write_text("WATCHTEXT", encoding="utf-8")
    (paths.reports / "daily_2026-10-24.md").write_text("DAILYTEXT", encoding="utf-8")
    r = client.get("/")
    assert "DAILYTEXT" in r.text and "WATCHTEXT" not in r.text and "Latest check" in r.text
    (paths.reports / "watch_2026-10-25.md").write_text("WATCHTEXT", encoding="utf-8")
    r = client.get("/")
    assert "WATCHTEXT" in r.text and "DAILYTEXT" not in r.text
    (paths.reports / "daily_2026-10-25.md").write_text("DAILYTEXT", encoding="utf-8")
    assert "DAILYTEXT" in client.get("/").text            # a tie goes to the daily check


@pytest.mark.unit
def test_dashboard_empty_state_mentions_both_checks(tmp_path):
    client, app, paths = _client(tmp_path)
    _seed_portfolio(client, app)
    r = client.get("/")
    assert "Saturday watch" in r.text and "weekday daily check" in r.text


@pytest.mark.unit
def test_run_page_offers_the_daily_job(tmp_path):
    client, app, paths = _client(tmp_path)
    assert 'value="daily"' in client.get("/run").text
    assert "daily" in app.state.job_funcs
