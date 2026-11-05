"""Trend-sleeve simulation engine on synthetic closes (no network; every price is built here)."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from value_pipeline import trend
from value_pipeline.config import DEFAULTS
from value_pipeline.trend import (
    SYSTEMS,
    Settings,
    cagr,
    latest_report,
    load_closes,
    max_drawdown,
    metrics,
    quality_windows,
    render_report,
    settings,
    simulate,
    verdict,
    write,
)

pytestmark = pytest.mark.unit

FIRST = "2020-01-01"


def _index(n: int, first: str = FIRST) -> pd.DatetimeIndex:
    return pd.bdate_range(first, periods=n)


def _spy(index: pd.DatetimeIndex, daily: float = 0.0005, warm: int = 300) -> pd.Series:
    """SPY rising ``daily`` a day, starting ``warm`` business days before ``index`` so its
    200-day average exists on every row of ``index``."""
    full = pd.bdate_range(end=index[-1], periods=len(index) + warm)
    return pd.Series(100.0 * (1 + daily) ** np.arange(len(full)), index=full)


def _frame(columns: dict, index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({k: np.asarray(v, dtype=float) for k, v in columns.items()}, index=index)


def _s(index: pd.DatetimeIndex, start_row: int = 60, **overrides) -> Settings:
    return settings({"trend": DEFAULTS["trend"]}, start=str(index[start_row].date()), **overrides)


def _d(index, row) -> date:
    return index[row].date()


def _breakout_path(base: float = 100.0, step: float = 0.0, flat: int = 80, jump: float = 1.0,
                   up: int = 30, down: int = 40) -> list[float]:
    """``flat`` rows around ``base`` (alternating +``step``), then a close ``jump`` above the
    flat range's high climbing 1 a day for ``up`` rows, then falling 2 a day for ``down`` rows."""
    flat_part = [base + (step if i % 2 else 0.0) for i in range(flat)]
    top = base + step + jump
    rising = [top + i for i in range(up)]
    falling = [rising[-1] - 2 * (i + 1) for i in range(down)]
    return flat_part + rising + falling


def test_settings_reads_the_trend_section_and_overrides():
    s = settings({"trend": DEFAULTS["trend"]}, max_positions=5)
    assert s == Settings(start="2015-07-01", equity=10000, risk_per_trade=0.01, stop_n=2.0, n_days=20,
                         max_positions=5, max_position_weight=0.20, cost_per_side=0.0035, market_filter=True)
    assert settings({}).max_positions == 10
    assert set(SYSTEMS) == {"breakout_55_20", "breakout_20_10", "momentum_12_1"}
    assert SYSTEMS["breakout_55_20"] == (55, 20) and SYSTEMS["breakout_20_10"] == (20, 10)
    assert SYSTEMS["momentum_12_1"] is None


def test_breakout_enters_the_day_after_the_signal_and_exits_after_the_low():
    path = _breakout_path()                       # day 80 = 101 (a 55-day high), up to 130 on day 109
    idx = _index(len(path))
    closes = _frame({"AAA": path}, idx)
    s = _s(idx, cost_per_side=0.0)
    out = simulate("breakout_55_20", closes, _spy(idx), s)

    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["ticker"] == "AAA"
    assert t["signal_date"] == _d(idx, 80)
    assert t["entry_date"] == _d(idx, 81)
    assert t["entry_price"] == pytest.approx(path[81])
    # the exit signal: the first close below the lowest of the prior 20 closes
    signal = next(i for i in range(110, len(path)) if path[i] < min(path[i - 20:i]))
    assert t["reason"] == "exit_low"
    assert t["exit_date"] == _d(idx, signal + 1)
    assert t["exit_price"] == pytest.approx(path[signal + 1])
    assert t["ret"] == pytest.approx(path[signal + 1] / path[81] - 1)
    assert t["days"] == (idx[signal + 1] - idx[81]).days
    assert t["pnl"] == pytest.approx(t["shares"] * (path[signal + 1] - path[81]))
    # series run from start; flat equity before the entry, the gain booked after the exit
    assert out["equity"].index[0] == idx[60] and out["exposure"].index[0] == idx[60]
    assert out["equity"].iloc[0] == pytest.approx(10_000)
    assert out["equity"].iloc[-1] == pytest.approx(10_000 + t["pnl"])
    assert out["exposure"].loc[idx[80]] == 0
    assert out["exposure"].loc[idx[81]] == pytest.approx(t["shares"] * path[81] / 10_000)
    assert out["exposure"].iloc[-1] == 0


def test_stop_exits_before_the_low():
    # alternating 100/101 gives N = 1.0; breakout to 103 on day 80, fill at 104 on day 81,
    # stop at 104 - 2 = 102; day 82 closes at 101 (3 N down) but above the 20-day low of 100.
    path = [100.0 + (i % 2) for i in range(80)] + [103.0, 104.0, 101.0, 100.5] + [100.5] * 10
    idx = _index(len(path))
    out = simulate("breakout_55_20", _frame({"AAA": path}, idx), _spy(idx), _s(idx, cost_per_side=0.0))
    (t,) = out["trades"]
    assert t["n"] == pytest.approx(1.0)
    assert t["entry_date"] == _d(idx, 81)
    assert t["reason"] == "stop"
    assert t["exit_date"] == _d(idx, 83)          # signalled on day 82's close, sold at day 83's
    assert t["exit_price"] == pytest.approx(100.5)


def test_sizing_risks_one_percent_and_respects_the_weight_cap():
    # N = 1.0: shares = 10,000 x 0.01 / (2 x 1.0) = 50; 50 x 34 = 1,700 <= 2,000
    path = [30.0 + (i % 2) for i in range(80)] + [33.0, 34.0] + [34.0 + i for i in range(10)]
    idx = _index(len(path))
    out = simulate("breakout_55_20", _frame({"AAA": path}, idx), _spy(idx), _s(idx, cost_per_side=0.0))
    (t,) = out["trades"]
    assert t["shares"] == pytest.approx(10_000 * 0.01 / 2.0)
    assert t["reason"] == "open"

    # N = 0.1: shares would be 500 (17,000 dollars); the position is capped at 0.20 x 10,000
    path = [30.0 + 0.1 * (i % 2) for i in range(80)] + [33.0, 34.0] + [34.0 + i for i in range(10)]
    out = simulate("breakout_55_20", _frame({"AAA": path}, idx), _spy(idx), _s(idx, cost_per_side=0.0))
    (t,) = out["trades"]
    assert t["n"] == pytest.approx(0.1)
    assert t["shares"] * t["entry_price"] == pytest.approx(2_000)

    # with no weight cap the purchase plus its cost is capped by the cash
    out = simulate("breakout_55_20", _frame({"AAA": path}, idx), _spy(idx),
                   _s(idx, cost_per_side=0.01, max_position_weight=5.0))
    (t,) = out["trades"]
    assert t["shares"] * t["entry_price"] * 1.01 == pytest.approx(10_000)


def test_an_entry_the_cash_cannot_fund_to_a_cent_is_dropped():
    # AAA takes all the cash; float rounding leaves about 1.8e-12 dollars, which must not open BBB
    path = [30.0 + 0.1 * (i % 2) for i in range(80)] + [33.0, 34.0] + [34.0 + i for i in range(10)]
    idx = _index(len(path))
    closes = _frame({"AAA": path, "BBB": [p - 1.0 for p in path]}, idx)
    s = _s(idx, equity=9999, cost_per_side=0.0035, max_position_weight=5.0)
    value = 9999 / 1.0035
    assert 9999 - (value + 0.0035 * value) > 0                     # the residue this test is about
    out = simulate("breakout_55_20", closes, _spy(idx), s)
    assert [t["ticker"] for t in out["trades"]] == ["AAA"]


def test_costs_are_charged_on_both_sides():
    path = _breakout_path()
    idx = _index(len(path))
    closes = _frame({"AAA": path}, idx)
    free = simulate("breakout_55_20", closes, _spy(idx), _s(idx, cost_per_side=0.0))
    paid = simulate("breakout_55_20", closes, _spy(idx), _s(idx, cost_per_side=0.01))
    (t0,), (t1,) = free["trades"], paid["trades"]
    assert t1["shares"] == pytest.approx(t0["shares"])
    buy, sell = t1["shares"] * t1["entry_price"], t1["shares"] * t1["exit_price"]
    assert free["costs"] == 0
    assert paid["costs"] == pytest.approx(0.01 * (buy + sell))
    assert t1["pnl"] == pytest.approx(sell - buy - paid["costs"])
    assert paid["equity"].iloc[-1] == pytest.approx(free["equity"].iloc[-1] - paid["costs"])


def test_slots_go_to_the_strongest_126_day_return():
    # all three flat at 100 from day 100 and break out to 102 on day 200; 126 days earlier
    # (day 74) they stood at 50, 60 and 70, so AAA then BBB have the higher 126-day returns.
    n = 215

    def path(base):
        return [base] * 100 + [100.0] * 100 + [102.0 + i for i in range(n - 200)]

    idx = _index(n)
    closes = _frame({"CCC": path(70.0), "AAA": path(50.0), "BBB": path(60.0)}, idx)
    out = simulate("breakout_55_20", closes, _spy(idx), _s(idx, start_row=150, max_positions=2))
    assert sorted(t["ticker"] for t in out["trades"]) == ["AAA", "BBB"]
    assert all(t["signal_date"] == _d(idx, 200) for t in out["trades"])


def test_market_filter_blocks_entries_not_exits():
    # SPY rises, then falls 30% on day 100 and stays down: below its 200-day average from then on.
    path_a = _breakout_path()                       # enters on day 81, exits on its low after day 110
    path_b = [100.0] * 105 + [101.0 + i for i in range(len(path_a) - 105)]   # breaks out on day 105
    idx = _index(len(path_a))
    spy = _spy(idx)
    spy.loc[idx[100]:] *= 0.7
    closes = _frame({"AAA": path_a, "BBB": path_b}, idx)
    out = simulate("breakout_55_20", closes, spy, _s(idx))
    (t,) = out["trades"]
    assert t["ticker"] == "AAA" and t["reason"] == "exit_low"
    assert t["exit_date"] > _d(idx, 100)

    unfiltered = simulate("breakout_55_20", closes, spy, _s(idx, market_filter=False))
    assert sorted(t["ticker"] for t in unfiltered["trades"]) == ["AAA", "BBB"]


def test_no_signal_from_a_window_that_is_not_full_and_no_fill_on_a_missing_close():
    nan = math.nan
    # 30 closes at 100 (days 50..79) then 101 on day 80 and flat: the 55-day window is never full
    # on a new high, so there is no signal.
    short = [nan] * 50 + [100.0] * 30 + [101.0] * 60
    idx = _index(len(short))
    out = simulate("breakout_55_20", _frame({"AAA": short}, idx), _spy(idx), _s(idx))
    assert out["trades"] == []
    # the same 30 closes are enough for the 20-day system
    out = simulate("breakout_20_10", _frame({"AAA": short}, idx), _spy(idx), _s(idx))
    assert [t["signal_date"] for t in out["trades"]] == [_d(idx, 80)]

    # a breakout on day 80 followed by five missing closes is cancelled
    gap5 = [100.0] * 80 + [101.0] + [nan] * 5 + [101.0] * 54
    out = simulate("breakout_55_20", _frame({"AAA": gap5}, idx), _spy(idx), _s(idx))
    assert out["trades"] == []
    # four missing closes: filled at the fifth row's close
    gap4 = [100.0] * 80 + [101.0] + [nan] * 4 + [101.0] * 55
    out = simulate("breakout_55_20", _frame({"AAA": gap4}, idx), _spy(idx), _s(idx))
    (t,) = out["trades"]
    assert t["signal_date"] == _d(idx, 80) and t["entry_date"] == _d(idx, 85)


def test_eligibility_gates_entries():
    # a breakout on day 80 (April 2020) and a second on day 150 (late July 2020)
    path = [100.0] * 80 + [101.0] * 70 + [103.0 + i for i in range(20)]
    idx = _index(len(path))
    assert idx[80].date() < date(2020, 6, 30) < idx[150].date()
    seen = []

    def eligible(ticker, day):
        seen.append((ticker, day))
        return day >= date(2020, 6, 30)

    out = simulate("breakout_55_20", _frame({"AAA": path}, idx), _spy(idx), _s(idx), eligible=eligible)
    (t,) = out["trades"]
    assert t["signal_date"] == _d(idx, 150)
    assert ("AAA", _d(idx, 80)) in seen
    assert all(isinstance(day, date) for _, day in seen)


def test_momentum_holds_the_top_names_monthly():
    idx = _index(340, first="2019-01-01")
    d1 = idx.get_loc(pd.Timestamp("2020-02-03"))      # first trading day of February 2020
    d2 = idx.get_loc(pd.Timestamp("2020-03-02"))      # first trading day of March 2020
    assert d1 >= 252
    rows = np.arange(len(idx))

    def growth(r):                                     # a 12-1 return of r on every day
        return 100.0 * np.exp(np.log(1 + r) / 231 * rows)

    lead = np.where(rows < d1 - 20, 100.0, 200.0)      # 12-1 return 0 on d1, +100% on d2
    fallen = np.where(rows < d1 - 250, 100.0, np.where(rows < d1 - 10, 300.0, 50.0))  # +200%, but below its average
    closes = _frame({"AAA": growth(0.40), "BBB": growth(0.30), "CCC": growth(0.20), "DDD": growth(-0.10),
                     "EEE": lead, "FFF": fallen}, idx)
    s = settings({"trend": DEFAULTS["trend"]}, start="2020-02-01", max_positions=2, max_position_weight=0.5,
                 cost_per_side=0.0)
    out = simulate("momentum_12_1", closes, _spy(idx), s)

    by = {t["ticker"]: t for t in out["trades"]}
    assert set(by) == {"AAA", "BBB", "EEE"}
    for name in ("AAA", "BBB"):
        assert by[name]["signal_date"] == _d(idx, d1)
        assert by[name]["entry_date"] == _d(idx, d1 + 1)
        assert by[name]["shares"] * by[name]["entry_price"] == pytest.approx(10_000 / 2)
    assert by["BBB"]["reason"] == "rebalance"
    assert by["BBB"]["exit_date"] == _d(idx, d2 + 1)
    assert by["AAA"]["reason"] == "open"              # stays in the top two, untouched
    assert by["EEE"]["signal_date"] == _d(idx, d2) and by["EEE"]["entry_date"] == _d(idx, d2 + 1)
    equity_d2 = out["equity"].loc[idx[d2]]
    assert by["EEE"]["shares"] * by["EEE"]["entry_price"] == pytest.approx(
        min(equity_d2 / 2, out["equity"].loc[idx[d2 + 1]] - by["AAA"]["shares"] * closes["AAA"].iloc[d2 + 1]))
    # closed trades first, then the open positions
    assert out["trades"][0]["ticker"] == "BBB"


def _random_closes(seed: int, tickers: int = 20, n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.02, size=(n, tickers))
    closes = 50.0 * np.exp(np.cumsum(steps, axis=0))
    closes[rng.random(size=closes.shape) < 0.0005] = np.nan        # a few scattered missing closes
    return pd.DataFrame(closes, index=_index(n), columns=[f"T{i:02d}" for i in range(tickers)])


def _cut_after(closes: pd.DataFrame, spy: pd.Series, d: pd.Timestamp, seed: int):
    """Every close after ``d`` (tickers and SPY) scaled by a seeded factor between 0.5 and 1.5."""
    rng = np.random.default_rng(seed)
    changed, spy_changed = closes.copy(), spy.copy()
    later = closes.index > d
    changed.loc[later] *= rng.uniform(0.5, 1.5, size=changed.loc[later].shape)
    spy_changed.loc[spy.index > d] *= rng.uniform(0.5, 1.5, size=(spy.index > d).sum())
    return changed, spy_changed


@pytest.mark.parametrize("system", sorted(SYSTEMS))
def test_no_lookahead(system):
    closes = _random_closes(7, tickers=40)
    idx = closes.index
    rng = np.random.default_rng(11)
    spy = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(idx) + 300))),
                    index=pd.bdate_range(end=idx[-1], periods=len(idx) + 300))
    s = _s(idx, start_row=300, max_positions=3)
    a = simulate(system, closes, spy, s)
    # cut on days an order was placed (momentum decision days for momentum), spread over the run
    signal_days = sorted({pd.Timestamp(t["signal_date"]) for t in a["trades"]})
    cuts = sorted({min(signal_days, key=lambda day: abs(idx.get_loc(day) - row)) for row in range(340, 580, 20)})
    assert len(cuts) >= 5

    for k, d in enumerate(cuts):
        b = simulate(system, *_cut_after(closes, spy, d, seed=100 + k), s)

        def decided(result, d=d):
            # what is fixed by the close of d: every order placed by then and the facts read at its signal,
            # and every fill, exit and mark up to d
            placed = sorted((t["ticker"], t["signal_date"], t["n"]) for t in result["trades"]
                            if t["signal_date"] <= d.date())
            filled = sorted((t["ticker"], t["signal_date"], t["entry_date"], t["entry_price"], t["shares"])
                            for t in result["trades"] if t["entry_date"] <= d.date())
            closed = sorted((t["ticker"], t["entry_date"], t["exit_date"], t["exit_price"], t["reason"], t["pnl"])
                            for t in result["trades"] if t["reason"] != "open" and t["exit_date"] <= d.date())
            return placed, filled, closed

        assert decided(a) == decided(b), d
        assert any(t["signal_date"] == d.date() for t in b["trades"])
        pd.testing.assert_series_equal(a["equity"].loc[:d], b["equity"].loc[:d])
        pd.testing.assert_series_equal(a["exposure"].loc[:d], b["exposure"].loc[:d])
        assert a["equity"].iloc[-1] != pytest.approx(b["equity"].iloc[-1])   # the changes did bite
    placed, filled, closed = decided(a, d=cuts[-1])
    assert len(filled) >= 3 and closed                                      # there was something to compare


def test_max_drawdown_and_cagr():
    s = pd.Series([100, 120, 90, 110], index=pd.to_datetime(["2020-01-01", "2020-07-01", "2021-01-01", "2022-01-01"]))
    assert max_drawdown(s) == pytest.approx(0.25)
    assert cagr(s) == pytest.approx((110 / 100) ** (1 / 2.0027) - 1, abs=1e-3)
    assert max_drawdown(pd.Series([1.0, 2.0, 3.0], index=_index(3))) == 0


def _trade(ret, pnl, days):
    return {"ticker": "X", "signal_date": date(2020, 1, 1), "entry_date": date(2020, 1, 2), "entry_price": 100.0,
            "shares": 10.0, "n": 1.0, "exit_date": date(2020, 2, 1), "exit_price": 100.0 * (1 + ret),
            "reason": "exit_low", "pnl": pnl, "ret": ret, "days": days}


def test_metrics_counts_wins_and_years():
    idx = pd.bdate_range("2020-01-01", "2022-12-30")
    # up in 2020, down in 2021, up in 2022
    values = np.where(idx.year == 2021, -0.0003, 0.0006)
    equity = pd.Series(10_000 * np.cumprod(1 + values), index=idx)
    equity.iloc[0] = 10_000
    spy = pd.Series(100.0 * 1.0005 ** np.arange(len(idx) + 300),
                    index=pd.bdate_range(end=idx[-1], periods=len(idx) + 300))
    trades = [_trade(0.10, 100.0, 10), _trade(-0.05, -50.0, 20), _trade(0.20, 200.0, 30),
              {**_trade(0.50, 500.0, 40), "reason": "open"}]
    result = {"equity": equity, "exposure": pd.Series(0.5, index=idx), "trades": trades, "costs": 30.0}
    m = metrics(result, spy)

    assert m["trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["avg_win"] == pytest.approx(0.15) and m["avg_loss"] == pytest.approx(-0.05)
    assert m["payoff"] == pytest.approx(15 / 5)
    assert m["avg_days"] == pytest.approx(20)
    assert m["top5_share"] == pytest.approx(1.0)
    assert m["exposure"] == pytest.approx(0.5)
    assert set(m["years"]) == {2020, 2021, 2022}
    assert m["years"][2020]["system"] > 0 > m["years"][2021]["system"]
    assert m["years_gained"] == pytest.approx(2 / 3)
    span = (idx[-1] - idx[0]).days / 365.25
    assert m["trades_per_year"] == pytest.approx(3 / span)
    assert m["costs_per_year"] == pytest.approx(30.0 / span / 10_000)
    assert m["total_return"] == pytest.approx(equity.iloc[-1] / 10_000 - 1)
    assert m["cagr"] == pytest.approx(cagr(equity))
    assert m["max_drawdown"] == pytest.approx(max_drawdown(equity))
    assert m["volatility"] == pytest.approx(equity.pct_change().std() * math.sqrt(252))
    assert m["return_over_vol"] == pytest.approx(m["cagr"] / m["volatility"])
    spy_span = spy.loc[idx[0]:]
    assert m["spy_cagr"] == pytest.approx(cagr(spy_span))
    assert m["spy_max_drawdown"] == 0
    assert 0 < m["spy_at_exposure_cagr"] < m["spy_cagr"]
    # SPY's 2021: the last 2020 value is the base
    spy_2021 = spy_span[spy_span.index.year == 2021].iloc[-1] / spy_span[spy_span.index.year == 2020].iloc[-1] - 1
    assert m["years"][2021]["spy"] == pytest.approx(spy_2021)


def test_metrics_without_trades():
    idx = _index(300)
    equity = pd.Series(10_000.0, index=idx)
    m = metrics({"equity": equity, "exposure": pd.Series(0.0, index=idx), "trades": [], "costs": 0.0}, _spy(idx))
    assert m["trades"] == 0 and m["win_rate"] is None and m["payoff"] is None and m["top5_share"] is None
    assert m["cagr"] == 0 and m["max_drawdown"] == 0 and m["spy_at_exposure_cagr"] == pytest.approx(0)


# --- review carry-overs: the trade statistics and SPY's base ---------------------------------

def test_trade_statistics_use_the_return_after_costs():
    idx = _index(300)
    equity = pd.Series(10_000.0, index=idx)
    # up 0.2% before costs, but the costs make it a loss of 0.5% of the entry value
    small = {**_trade(0.002, -5.0, 10)}
    trades = [_trade(0.10, 100.0, 10), small]
    m = metrics({"equity": equity, "exposure": pd.Series(0.0, index=idx), "trades": trades, "costs": 0.0},
                _spy(idx))
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["avg_win"] == pytest.approx(100.0 / 1000.0)
    assert m["avg_loss"] == pytest.approx(-5.0 / 1000.0)          # negative, not +0.2%
    assert m["payoff"] == pytest.approx(0.10 / 0.005)


def test_metrics_takes_spy_on_or_before_the_first_equity_day():
    idx = _index(300)
    equity = pd.Series(10_000.0, index=idx)
    # SPY has no close on the first equity day: the day before (100) is the base, not the day after (200)
    spy = pd.Series([100.0, 200.0, 200.0], index=[idx[0] - pd.Timedelta(days=3), idx[1], idx[-1]])
    m = metrics({"equity": equity, "exposure": pd.Series(0.0, index=idx), "trades": [], "costs": 0.0}, spy)
    assert m["spy_cagr"] == pytest.approx(cagr(pd.Series([100.0, 200.0], index=[idx[0], idx[-1]])))
    assert m["spy_max_drawdown"] == 0
    assert m["years"][2020]["spy"] == pytest.approx(1.0)                       # 100 -> 200 in 2020


# --- data loading ----------------------------------------------------------------------------

def _write_prices(paths, ticker: str, dates, closes) -> None:
    folder = paths.cache / "backtest" / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"date": pd.DatetimeIndex(dates).strftime("%Y-%m-%d"), "close": closes,
                          "adj_close": [c / 2 for c in closes], "split": 0.0})
    frame.dropna(subset=["close"]).to_csv(folder / f"{ticker}.csv", index=False)


def test_load_closes_aligns_to_spy_and_fills_short_inside_gaps(tmp_path):
    from value_pipeline.config import Paths

    paths = Paths(tmp_path / "data")
    days = pd.bdate_range("2019-01-01", periods=420)
    _write_prices(paths, "SPY", days, [100.0 + i for i in range(420)])
    aaa = [10.0 + i for i in range(420)]
    for i in list(range(330, 333)) + list(range(350, 357)):     # a 3-row gap and a 7-row gap
        aaa[i] = float("nan")
    _write_prices(paths, "AAA", days, aaa)
    late = [float("nan")] * 340 + [50.0 + i for i in range(60)] + [float("nan")] * 20   # starts late, ends early
    _write_prices(paths, "BBB", days, late)
    start = str(days[400].date())
    closes, spy = load_closes(paths, ["AAA", "BBB", "NOPE"], start, fetch=False)

    assert closes.index[0] == days[100] and closes.index[-1] == days[-1]     # 300 rows before the start
    assert list(closes.columns) == ["AAA", "BBB"]
    assert spy.index.equals(closes.index) and spy.iloc[0] == pytest.approx((100.0 + 100) / 2)   # adj_close
    a = closes["AAA"]
    assert a.loc[days[330:333]].tolist() == [a.loc[days[329]]] * 3            # 3-row gap carried forward
    assert a.loc[days[350:357]].isna().all()                                   # 7-row gap left alone
    b = closes["BBB"]
    assert b.loc[:days[339]].isna().all() and b.loc[days[400]:].isna().all()   # never before the first or after the last
    assert closes.attrs["filled"] == {"AAA": 3}
    assert closes.attrs["missing"] == ["NOPE"]


# --- universes, verdict ----------------------------------------------------------------------

def test_quality_windows_run_from_cohort_to_cohort_and_the_last_to_the_end():
    rows = {"2015-06-30": [{"ticker": "AAA", "passes_filters": True}, {"ticker": "BBB", "passes_filters": False}],
            "2016-06-30": [{"ticker": "BBB", "passes_filters": True}]}
    w = quality_windows(rows, "2017-03-01")
    assert w["AAA"] == [(date(2015, 6, 30), date(2016, 6, 30))]
    assert w["BBB"] == [(date(2016, 6, 30), date(2017, 3, 1))]
    assert set(w) == {"AAA", "BBB"}


def test_quality_windows_join_consecutive_cohorts_and_keep_gaps():
    rows = {"2015-06-30": [{"ticker": "AAA", "passes_filters": True}],
            "2016-06-30": [{"ticker": "AAA", "passes_filters": True}],
            "2017-06-30": [{"ticker": "AAA", "passes_filters": False}],
            "2018-06-30": [{"ticker": "AAA", "passes_filters": True}]}
    assert quality_windows(rows, "2019-01-01")["AAA"] == [(date(2015, 6, 30), date(2017, 6, 30)),
                                                          (date(2018, 6, 30), date(2019, 1, 1))]


def test_verdict_supports_on_return_or_on_drawdown():
    base = {"spy_cagr": 0.12, "spy_max_drawdown": 0.33, "years_gained": 0.7}
    ok_return = {**base, "cagr": 0.135, "max_drawdown": 0.40}
    ok_risk = {**base, "cagr": 0.105, "max_drawdown": 0.20}
    assert verdict(ok_return, ok_return)["supports"] is True
    assert verdict(ok_risk, ok_risk)["supports"] is True
    assert verdict({**base, "cagr": 0.11, "max_drawdown": 0.30}, ok_return)["supports"] is False   # neither leg
    assert verdict(ok_return, {**base, "cagr": 0.09, "max_drawdown": 0.40})["supports"] is False   # fails at the high cost
    assert verdict({**ok_return, "years_gained": 0.5}, ok_return)["supports"] is False             # too few good years
    assert "supports a live sleeve" in verdict(ok_return, ok_return)["sentence"]
    assert "does not support a live sleeve" in verdict({**ok_return, "years_gained": 0.5}, ok_return)["sentence"]


def test_verdict_returns_the_numbers_and_takes_the_bounds_inclusive():
    edge = {"spy_cagr": 0.12, "spy_max_drawdown": 0.30, "years_gained": 0.6, "cagr": 0.10, "max_drawdown": 0.20}
    v = verdict(edge, {**edge, "cagr": 0.13, "max_drawdown": 0.5})
    assert v["supports"] is True
    assert {k: v[k] for k in ("cagr", "spy_cagr", "max_drawdown", "spy_max_drawdown", "years_gained")} == {
        "cagr": 0.10, "spy_cagr": 0.12, "max_drawdown": 0.20, "spy_max_drawdown": 0.30, "years_gained": 0.6}
    assert "10.0%" in v["sentence"] and "12.0%" in v["sentence"] and "60%" in v["sentence"]
    assert verdict({**edge, "years_gained": 0.59}, edge)["supports"] is False
    assert verdict({**edge, "max_drawdown": 0.2001}, edge)["supports"] is False


# --- the run, the report, the files, the command ---------------------------------------------

RUN_TICKERS = ["AAA", "BBB", "CCC"]


def _run_closes(rows: int = 700) -> tuple[pd.DataFrame, pd.Series]:
    idx = pd.bdate_range("2014-01-01", periods=rows)
    rng = np.random.default_rng(7)
    cols = {}
    for k, t in enumerate(RUN_TICKERS):
        steps = rng.normal(0.0008 * (k + 1), 0.015, rows)
        cols[t] = 50.0 * np.cumprod(1 + steps)
    closes = pd.DataFrame(cols, index=idx)
    closes.attrs = {"filled": {"BBB": 2}, "missing": ["ZZZ"]}
    spy = pd.Series(200.0 * np.cumprod(1 + rng.normal(0.0004, 0.01, rows)), index=idx)
    return closes, spy


def _patched_run(tmp_path, monkeypatch, start_row: int = 320):
    from value_pipeline import backtest, market_data
    from value_pipeline.config import Paths

    closes, spy = _run_closes()
    start = str(closes.index[start_row].date())
    seen = {}

    def fake_lane(paths, cfg, **kw):
        seen["lane"] = (cfg["universe"]["indexes"], kw)
        return {"rows": {"2015-06-30": [{"ticker": "AAA", "passes_filters": True},
                                        {"ticker": "BBB", "passes_filters": True},
                                        {"ticker": "CCC", "passes_filters": False}]},
                "added": {"CCC": "2015-03-01"}}

    def fake_load(paths, tickers, start_, fetch=True, progress=None):
        seen["load"] = (sorted(tickers), start_, fetch)
        return closes, spy

    def fake_universe(paths, indexes):
        seen["universe"] = indexes
        return pd.DataFrame({"ticker": RUN_TICKERS, "sector": ["Industrials"] * 3})

    monkeypatch.setattr(backtest, "run", fake_lane)
    monkeypatch.setattr(trend, "load_closes", fake_load)
    monkeypatch.setattr(market_data, "universe", fake_universe)
    cfg = {"trend": {**DEFAULTS["trend"], "start": start}, "universe": {"indexes": ["sp500", "ftse100"]}}
    result = trend.run(Paths(tmp_path / "data"), cfg, today=date(2026, 11, 5), fetch=False)
    return result, seen, cfg


def test_run_reports_every_system_and_universe(tmp_path, monkeypatch):
    result, seen, cfg = _patched_run(tmp_path, monkeypatch)
    assert seen["universe"] == ["sp500", "sp400"]
    assert seen["lane"][0] == ["sp500", "sp400"] and seen["lane"][1]["keep_rows"] is True
    assert seen["lane"][1]["fetch"] is False
    assert cfg["universe"]["indexes"] == ["sp500", "ftse100"]                  # the caller's config untouched
    assert seen["load"] == (RUN_TICKERS, cfg["trend"]["start"], False)
    assert result["generated"] == "2026-11-05" and result["start"] == cfg["trend"]["start"]
    assert result["end"] == "2016-09-06"
    assert result["settings"] == settings(cfg)
    assert result["universe_sizes"] == {"all": 3, "quality": 2}
    assert set(result["results"]) == {(s, u) for s in SYSTEMS for u in ("all", "quality")}
    assert set(result["trades"]) == set(result["results"])
    for m in result["results"].values():
        assert {"cagr", "max_drawdown", "spy_cagr", "years_gained"} <= set(m)
    assert result["filled"] == {"cells": 2, "tickers": 1} and result["missing"] == ["ZZZ"]
    # quality trades only in AAA and BBB; CCC trades in `all` only from its join date
    assert {t["ticker"] for t in result["trades"][("breakout_20_10", "quality")]} <= {"AAA", "BBB"}
    assert all(t["signal_date"] >= date(2015, 3, 1)
               for t in result["trades"][("breakout_20_10", "all")] if t["ticker"] == "CCC")
    assert any(t["ticker"] == "CCC" for k, ts in result["trades"].items() if k[1] == "all" for t in ts)
    # the sensitivity grid on the primary
    grid = [(r["setting"], r["value"]) for r in result["sensitivity"]]
    assert grid == [("max_positions", 5), ("max_positions", 10), ("max_positions", 20),
                    ("cost_per_side", 0.0), ("cost_per_side", 0.0035), ("cost_per_side", 0.006),
                    ("market_filter", True), ("market_filter", False),
                    ("risk_per_trade", 0.005), ("risk_per_trade", 0.01), ("risk_per_trade", 0.02)]
    default = next(r for r in result["sensitivity"] if r["setting"] == "max_positions" and r["value"] == 10)
    assert default["cagr"] == pytest.approx(result["results"][("breakout_55_20", "quality")]["cagr"])
    assert set(result["verdicts"]) == {"breakout_55_20", "momentum_12_1"}
    high = next(r for r in result["sensitivity"] if r["setting"] == "cost_per_side" and r["value"] == 0.006)
    primary = result["results"][("breakout_55_20", "quality")]
    assert result["verdicts"]["breakout_55_20"] == verdict(primary, high)


def test_report_states_the_verdict_and_the_limits(tmp_path, monkeypatch):
    result, _, _ = _patched_run(tmp_path, monkeypatch)
    text = render_report(result)
    for v in result["verdicts"].values():
        assert v["sentence"] in text
    for want in ("survivorship", "next close", "fractional", "close-only", "shorted",
                 "2 missing closes in 1 ticker", "ZZZ"):
        assert want in text.lower() or want in text, want
    assert "| Year | System | SPY |" in text
    years = text.split("| Year | System | SPY |")[1].split("\n## ")[0]
    assert "| 2015 |" in years and "| 2016 |" in years
    summary = text.split("## Summary")[1].split("\n## ")[0]
    for system in SYSTEMS:
        for universe in ("all", "quality"):
            assert summary.count(f"| {system} | {universe} |") == 1, (system, universe)
    sens = text.split("## Sensitivity")[1].split("\n## ")[0]
    assert "| max_positions | 20 |" in sens and "| market_filter | off |" in sens
    assert result["start"] in text and result["end"] in text


def test_write_saves_markdown_html_and_trades(tmp_path, monkeypatch):
    from value_pipeline.config import Paths

    result, _, _ = _patched_run(tmp_path, monkeypatch)
    paths = Paths(tmp_path / "data")
    assert latest_report(paths) is None
    md = write(paths, result)
    folder = paths.reports / "backtest"
    assert md == folder / "trend_2026-11-05.md"
    assert md.read_text(encoding="utf-8") == render_report(result)
    html = (folder / "trend_2026-11-05.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "<table>" in html
    trades = pd.read_csv(folder / "trend_2026-11-05_trades.csv")
    assert list(trades.columns[:3]) == ["system", "universe", "ticker"]
    assert len(trades) == sum(len(t) for t in result["trades"].values())
    assert sorted(p.name for p in folder.iterdir()) == ["trend_2026-11-05.html", "trend_2026-11-05.md",
                                                        "trend_2026-11-05_trades.csv"]
    (folder / "trend_2026-01-02.html").write_text("old")
    assert latest_report(paths) == folder / "trend_2026-11-05.html"


def test_cli_trend_backtest_writes_the_report(tmp_path, monkeypatch, capsys):
    from value_pipeline import cli

    result, _, _ = _patched_run(tmp_path, monkeypatch)
    calls = {}

    def fake_run(paths, cfg, today=None, fetch=True, progress=None):
        calls.update(start=cfg["trend"]["start"], fetch=fetch)
        return result

    monkeypatch.setattr(trend, "run", fake_run)
    data = tmp_path / "data"
    cli.main(["--data-dir", str(data), "trend-backtest", "--from", "2016-01-04", "--no-fetch"])
    out = " ".join(capsys.readouterr().out.split())        # rich wraps the long lines
    assert calls == {"start": "2016-01-04", "fetch": False}
    assert "live sleeve" in out and "trend_2026-11-05.md" in out
    assert (data / "reports" / "backtest" / "trend_2026-11-05.html").exists()
