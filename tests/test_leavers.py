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


def _changes_html(rows, date_header: str = "Effective Date") -> str:
    """The constituents page with the changes table: ``(date, added, removed, reason)``."""
    head = (f"<tr><th rowspan=2>{date_header}</th><th colspan=2>Added</th><th colspan=2>Removed</th>"
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
        "OLD": {"left": "2026-10-01", "reason": "Market capitalization change.", "name": "OLD Inc",
                "intervals": [[None, "2015-06-30"], [None, "2026-10-01"]]},
        "BRK-B": {"left": "2024-09-23", "reason": "Acquired by Someone Else.", "name": "BRK.B Inc",
                  "intervals": [[None, "2024-09-23"]]}}
    assert urls == [backtest.CHANGES_URL]


def test_removed_dates_give_each_removal_its_most_recent_add():
    html = _changes_html([
        ("May 1, 2023", "", "TWICE", "Second removal."),
        ("March 19, 2018", "TWICE", "", "Rejoined."),
        ("June 30, 2015", "", "TWICE", "First removal."),
        ("April 2, 2012", "TWICE", "", "First add."),
        ("March 5, 2018", "LATE", "", "Joined."),
        ("July 1, 2021", "", "LATE", "Left."),
    ])
    out = backtest.removed_dates(lambda url: html)
    assert out["TWICE"]["intervals"] == [["2012-04-02", "2015-06-30"], ["2018-03-19", "2023-05-01"]]
    assert out["LATE"]["intervals"] == [["2018-03-05", "2021-07-01"]]


def test_removed_dates_reads_the_pinned_revision_and_either_date_header():
    seen = []

    def fetch(url):
        seen.append(url)
        return _changes_html([("May 5, 2020", "A", "GONE", "Acquired.")], date_header="Date")

    assert backtest.removed_dates(fetch) == {"GONE": {"left": "2020-05-05", "reason": "Acquired.",
                                                      "name": "GONE Inc", "intervals": [[None, "2020-05-05"]]}}
    assert seen == [backtest.CHANGES_URL]


def test_removed_dates_is_empty_when_the_page_cannot_be_read():
    def boom(url):
        raise RuntimeError("offline")

    assert backtest.removed_dates(boom) == {}
    assert backtest.removed_dates(lambda url: "<table><tr><th>X</th></tr><tr><td>1</td></tr></table>") == {}
    assert backtest.removed_dates(lambda url: _changes_html([]).split("<table>")[1]) == {}


def test_leave_dates_cache(tmp_path):
    paths = Paths(tmp_path / "data")
    cache = paths.cache / "backtest" / backtest.LEAVE_DATES_CACHE
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


def test_ticker_rows_keep_a_leaver_to_its_membership_intervals():
    cohorts = ["2022-06-30", "2023-06-30", "2024-06-30"]
    rates = dict.fromkeys(cohorts, 0.09)
    args = ("OLD", None, None, _e2e_store(), _e2e_hist(), cohorts, rates, {}, _bt_cfg())
    # Joined in 2023 and left in 2024: not a member in 2022, before it joined.
    joined = {"left": "2024-01-01", "reason": "x", "intervals": [["2023-01-01", "2024-01-01"]]}
    assert list(backtest._ticker_rows(*args, left=joined)[0]) == ["2023-06-30"]
    # Left in 2022, back in 2024 and left again: the gap between is not membership.
    gap = {"left": "2025-01-01", "reason": "x",
           "intervals": [[None, "2022-12-01"], ["2024-03-01", "2025-01-01"]]}
    assert list(backtest._ticker_rows(*args, left=gap)[0]) == ["2022-06-30", "2024-06-30"]


def test_leavers_gone_before_the_first_cohort_are_not_loaded(tmp_path):
    paths = Paths(tmp_path / "data")
    removed = {"EARLY": {"left": "2001-03-01", "reason": "x"}, "LATER": {"left": "2023-01-10", "reason": "y"}}
    gone = backtest._leavers(paths, True, lambda: removed, [], {}, None, ["2015-06-30", "2016-06-30"])
    assert list(gone) == ["LATER"]


LONG = "Acquired by A Very Large Company Incorporated Today."


def _gone(left, reason, name):
    return {"left": left, "reason": reason, "name": name, "intervals": [[None, left]]}


REMOVED = {"LEFT": _gone("2023-01-10", "Merged.", "Left Industries Inc."),
           "GONE": _gone("2023-01-10", LONG, "Gone Co"),
           "LOST": _gone("2023-01-10", "Bankruptcy.", "Lost Corp"),
           "AAA": _gone("2023-01-10", "Left and rejoined.", "A Inc"),
           "BACK": _gone("2022-12-01", "Left and rejoined.", "Back Inc")}


def _name_store(paths, ticker, name):
    """Give a cached fact store the EDGAR entity name ``facts_for`` keeps."""
    path = paths.cache / "backtest" / "facts" / f"{ticker}.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**store, "name": name}), encoding="utf-8")


def _setup(tmp_path, monkeypatch):
    """The end-to-end caches plus: LEFT (full data), GONE (prices only), LOST (statements only)
    and BACK (full data, but rejoined in 2023); AAA, a member today, is also listed as removed."""
    paths, cfg = _e2e_setup(tmp_path)
    cfg["universe"]["indexes"] = list(backtest.DEFAULT_INDEXES)
    for t in ("LEFT", "BACK"):
        _write_caches(paths, t, _e2e_store(), _e2e_hist())
    _write_caches(paths, "GONE", hist=_e2e_hist())
    _write_caches(paths, "LOST", store=_e2e_store())
    for t, name in (("LEFT", "LEFT INDUSTRIES INC"), ("BACK", "Back Inc"), ("LOST", "Lost Corp")):
        _name_store(paths, t, name)
    cache = paths.cache / "backtest"
    (cache / "added_dates.json").write_text(
        json.dumps({**_e2e_added(), "BACK": "2023-03-01"}), encoding="utf-8")
    (cache / backtest.LEAVE_DATES_CACHE).write_text(json.dumps(REMOVED), encoding="utf-8")
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


def test_a_leaver_whose_ticker_now_names_another_issuer_is_not_replayed(tmp_path, monkeypatch):
    paths, cfg = _setup(tmp_path, monkeypatch)
    _name_store(paths, "LEFT", "Newco Holdings Inc")        # the symbol was reused
    result = _run(paths, cfg, leavers=True)
    assert result["leavers"]["included"] == 0 and result["leavers"]["no_facts"] == 2
    assert result["leavers"]["reasons"]["ticker reused"] == 1
    assert result["skipped"]["LEFT"] == "ticker reused"
    assert result["rows_per_cohort"] == {"2022-06-30": 1, "2023-06-30": 2}


def test_a_leaver_whose_prices_start_after_it_left_is_not_replayed(tmp_path, monkeypatch):
    paths, cfg = _setup(tmp_path, monkeypatch)
    hist = _e2e_hist()
    _write_caches(paths, "LEFT", hist=hist[hist["date"] > "2023-02-01"].reset_index(drop=True))
    result = _run(paths, cfg, leavers=True)
    assert result["skipped"]["LEFT"] == "ticker reused" and result["leavers"]["included"] == 0


def test_leavers_without_any_leave_dates_raise(tmp_path, monkeypatch):
    paths, cfg = _setup(tmp_path, monkeypatch)
    (paths.cache / "backtest" / backtest.LEAVE_DATES_CACHE).unlink()
    with pytest.raises(RuntimeError, match="leave dates"):
        _run(paths, cfg, leavers=True)
    monkeypatch.setattr(backtest, "removed_dates", lambda fetch=None: {})     # an empty fetch
    monkeypatch.setattr(backtest, "_join_dates", lambda *a: _e2e_added())
    with pytest.raises(RuntimeError, match="leave dates"):
        backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=True, universe_fn=_e2e_universe,
                     leavers=True, removed_fn=lambda: {}, tickers=["NONE"])


def test_the_cli_reports_missing_leave_dates_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    from value_pipeline import cli

    def no_dates(*a, **kw):
        raise cli.backtest.LeaveDatesUnavailable("--leavers: no S&P 500 leave dates")

    monkeypatch.setattr(cli.backtest, "run", no_dates)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--data-dir", str(tmp_path / "data"), "backtest", "--leavers", "--no-fetch"])
    assert exc.value.code not in (0, None)
    assert "no S&P 500 leave dates" in str(exc.value.code) + capsys.readouterr().out


def test_the_cli_does_not_swallow_other_runtime_errors(tmp_path, monkeypatch):
    from value_pipeline import cli

    def boom(*a, **kw):
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(cli.backtest, "run", boom)
    with pytest.raises(RuntimeError, match="dictionary changed"):
        cli.main(["--data-dir", str(tmp_path / "data"), "backtest", "--no-fetch"])


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
            "exclusions did not apply to them. Removals after 2025-12-22 are not in the table, so "
            "names that left since are still missing.") in tested
    md, csv = backtest.write(paths, result, "2026-11-07")
    assert md.name == "backtest_2026-11-07_leavers.md"
    assert csv.name == "backtest_2026-11-07_leavers_picks.csv"
    result["indexes"] = ["sp500"]
    assert backtest.write(paths, result, "2026-11-07")[0].name == "backtest_2026-11-07_sp500_leavers.md"
    plain = _fake_result()
    assert "Leavers:" not in backtest.markdown(plain)
    assert backtest.write(paths, plain, "2026-11-08")[0].name == "backtest_2026-11-08.md"


def test_a_leavers_store_without_the_edgar_name_is_refetched(tmp_path, monkeypatch):
    calls = []

    def facts(ticker, paths, fetch=True, refresh=False, stale_before=None):
        calls.append(refresh)
        return {"facts": {}, **({"name": "Left Inc"} if refresh else {})}

    monkeypatch.setattr(backtest, "facts_for", facts)
    monkeypatch.setattr(backtest, "price_history", lambda *a, **k: None)
    monkeypatch.setattr(backtest.market_data, "throttle", lambda s: None)
    store, _ = backtest._load("LEFT", Paths(tmp_path / "data"), _bt_cfg(), False, False, need_name=True)
    assert calls == [False] and "name" not in store            # no fetch: the cache as it is
    calls.clear()
    store, _ = backtest._load("LEFT", Paths(tmp_path / "data"), _bt_cfg(), True, False, need_name=True)
    assert calls == [False, True] and store["name"] == "Left Inc"
