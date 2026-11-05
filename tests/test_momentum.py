"""Momentum sleeve engine, fixed verdict and fund record on synthetic prices (no network)."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from value_pipeline import backtest, momentum, trend
from value_pipeline.config import DEFAULTS, Paths, validate_config

pytestmark = pytest.mark.unit

WARM = 300


def _index(n: int, first: str = "2018-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(first, periods=n)


def _spy(index: pd.DatetimeIndex, daily: float = 0.0005) -> pd.Series:
    return pd.Series(100.0 * (1 + daily) ** np.arange(len(index)), index=index)


def _rising(index: pd.DatetimeIndex, daily: float) -> np.ndarray:
    return 100.0 * (1 + daily) ** np.arange(len(index))


def _frame(columns: dict, index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({k: np.asarray(v, dtype=float) for k, v in columns.items()}, index=index)


def _twelve(n: int = 520):
    """Twelve names rising 0.08% to 0.96% a day (N00 flattest), SPY steady; start at row 300."""
    idx = _index(n)
    names = {f"N{i:02d}": _rising(idx, 0.0008 * (i + 1)) for i in range(12)}
    return _frame(names, idx), _spy(idx), idx


OLD = "2000-01-01"


def _m() -> dict:
    return {**DEFAULTS["momentum"]}


# --- universe_equity -------------------------------------------------------------------------

def test_universe_equity_is_the_mean_daily_return_of_joined_names():
    idx = _index(6)
    closes = _frame({"A": 100 * 1.01 ** np.arange(6), "B": 100 * 1.03 ** np.arange(6)}, idx)
    eq = momentum.universe_equity(closes, {"A": OLD, "B": OLD}, str(idx[0].date()))
    assert eq.iloc[0] == 1.0 and len(eq) == 6
    assert eq.iloc[-1] == pytest.approx(1.02 ** 5)


def test_universe_equity_adds_a_name_the_day_after_its_join_date():
    idx = _index(6)
    closes = _frame({"A": 100 * 1.01 ** np.arange(6), "C": 100 * 1.05 ** np.arange(6)}, idx)
    added = {"A": OLD, "C": str(idx[2].date())}
    eq = momentum.universe_equity(closes, added, str(idx[0].date()))
    returns = eq.pct_change().dropna().to_numpy()
    # joined on day 2: in the mean from day 3's return (day 2 -> 3) on
    assert returns[:2] == pytest.approx([0.01, 0.01])          # days 1, 2: A alone
    assert returns[2:] == pytest.approx([0.03, 0.03, 0.03])    # days 3 to 5: A and C


def test_universe_equity_ignores_unjoined_and_missing_closes_and_empty_days():
    idx = _index(5)
    a = 100 * 1.01 ** np.arange(5)
    b = 100 * 1.10 ** np.arange(5)
    b[2] = np.nan                                     # no close: B out on days 2 and 3
    closes = _frame({"A": a, "B": b, "NOJOIN": 100 * 2.0 ** np.arange(5)}, idx)
    eq = momentum.universe_equity(closes, {"A": OLD, "B": OLD}, str(idx[0].date()))
    returns = eq.pct_change().dropna().to_numpy()
    assert returns == pytest.approx([0.055, 0.01, 0.01, 0.055])
    empty = momentum.universe_equity(closes, {}, str(idx[0].date()))
    assert (empty == 1.0).all()


def test_universe_equity_starts_at_the_first_row_on_or_after_start():
    idx = _index(8)
    closes = _frame({"A": 100 * 1.01 ** np.arange(8)}, idx)
    eq = momentum.universe_equity(closes, {"A": OLD}, str(idx[3].date()))
    assert eq.index[0] == idx[3] and eq.iloc[0] == 1.0
    eq = momentum.universe_equity(closes, {"A": OLD}, "2018-01-06")     # a Saturday
    assert eq.index[0] == idx[5]


# --- sleeve ----------------------------------------------------------------------------------

def _start(idx) -> str:
    return str(idx[WARM].date())


def test_sleeve_holds_the_strongest_and_never_the_two_flattest():
    closes, spy, idx = _twelve()
    added = dict.fromkeys(closes.columns, OLD)
    out = momentum.sleeve(closes, spy, added, _m(), _start(idx), positions=10)
    traded = {t["ticker"] for t in out["trades"]}
    assert traded and traded.isdisjoint({"N00", "N01"})
    m = out["metrics"]
    assert m["positions"] == 10 and m["cost_per_side"] == 0.005
    assert m["market_filter"] is True
    assert {"universe_cagr", "universe_max_drawdown", "years_ahead", "years_counted"} <= set(m)
    assert m["start"] == _start(idx) and m["end"] == str(idx[-1].date())
    assert m["years_counted"] >= 1 and 0 <= m["years_ahead"] <= m["years_counted"]


def test_sleeve_never_trades_a_name_joined_after_the_last_row_or_absent_from_added():
    closes, spy, idx = _twelve()
    added = dict.fromkeys(closes.columns, OLD)
    added["N11"] = "2099-01-01"
    del added["N10"]
    out = momentum.sleeve(closes, spy, added, _m(), _start(idx))
    traded = {t["ticker"] for t in out["trades"]}
    assert "N11" not in traded and "N10" not in traded and traded


def test_sleeve_with_four_eligible_names_holds_at_most_four():
    closes, spy, idx = _twelve()
    added = {f"N{i:02d}": OLD for i in range(4)}
    out = momentum.sleeve(closes, spy, added, _m(), _start(idx), positions=10)
    assert {t["ticker"] for t in out["trades"]} <= set(added) and out["trades"]
    open_now = [t for t in out["trades"] if t["reason"] == "open"]
    assert len(open_now) <= 4


def test_sleeve_end_matches_frames_cut_by_hand():
    closes, spy, idx = _twelve()
    added = dict.fromkeys(closes.columns, OLD)
    end = str(idx[420].date())
    cut = momentum.sleeve(closes, spy, added, _m(), _start(idx), end=end)
    by_hand = momentum.sleeve(closes.loc[:end], spy.loc[:end], added, _m(), _start(idx))
    assert cut["metrics"]["end"] <= end
    assert cut["metrics"] == by_hand["metrics"] and cut["trades"] == by_hand["trades"]


def test_sleeve_higher_cost_gives_a_lower_cagr():
    closes, spy, idx = _twelve()
    added = dict.fromkeys(closes.columns, OLD)
    cheap = momentum.sleeve(closes, spy, added, _m(), _start(idx), cost_per_side=0.0)
    dear = momentum.sleeve(closes, spy, added, _m(), _start(idx), cost_per_side=0.0075)
    assert dear["metrics"]["cagr"] < cheap["metrics"]["cagr"]
    assert dear["metrics"]["cost_per_side"] == 0.0075


# --- verdict ---------------------------------------------------------------------------------

def _mm(cagr, spy, universe, dd, spy_dd, ahead, counted) -> dict:
    return {"cagr": cagr, "spy_cagr": spy, "universe_cagr": universe, "max_drawdown": dd,
            "spy_max_drawdown": spy_dd, "years_ahead": ahead, "years_counted": counted}


def _passing() -> dict:
    return {
        "recent": _mm(0.12, 0.10, 0.11, 0.25, 0.20, 6, 10),
        "holdout": _mm(0.08, 0.08, 0.08, 0.25, 0.20, 5, 10),
        "recent_check": _mm(0.11, 0.10, 0.10, 0.2, 0.2, 0, 0),
        "holdout_check": _mm(0.08, 0.08, 0.08, 0.2, 0.2, 0, 0),
    }


def test_verdict_passes_exactly_at_every_bound():
    out = momentum.verdict(**_passing())
    assert out["supports"] is True
    assert out["checks"] == {str(n): True for n in range(1, 7)}
    assert out["years_ahead"] == 11 and out["years_counted"] == 20
    assert out["sentence"].startswith("The test supports a small live sleeve: ")
    assert "11 of 20 calendar years" in out["sentence"]
    assert "Failed" not in out["sentence"]


def _past(case: str) -> dict:
    d = _passing()
    if case == "1":
        d["recent"]["spy_cagr"] = 0.1001
    elif case == "2":
        d["recent"]["universe_cagr"] = 0.1101
    elif case == "3":
        d["holdout"]["spy_cagr"] = 0.0801
    elif case == "4":
        d["holdout"]["max_drawdown"] = 0.2501
    elif case == "5":
        d["recent"]["years_ahead"] = 5
    elif case == "6":
        d["recent_check"]["cagr"] = 0.1099
    return d


@pytest.mark.parametrize("n", ["1", "2", "3", "4", "5", "6"])
def test_verdict_fails_alone_on_each_condition_just_past_its_bound(n):
    out = momentum.verdict(**_past(n))
    assert out["supports"] is False
    assert {k for k, v in out["checks"].items() if not v} == {n}
    assert out["sentence"].startswith("The test does not support a live sleeve: ")
    assert f"Failed: ({n})" in out["sentence"]
    for other in "123456":
        if other != n:
            assert f"({other})" not in out["sentence"]


# --- fund_record -----------------------------------------------------------------------------

def _hist(dates, values) -> pd.DataFrame:
    return pd.DataFrame({"date": pd.DatetimeIndex(dates).strftime("%Y-%m-%d"), "close": values,
                         "adj_close": values, "split": 0.0})


def _patch_history(monkeypatch, table: dict):
    calls = []

    def fake(ticker, paths, fetch=True, refresh=False, stale_before=None):
        calls.append((ticker, fetch, stale_before))
        return table.get(ticker)

    monkeypatch.setattr(backtest, "price_history", fake)
    return calls


PAIR = {"fund": "FND", "parent": "PAR", "label": "A fund against its parent"}


def test_fund_record_compares_cagr_and_drawdown_on_common_dates(tmp_path, monkeypatch):
    dates = pd.to_datetime(["2020-01-01", "2021-01-01", "2022-01-01"])
    calls = _patch_history(monkeypatch, {"FND": _hist(dates, [100.0, 80.0, 200.0]),
                                         "PAR": _hist(dates, [100.0, 110.0, 121.0])})
    (row,) = momentum.fund_record(Paths(tmp_path / "data"), [PAIR], fetch=True,
                                  today=date(2026, 11, 5))
    assert row["fund_cagr"] == pytest.approx(2 ** 0.5 - 1, abs=1e-3)
    assert row["parent_cagr"] == pytest.approx(0.10, abs=1e-3)
    assert row["excess"] == pytest.approx(row["fund_cagr"] - row["parent_cagr"])
    assert row["fund_max_drawdown"] == pytest.approx(0.2) and row["parent_max_drawdown"] == 0.0
    assert row["start"] == "2020-01-01" and row["end"] == "2022-01-01"
    assert row["years"] == pytest.approx(731 / 365.25) and row["error"] is None
    assert row["label"] == PAIR["label"] and row["fund"] == "FND" and row["parent"] == "PAR"
    assert {c[2] for c in calls} == {"2026-10-29"}


def test_fund_record_starts_on_the_later_first_date(tmp_path, monkeypatch):
    dates = pd.to_datetime(["2020-01-01", "2021-01-01", "2022-01-01"])
    _patch_history(monkeypatch, {"FND": _hist(dates, [100.0, 150.0, 200.0]),
                                 "PAR": _hist(dates[1:], [100.0, 110.0])})
    (row,) = momentum.fund_record(Paths(tmp_path / "data"), [PAIR], today=date(2026, 11, 5))
    assert row["start"] == "2021-01-01"


def test_fund_record_names_a_missing_history_and_passes_fetch_through(tmp_path, monkeypatch):
    dates = pd.to_datetime(["2020-01-01", "2021-01-01"])
    calls = _patch_history(monkeypatch, {"PAR": _hist(dates, [100.0, 110.0])})
    (row,) = momentum.fund_record(Paths(tmp_path / "data"), [{**PAIR, "fund": "XXX"}],
                                  fetch=False, today=date(2026, 11, 5))
    assert row["error"] == "no price history for XXX"
    assert row["fund_cagr"] is None and row["parent_cagr"] is None and row["excess"] is None
    assert all(c[1] is False for c in calls)


def test_fund_record_without_two_common_dates(tmp_path, monkeypatch):
    _patch_history(monkeypatch, {"FND": _hist(pd.to_datetime(["2020-01-01", "2020-06-01"]), [1.0, 2.0]),
                                 "PAR": _hist(pd.to_datetime(["2021-01-01", "2021-06-01"]), [1.0, 2.0])})
    (row,) = momentum.fund_record(Paths(tmp_path / "data"), [PAIR], today=date(2026, 11, 5))
    assert row["error"] == "no common dates" and row["fund_cagr"] is None


def _cagr_of(dates, values) -> float:
    return trend.cagr(pd.Series(np.asarray(values, dtype=float), index=pd.DatetimeIndex(dates)))


def _record(tmp_path, monkeypatch, fund, parent, idx):
    _patch_history(monkeypatch, {"FND": _hist(idx, fund), "PAR": _hist(idx, parent)})
    return momentum.fund_record(Paths(tmp_path / "data"), [PAIR], today=date(2026, 11, 5))[0]


def test_fund_record_drops_the_unsettled_bar_dated_today(tmp_path, monkeypatch):
    idx = pd.DatetimeIndex(list(pd.bdate_range("2025-06-02", periods=300)[:299]) + [pd.Timestamp("2026-11-05")])
    idx = idx.sort_values()
    good = _rising(idx[:-1], 0.001)
    fund = np.append(good, good[-1] * 0.25)
    row = _record(tmp_path, monkeypatch, fund, _rising(idx, 0.0005), idx)
    assert row["end"] == idx[-2].date().isoformat() and row["cut"] is False
    assert row["fund_cagr"] == pytest.approx(_cagr_of(idx[:-1], good))
    assert row["fund_max_drawdown"] == pytest.approx(0.0)


def _flipping(n_steady=500):
    idx = pd.bdate_range("2020-01-01", periods=20 + n_steady)
    flips = [25.0 if i % 2 == 0 else 19.0 for i in range(20)]
    steady = list(19.0 * 1.001 ** np.arange(1, n_steady + 1))
    return idx, np.array(flips + steady)


def test_fund_record_starts_at_the_last_flip_of_a_bad_start(tmp_path, monkeypatch):
    idx, fund = _flipping()
    row = _record(tmp_path, monkeypatch, fund, _rising(idx, 0.0005), idx)
    assert row["start"] == idx[19].date().isoformat() and row["cut"] is True
    assert row["end"] == idx[-1].date().isoformat()
    assert row["fund_cagr"] == pytest.approx(_cagr_of(idx[19:], fund[19:]))
    assert row["parent_cagr"] == pytest.approx(_cagr_of(idx[19:], _rising(idx, 0.0005)[19:]))
    assert row["fund_max_drawdown"] == pytest.approx(0.0)


def test_fund_record_splits_on_a_bad_tick_in_the_parent_too(tmp_path, monkeypatch):
    idx, bad = _flipping()
    row = _record(tmp_path, monkeypatch, _rising(idx, 0.001), bad, idx)
    assert row["start"] == idx[19].date().isoformat() and row["cut"] is True
    assert row["fund_cagr"] == pytest.approx(_cagr_of(idx[19:], _rising(idx, 0.001)[19:]))


def _dropped(n: int, at: int):
    idx = pd.bdate_range("2020-01-01", periods=n)
    values = _rising(idx, 0.001)
    values[at:] *= 0.8
    return idx, values


def test_fund_record_uses_the_longer_side_of_a_single_break(tmp_path, monkeypatch):
    idx, values = _dropped(400, 100)                      # stretches of 100 and 300
    row = _record(tmp_path, monkeypatch, values, _rising(idx, 0.0005), idx)
    assert row["start"] == idx[100].date().isoformat() and row["cut"] is True
    idx, values = _dropped(400, 300)                      # stretches of 300 and 100
    row = _record(tmp_path, monkeypatch, values, _rising(idx, 0.0005), idx)
    assert row["start"] == idx[0].date().isoformat() and row["end"] == idx[299].date().isoformat()
    assert row["cut"] is True


def test_fund_record_takes_the_later_stretch_on_equal_lengths(tmp_path, monkeypatch):
    idx, values = _dropped(400, 200)
    row = _record(tmp_path, monkeypatch, values, _rising(idx, 0.0005), idx)
    assert row["start"] == idx[200].date().isoformat() and row["end"] == idx[-1].date().isoformat()


def test_fund_record_break_threshold_is_above_fifteen_percent(tmp_path, monkeypatch):
    idx = pd.bdate_range("2020-01-01", periods=10)
    for jump, cut in ((1.15, False), (1.1501, True)):
        fund = np.array([100.0] * 5 + [100.0 * jump] * 5)
        row = _record(tmp_path, monkeypatch, fund, _rising(idx, 0.0005), idx)
        assert row["cut"] is cut
        assert row["start"] == (idx[5] if cut else idx[0]).date().isoformat()


def test_fund_record_only_counts_a_move_over_at_most_seven_days_as_a_break(tmp_path, monkeypatch):
    for gap, cut in ((10, False), (7, True)):
        first = pd.date_range("2020-01-01", periods=5)
        idx = first.append(pd.date_range(first[-1] + pd.Timedelta(days=gap), periods=5))
        fund = np.array([100.0] * 5 + [150.0] * 5)
        row = _record(tmp_path, monkeypatch, fund, np.full(10, 100.0), idx)
        assert row["cut"] is cut
        assert row["start"] == (idx[5] if cut else idx[0]).date().isoformat()


def test_fund_record_without_breaks_is_not_cut(tmp_path, monkeypatch):
    idx = pd.bdate_range("2020-01-01", periods=300)
    row = _record(tmp_path, monkeypatch, _rising(idx, 0.001), _rising(idx, 0.0005), idx)
    assert row["cut"] is False and row["start"] == idx[0].date().isoformat()


def test_fund_record_with_one_date_left_after_the_cuts(tmp_path, monkeypatch):
    idx = pd.DatetimeIndex(["2020-01-01", "2026-11-05", "2026-11-06"])   # only one dated before today
    row = _record(tmp_path, monkeypatch, [100.0, 100.0, 100.0], [100.0, 100.0, 100.0], idx)
    assert row["error"] == "no common dates" and row["fund_cagr"] is None and row["start"] is None
    idx = pd.bdate_range("2020-01-01", periods=2)         # a break leaves two stretches of one
    row = _record(tmp_path, monkeypatch, [100.0, 130.0], [100.0, 100.0], idx)
    assert row["error"] == "no common dates" and row["fund_cagr"] is None


def test_render_report_marks_a_cut_pair_and_prints_the_footnote_once():
    result = _result()
    result["funds"][0]["cut"] = True
    text = momentum.render_report(result)
    note = ("* A one-day move above 15% in either fund is treated as a data error; this pair "
            "uses its longest stretch without one.")
    assert "MTUM label * |" in text and "QDVA label *" not in text
    assert text.count(note) == 1
    assert text.index(note) < text.index("## Limits")
    plain = momentum.render_report(_result())
    assert " *" not in plain and "data error" not in plain


# --- run -------------------------------------------------------------------------------------

def _patched_run(tmp_path, monkeypatch, added=None):
    closes, spy, idx = _twelve(1000)
    closes.attrs = {"filled": {"N03": 2}, "missing": ["ZZZ"], "short": ["N04"]}
    cfg = {"momentum": {"holdout_start": str(idx[WARM].date()), "holdout_end": str(idx[620].date()),
                        "recent_start": str(idx[650].date())}}
    seen = {}
    joins = dict.fromkeys(closes.columns, OLD) if added is None else added
    monkeypatch.setattr(backtest, "_join_dates", lambda paths, fetch, fn: joins)

    def fake_load(paths, tickers, start, fetch=True, progress=None):
        seen["load"] = (list(tickers), start, fetch)
        return closes, spy

    monkeypatch.setattr(trend, "load_closes", fake_load)
    monkeypatch.setattr(momentum, "fund_record", lambda paths, pairs, fetch=True, today=None: [
        {"fund": "F"}])
    result = momentum.run(Paths(tmp_path / "data"), cfg, today=date(2026, 11, 5), fetch=False)
    return result, seen, cfg, idx


def test_run_returns_the_four_results_sensitivity_and_verdict(tmp_path, monkeypatch):
    result, seen, cfg, idx = _patched_run(tmp_path, monkeypatch)
    assert seen["load"] == ([f"N{i:02d}" for i in range(12)], cfg["momentum"]["holdout_start"], False)
    assert result["generated"] == "2026-11-05" and result["members"] == 12 and result["used"] == 12
    assert set(result["results"]) == {("recent", 10), ("holdout", 10), ("recent", 20), ("holdout", 20)}
    assert set(result["trades"]) == {("recent", 10), ("holdout", 10)}
    assert result["periods"]["holdout"]["eligible_at_start"] == 12
    assert result["periods"]["recent"]["eligible_at_start"] == 12
    assert result["periods"]["holdout"]["end"] <= cfg["momentum"]["holdout_end"]
    assert result["results"][("holdout", 10)]["end"] <= cfg["momentum"]["holdout_end"]
    assert result["periods"]["recent"]["start"] == cfg["momentum"]["recent_start"]
    assert result["settings"]["positions"] == 10 and result["funds"] == [{"fund": "F"}]
    rows = result["sensitivity"]
    for period in ("recent", "holdout"):
        for setting, values in momentum.SENSITIVITY:
            got = [r["value"] for r in rows if r["period"] == period and r["setting"] == setting]
            assert got == values
    assert {"supports", "checks", "sentence"} <= set(result["verdict"])
    assert result["missing"] == ["ZZZ"] and result["short"] == ["N04"]
    assert result["filled"] == {"cells": 2, "tickers": 1}


def test_run_counts_eligible_names_by_join_date(tmp_path, monkeypatch):
    joins = {f"N{i:02d}": OLD for i in range(12)}
    joins["N11"] = "2099-01-01"
    result, _, _, _ = _patched_run(tmp_path, monkeypatch, joins)
    assert result["periods"]["recent"]["eligible_at_start"] == 11


def test_run_without_join_dates_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "_join_dates", lambda paths, fetch, fn: {})
    with pytest.raises(ValueError, match="no S&P 500 join dates in the cache; run without --no-fetch"):
        momentum.run(Paths(tmp_path / "data"), {}, fetch=False)


# --- config ----------------------------------------------------------------------------------

def test_config_defaults_validate_and_zero_positions_do_not():
    import copy
    cfg = copy.deepcopy(DEFAULTS)
    validate_config(cfg)
    cfg["momentum"]["positions"] = 0
    with pytest.raises(ValueError, match="momentum.positions"):
        validate_config(cfg)


# --- report, write, latest_report, command -----------------------------------------------------

def _metrics(period: str, n: int, cagr: float = 0.15) -> dict:
    first = 2015 if period == "recent" else 2005
    years = {y: {"system": 0.10, "spy": 0.08} for y in (range(2015, 2018) if period == "recent"
                                                          else range(2005, 2008))}
    return {"cagr": cagr, "spy_cagr": 0.10, "universe_cagr": 0.11, "max_drawdown": 0.30,
            "spy_max_drawdown": 0.25, "years": years, "years_ahead": 3, "years_counted": 3,
            "trades_per_year": 40.5, "costs_per_year": 0.012, "start": f"{first}-07-01",
            "end": "2026-11-04" if period == "recent" else "2015-06-30", "positions": n,
            "cost_per_side": 0.005, "market_filter": True}


def _result() -> dict:
    trade = {"ticker": "AAA", "signal_date": "2016-01-04", "entry_date": "2016-01-05",
             "entry_price": 10.0, "shares": 5.0, "n": 0.2, "exit_date": "2016-03-01",
             "exit_price": 12.0, "reason": "rotation", "pnl": 9.0, "ret": 0.18, "days": 56}
    results = {(p, n): _metrics(p, n) for p in ("recent", "holdout") for n in (10, 20)}
    return {
        "generated": "2026-11-05", "settings": {**DEFAULTS["momentum"]}, "members": 503,
        "used": 480, "periods": {"recent": {"start": "2015-07-01", "end": "2026-11-04",
                                            "eligible_at_start": 400},
                                 "holdout": {"start": "2005-01-03", "end": "2015-06-30",
                                             "eligible_at_start": 300}},
        "results": results,
        "trades": {("recent", 10): [trade, {**trade, "ticker": "BBB"}], ("holdout", 10): [trade]},
        "sensitivity": [{"period": "recent", "setting": "positions", "value": 30,
                         **_metrics("recent", 30)}],
        "verdict": {"supports": False, "sentence": "The test does not support a live sleeve: X.",
                    "checks": {}, "years_ahead": 6, "years_counted": 6},
        "funds": [
            {"fund": "MTUM", "parent": "SPY", "label": "MTUM label", "start": "2013-04-18",
             "end": "2026-11-04", "fund_cagr": 0.12, "parent_cagr": 0.13, "excess": -0.01,
             "fund_max_drawdown": 0.35, "parent_max_drawdown": 0.34, "error": None},
            {"fund": "QDVA.DE", "parent": "SXR8.DE", "label": "QDVA label", "start": None,
             "end": None, "fund_cagr": None, "parent_cagr": None, "excess": None,
             "fund_max_drawdown": None, "parent_max_drawdown": None,
             "error": "no price history for QDVA.DE"}],
        "missing": ["GONE"], "short": ["NEW1", "NEW2"], "filled": {"cells": 0, "tickers": 0},
    }


def test_render_report_sections_in_order_and_columns():
    text = momentum.render_report(_result())
    heads = ["# Momentum sleeve backtest, 2026-11-05", "## Verdict", "## Results",
             "## Calendar years", "## Sensitivity", "## Real momentum funds", "## Limits"]
    positions = [text.index(h) for h in heads]
    assert positions == sorted(positions)
    assert "today's S&P 500 members, each from the day it joined the index" in text
    assert "503 members" in text and "480 used" in text
    assert "400" in text and "2005-01-03" in text
    assert ("| Period | Positions | CAGR | SPY | Equal-weight universe | Max drawdown | SPY drawdown "
            "| Years ahead of SPY | Trades a year | Costs a year |") in text
    assert "| Year | Sleeve | SPY |" in text
    assert "| Period | Setting | Value | CAGR | SPY | Max drawdown |" in text
    assert ("| Fund | Against | From | To | Fund a year | Parent a year | Difference "
            "| Fund drawdown | Parent drawdown |") in text
    assert text.count("| recent | 10 |") == 1 and text.count("| holdout | 20 |") == 1
    years = text.split("## Calendar years")[1].split("## Sensitivity")[0]
    assert years.index("| 2007 |") < years.index("| 2015 |")        # hold-out years first


def test_render_report_verdict_rule_and_sentence():
    r = _result()
    text = momentum.render_report(r)
    assert r["verdict"]["sentence"] in text
    for words in ("2 points", "1 point", "1.25 times", "55%", "top 20", "turns nothing on"):
        assert words in text


def test_render_report_funds_error_and_limits():
    text = momentum.render_report(_result())
    assert "QDVA label" in text and "unavailable: no price history for QDVA.DE" in text
    assert "MTUM label" in text and "-1.0%" in text
    for limit in ("names that left the index are missing", "next close", "cash earns nothing",
                  "currency", "concentrated"):
        assert limit in text.lower()
    assert "1 ticker" in text and "2 tickers" in text      # missing and short counts
    quiet = _result() | {"missing": [], "short": []}
    assert "no price history" not in momentum.render_report(quiet).split("## Limits")[1]


def test_write_creates_three_files_and_latest_report(tmp_path):
    paths = Paths(tmp_path)
    assert momentum.latest_report(paths) is None
    md = momentum.write(paths, _result())
    folder = paths.reports / "backtest"
    assert md == folder / "momentum_2026-11-05.md"
    assert sorted(p.name for p in folder.iterdir()) == [
        "momentum_2026-11-05.html", "momentum_2026-11-05.md", "momentum_2026-11-05_trades.csv"]
    csv = pd.read_csv(folder / "momentum_2026-11-05_trades.csv")
    assert list(csv.columns[:2]) == ["period", "positions"] and len(csv) == 3
    (folder / "momentum_2026-01-02.html").write_text("old")
    assert momentum.latest_report(paths) == folder / "momentum_2026-11-05.html"


def test_shipped_config_momentum_section_matches_defaults():
    from value_pipeline.config import load_config
    cfg = load_config()
    assert cfg["momentum"] == DEFAULTS["momentum"]
    validate_config(cfg)


def test_cli_momentum_backtest_prints_and_saves(tmp_path, monkeypatch, capsys):
    from value_pipeline import cli
    calls = {}

    def fake_run(paths, cfg, today=None, fetch=True, progress=None):
        calls["fetch"] = fetch
        return _result()

    monkeypatch.setattr(momentum, "run", fake_run)
    data = tmp_path / "data"
    cli.main(["--data-dir", str(data), "momentum-backtest", "--no-fetch"])
    out = " ".join(capsys.readouterr().out.split())
    assert calls == {"fetch": False}
    assert "Saved" in out and "momentum_2026-11-05.md" in out and "live sleeve" in out
    assert (data / "reports" / "backtest" / "momentum_2026-11-05.html").exists()


def test_a_fetching_run_refreshes_spy_before_loading_closes(tmp_path, monkeypatch):
    order = []

    def fake_history(ticker, paths, fetch=True, refresh=False, stale_before=None):
        order.append(("history", ticker, fetch, stale_before))
        return None

    monkeypatch.setattr(backtest, "price_history", fake_history)
    closes, spy, idx = _twelve(1000)
    closes.attrs = {}
    monkeypatch.setattr(backtest, "_join_dates",
                        lambda paths, fetch, fn: dict.fromkeys(closes.columns, OLD))
    monkeypatch.setattr(trend, "load_closes",
                        lambda paths, tickers, start, fetch=True, progress=None:
                        order.append(("load",)) or (closes, spy))
    monkeypatch.setattr(momentum, "fund_record", lambda *a, **k: [])
    cfg = {"momentum": {"holdout_start": str(idx[WARM].date()), "holdout_end": str(idx[620].date()),
                        "recent_start": str(idx[650].date())}}
    momentum.run(Paths(tmp_path / "data"), cfg, today=date(2026, 11, 5), fetch=True)
    assert order[0] == ("history", trend.BENCHMARK, True,
                        (date(2026, 11, 5) - timedelta(days=backtest.PRICE_SLACK_DAYS)).isoformat())
    assert order.index(("load",)) > 0
    order.clear()
    momentum.run(Paths(tmp_path / "data"), cfg, today=date(2026, 11, 5), fetch=False)
    assert order == [("load",)]


def test_run_says_whether_the_settings_are_the_fixed_ones(tmp_path, monkeypatch):
    result, _, _, _ = _patched_run(tmp_path, monkeypatch)
    assert result["as_fixed"] is False       # the helper moves the period dates
    assert momentum.FIXED["positions"] == 10 and momentum.FIXED["cost_per_side"] == 0.005
    assert momentum.differing({**DEFAULTS["momentum"]}) == {}
    assert momentum.differing({**DEFAULTS["momentum"], "positions": 12}) == {"positions": 12}


def test_report_flags_settings_that_differ_from_the_fixed_ones():
    r = _result()
    text = momentum.render_report(r)
    assert "do not count" not in text and "The rule was fixed before any run" in text
    assert "10 positions is a concentrated book." in text
    r["settings"] = {**r["settings"], "positions": 12, "cost_per_side": 0.0075}
    r["as_fixed"] = False
    r["results"] = {(p, 12 if n == 10 else n): m for (p, n), m in r["results"].items()}
    text = momentum.render_report(r)
    assert ("These settings differ from the ones the rule was fixed for (positions 12, "
            "cost_per_side 0.0075), so this verdict does not count.") in text
    assert text.index("so this verdict does not count") > text.index(r["verdict"]["sentence"])
    assert "12 positions is a concentrated book." in text


def test_report_renders_a_dash_for_a_missing_spy_return():
    r = _result()
    r["results"][("recent", 10)]["years"][2015] = {"system": 0.1, "spy": None}
    r["results"][("recent", 10)]["spy_cagr"] = None
    text = momentum.render_report(r)
    assert "| 2015 | 10.0% | – |" in text


def test_report_limits_name_the_unfinished_last_session():
    assert ("The last day's close can come from an unfinished session if the prices were "
            "cached during US trading hours.") in momentum.render_report(_result())
