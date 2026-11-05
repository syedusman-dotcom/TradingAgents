"""Trend-sleeve simulation engine on synthetic closes (no network; every price is built here)."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from value_pipeline.config import DEFAULTS
from value_pipeline.trend import SYSTEMS, Settings, cagr, max_drawdown, metrics, settings, simulate

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
