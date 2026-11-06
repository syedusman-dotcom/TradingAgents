"""The pullback study: fills, one-year outcomes from the fill, the study's means, the fixed
verdict and the run, on synthetic prices (no network)."""

import pandas as pd
import pytest

from value_pipeline import backtest, pullback
from value_pipeline.config import Paths

pytestmark = pytest.mark.unit

START = "2020-01-01"


def _hist(price_at, days: int = 600) -> pd.DataFrame:
    dates = pd.date_range(START, periods=days, freq="D")
    close = [float(price_at(i)) for i in range(len(dates))]
    return pd.DataFrame({"date": dates, "close": close, "adj_close": close, "split": 0.0})


def _dip_on(day: int, low: float, later: float):
    """100 until ``day``, ``low`` from there to day 200, ``later`` after."""
    return lambda i: 100 if i < day else (low if i < 200 else later)


def _flat_then(later: float):
    return lambda i: 100 if i < 200 else later


def test_fill_takes_the_first_close_at_or_below_the_dip():
    hist = _hist(_dip_on(5, 88, 110))
    assert pullback.fill(hist, START, 0.10) == ("2020-01-06", 88.0)


def test_fill_includes_a_close_exactly_at_the_dip():
    hist = _hist(_dip_on(5, 90, 110))
    assert pullback.fill(hist, START, 0.10) == ("2020-01-06", 90.0)


def test_fill_is_none_when_the_dip_is_never_reached():
    assert pullback.fill(_hist(_dip_on(5, 95, 110)), START, 0.10) is None


def test_fill_ignores_the_start_day_itself():
    hist = _hist(lambda i: 100)
    hist.loc[0, "close"] = 100.0
    assert pullback.fill(hist, START, 0.0) == ("2020-01-02", 100.0)
    assert pullback.fill(hist, START, 0.10) is None


def test_fill_ignores_a_dip_after_the_window():
    hist = _hist(_dip_on(130, 80, 110))
    assert pullback.fill(hist, START, 0.10) is None
    assert pullback.fill(hist, START, 0.10, window_days=140) == ("2020-05-10", 80.0)


def test_fill_measures_from_the_first_trading_day_on_or_after_start():
    hist = _hist(lambda i: 100 if i == 0 else (50 if i < 3 else 100))
    hist = hist.drop(index=0).reset_index(drop=True)
    assert pullback.fill(hist, START, 0.10) is None


def test_fill_is_none_without_history():
    assert pullback.fill(None, START, 0.10) is None


def test_pick_outcome_runs_a_year_from_the_fill():
    hist = _hist(_dip_on(5, 88, 110))
    spy = _hist(_flat_then(110))
    out = pullback.pick_outcome(hist, spy, {"start_date": START}, 0.10)
    assert out["filled"] is True
    assert out["fill_date"] == "2020-01-06"
    assert out["ret_fill"] == pytest.approx(110 / 88 - 1)
    assert out["spy_fill"] == pytest.approx(0.10)
    assert out["excess_fill"] == pytest.approx(110 / 88 - 1 - 0.10)


def test_pick_outcome_ending_early_is_unmeasured():
    hist = _hist(_dip_on(5, 88, 110), days=300)          # the year from the fill runs past the data
    out = pullback.pick_outcome(hist, _hist(_flat_then(110)), {"start_date": START}, 0.10)
    assert out["filled"] is True and out["excess_fill"] is None
    spy_short = pullback.pick_outcome(_hist(_dip_on(5, 88, 110)), _hist(_flat_then(110), days=300),
                                      {"start_date": START}, 0.10)
    assert spy_short["excess_fill"] is None


def test_run_measures_against_the_configured_benchmark(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    other = tmp_path / "elsewhere"
    _write_picks(other, "mine_picks.csv", ["2020-03-31,AAA,compounder,100,1,2020-01-01,2021-01-01,0.1,0.05,0.05"])
    histories = {"AAA": _hist(_dip_on(5, 88, 110)), "VOO": _hist(_flat_then(110))}
    asked = []

    def fake(t, p, fetch=True, **kw):
        asked.append(t)
        return histories.get(t)

    monkeypatch.setattr(backtest, "price_history", fake)
    _, result = pullback.run(paths, other / "mine_picks.csv", benchmark="VOO")
    assert asked[0] == "VOO" and result["compounder"][0.10]["n"] == 1


def test_pick_outcome_unfilled_carries_none():
    out = pullback.pick_outcome(_hist(_flat_then(110)), _hist(_flat_then(110)),
                                {"start_date": START}, 0.10)
    assert out == {"filled": False, "fill_date": None, "ret_fill": None, "spy_fill": None,
                   "excess_fill": None}


def _pick(ticker: str, ret: float | None, spy: float | None, rule: str = "value_graded",
          hold: int = 1) -> dict:
    excess = None if ret is None or spy is None else ret - spy
    return {"rule": rule, "ticker": ticker, "hold": hold, "start_date": START, "ret": ret,
            "spy": spy, "excess_spy": excess}


def _histories():
    return {"A": _hist(_dip_on(5, 88, 110)), "B": _hist(_dip_on(5, 85, 102)),
            "C": _hist(lambda i: 100), "D": _hist(_dip_on(5, 88, 110))}


def test_study_means_on_three_picks():
    picks = [_pick("A", 0.10, 0.05), _pick("B", 0.30, 0.05), _pick("C", -0.05, 0.05)]
    histories = _histories()
    result = pullback.study(picks, histories.get, _hist(_flat_then(110)),
                            dips=(0.10,), rules=("value_graded",))
    cell = result["value_graded"][0.10]
    assert cell["n"] == 3
    assert cell["fill_rate"] == pytest.approx(2 / 3)
    assert cell["buy_now_excess"] == pytest.approx((0.05 + 0.25 - 0.10) / 3)
    assert cell["filled_excess"] == pytest.approx(((110 / 88 - 1 - 0.10) + (102 / 85 - 1 - 0.10)) / 2)
    assert cell["unfilled_excess"] == pytest.approx(-0.10)
    assert cell["filled_buy_now_excess"] == pytest.approx((0.05 + 0.25) / 2)


def test_study_skips_picks_without_a_return_and_other_holds_and_rules():
    picks = [_pick("A", 0.10, 0.05), _pick("D", None, 0.05), _pick("B", 0.3, 0.05, hold=3),
             _pick("B", 0.3, 0.05, rule="compounder")]
    result = pullback.study(picks, _histories().get, _hist(_flat_then(110)),
                            dips=(0.10,), rules=("value_graded",))
    assert result["value_graded"][0.10]["n"] == 1


def test_study_means_are_none_when_empty():
    result = pullback.study([], _histories().get, _hist(_flat_then(110)),
                            dips=(0.10,), rules=("value_graded",))
    cell = result["value_graded"][0.10]
    assert cell["n"] == 0
    assert cell["fill_rate"] is None
    assert cell["unfilled_excess"] is None and cell["filled_excess"] is None


def _cell(fill_rate, unfilled, filled, same, buy_now=0.0, n=40) -> dict:
    return {"n": n, "fill_rate": fill_rate, "buy_now_excess": buy_now, "filled_excess": filled,
            "unfilled_excess": unfilled, "filled_buy_now_excess": same}


def _result(cells: dict) -> dict:
    return {rule: {0.10: cell, 0.15: cell} for rule, cell in cells.items()}


def test_verdict_costs_money():
    gives_up = _cell(0.4, 0.10, 0.02, 0.02)
    result = _result({"magic_formula": gives_up, "compounder_record": gives_up,
                      "value_graded": gives_up, "compounder": _cell(0.6, 0.0, 0.0, 0.0)})
    v = pullback.verdict(result)
    assert v["verdict"] == "costs money"
    assert sorted(v["costs_rules"]) == ["compounder_record", "magic_formula", "value_graded"]
    assert v["earns_rules"] == []
    assert "40%" in v["sentence"] and "+8.0 points" in v["sentence"]


def test_verdict_costs_needs_a_low_fill_rate():
    gives_up = _cell(0.6, 0.10, 0.02, 0.02)
    v = pullback.verdict(_result(dict.fromkeys(pullback.RULES, gives_up)))
    assert v["verdict"] == "inconclusive"


def test_verdict_earns_its_keep():
    helps = _cell(0.7, 0.0, 0.08, 0.05)
    result = _result({"magic_formula": helps, "compounder_record": helps,
                      "value_graded": _cell(0.7, 0.0, 0.05, 0.05), "compounder": helps})
    v = pullback.verdict(result)
    assert v["verdict"] == "earns its keep"
    assert sorted(v["earns_rules"]) == ["compounder", "compounder_record", "magic_formula"]
    assert v["costs_rules"] == []


def test_verdict_inconclusive():
    flat = _cell(0.7, 0.01, 0.02, 0.02)
    v = pullback.verdict(_result(dict.fromkeys(pullback.RULES, flat)))
    assert v["verdict"] == "inconclusive"
    assert v["costs_rules"] == [] and v["earns_rules"] == []
    assert v["sentence"]


def test_verdict_handles_empty_rules():
    empty = _cell(None, None, None, None, buy_now=None, n=0)
    v = pullback.verdict(_result(dict.fromkeys(pullback.RULES, empty)))
    assert v["verdict"] == "inconclusive"


PICK_HEAD = "cohort,ticker,rule,price,hold,start_date,end_date,ret,spy,excess_spy\n"


def _write_picks(folder, name: str, rows: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(PICK_HEAD + "".join(r + "\n" for r in rows), encoding="utf-8")


def test_run_uses_the_newest_picks_and_ignores_sp500(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    folder = paths.reports / "backtest"
    row = "2020-03-31,AAA,value_graded,100,1,2020-01-01,2021-01-01,0.1,0.05,0.05"
    _write_picks(folder, "backtest_2026-10-01_picks.csv", [row])
    _write_picks(folder, "backtest_2026-10-20_picks.csv", [row, row.replace("AAA", "BBB")])
    _write_picks(folder, "backtest_2026-11-01_sp500_picks.csv", [row])
    _write_picks(folder, "backtest_2026-11-02_sp600_picks.csv", [row])
    _write_picks(folder, "backtest_2026-11-03_leavers_picks.csv", [row])
    _write_picks(folder, "backtest_2026-11-04_sp400_picks.csv", [row])
    histories = {"AAA": _hist(_dip_on(5, 88, 110)), "BBB": _hist(lambda i: 100),
                 "SPY": _hist(_flat_then(110))}
    asked = []

    def fake(ticker, p, fetch=True, **kwargs):
        asked.append((ticker, fetch))
        return histories.get(ticker)

    monkeypatch.setattr(backtest, "price_history", fake)
    path, result = pullback.run(paths)
    assert path.parent == folder
    assert path.name.startswith("pullback_") and path.suffix == ".md"
    assert path.read_text(encoding="utf-8").strip()
    assert result["value_graded"][0.10]["n"] == 2
    assert result["value_graded"][0.10]["fill_rate"] == pytest.approx(0.5)
    assert all(fetch is False for _, fetch in asked)


def test_run_takes_an_explicit_picks_path(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    other = tmp_path / "elsewhere"
    row = "2020-03-31,AAA,compounder,100,1,2020-01-01,2021-01-01,0.1,0.05,0.05"
    _write_picks(other, "mine_picks.csv", [row])
    histories = {"AAA": _hist(_dip_on(5, 88, 110)), "SPY": _hist(_flat_then(110))}
    monkeypatch.setattr(backtest, "price_history", lambda t, p, fetch=True, **kw: histories.get(t))
    path, result = pullback.run(paths, other / "mine_picks.csv")
    assert path.parent == other
    assert result["compounder"][0.10]["n"] == 1


def test_run_without_picks_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        pullback.run(Paths(tmp_path))
