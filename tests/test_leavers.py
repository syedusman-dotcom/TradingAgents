"""``backtest --leavers``: names that left the S&P 500 are members until their leave date."""

import json
from datetime import date

import pytest

from tests.test_lane_backtest import (
    _bt_cfg,
    _e2e_added,
    _e2e_hist,
    _e2e_setup,
    _e2e_store,
    _e2e_universe,
    _fake_result,
    _no_wiki,
    _write_caches,
)
from value_pipeline import backtest
from value_pipeline.config import Paths


def _changes_html(rows) -> str:
    """The constituents page with the changes table: ``(date, added, removed, reason)``."""
    head = ("<tr><th rowspan=2>Date</th><th colspan=2>Added</th><th colspan=2>Removed</th>"
            "<th rowspan=2>Reason</th></tr>"
            "<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>")
    body = "".join(f"<tr><td>{d}</td><td>{a}</td><td>{a} Inc</td><td>{r}</td><td>{r} Inc</td>"
                   f"<td>{why}</td></tr>" for d, a, r, why in rows)
    return ("<html><body><table><tr><th>Symbol</th><th>Date added</th></tr>"
            "<tr><td>AAA</td><td>1990-01-01</td></tr></table>"
            f"<table>{head}{body}</table></body></html>")


CHANGES_HTML = _changes_html([
    ("October 1, 2026", "NEW", "OLD", "Market capitalization change."),
    ("September 23, 2024", "", "BRK.B", "Acquired by Someone Else."),
    ("March 4, 2020", "ADD", "", "Replacing a company."),
    ("June 30, 2015", "", "OLD", "Earlier removal of the same name."),
    ("not a date", "", "BAD", "Unreadable date."),
])


def test_removed_dates_reads_the_changes_table():
    urls = []

    def fetch(url):
        urls.append(url)
        return CHANGES_HTML

    assert backtest.removed_dates(fetch) == {
        "OLD": {"left": "2026-10-01", "reason": "Market capitalization change."},
        "BRK-B": {"left": "2024-09-23", "reason": "Acquired by Someone Else."}}
    assert urls == [backtest.market_data.MARKETS["sp500"].pages[0]]


def test_removed_dates_is_empty_when_the_page_cannot_be_read():
    def boom(url):
        raise RuntimeError("offline")

    assert backtest.removed_dates(boom) == {}
    assert backtest.removed_dates(lambda url: "<table><tr><th>X</th></tr><tr><td>1</td></tr></table>") == {}
    assert backtest.removed_dates(lambda url: _changes_html([]).split("<table>")[1]) == {}


def test_leave_dates_cache(tmp_path):
    paths = Paths(tmp_path / "data")
    cache = paths.cache / "backtest" / "removed_dates.json"
    left = {"OLD": {"left": "2024-01-02", "reason": "acquired"}}
    assert backtest._leave_dates(paths, False, lambda: pytest.fail("no fetch")) == {}
    assert backtest._leave_dates(paths, True, lambda: left) == left
    assert json.loads(cache.read_text(encoding="utf-8")) == left
    assert backtest._leave_dates(paths, False, lambda: pytest.fail("no fetch")) == left
    assert backtest._leave_dates(paths, True, lambda: {}) == left        # empty keeps the cache
    assert json.loads(cache.read_text(encoding="utf-8")) == left


def test_ticker_rows_skip_cohorts_on_or_after_the_leave_date():
    cohorts = ["2022-06-30", "2023-06-30", "2024-06-30"]
    rates = dict.fromkeys(cohorts, 0.09)
    args = ("OLD", None, None, _e2e_store(), _e2e_hist(), cohorts, rates, {}, _bt_cfg())
    kept, _ = backtest._ticker_rows(*args, left={"left": "2023-06-30", "reason": "x"})
    assert list(kept) == ["2022-06-30"]
    kept, _ = backtest._ticker_rows(*args, left={"left": "2024-01-01", "reason": "x"})
    assert list(kept) == ["2022-06-30", "2023-06-30"]
    kept, _ = backtest._ticker_rows(*args)
    assert list(kept) == cohorts


LONG = "Acquired by A Very Large Company Incorporated Today."
REMOVED = {"LEFT": {"left": "2023-01-10", "reason": "Merged."},
           "GONE": {"left": "2023-01-10", "reason": LONG},
           "LOST": {"left": "2023-01-10", "reason": "Bankruptcy."},
           "AAA": {"left": "2023-01-10", "reason": "Left and rejoined."},
           "BACK": {"left": "2022-12-01", "reason": "Left and rejoined."}}


def _setup(tmp_path, monkeypatch):
    """The end-to-end caches plus: LEFT (full data), GONE (prices only), LOST (statements only)
    and BACK (full data, but rejoined in 2023); AAA, a member today, is also listed as removed."""
    paths, cfg = _e2e_setup(tmp_path)
    cfg["universe"]["indexes"] = list(backtest.DEFAULT_INDEXES)
    for t in ("LEFT", "BACK"):
        _write_caches(paths, t, _e2e_store(), _e2e_hist())
    _write_caches(paths, "GONE", hist=_e2e_hist())
    _write_caches(paths, "LOST", store=_e2e_store())
    cache = paths.cache / "backtest"
    (cache / "added_dates.json").write_text(
        json.dumps({**_e2e_added(), "BACK": "2023-03-01"}), encoding="utf-8")
    (cache / "removed_dates.json").write_text(json.dumps(REMOVED), encoding="utf-8")
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)
    monkeypatch.setattr(backtest, "removed_dates", _no_wiki)
    monkeypatch.setattr(backtest.market_data, "throttle", lambda s: pytest.fail("no Yahoo fetch"))
    return paths, cfg


def _run(paths, cfg, **kw):
    return backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False,
                        universe_fn=_e2e_universe, **kw)


def test_run_adds_leavers_until_their_leave_date(tmp_path, monkeypatch):
    paths, cfg = _setup(tmp_path, monkeypatch)
    base = _run(paths, cfg)
    result = _run(paths, cfg, leavers=True)
    assert base["leavers"] is None
    assert base["rows_per_cohort"] == {"2022-06-30": 1, "2023-06-30": 2}
    # LEFT is a member in 2022 only. BACK rejoined and AAA is a member today: neither is a leaver.
    assert result["rows_per_cohort"] == {"2022-06-30": 2, "2023-06-30": 2}
    assert result["universe_size"] == base["universe_size"] + 3
    assert result["with_data"] == base["with_data"] + 1
    assert result["leavers"] == {
        "included": 1, "no_facts": 1, "no_prices": 1,
        "reasons": {LONG.lower()[:40]: 1, "bankruptcy.": 1}}
    assert result["skipped"]["GONE"] == "no EDGAR statements"
    assert result["skipped"]["LOST"] == "no price history"
    assert "BACK" not in result["skipped"] and "LEFT" not in result["skipped"]


def test_leavers_need_the_sp500_in_the_lists(tmp_path, monkeypatch):
    paths, cfg = _setup(tmp_path, monkeypatch)
    cfg["universe"]["indexes"] = ["sp400"]
    assert _run(paths, cfg, leavers=True)["leavers"] is None


def test_the_report_names_the_leavers_and_the_file_says_so(tmp_path, monkeypatch):
    paths, cfg = _setup(tmp_path, monkeypatch)
    result = _run(paths, cfg, leavers=True)
    text = backtest.markdown(result)
    tested = text.split("## What was tested")[1].split("## Verdict")[0]
    assert ("Leavers: 1 names that left the S&P 500 since 2013 were added as members until "
            "their leave date; 2 could not be (no EDGAR facts: 1; no price history: 1), mostly "
            f"{LONG.lower()[:40]}, bankruptcy. Leavers carry no sector, so the sector "
            "exclusions did not apply to them.") in tested
    md, csv = backtest.write(paths, result, "2026-11-07")
    assert md.name == "backtest_2026-11-07_leavers.md"
    assert csv.name == "backtest_2026-11-07_leavers_picks.csv"
    result["indexes"] = ["sp500"]
    assert backtest.write(paths, result, "2026-11-07")[0].name == "backtest_2026-11-07_sp500_leavers.md"
    plain = _fake_result()
    assert "Leavers:" not in backtest.markdown(plain)
    assert backtest.write(paths, plain, "2026-11-08")[0].name == "backtest_2026-11-08.md"
