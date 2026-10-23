import json
from datetime import date

import pytest

from value_pipeline import (
    cli,
    daily,
    lock,
    market_data,
    metrics,
    operations as ops,
    portfolio as pfmod,
    review,
)
from value_pipeline.config import DEFAULTS, Paths
from value_pipeline.daily import KINDS, Trigger, select, triggers

pytestmark = pytest.mark.unit

TODAY = date(2026, 10, 23)
CFG = {"daily": dict(DEFAULTS["daily"])}


def holding(**kw):
    base = {"ticker": "HRB", "intrinsic_value": 55.0, "entry_price": None, "next_earnings_date": None,
            "last_review_date": "2026-10-01", "last_price": None, "last_trend": "UPTREND"}
    return {**base, **kw}


def idea(**kw):
    base = {"ticker": "DNLM.L", "date": "2026-10-01", "price": 800.0, "trend": "UPTREND",
            "entry_price": 750.0, "intrinsic_value": 1000.0, "earnings_date": None}
    return {**base, **kw}


def kinds(ts):
    return [t.kind for t in ts]


def test_entry_price_fires_for_ideas_and_holdings_with_entry():
    ts = triggers([holding(ticker="A", entry_price=40.0)], [idea()],
                  {"A": 39.0, "DNLM.L": 740.0}, {}, TODAY, CFG)
    assert [(t.ticker, t.kind) for t in ts] == [("A", "ENTRY PRICE"), ("DNLM.L", "ENTRY PRICE")]
    assert ts[1].message == "DNLM.L at 740.00 is at or below its entry price 750.00"


def test_entry_price_quiet_when_above_or_missing():
    assert triggers([], [idea()], {"DNLM.L": 760.0}, {}, TODAY, CFG) == []
    assert triggers([], [idea(entry_price=None)], {"DNLM.L": 700.0}, {}, TODAY, CFG) == []
    assert triggers([holding()], [], {"HRB": 30.0}, {}, TODAY, CFG) == []
    assert triggers([], [idea()], {}, {}, TODAY, CFG) == []


def test_reached_value_fires_for_holdings_only():
    ts = triggers([holding()], [idea(intrinsic_value=700.0, entry_price=None)],
                  {"HRB": 56.10, "DNLM.L": 800.0}, {}, TODAY, CFG)
    assert [(t.ticker, t.kind) for t in ts] == [("HRB", "REACHED VALUE")]
    assert ts[0].message == "HRB at 56.10 is at or above its value 55.00"


def test_reached_value_quiet_below_or_missing():
    assert triggers([holding()], [], {"HRB": 54.0}, {}, TODAY, CFG) == []
    assert triggers([holding(intrinsic_value=None)], [], {"HRB": 99.0}, {}, TODAY, CFG) == []


def test_reached_value_quiet_when_the_last_review_already_saw_it():
    assert triggers([holding(last_price=56.0)], [], {"HRB": 57.0}, {}, TODAY, CFG) == []
    assert kinds(triggers([holding(last_price=54.0)], [], {"HRB": 57.0}, {}, TODAY, CFG)) == [
        "REACHED VALUE"]


def test_entry_price_quiet_when_the_last_review_already_saw_it():
    assert triggers([], [idea(price=740.0)], {"DNLM.L": 745.0}, {}, TODAY, CFG) == []
    assert kinds(triggers([], [idea(price=760.0)], {"DNLM.L": 745.0}, {}, TODAY, CFG)) == [
        "ENTRY PRICE"]
    assert triggers([holding(entry_price=40.0, last_price=39.0)], [], {"HRB": 38.0}, {},
                    TODAY, CFG) == []


def test_freefall_fires_only_on_a_new_freefall():
    ts = triggers([holding()], [], {"HRB": 50.0}, {"HRB": "FREEFALL"}, TODAY, CFG)
    assert kinds(ts) == ["FREEFALL"]
    assert triggers([holding(last_trend="FREEFALL")], [], {"HRB": 50.0},
                    {"HRB": "FREEFALL"}, TODAY, CFG) == []
    assert triggers([holding()], [], {"HRB": 50.0}, {"HRB": "DOWNTREND"}, TODAY, CFG) == []
    assert triggers([holding()], [], {"HRB": 50.0}, {}, TODAY, CFG) == []
    assert kinds(triggers([], [idea(trend=None)], {"DNLM.L": 800.0},
                          {"DNLM.L": "FREEFALL"}, TODAY, CFG)) == ["FREEFALL"]


def test_earnings_fires_once_per_report():
    h = holding(next_earnings_date="2026-10-22", last_review_date="2026-10-01")
    ts = triggers([h], [], {"HRB": 50.0}, {}, TODAY, CFG)
    assert kinds(ts) == ["EARNINGS"]
    assert ts[0].message == "HRB reported on 2026-10-22 (last review 2026-10-01)"
    # reviewed since the report: quiet
    assert triggers([holding(next_earnings_date="2026-10-22", last_review_date="2026-10-23")], [],
                    {"HRB": 50.0}, {}, TODAY, CFG) == []
    # reports today or later: not yet
    assert triggers([holding(next_earnings_date="2026-10-23")], [], {"HRB": 50.0}, {}, TODAY, CFG) == []
    assert triggers([holding(next_earnings_date=None)], [], {"HRB": 50.0}, {}, TODAY, CFG) == []
    assert kinds(triggers([], [idea(earnings_date="2026-10-10")], {"DNLM.L": 800.0},
                          {}, TODAY, CFG)) == ["EARNINGS"]


def test_big_move_fires_at_threshold_either_way():
    ts = triggers([holding(last_price=85.28, intrinsic_value=None)], [], {"HRB": 70.0}, {}, TODAY, CFG)
    assert kinds(ts) == ["BIG MOVE"]
    assert ts[0].message == "HRB moved -18% since its review at 85.28"
    assert kinds(triggers([holding(last_price=100.0, intrinsic_value=None)], [], {"HRB": 115.0}, {}, TODAY, CFG)) == ["BIG MOVE"]
    assert triggers([holding(last_price=100.0, intrinsic_value=None)], [], {"HRB": 110.0}, {}, TODAY, CFG) == []
    assert triggers([holding(last_price=None, intrinsic_value=None)], [], {"HRB": 110.0}, {}, TODAY, CFG) == []


def test_cooldown_holds_back_except_value_and_earnings():
    ts = [Trigger("A", "BIG MOVE", ""), Trigger("B", "REACHED VALUE", ""),
          Trigger("C", "EARNINGS", ""), Trigger("D", "ENTRY PRICE", "")]
    last = dict.fromkeys("ABCD", "2026-10-20")
    last["D"] = "2026-10-18"  # 5 days ago: outside the cooldown
    chosen, deferred = select(ts, last, TODAY, CFG)
    assert chosen == ["D", "B", "C"]
    assert deferred == [("A", "cooldown: reviewed on 2026-10-20")]


def test_selection_order_and_cap():
    ts = [Trigger("Z", "ENTRY PRICE", ""), Trigger("Y", "BIG MOVE", ""), Trigger("X", "FREEFALL", ""),
          Trigger("W", "EARNINGS", ""), Trigger("Y", "REACHED VALUE", ""), Trigger("A", "FREEFALL", "")]
    chosen, deferred = select(ts, {}, TODAY, CFG)
    assert chosen == ["Z", "Y", "W"]
    assert deferred == [("A", "over the daily cap of 3"), ("X", "over the daily cap of 3")]
    assert KINDS[0] == "ENTRY PRICE"


# ---- the run -------------------------------------------------------------------------------

RUN_CFG = {"daily": dict(DEFAULTS["daily"]), "watch": dict(DEFAULTS["watch"])}


def _data(tmp_path, monkeypatch, prices):
    """HRB held (value 55, reviewed 2026-10-01 at 50); DNLM.L an idea with entry 750; SELLME rated Sell."""
    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 10_000.0, on="2026-09-01")
    pf = pfmod.load(paths)
    held = pfmod.apply_buy(pf, "HRB", 10, 50.0, on="2026-09-02", intrinsic_value=55.0,
                           kill_criteria=["Revenue falls two years running", "Net debt above 3x EBITDA"])
    held.last_review_date = "2026-10-01"
    pfmod.save(paths, pf)
    folder = paths.reports / "2026-10-01"
    folder.mkdir(parents=True)
    rows = [{"ticker": "HRB", "rating": "Buy", "price": 50.0, "intrinsic_value": 55.0, "error": None},
            {"ticker": "DNLM.L", "rating": "Overweight", "price": 800.0, "entry_price": 750.0,
             "intrinsic_value": 1000.0, "error": None},
            {"ticker": "SELLME", "rating": "Sell", "price": 10.0, "entry_price": 20.0, "error": None}]
    (folder / "results.json").write_text(json.dumps(rows))
    monkeypatch.setattr(market_data, "latest_price", lambda t: prices.get(t))
    monkeypatch.setattr(market_data, "closes", lambda t: [])
    monkeypatch.setattr(metrics, "trend", lambda closes: {"trend": "UPTREND"})
    return paths


def _no_review(*a, **kw):
    raise AssertionError("review.run must not be called")


def test_daily_dry_run_writes_the_report_and_runs_nothing(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0, "DNLM.L": 740.0, "SELLME": 5.0})
    monkeypatch.setattr(review, "run", _no_review)
    found, out, code = daily.run(paths, RUN_CFG, dry_run=True, notify=False, today=TODAY)
    assert code == 0
    assert out == paths.reports / "daily_2026-10-23.md"
    assert [(t.ticker, t.kind) for t in found] == [("HRB", "REACHED VALUE"), ("DNLM.L", "ENTRY PRICE")]
    text = out.read_text()
    assert text.startswith("# Daily check 2026-10-23")
    assert "nothing is traded" in text.lower()
    assert "| Ticker | Price | Since review | Trend |" in text
    assert "| HRB | 56.00 | +12% | UPTREND |" in text
    assert "| DNLM.L | 740.00 | -7% | UPTREND |" in text
    assert "SELLME" not in text
    assert "- **REACHED VALUE**: HRB at 56.00 is at or above its value 55.00" in text
    assert "Dry run: nothing re-run." in text
    assert "## Kill criteria to check" in text
    assert "- HRB: 1. Revenue falls two years running; 2. Net debt above 3x EBITDA" in text


def test_daily_runs_the_review_on_fired_tickers(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0, "DNLM.L": 740.0})
    calls = []

    def fake_run(p, cfg, *, yes, dry_run, only):
        calls.append((yes, dry_run, only))
        return paths.reports / "2026-10-23" / "ACTIONS.md"

    monkeypatch.setattr(review, "run", fake_run)
    notes = []
    monkeypatch.setattr(daily.watch, "_notify", lambda title, text: notes.append(text))
    found, out, code = daily.run(paths, RUN_CFG, today=TODAY)
    assert code == 0
    assert calls == [(True, False, ["DNLM.L", "HRB"])]
    text = out.read_text()
    assert "- [ACTIONS](/reports/2026-10-23/ACTIONS.html)" in text
    assert "- [DNLM.L](/reports/2026-10-23/DNLM.L/complete_report.html)" in text
    assert "- [HRB](/reports/2026-10-23/HRB/complete_report.html)" in text
    assert notes == ["2 trigger(s): HRB REACHED VALUE; DNLM.L ENTRY PRICE — 2 review(s) run"]


def test_daily_nothing_fired_notifies_so(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 51.0, "DNLM.L": 790.0})
    monkeypatch.setattr(review, "run", _no_review)
    notes = []
    monkeypatch.setattr(daily.watch, "_notify", lambda title, text: notes.append(text))
    found, out, code = daily.run(paths, RUN_CFG, today=TODAY)
    assert (found, code) == ([], 0)
    assert "Nothing fired." in out.read_text()
    assert "None." in out.read_text()
    assert notes == ["Nothing fired"]


def test_daily_skips_reviews_when_the_lock_is_held(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0})

    def locked(*a, **kw):
        raise lock.Locked("another review is running")

    monkeypatch.setattr(review, "run", locked)
    found, out, code = daily.run(paths, RUN_CFG, notify=False, today=TODAY)
    assert code == 0
    assert "a review is already running; triggers are listed, nothing was re-run" in out.read_text()


def test_daily_reports_a_failed_review_and_exits_1(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0})

    def boom(*a, **kw):
        raise RuntimeError("agents fell over")

    monkeypatch.setattr(review, "run", boom)
    found, out, code = daily.run(paths, RUN_CFG, notify=False, today=TODAY)
    assert code == 1
    assert "review failed: agents fell over" in out.read_text()


def test_daily_disabled_runs_nothing(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0})
    monkeypatch.setattr(review, "run", _no_review)
    cfg = {**RUN_CFG, "daily": {**RUN_CFG["daily"], "enabled": False}}
    found, out, code = daily.run(paths, cfg, notify=False, today=TODAY)
    assert code == 0 and [t.ticker for t in found] == ["HRB"]
    assert "Disabled in config." in out.read_text()


def test_daily_max_and_cooldown_defer_and_bad_dates_do_not_abort(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0, "DNLM.L": 740.0})
    pf = pfmod.load(paths)
    pf.positions[0].last_review_date = "not-a-date"
    pfmod.save(paths, pf)
    monkeypatch.setattr(review, "run", _no_review)
    found, out, code = daily.run(paths, RUN_CFG, dry_run=True, notify=False, max_reviews=1, today=TODAY)
    assert code == 0
    assert "## Deferred" in out.read_text()
    assert "- HRB: over the daily cap of 1" in out.read_text()


def test_cli_daily_exit_code(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0})
    monkeypatch.setattr(review, "run", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--data-dir", str(paths.root), "daily", "--no-notify", "--max", "2"])
    assert exc.value.code == 1
    cli.main(["--data-dir", str(paths.root), "daily", "--no-notify", "--dry-run"])


def test_daily_freefall_fires_only_on_entering_it(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 51.0, "DNLM.L": 790.0})
    monkeypatch.setattr(metrics, "trend", lambda closes: {"trend": "FREEFALL"})
    found, _, _ = daily.run(paths, RUN_CFG, dry_run=True, notify=False, today=TODAY)
    assert [(t.ticker, t.kind) for t in found] == [("HRB", "FREEFALL"), ("DNLM.L", "FREEFALL")]
    folder = paths.reports / "2026-10-01"
    rows = json.loads((folder / "results.json").read_text())
    for r in rows:
        r["trend"] = "FREEFALL"
    (folder / "results.json").write_text(json.dumps(rows))
    found, _, _ = daily.run(paths, RUN_CFG, dry_run=True, notify=False, today=TODAY)
    assert found == []


def test_daily_links_no_report_for_an_errored_ticker(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0, "DNLM.L": 740.0})

    def fake_run(p, cfg, *, yes, dry_run, only):
        folder = paths.reports / "2026-10-23"
        folder.mkdir()
        (folder / "results.json").write_text(json.dumps([
            {"ticker": "DNLM.L", "rating": "REVIEW", "error": "timed out"},
            {"ticker": "HRB", "rating": "Buy", "error": None}]))
        return folder / "ACTIONS.md"

    monkeypatch.setattr(review, "run", fake_run)
    _, out, code = daily.run(paths, RUN_CFG, notify=False, today=TODAY)
    text = out.read_text()
    assert code == 0
    assert "- [HRB](/reports/2026-10-23/HRB/complete_report.html)" in text
    assert "DNLM.L/complete_report.html" not in text


def test_daily_negative_max_runs_nothing(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0, "DNLM.L": 740.0})
    monkeypatch.setattr(review, "run", _no_review)
    found, out, code = daily.run(paths, RUN_CFG, notify=False, max_reviews=-1, today=TODAY)
    assert code == 0 and len(found) == 2
    text = out.read_text()
    assert "- HRB: over the daily cap of 0" in text
    assert "- DNLM.L: over the daily cap of 0" in text


def test_web_daily_job_fails_when_the_review_failed(tmp_path, monkeypatch):
    from value_pipeline.web import app as webapp

    monkeypatch.setattr(daily, "run", lambda paths, cfg, notify: ([], tmp_path / "d.md", 1))
    job = webapp._default_job_funcs()["daily"]
    with pytest.raises(RuntimeError, match="the daily re-review failed"):
        job(Paths(tmp_path), RUN_CFG)
    monkeypatch.setattr(daily, "run", lambda paths, cfg, notify: ([], tmp_path / "d.md", 0))
    job(Paths(tmp_path), RUN_CFG)


def test_daily_lists_an_inflection_holdings_demand_criteria_first(tmp_path, monkeypatch):
    paths = _data(tmp_path, monkeypatch, {"HRB": 56.0, "DNLM.L": 740.0, "SELLME": 5.0})
    pf = pfmod.load(paths)
    pf.position("HRB").lane = "inflection"
    pf.position("HRB").kill_criteria = ["Net debt above 3x EBITDA", "Backlog falls year on year"]
    pfmod.save(paths, pf)
    monkeypatch.setattr(review, "run", _no_review)
    _, out, _ = daily.run(paths, RUN_CFG, dry_run=True, notify=False, today=TODAY)
    assert "- HRB: 1. Backlog falls year on year; 2. Net debt above 3x EBITDA" in out.read_text()
