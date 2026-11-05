"""Lane-rule backtest: prices, the cohort snapshot, rules, returns and summary (no network; data faked)."""

import copy
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from tradingagents.agents import decision_math as dm
from tradingagents.dataflows.value_metrics import EQUITY_PREMIUM, MIN_DISCOUNT_RATE
from value_pipeline import backtest, metrics, pit_statements as pit
from value_pipeline.config import CONFIG_PATH, DEFAULTS, Paths, load_config

pytestmark = pytest.mark.unit

SPLIT_DAY = "2024-09-03"


def _hist(start="2023-06-01", days=500, split_day=SPLIT_DAY) -> pd.DataFrame:
    """~500 business days of closes rising 0.1 a day, dividends folded into ``adj_close`` and
    a 2:1 split row."""
    dates = pd.bdate_range(start, periods=days)
    close = [100.0 + 0.1 * i for i in range(days)]
    return pd.DataFrame({
        "date": dates,
        "close": close,
        "adj_close": [c * 0.9 for c in close],
        "split": [2.0 if d == pd.Timestamp(split_day) else 0.0 for d in dates],
    })


# --- price history -------------------------------------------------------------------------

class _FakeTicker:
    frames: dict = {}
    calls: list = []

    def __init__(self, ticker):
        self.ticker = ticker

    def history(self, **kwargs):
        _FakeTicker.calls.append((self.ticker, kwargs))
        frame = _FakeTicker.frames[self.ticker]
        if isinstance(frame, Exception):
            raise frame
        return frame


def _yahoo_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"]).tz_localize(
        "America/New_York")
    return pd.DataFrame({"Open": [1.0, 1.0, 1.0], "Close": [10.0, 11.0, 12.0],
                         "Adj Close": [9.0, 10.0, 11.0], "Stock Splits": [0.0, 2.0, 0.0]},
                        index=index)


def test_price_history_caches_csv_and_fails_open(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    _FakeTicker.frames = {"ABC": _yahoo_frame(), "^TNX": _yahoo_frame(),
                          "EMPTY": pd.DataFrame(), "BOOM": RuntimeError("Yahoo down")}
    _FakeTicker.calls = []
    monkeypatch.setattr(backtest.yf, "Ticker", _FakeTicker)

    hist = backtest.price_history("ABC", paths)
    assert list(hist.columns) == ["date", "close", "adj_close", "split"]
    assert pd.api.types.is_datetime64_any_dtype(hist["date"])
    assert hist["date"].dt.tz is None
    assert [d.date().isoformat() for d in hist["date"]] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert hist["close"].tolist() == [10.0, 11.0, 12.0]
    assert hist["adj_close"].tolist() == [9.0, 10.0, 11.0]
    assert hist["split"].tolist() == [0.0, 2.0, 0.0]
    assert _FakeTicker.calls == [("ABC", {"period": "max", "auto_adjust": False})]
    csv = paths.cache / "backtest" / "prices" / "ABC.csv"
    assert csv.read_text().splitlines()[1].startswith("2024-01-02,")

    # Cached: neither a second call nor fetch=False touches Yahoo, and the frame is the same.
    _FakeTicker.frames["ABC"] = RuntimeError("must not be called")
    again = backtest.price_history("ABC", paths, fetch=False)
    pd.testing.assert_frame_equal(again, hist)
    pd.testing.assert_frame_equal(backtest.price_history("ABC", paths), hist)
    assert len(_FakeTicker.calls) == 1

    # A refresh that fails keeps the cached copy.
    pd.testing.assert_frame_equal(backtest.price_history("ABC", paths, refresh=True), hist)

    assert backtest.price_history("^TNX", paths) is not None
    assert (paths.cache / "backtest" / "prices" / "TNX.csv").exists()

    assert backtest.price_history("EMPTY", paths) is None
    assert backtest.price_history("BOOM", paths) is None
    calls = len(_FakeTicker.calls)
    assert backtest.price_history("NEW", paths, fetch=False) is None
    assert len(_FakeTicker.calls) == calls
    (paths.cache / "backtest" / "prices" / "BAD.csv").write_text("not,a\nprice,file\n")
    assert backtest.price_history("BAD", paths, fetch=False) is None


def test_price_history_refetches_a_stale_cache_only_when_fetching(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    _FakeTicker.frames = {"ABC": _yahoo_frame()}                  # ends 2024-01-04
    _FakeTicker.calls = []
    monkeypatch.setattr(backtest.yf, "Ticker", _FakeTicker)
    first = backtest.price_history("ABC", paths)
    assert len(_FakeTicker.calls) == 1
    # Fresh: the cache reaches the date asked for.
    backtest.price_history("ABC", paths, stale_before="2024-01-04")
    assert len(_FakeTicker.calls) == 1
    # Stale, but fetch=False: the cache as it is.
    stale = backtest.price_history("ABC", paths, fetch=False, stale_before="2024-03-01")
    pd.testing.assert_frame_equal(stale, first)
    assert len(_FakeTicker.calls) == 1
    # Stale and fetching: refetched and rewritten.
    longer = _yahoo_frame()
    longer.index = longer.index + pd.Timedelta(days=60)
    _FakeTicker.frames["ABC"] = longer
    got = backtest.price_history("ABC", paths, stale_before="2024-03-01")
    assert len(_FakeTicker.calls) == 2
    assert got["date"].iloc[-1] == pd.Timestamp("2024-03-04")
    # A stale cache whose refetch fails is kept.
    _FakeTicker.frames["ABC"] = RuntimeError("Yahoo down")
    kept = backtest.price_history("ABC", paths, stale_before="2025-01-01")
    assert len(_FakeTicker.calls) == 3 and kept["date"].iloc[-1] == pd.Timestamp("2024-03-04")


def test_price_history_warns_when_yahoo_has_no_adjusted_close(tmp_path, monkeypatch, caplog):
    paths = Paths(tmp_path / "data")
    _FakeTicker.frames = {"NOADJ": _yahoo_frame().drop(columns=["Adj Close"])}
    _FakeTicker.calls = []
    monkeypatch.setattr(backtest.yf, "Ticker", _FakeTicker)
    with caplog.at_level("WARNING", logger=backtest.log.name):
        hist = backtest.price_history("NOADJ", paths)
    assert hist["adj_close"].tolist() == hist["close"].tolist() == [10.0, 11.0, 12.0]
    assert any("NOADJ" in r.getMessage() and "Adj Close" in r.getMessage()
               for r in caplog.records if r.levelname == "WARNING")
    assert [p.name for p in (paths.cache / "backtest" / "prices").iterdir()] == ["NOADJ.csv"]


# --- helpers -------------------------------------------------------------------------------

def test_close_on_or_after_skips_a_weekend():
    hist = _hist()
    # 2023-06-03 is a Saturday: the next close is Monday the 5th (the third row).
    assert backtest.close_on_or_after(hist, "2023-06-03") == (date(2023, 6, 5), pytest.approx(100.2))
    assert backtest.adj_close_on_or_after(hist, "2023-06-03") == (
        date(2023, 6, 5), pytest.approx(100.2 * 0.9))
    assert backtest.close_on_or_after(hist, "2023-06-01") == (date(2023, 6, 1), 100.0)
    last = hist["date"].iloc[-1].date()
    assert backtest.close_on_or_after(hist, (last + timedelta(days=1)).isoformat()) is None
    gap = hist[(hist["date"] < "2024-01-01") | (hist["date"] > "2024-01-20")]
    assert backtest.close_on_or_after(gap, "2024-01-05") is None
    assert backtest.close_on_or_after(gap, "2024-01-05", within=20)[0] == date(2024, 1, 22)
    assert backtest.close_on_or_after(None, "2024-01-05") is None


def test_closes_before_is_oldest_first_and_capped():
    hist = _hist()
    closes = backtest.closes_before(hist, "2023-06-07")      # Thursday: rows 0..4
    assert closes == pytest.approx([100.0, 100.1, 100.2, 100.3, 100.4])
    assert backtest.closes_before(hist, "2023-06-07", n=2) == pytest.approx([100.3, 100.4])
    full = backtest.closes_before(hist, "2030-01-01")
    assert len(full) == 420 and full[-1] == pytest.approx(100.0 + 0.1 * 499)
    assert full[0] == pytest.approx(100.0 + 0.1 * 80)
    assert backtest.closes_before(hist, "2023-05-31") == []
    assert backtest.closes_before(None, "2023-05-31") == []


def test_split_factor_after():
    hist = _hist()
    assert backtest.split_factor_after(hist, "2024-01-01") == 2.0
    assert backtest.split_factor_after(hist, SPLIT_DAY) == 1.0      # on the day: already counted
    assert backtest.split_factor_after(hist, "2024-08-30") == 2.0
    hist.loc[hist["date"] == pd.Timestamp("2024-12-02"), "split"] = 3.0
    assert backtest.split_factor_after(hist, "2024-01-01") == 6.0
    assert backtest.split_factor_after(hist, "2024-10-01") == 3.0
    assert backtest.split_factor_after(None, "2024-01-01") == 1.0


def test_total_return_normal():
    hist = _hist()
    out = backtest.total_return(hist, "2023-06-03", "2024-06-01")   # Sat -> Mon; Sat -> Mon
    assert out["start_date"] == "2023-06-05" and out["end_date"] == "2024-06-03"
    start = hist.loc[hist["date"] == "2023-06-05", "adj_close"].iloc[0]
    end = hist.loc[hist["date"] == "2024-06-03", "adj_close"].iloc[0]
    assert out["ret"] == pytest.approx(end / start - 1)
    assert out["ended_early"] is False


def test_total_return_ended_early():
    hist = _hist()
    out = backtest.total_return(hist, "2023-06-05", "2026-01-01")
    last = hist.iloc[-1]
    assert out["end_date"] == last["date"].date().isoformat()
    assert out["ret"] == pytest.approx(last["adj_close"] / (100.2 * 0.9) - 1)
    assert out["ended_early"] is True


def test_total_return_missing_start():
    hist = _hist()
    assert backtest.total_return(hist, "2020-01-01", "2024-01-01") is None   # none within 10d
    assert backtest.total_return(hist, "2023-05-25", "2024-01-01")["start_date"] == "2023-06-01"
    late = hist[hist["date"] >= "2023-07-01"]
    assert backtest.total_return(late, "2023-06-01", "2024-01-01") is None
    assert backtest.total_return(hist, "2026-01-01", "2027-01-01") is None
    assert backtest.total_return(None, "2023-06-01", "2024-01-01") is None


def test_discount_rate_on_floor_and_tnx():
    tnx = pd.DataFrame({"date": pd.to_datetime(["2023-01-03", "2023-06-01", "2023-06-05"]),
                        "close": [3.0, 5.0, 6.0], "adj_close": [3.0, 5.0, 6.0],
                        "split": [0.0, 0.0, 0.0]})
    assert backtest.discount_rate_on(None, "2023-06-01") == MIN_DISCOUNT_RATE
    assert backtest.discount_rate_on(tnx, "2022-12-30") == MIN_DISCOUNT_RATE     # no row yet
    assert backtest.discount_rate_on(tnx, "2023-03-01") == MIN_DISCOUNT_RATE     # 3% + premium
    assert backtest.discount_rate_on(tnx, "2023-06-04") == pytest.approx(0.05 + EQUITY_PREMIUM)
    assert backtest.discount_rate_on(tnx, "2023-06-05") == pytest.approx(0.06 + EQUITY_PREMIUM)


# --- snapshot ------------------------------------------------------------------------------

AS_OF = "2024-06-28"
SHARES = 1e8
YEARS = range(2018, 2024)          # six fiscal years, each filed the next 02-20


def _fact(end, val, filed, start=None, form="10-K"):
    fact = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        fact["start"] = start
    return fact


M = 1e7           # dollar figures in tens of millions, shares in millions: a $25bn company


def _flows(fn, unit="USD"):
    return {"units": {unit: [_fact(f"{y}-12-31", fn(y) * M, f"{y + 1}-02-20", start=f"{y}-01-01")
                             for y in YEARS]}}


def _points(fn, unit="USD"):
    return {"units": {unit: [_fact(f"{y}-12-31", fn(y) * M, f"{y + 1}-02-20") for y in YEARS]}}


def _snap_store() -> dict:
    """A compounder's filings in ``extract``'s shape: revenue and FCF growing 10%/yr, ROIC
    including goodwill near 50% every year, 100M shares (a cover-page count on 2024-02-10)."""
    def g(y):
        return 1.1 ** (y - 2018)
    return {
        "Revenues": _flows(lambda y: 1000.0 * g(y)),
        "OperatingIncomeLoss": _flows(lambda y: 250.0 * g(y)),
        "NetIncomeLoss": _flows(lambda y: 180.0 * g(y)),
        "InterestExpense": _flows(lambda y: 5.0),
        "Assets": _points(lambda y: 2000.0 * g(y)),
        "AssetsCurrent": _points(lambda y: 600.0 * g(y)),
        "LiabilitiesCurrent": _points(lambda y: 300.0 * g(y)),
        "CashAndCashEquivalentsAtCarryingValue": _points(lambda y: 200.0 * g(y)),
        "PropertyPlantAndEquipmentNet": _points(lambda y: 300.0 * g(y)),
        "Goodwill": _points(lambda y: 100.0),
        "LongTermDebtNoncurrent": _points(lambda y: 200.0),
        "StockholdersEquity": _points(lambda y: 1200.0 * g(y)),
        "Liabilities": _points(lambda y: 800.0 * g(y)),
        "RetainedEarningsAccumulatedDeficit": _points(lambda y: 900.0 * g(y)),
        "NetCashProvidedByUsedInOperatingActivities": _flows(lambda y: 260.0 * g(y)),
        "PaymentsToAcquirePropertyPlantAndEquipment": _flows(lambda y: 60.0 * g(y)),
        "CommonStockSharesOutstanding": _points(lambda y: SHARES / M, unit="shares"),
        pit.SHARES_DEI: {"units": {"shares": [_fact("2024-02-10", SHARES, "2024-02-20")]}},
    }


def _cfg() -> dict:
    return {"screen": dict(DEFAULTS["screen"]), "filters": dict(DEFAULTS["filters"])}


def _snapshot(store, hist=None, as_of=AS_OF, rate=0.09):
    return backtest.snapshot("SYN", as_of, store, _hist() if hist is None else hist, rate, _cfg(),
                             sector="Industrials")


def test_market_cap_uses_split_factor():
    row, (years, price, market_cap, shares) = _snapshot(_snap_store())
    assert price == backtest.closes_before(_hist(), AS_OF)[-1]
    # The 2:1 split after the date brings EDGAR's 100M shares to today's basis.
    assert shares == 2 * SHARES
    assert market_cap == pytest.approx(price * 2 * SHARES)
    assert row["market_cap"] == pytest.approx(market_cap)
    assert row["price"] == price and row["sector"] == "Industrials" and row["as_of"] == AS_OF
    assert all(y["shares"] == 2 * SHARES for y in years)      # years on the same split basis
    no_split = _hist(split_day="2030-01-01")
    _, (_, _, cap, n) = _snapshot(_snap_store(), hist=no_split)
    assert n == SHARES and cap == pytest.approx(price * SHARES)


def test_market_cap_counts_a_split_between_the_cover_date_and_the_date():
    # The cover count (100M) is dated 2024-02-10; a 2:1 split on 2024-04-01 is before AS_OF
    # and already in Yahoo's close, so the count is doubled to meet it.
    hist = _hist(split_day="2024-04-01")
    row, (years, price, market_cap, shares) = _snapshot(_snap_store(), hist=hist)
    assert shares == 2 * SHARES == years[0]["shares"]
    assert market_cap == pytest.approx(price * 2 * SHARES)
    assert row["market_cap"] == pytest.approx(market_cap)


def test_snapshot_none_on_a_stale_price():
    hist = _hist()
    stopped = hist[hist["date"] <= "2024-06-14"]          # 14 days before AS_OF: delisted
    assert _snapshot(_snap_store(), hist=stopped) is None
    recent = hist[hist["date"] <= "2024-06-18"]           # 10 days before: still trading
    assert _snapshot(_snap_store(), hist=recent) is not None


def test_snapshot_feeds_four_years_and_the_pm_record():
    store = _snap_store()
    row, (years, *_) = _snapshot(store)
    assert [y["fiscal_year_end"] for y in years] == [f"{y}-12-31" for y in (2023, 2022, 2021, 2020)]
    assert row["fiscal_year_end"] == "2023-12-31"
    assert row["roic_years"] <= backtest.SCREEN_YEARS
    assert row["revenue_cagr_years"] == 3                  # the screen's record: four years only
    rec = metrics.record(years, pit.revenue_history(store, AS_OF))
    assert rec["revenue_cagr_years"] == 5                  # EDGAR's six years lengthen the PM's
    assert backtest.pm_record_ok(rec) is True
    assert row["pm_record_ok"] is True
    assert row["passes_filters"] is True and row["fail_reasons"] == ""
    assert row["lane_hints"] == ["compounder"] and row["lane_hint"] == "compounder"
    assert "incremental_roic" in row
    assert row["incremental_roic"] == metrics.inflection(
        years, pit.quarters_as_of(store, AS_OF))["incremental_roic"]

    # Two years of history: the screen still runs, the PM's record (five-year CAGR) does not.
    short = {tag: {"units": {u: [f for f in facts if f["end"] >= "2022"]
                             for u, facts in v["units"].items()}} for tag, v in store.items()}
    row2, _ = _snapshot(short)
    assert row2["pm_record_ok"] is False
    assert row2["lane_hints"] == []


def _rec(**over) -> dict:
    """A record exactly at every PM bar."""
    n = dm.COMPOUNDER_MIN_ROIC_YEARS
    rec = {"roic_years_counted": n, "roic_gw_years_above_15": n,
           "roic_gw_min": dm.COMPOUNDER_ROIC_INCL_GOODWILL,
           "revenue_cagr": dm.COMPOUNDER_REVENUE_CAGR,
           "revenue_cagr_years": dm.COMPOUNDER_MIN_CAGR_YEARS,
           "fcf_cagr": dm.COMPOUNDER_FCF_CAGR, "fcf_cagr_years": dm.COMPOUNDER_MIN_FCF_YEARS}
    rec.update(over)
    return rec


def test_pm_record_ok_at_each_bar():
    n = dm.COMPOUNDER_MIN_ROIC_YEARS
    roic, cagr, fcf = (dm.COMPOUNDER_ROIC_INCL_GOODWILL, dm.COMPOUNDER_REVENUE_CAGR,
                       dm.COMPOUNDER_FCF_CAGR)
    assert backtest.pm_record_ok(_rec()) is True
    assert backtest.pm_record_ok(_rec(roic_gw_min=roic - 1e-12, fcf_cagr=fcf - 1e-12)) is True
    for change in (
        {"roic_years_counted": n - 1, "roic_gw_years_above_15": n - 1},
        {"roic_gw_years_above_15": n - 1},
        {"roic_gw_min": roic - 1e-4},
        {"roic_years_counted": None},
        {"roic_gw_years_above_15": None},
        {"revenue_cagr": cagr - 1e-4},
        {"revenue_cagr": None},
        {"revenue_cagr_years": dm.COMPOUNDER_MIN_CAGR_YEARS - 1},
        {"revenue_cagr_years": None},
        {"fcf_cagr": fcf - 1e-4},
        {"fcf_cagr": None},
        {"fcf_cagr_years": dm.COMPOUNDER_MIN_FCF_YEARS - 1},
        {"fcf_cagr_years": None},
    ):
        assert backtest.pm_record_ok(_rec(**change)) is False, change
    assert backtest.pm_record_ok(_rec(roic_gw_min=None)) is True   # as _compounder_problems


def _dump(row) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def test_snapshot_ignores_facts_filed_after_the_date():
    before = _snapshot(_snap_store())
    store = _snap_store()
    later = (date.fromisoformat(AS_OF) + timedelta(days=1)).isoformat()
    store["Revenues"]["units"]["USD"].append(
        _fact("2023-12-31", 5000.0 * M, later, start="2023-01-01", form="10-K/A"))
    store["Revenues"]["units"]["USD"].append(
        _fact("2024-03-31", 400.0 * M, later, start="2024-01-01", form="10-Q"))
    store[pit.SHARES_DEI]["units"]["shares"].append(_fact("2024-06-20", 9 * SHARES, later))
    after = _snapshot(store)
    assert _dump(after[0]) == _dump(before[0])
    assert _dump(after[1]) == _dump(before[1])
    # The closes after the date are in the frame too, and unused: cut them off, same row.
    hist = _hist()
    cut = _snapshot(_snap_store(), hist=hist[hist["date"] <= AS_OF].reset_index(drop=True)
                    .assign(split=0.0))
    assert cut[0]["price"] == before[0]["price"] and cut[0]["trend"] == before[0]["trend"]


def test_snapshot_none_without_two_years():
    store = _snap_store()
    early = _hist(start="2020-06-01")
    assert _snapshot(store, hist=early, as_of="2020-01-01") is None    # only 2018 filed by then
    assert _snapshot(store, hist=early, as_of="2021-01-01") is not None  # 2018 and 2019
    assert _snapshot(store, hist=_hist(start="2024-07-01")) is None   # no close by the date
    assert backtest.snapshot("SYN", AS_OF, store, None, 0.09, _cfg()) is None


# --- cohorts -------------------------------------------------------------------------------

def _bt_cfg(**over) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    cfg["backtest"].update(over)
    return cfg


def test_cohort_dates_stop_at_completed_holds():
    cfg = _bt_cfg(cohort_days=["06-30"])
    assert backtest.cohort_dates(cfg, date(2025, 6, 30)) == [f"{y}-06-30" for y in range(2015, 2025)]
    assert backtest.cohort_dates(cfg, date(2025, 6, 29)) == [f"{y}-06-30" for y in range(2015, 2024)]
    assert backtest.cohort_dates(_bt_cfg(first_year=2018, last_year=2030, cohort_days=["03-31"]),
                                 date(2021, 3, 30)) == ["2018-03-31", "2019-03-31"]
    assert backtest.cohort_dates(cfg, date(2016, 6, 1)) == []


def test_cohort_dates_are_quarterly_and_in_order():
    cfg = _bt_cfg(first_year=2023, last_year=2024, cohort_days=["03-31", "06-30", "09-30", "12-31"])
    assert backtest.cohort_dates(cfg, date(2025, 7, 1)) == [
        "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30"]


def test_the_default_cohorts_are_quarterly():
    assert backtest.cohort_days(DEFAULTS) == ["03-31", "06-30", "09-30", "12-31"]


def test_an_old_config_with_cohort_day_still_means_one_date_a_year():
    cfg = {"backtest": {"first_year": 2022, "last_year": 2023, "cohort_day": "06-30", "holds": [1, 3]}}
    assert backtest.cohort_days(cfg) == ["06-30"]
    assert backtest.cohort_dates(cfg, date(2030, 1, 1)) == ["2022-06-30", "2023-06-30"]


def test_cohort_days_wins_over_cohort_day():
    cfg = {"backtest": {"first_year": 2022, "last_year": 2022, "cohort_day": "06-30",
                        "cohort_days": ["03-31"], "holds": [1]}}
    assert backtest.cohort_dates(cfg, date(2030, 1, 1)) == ["2022-03-31"]


def test_finish_cohort_applies_sector_targets_and_ranks(monkeypatch):
    calls = []

    def fake_targets(rows, inputs):
        calls.append((rows, inputs))
        for r in rows:
            r["compounder_return"] = "sector-adjusted"

    monkeypatch.setattr(backtest.screener, "apply_sector_targets", fake_targets)
    ey, roic = backtest.screener.EY_KEY, backtest.screener.ROIC_KEY

    def row(ticker, ey_v, roic_v, passes=True, trend="UPTREND"):
        return {"ticker": ticker, ey: ey_v, roic: roic_v, "passes_filters": passes, "trend": trend}

    rows = [row("B", 0.05, 0.20), row("A", 0.10, 0.30), row("C", 0.20, 0.50, trend="FREEFALL"),
            row("D", 0.30, 0.90, passes=False), row("E", None, 0.40)]
    inputs = {r["ticker"]: ([], 1.0, 1.0, 1.0) for r in rows}
    backtest.finish_cohort(rows, inputs)
    assert len(calls) == 1 and calls[0][0] is rows and calls[0][1] is inputs
    assert all(r["compounder_return"] == "sector-adjusted" for r in rows)
    assert {r["ticker"]: r["mf_position"] for r in rows} == {
        "A": 1, "B": 2, "C": None, "D": None, "E": None}


# --- rules ---------------------------------------------------------------------------------

def _lane_row(ticker, **over) -> dict:
    """A row that clears nothing until ``over`` says so: passing, uptrend, no lane, no rank."""
    row = {"ticker": ticker, "passes_filters": True, "trend": "UPTREND", "mf_position": None,
           "mos_formula": 0.0, "lane_hints": [], "pm_record_ok": False, "price": 100.0,
           "value_base": 100.0, "compounder_return": 0.0, "implied_growth": 0.05,
           "sustainable_growth": 0.06, "earned_growth": 0.08, "incremental_roic": None}
    row.update(over)
    return row


def _picked(rule, rows, bars=None) -> set:
    return {r["ticker"] for r in rule.picks(rows, rule.bars if bars is None else bars)}


def test_rules_pick_at_the_bar():
    cfg = _bt_cfg()
    rules = backtest.rules(cfg)
    assert list(rules)[:8] == ["value", "magic_formula", "compounder", "compounder_overweight",
                               "compounder_record", "compounder_hint", "inflection",
                               "dislocation"]
    top_n, mos = cfg["backtest"]["value_top_n"], cfg["watch"]["buy_margin_of_safety"]
    assert rules["value"].bars == {"top_n": top_n, "mos": mos}
    assert rules["magic_formula"].bars == {"top_n": top_n}
    assert rules["compounder"].bars == {"premium": dm.COMPOUNDER_BUY_PREMIUM,
                                        "min_return": dm.COMPOUNDER_BUY_RETURN}
    assert rules["compounder_overweight"].bars == {"premium": dm.COMPOUNDER_OVERWEIGHT_PREMIUM,
                                                   "min_return": dm.COMPOUNDER_OVERWEIGHT_RETURN}
    assert rules["compounder_record"].bars == {} and rules["compounder_hint"].bars == {}
    assert rules["inflection"].bars == {"incremental_roic": dm.INFLECTION_INCREMENTAL_ROIC}
    assert rules["dislocation"].bars == {}
    assert all(name == rule.name for name, rule in rules.items())

    ceiling = 0.08      # max(sustainable 0.06, earned 0.08)
    comp = {"lane_hints": ["compounder"], "pm_record_ok": True}
    buy = {"price": dm.COMPOUNDER_BUY_PREMIUM * 100.0, "compounder_return": dm.COMPOUNDER_BUY_RETURN,
           "implied_growth": ceiling}
    rows = [
        # value / Magic Formula: at, above and below the rank and MOS bars
        _lane_row("V_AT", mf_position=top_n, mos_formula=mos),
        _lane_row("V_HIGH", mf_position=1, mos_formula=mos + 0.3),
        _lane_row("V_MOS_LOW", mf_position=1, mos_formula=mos - 1e-4),
        _lane_row("V_RANK_OUT", mf_position=top_n + 1, mos_formula=mos + 0.3),
        _lane_row("V_FREEFALL", mf_position=2, mos_formula=mos + 0.3, trend="FREEFALL"),
        _lane_row("V_FAIL", mf_position=3, mos_formula=mos + 0.3, passes_filters=False),
        _lane_row("V_NONE", mos_formula=mos + 0.3),
        # compounders: at, inside and outside each price and return bar
        _lane_row("C_AT", **comp, **buy),
        _lane_row("C_CHEAP", **comp, price=80.0, compounder_return=0.2, implied_growth=0.01),
        _lane_row("C_OW", **comp, price=dm.COMPOUNDER_OVERWEIGHT_PREMIUM * 100.0,
                  compounder_return=dm.COMPOUNDER_OVERWEIGHT_RETURN, implied_growth=ceiling),
        _lane_row("C_PRICE_HIGH", **comp, **{**buy, "compounder_return": 0.2,
                                            "price": dm.COMPOUNDER_OVERWEIGHT_PREMIUM * 100 + 0.01}),
        _lane_row("C_RET_LOW", **comp, **{**buy, "compounder_return":
                                         dm.COMPOUNDER_OVERWEIGHT_RETURN - 1e-4}),
        _lane_row("C_IMPLIED_HIGH", **comp, **{**buy, "implied_growth": ceiling + 1e-4}),
        _lane_row("C_IMPLIED_NONE", **comp, **{**buy, "implied_growth": None}),
        _lane_row("C_NO_BASE", **comp, **{**buy, "value_base": None}),
        _lane_row("C_NO_RECORD", lane_hints=["compounder"], **buy),
        _lane_row("C_NO_HINT", pm_record_ok=True, **buy),
        # inflection and dislocation
        _lane_row("I_AT", lane_hints=["inflection"], incremental_roic=dm.INFLECTION_INCREMENTAL_ROIC),
        _lane_row("I_HIGH", lane_hints=["inflection"], incremental_roic=0.5),
        _lane_row("I_LOW", lane_hints=["inflection"],
                  incremental_roic=dm.INFLECTION_INCREMENTAL_ROIC - 1e-4),
        _lane_row("I_NONE", lane_hints=["inflection"]),
        _lane_row("I_NO_HINT", incremental_roic=0.5),
        _lane_row("D", lane_hints=["dislocation"]),
    ]
    picked = {name: _picked(rule, rows) for name, rule in rules.items()}
    assert picked["value"] == {"V_AT", "V_HIGH"}
    assert picked["magic_formula"] == {"V_AT", "V_HIGH", "V_MOS_LOW"}
    assert picked["compounder"] == {"C_AT", "C_CHEAP"}
    assert picked["compounder_overweight"] == {"C_AT", "C_CHEAP", "C_OW"}
    record = {"C_AT", "C_CHEAP", "C_OW", "C_PRICE_HIGH", "C_RET_LOW", "C_IMPLIED_HIGH",
              "C_IMPLIED_NONE", "C_NO_BASE"}
    assert picked["compounder_record"] == record
    assert picked["compounder_hint"] == record | {"C_NO_RECORD"}
    assert picked["inflection"] == {"I_AT", "I_HIGH"}
    assert picked["dislocation"] == {"D"}
    assert picked["value"] <= picked["magic_formula"]
    assert picked["compounder"] <= picked["compounder_record"] <= picked["compounder_hint"]
    # A moved bar is honoured: the value rule at a lower MOS takes the row it refused.
    assert "V_MOS_LOW" in _picked(rules["value"], rows, {"top_n": top_n, "mos": mos - 0.01})
    assert _picked(rules["magic_formula"], rows, {"top_n": 1}) == {"V_HIGH", "V_MOS_LOW"}


# --- returns -------------------------------------------------------------------------------

def _two_day(start_adj, end_adj, days=("2020-06-30", "2021-06-30")) -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime(list(days)), "close": [start_adj, end_adj],
                         "adj_close": [start_adj, end_adj], "split": [0.0, 0.0]})


def test_attach_returns_and_benchmarks():
    cohort = "2020-06-30"
    rows = [{"ticker": "AAA"}, {"ticker": "BBB"}, {"ticker": "CCC"}]
    hists = {"AAA": _two_day(100.0, 120.0), "BBB": _two_day(50.0, 45.0)}
    backtest.attach_returns(rows, hists, cohort, [1, 3])
    a, b, c = rows
    assert a["ret_1"] == pytest.approx(0.2) and b["ret_1"] == pytest.approx(-0.1)
    assert a["start_date"] == "2020-06-30" and a["end_date_1"] == "2021-06-30"
    assert a["ended_early_1"] is False
    # The series stops before the three-year end: last close, flagged.
    assert a["ret_3"] == pytest.approx(0.2) and a["ended_early_3"] is True
    assert c["ret_1"] is None and c["end_date_1"] is None and c["ended_early_1"] is None
    assert c["start_date"] is None and c["ret_3"] is None

    spy = _two_day(300.0, 330.0)
    bench = backtest.benchmarks(spy, {cohort: rows}, [1, 3])
    assert bench[(cohort, 1)]["spy"] == pytest.approx(0.1)
    assert bench[(cohort, 1)]["universe"] == pytest.approx(0.05)      # (0.2 - 0.1) / 2; CCC none
    assert bench[(cohort, 3)]["spy"] is None                          # SPY's hold has not ended
    none = backtest.benchmarks(None, {cohort: [{"ticker": "CCC", "ret_1": None}]}, [1])
    assert none[(cohort, 1)] == {"spy": None, "universe": None}


# --- summary -------------------------------------------------------------------------------

C1, C2, C3 = "2020-06-30", "2021-06-30", "2022-06-30"
BENCH = {(C1, 1): {"spy": 0.15, "universe": 0.05}, (C2, 1): {"spy": 0.05, "universe": 0.0},
         (C3, 1): {"spy": None, "universe": 0.1}}


def _pick(rule, cohort, ret, ticker="T", ended_early=False) -> dict:
    b = BENCH[(cohort, 1)]
    excess = None if ret is None or b["spy"] is None else ret - b["spy"]
    return {"cohort": cohort, "ticker": ticker, "rule": rule, "hold": 1, "ret": ret,
            "spy": b["spy"], "universe": b["universe"], "excess_spy": excess,
            "ended_early": ended_early}


def _picks_at(cohort, n, ret):
    return [{"cohort": cohort, "ticker": f"T{i}", "rule": "r", "hold": 1, "ret": ret}
            for i in range(n)]


def test_summary_counts_only_cohorts_with_five_picks_for_the_verdict_fields():
    bench = {("A", 1): {"spy": 0.12, "universe": 0.10}, ("B", 1): {"spy": 0.09, "universe": 0.08},
             ("C", 1): {"spy": 0.0, "universe": 0.0}, ("D", 1): {"spy": 0.0, "universe": None}}
    picks = (_picks_at("A", 5, 0.20) + _picks_at("B", 6, 0.05) + _picks_at("C", 2, 0.50)
             + _picks_at("D", 5, 0.30))
    s = backtest._summary("r", 1, picks, bench)
    assert s["cohorts_with_picks"] == 4          # D has five picks but no universe return
    assert s["cohorts_min5"] == 2 and s["beat_universe_min5"] == 1
    assert s["mean_excess_universe_min5"] == pytest.approx((0.10 - 0.03) / 2)
    assert s["mean_excess_spy_min5"] == pytest.approx((0.08 - 0.04) / 2)


def test_summary_min5_fields_are_empty_without_such_a_cohort():
    bench = {("A", 1): {"spy": 0.1, "universe": 0.1}}
    s = backtest._summary("r", 1, _picks_at("A", 2, 0.3), bench)
    assert s["cohorts_min5"] == 0 and s["beat_universe_min5"] == 0
    assert s["mean_excess_universe_min5"] is None and s["mean_excess_spy_min5"] is None


def test_summarize_by_hand():
    picks = [_pick("a", C1, 0.10), _pick("a", C1, 0.30, ended_early=True),
             _pick("a", C2, -0.10), _pick("a", C2, None), _pick("a", C3, 0.50),
             _pick("c", C1, None)]
    out = backtest.summarize(picks, BENCH, [1], names=["a", "b", "c"])
    a = out["a"][1]
    c1, c2 = a["cohorts"]
    assert c1["cohort"] == C1 and c1["n"] == 2
    assert c1["basket"] == pytest.approx(0.2) and c1["median"] == pytest.approx(0.2)
    assert c1["spy"] == 0.15 and c1["universe"] == 0.05
    assert c1["excess_spy"] == pytest.approx(0.05) and c1["excess_universe"] == pytest.approx(0.15)
    assert c1["hit_rate"] == pytest.approx(0.5) and c1["ended_early"] == 1
    assert c2["cohort"] == C2 and c2["n"] == 1 and c2["basket"] == pytest.approx(-0.1)
    assert c2["excess_spy"] == pytest.approx(-0.15) and c2["hit_rate"] == 0.0
    assert c2["ended_early"] == 0
    assert a["beat_spy"] == 1 and a["cohorts_with_picks"] == 2 and a["picks"] == 3
    assert a["mean_excess_spy"] == pytest.approx(-0.05)
    assert a["median_excess_spy"] == pytest.approx(-0.05)
    assert a["mean_excess_universe"] == pytest.approx(0.025)
    assert a["all_picks_median"] == pytest.approx(0.1)
    assert a["worst_cohort"] == C2
    assert a["sentence"] == ("a: beat SPY in 1 of 2 cohorts with picks (picked in 2 of 2 cohort "
                             "dates; 1-year hold, equal weight), median excess -5.0 points; "
                             "3 picks in all.")
    for name in ("b", "c"):
        s = out[name][1]
        assert s["sentence"] == f"{name}: no picks in any cohort."
        assert s["cohorts"] == [] and s["picks"] == 0 and s["beat_spy"] == 0
        assert s["median_excess_spy"] is None and s["worst_cohort"] is None
    plus = backtest.summarize([_pick("a", C1, 0.30)], BENCH, [1])
    # C3's hold has no SPY return yet: two cohort dates count, and one pick is singular.
    assert plus["a"][1]["sentence"] == ("a: beat SPY in 1 of 1 cohorts with picks (picked in 1 "
                                        "of 2 cohort dates; 1-year hold, equal weight), median "
                                        "excess +15.0 points; 1 pick in all.")


def _cohort_rows():
    """One cohort: a value pick at the MOS bar, one just under, a compounder at the bar and one
    over the price bar; each with a one-year return."""
    cfg = _bt_cfg()
    mos = cfg["watch"]["buy_margin_of_safety"]
    comp = {"lane_hints": ["compounder"], "pm_record_ok": True,
            "compounder_return": dm.COMPOUNDER_BUY_RETURN, "implied_growth": 0.0}
    rows = [_lane_row("V1", mf_position=1, mos_formula=mos, ret_1=0.20),
            _lane_row("V2", mf_position=2, mos_formula=0.25, ret_1=0.40),
            _lane_row("C1", **comp, price=100.0, ret_1=0.10),
            _lane_row("C2", **comp, price=dm.COMPOUNDER_BUY_PREMIUM * 100 + 1, ret_1=0.30),
            _lane_row("H", lane_hints=["compounder"], ret_1=0.50)]
    for r in rows:
        r.update(start_date=C1, end_date_1=C2, ended_early_1=False)
    return cfg, {C1: rows}


def test_picks_for_builds_one_pick_per_hold():
    cfg, by_cohort = _cohort_rows()
    rule = backtest.rules(cfg)["value"]
    picks = backtest.picks_for(rule, by_cohort, [1, 3], BENCH)
    assert [(p["ticker"], p["hold"]) for p in picks] == [("V1", 1), ("V1", 3)]
    one, three = picks
    assert set(one) == {"cohort", "ticker", "rule", "price", "value_base", "mos_formula",
                        "market_cap", "hold", "start_date", "end_date", "ret", "spy", "universe",
                        "excess_spy", "ended_early"}
    assert one["rule"] == "value" and one["cohort"] == C1 and one["ret"] == 0.20
    assert one["excess_spy"] == pytest.approx(0.05) and one["end_date"] == C2
    assert three["ret"] is None and three["spy"] is None and three["excess_spy"] is None
    lower = backtest.picks_for(rule, by_cohort, [1], BENCH, bars={**rule.bars, "mos": 0.20})
    assert [p["ticker"] for p in lower] == ["V1", "V2"]


def test_sensitivity_grid_rows():
    cfg, by_cohort = _cohort_rows()
    rules = backtest.rules(cfg)
    rows = backtest.sensitivity(rules, by_cohort, BENCH)
    expected = [(name, bar, value) for name, rule in rules.items() for bar in rule.bars
                for value in backtest.SENSITIVITY_GRID[bar]]
    assert [(r["rule"], r["bar"], r["value"]) for r in rows] == expected
    assert len(rows) == 75
    assert all(set(r) == {"rule", "bar", "value", "n", "beat_spy", "cohorts", "mean_excess_spy"}
               for r in rows)
    by = {(r["rule"], r["bar"], r["value"]): r for r in rows}
    assert by[("value", "mos", 0.30)]["n"] == 1
    low = by[("value", "mos", 0.20)]
    assert low["n"] == 2 and low["cohorts"] == 1 and low["beat_spy"] == 1
    assert low["mean_excess_spy"] == pytest.approx(0.30 - 0.15)
    assert by[("compounder", "premium", 1.5)]["n"] == 2
    assert by[("compounder", "premium", 1.0)]["n"] == 1


def test_bound_by_price_is_hint_minus_rule():
    cfg, by_cohort = _cohort_rows()
    rules = backtest.rules(cfg)
    bound = backtest.bound_by_price(rules, by_cohort, BENCH, [1])
    assert list(bound) == ["compounder_price_bar"]
    s = bound["compounder_price_bar"][1]
    assert s["picks"] == 1 and s["cohorts"][0]["basket"] == pytest.approx(0.30)   # C2 only
    assert s["sentence"].startswith("compounder_price_bar: beat SPY in 1 of 1 cohorts")
    # Nothing between the record and the price bar: the "no picks" sentence.
    only = {C1: [r for r in by_cohort[C1] if r["ticker"] != "C2"]}
    empty = backtest.bound_by_price(rules, only, BENCH, [1])["compounder_price_bar"][1]
    assert empty["sentence"] == "compounder_price_bar: no picks in any cohort."


# --- survivorship --------------------------------------------------------------------------

def _constituents_html(rows) -> str:
    head = ("Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Headquarters Location",
            "Date added", "CIK", "Founded")
    body = "".join(f"<tr><td>{sym}</td><td>{sym} Inc</td><td>Industrials</td><td>Machinery</td>"
                   f"<td>Somewhere</td><td>{added}</td><td>1</td><td>1900</td></tr>"
                   for sym, added in rows)
    return ("<html><body><table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>"
            f"{body}</table><table><tr><td>navbox</td></tr></table></body></html>")


SP500_HTML = _constituents_html([
    ("AAA", "1957-03-04"), ("BRK.B", "2010-02-16"), ("NEW", "2025-10-01"),
    ("MID", "2023-03-20"), ("ODD", "unknown"), ("EDGE", "2020-06-30"),
    ("FOOT", "2001-05-07[12]")])


def test_added_dates_reads_the_constituents_table():
    urls = []

    def fetch(url):
        urls.append(url)
        return SP500_HTML

    assert backtest.added_dates(fetch) == {
        "AAA": "1957-03-04", "BRK-B": "2010-02-16", "NEW": "2025-10-01", "MID": "2023-03-20",
        "EDGE": "2020-06-30", "FOOT": "2001-05-07"}               # a footnoted cell still parses
    assert urls == [backtest.market_data.MARKETS["sp500"].pages[0]]

    def boom(url):
        raise RuntimeError("offline")

    assert backtest.added_dates(boom) == {}
    assert backtest.added_dates(lambda url: "<table><tr><th>X</th></tr><tr><td>1</td></tr></table>") == {}


def test_survivorship_counts_members_added_after_each_cohort(tmp_path):
    paths = Paths(tmp_path / "data")
    cohorts = ["2020-06-30", "2022-06-30", "2024-06-30", "2026-06-30"]
    # EDGE joined on the 2020 cohort date: not after it.
    assert backtest.survivorship(paths, cohorts, fetch=lambda url: SP500_HTML) == {
        "2020-06-30": 2, "2022-06-30": 2, "2024-06-30": 1, "2026-06-30": 0}

    def boom(url):
        raise RuntimeError("offline")

    assert backtest.survivorship(paths, cohorts, fetch=boom) == {}


# --- config --------------------------------------------------------------------------------

def test_backtest_defaults_in_config():
    assert DEFAULTS["backtest"] == {"first_year": 2015, "last_year": 2024,
                                    "cohort_days": ["03-31", "06-30", "09-30", "12-31"],
                                    "holds": [1, 3], "value_top_n": 30, "benchmark": "SPY"}
    cfg = load_config()      # the user's config.yaml may override values; the keys stay
    assert set(cfg["backtest"]) >= set(DEFAULTS["backtest"])
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for key in DEFAULTS["backtest"]:
        assert f"#   {key}:" in text


# --- run, report, command ------------------------------------------------------------------

E2E_COHORTS = ["2022-06-30", "2023-06-30"]


def _e2e_hist(slope=0.1) -> pd.DataFrame:
    """Closes from 2021-01-04 into mid-2025 rising ``slope`` a day, no split in range."""
    days = 1200
    dates = pd.bdate_range("2021-01-04", periods=days)
    close = [100.0 + slope * i for i in range(days)]
    return pd.DataFrame({"date": dates, "close": close, "adj_close": close,
                         "split": [0.0] * days})


def _write_caches(paths, ticker, store=None, hist=None) -> None:
    if store is not None:
        facts = paths.cache / "backtest" / "facts" / f"{ticker}.json"
        facts.parent.mkdir(parents=True, exist_ok=True)
        facts.write_text(json.dumps({"cik": 1, "fetched_at": "2026-10-24T00:00:00",
                                     "facts": store}), encoding="utf-8")
    if hist is not None:
        prices = paths.cache / "backtest" / "prices" / f"{ticker.lstrip('^')}.csv"
        prices.parent.mkdir(parents=True, exist_ok=True)
        hist.assign(date=hist["date"].dt.strftime("%Y-%m-%d")).to_csv(prices, index=False)


def _e2e_store() -> dict:
    """``_snap_store`` with net income growing a little faster than assets, so ROA improves
    (an F-Score point) in every year, not only by rounding."""
    store = _snap_store()
    store["NetIncomeLoss"] = _flows(lambda y: 180.0 * 1.12 ** (y - 2018))
    return store


def _e2e_universe(paths, indexes):
    return pd.DataFrame({"ticker": ["AAA", "BBB", "BANK", "NOHIST", "NOFACTS"],
                         "name": ["A", "B", "Bank", "No hist", "No facts"],
                         "sector": ["Industrials", "Industrials", "Financials", "Industrials",
                                    "Industrials"],
                         "index": ["sp500"] * 5})


def _e2e_setup(tmp_path):
    paths = Paths(tmp_path / "data")
    for t in ("AAA", "BBB", "BANK"):
        _write_caches(paths, t, _e2e_store(), _e2e_hist())
    _write_caches(paths, "NOHIST", store=_e2e_store())
    _write_caches(paths, "NOFACTS", hist=_e2e_hist())
    _write_caches(paths, "SPY", hist=_e2e_hist(slope=0.02))
    added = paths.cache / "backtest" / "added_dates.json"
    added.write_text(json.dumps(_e2e_added()), encoding="utf-8")   # the cached join dates
    cfg = _bt_cfg(first_year=2022, last_year=2023, cohort_days=["06-30"])
    return paths, cfg


def _no_wiki(fetch=None):
    pytest.fail("fetch=False reads the cached join dates, never Wikipedia")


def _e2e_added():
    # BBB joined the S&P 500 after the first cohort date: out of 2022, in 2023.
    return {"AAA": "1990-01-01", "BBB": "2023-01-15"}


def _e2e_run(tmp_path, monkeypatch, **kw):
    paths, cfg = _e2e_setup(tmp_path)
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)
    monkeypatch.setattr(backtest.market_data, "throttle", lambda s: pytest.fail("no Yahoo fetch"))
    seen = []
    result = backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False,
                          progress=lambda i, n, t: seen.append((i, n, t)),
                          universe_fn=_e2e_universe, **kw)
    return paths, result, seen


def test_run_end_to_end_with_patched_caches(tmp_path, monkeypatch):
    paths, result, seen = _e2e_run(tmp_path, monkeypatch)
    assert set(result) >= {"generated", "cohorts", "holds", "universe_size", "with_data", "picks",
                           "summary", "bar_verdicts", "sensitivity", "bound", "survivorship", "skipped"}
    assert result["generated"] == "2024-10-24"
    assert result["cohorts"] == E2E_COHORTS and result["holds"] == [1, 3]
    assert result["universe_size"] == 4                     # BANK's sector is excluded
    assert [t for _, _, t in seen] == ["AAA", "BBB", "NOHIST", "NOFACTS"]
    assert seen[-1][:2] == (4, 4)
    assert result["with_data"] == 2
    assert set(result["skipped"]) == {"NOHIST", "NOFACTS"}
    assert result["rows_per_cohort"] == {"2022-06-30": 1, "2023-06-30": 2}
    mf = {(p["cohort"], p["ticker"]) for p in result["picks"]
          if p["rule"] == "magic_formula" and p["hold"] == 1}
    assert mf == {("2022-06-30", "AAA"), ("2023-06-30", "AAA"), ("2023-06-30", "BBB")}
    assert {p["rule"] for p in result["picks"]} <= set(backtest.rules(_bt_cfg()))
    # Every rule has its sentence, picks or not; the 3-year hold of 2023 has not ended.
    assert list(result["summary"]) == list(backtest.rules(_bt_cfg()))
    assert result["summary"]["magic_formula"][1]["cohorts_with_picks"] == 2
    assert result["summary"]["magic_formula"][3]["cohorts_with_picks"] == 1
    assert result["summary"]["magic_formula"][1]["beat_spy"] == 2     # rising faster than SPY
    assert result["sensitivity"] and backtest.BOUND in result["bound"]
    assert list(result["bar_verdicts"]) == list(backtest.BAR_CANDIDATES)
    mf = result["summary"]["magic_formula"]
    for name, v in result["bar_verdicts"].items():
        assert v == backtest.bar_verdict(result["summary"][name][1], result["summary"][name][3],
                                         mf[1], rule=name)
        assert v["supports"] is False and v["sentence"].startswith(f"{name}: ")
    assert {"min_er", "pool", "min_fcf_yield"} <= {p["bar"] for p in result["sensitivity"]}
    report = backtest.markdown(result)
    assert "## Buy-bar candidates" in report
    assert all(v["sentence"] in report for v in result["bar_verdicts"].values())
    assert all(f"### {n}\n" in report for n in backtest.BAR_CANDIDATES)
    assert "| min_er |" in report and "| pool |" in report and "| min_fcf_yield |" in report
    assert result["survivorship"] == {"2022-06-30": 1, "2023-06-30": 0}
    assert result["joins_known"] is True
    assert result["defaults"]["value"] == backtest.rules(_bt_cfg())["value"].bars
    aaa = next(p for p in result["picks"] if p["ticker"] == "AAA")
    assert aaa["start_date"] == "2022-06-30"

    md_path, csv_path = backtest.write(paths, result, "2024-10-24")
    picks = pd.read_csv(csv_path)
    assert list(picks.columns) == ["cohort", "ticker", "rule", "price", "value_base",
                                   "mos_formula", "market_cap", "hold", "start_date", "end_date",
                                   "ret", "spy", "universe", "excess_spy", "ended_early"]
    assert len(picks) == len(result["picks"])


def test_backtest_run_keep_rows(tmp_path, monkeypatch):
    _, plain, _ = _e2e_run(tmp_path / "plain", monkeypatch)
    assert "rows" not in plain and "added" not in plain
    _, kept, _ = _e2e_run(tmp_path / "kept", monkeypatch, keep_rows=True)
    assert set(kept["rows"]) == set(E2E_COHORTS)
    assert {r["ticker"] for r in kept["rows"]["2023-06-30"]} == {"AAA", "BBB"}
    assert [r["ticker"] for r in kept["rows"]["2022-06-30"]] == ["AAA"]      # BBB joined later
    assert all("passes_filters" in r for rows in kept["rows"].values() for r in rows)
    assert kept["added"] == _e2e_added()
    assert {k: v for k, v in kept.items() if k not in ("rows", "added")} == plain


def test_run_counts_rows_whose_newest_year_lacks_a_field(tmp_path, monkeypatch):
    paths, cfg = _e2e_setup(tmp_path)
    store = _e2e_store()
    del store["NetCashProvidedByUsedInOperatingActivities"]       # no free cash flow
    store["OperatingIncomeLoss"]["units"]["USD"] = [               # no EBIT for 2022
        f for f in store["OperatingIncomeLoss"]["units"]["USD"] if f["end"] != "2022-12-31"]
    _write_caches(paths, "AAA", store)
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)
    result = backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False,
                          universe_fn=_e2e_universe)
    assert result["missing_fields"] == {
        "2022-06-30": {"revenue": 0, "ebit": 0, "free_cash_flow": 1},
        "2023-06-30": {"revenue": 0, "ebit": 1, "free_cash_flow": 1}}
    coverage = backtest.markdown(result).split("## Coverage")[1]
    assert ("- Newest year lacking a field, 2023-06-30: revenue 0, EBIT 1, free cash flow 1 "
            "(of 2 rows)") in coverage


def test_run_asks_for_fresh_caches_and_reports_a_short_spy(tmp_path, monkeypatch):
    paths, cfg = _e2e_setup(tmp_path)
    asked = {"prices": {}, "facts": {}}
    real_prices, real_facts = backtest.price_history, backtest.facts_for

    def prices(ticker, paths, fetch=True, refresh=False, stale_before=None):
        asked["prices"][ticker] = stale_before
        return real_prices(ticker, paths, fetch=fetch, refresh=refresh, stale_before=stale_before)

    def facts(ticker, paths, fetch=True, refresh=False, stale_before=None):
        asked["facts"][ticker] = stale_before
        return real_facts(ticker, paths, fetch=fetch, refresh=refresh, stale_before=stale_before)

    monkeypatch.setattr(backtest, "price_history", prices)
    monkeypatch.setattr(backtest, "facts_for", facts)
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)
    short_spy = _e2e_hist(slope=0.02)
    _write_caches(paths, "SPY", hist=short_spy[short_spy["date"] <= "2024-03-01"])
    result = backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False,
                          universe_fn=_e2e_universe)
    # The newest hold end needed is today (2026-06-30 is later): a week's slack for prices.
    assert asked["prices"]["SPY"] == "2024-10-17" and asked["prices"]["AAA"] == "2024-10-17"
    assert asked["facts"]["AAA"] == "2023-06-30"                    # the newest cohort date
    assert result["spy_ends"] == "2024-03-01"
    assert result["stale_cohorts"] == ["2023-06-30"]
    coverage = backtest.markdown(result).split("## Coverage")[1]
    assert "- SPY history ends 2024-03-01" in coverage
    assert ("- cohorts 2023-06-30 need --refresh: a hold has ended by today, but SPY's cached "
            "history stops at 2024-03-01, before it") in coverage
    # A SPY history that reaches every ended hold: no such line.
    _write_caches(paths, "SPY", hist=_e2e_hist(slope=0.02))
    fresh = backtest.run(paths, cfg, today=date(2024, 10, 24), fetch=False,
                         universe_fn=_e2e_universe)
    assert fresh["stale_cohorts"] == []
    assert "need --refresh" not in backtest.markdown(fresh)


def test_run_narrows_to_tickers_and_fails_open(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad facts")

    real = backtest.snapshot
    calls = []

    def snap(ticker, as_of, store, hist, rate, cfg, sector=None):
        calls.append((ticker, as_of, rate, sector))
        if ticker == "BBB":
            boom()
        return real(ticker, as_of, store, hist, rate, cfg, sector)

    monkeypatch.setattr(backtest, "snapshot", snap)
    paths = Paths(tmp_path / "data")
    tnx = _e2e_hist(slope=0.0).assign(close=5.0)            # the 10-year at 5%
    tnx.loc[tnx["date"] > "2022-06-30", "close"] = 1.0       # after the first cohort: floor
    _write_caches(paths, "^TNX", hist=tnx)
    _, result, seen = _e2e_run(tmp_path, monkeypatch, tickers=["aaa", "BBB"])
    assert calls[:2] == [("AAA", "2022-06-30", pytest.approx(0.05 + EQUITY_PREMIUM), "Industrials"),
                         ("AAA", "2023-06-30", MIN_DISCOUNT_RATE, "Industrials")]
    assert ("BBB", "2022-06-30") not in {c[:2] for c in calls}     # joined after it
    assert [t for _, _, t in seen] == ["AAA", "BBB"]
    assert result["universe_size"] == 2
    assert result["skipped"] == {"BBB": "snapshot error (RuntimeError) at 2023-06-30"}
    assert result["with_data"] == 1                          # skipped, not also counted
    assert result["rows_per_cohort"] == {"2022-06-30": 1, "2023-06-30": 1}


def test_one_bad_cohort_keeps_the_tickers_other_cohorts(tmp_path, monkeypatch):
    real = backtest.snapshot

    def snap(ticker, as_of, store, hist, rate, cfg, sector=None):
        if (ticker, as_of) == ("AAA", "2022-06-30"):
            raise ValueError("odd filing")
        return real(ticker, as_of, store, hist, rate, cfg, sector)

    monkeypatch.setattr(backtest, "snapshot", snap)
    _, result, _ = _e2e_run(tmp_path, monkeypatch, tickers=["AAA", "BBB"])
    assert result["rows_per_cohort"] == {"2022-06-30": 0, "2023-06-30": 2}
    assert result["skipped"] == {"AAA": "snapshot error (ValueError) at 2022-06-30"}
    assert result["with_data"] == 2                          # AAA has a good cohort


def test_join_date_cache_that_is_not_a_mapping_counts_as_empty(tmp_path, monkeypatch):
    paths, cfg = _e2e_setup(tmp_path)
    cache = paths.cache / "backtest" / "added_dates.json"
    cache.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)
    result = backtest.run(paths, cfg, today=date(2024, 10, 24), tickers=["AAA"], fetch=False,
                          universe_fn=_e2e_universe)
    assert result["joins_known"] is False and result["survivorship"] == {}
    # A fetching run rewrites it whole, through a temp file.
    got = backtest._join_dates(paths, True, _e2e_added)
    assert got == _e2e_added() and json.loads(cache.read_text(encoding="utf-8")) == got
    assert sorted(p.name for p in cache.parent.iterdir() if p.is_file()) == ["added_dates.json"]


def test_run_caches_the_join_dates_and_warns_without_them(tmp_path, monkeypatch):
    paths, cfg = _e2e_setup(tmp_path)
    cache = paths.cache / "backtest" / "added_dates.json"
    cache.unlink()
    monkeypatch.setattr(backtest.yf, "Ticker", _FakeTicker)          # ^TNX is not cached
    _FakeTicker.frames = {"^TNX": RuntimeError("offline")}
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)

    def go(added_fn, fetch=True):
        return backtest.run(paths, cfg, today=date(2024, 10, 24), tickers=["AAA", "BBB"],
                            fetch=fetch, universe_fn=_e2e_universe, added_fn=added_fn)

    # Fetched: the dates are used and cached.
    result = go(_e2e_added)
    assert result["rows_per_cohort"] == {"2022-06-30": 1, "2023-06-30": 2}
    assert json.loads(cache.read_text(encoding="utf-8")) == _e2e_added()
    # A failed fetch falls back to the cache.
    assert go(lambda: {})["rows_per_cohort"] == {"2022-06-30": 1, "2023-06-30": 2}
    # Neither: BBB stays in every cohort, and the report says so plainly.
    cache.unlink()
    bare = go(lambda: {})
    assert bare["joins_known"] is False and bare["survivorship"] == {}
    assert bare["rows_per_cohort"] == {"2022-06-30": 2, "2023-06-30": 2}
    assert not cache.exists()
    text = backtest.markdown(bare)
    tested = text.split("## What was tested")[1].split("## Verdict")[0]
    assert backtest.NO_JOINS in tested and backtest.SURVIVORSHIP not in text
    verdict = text.split("## Verdict")[1].split("## By cohort")[0]
    assert verdict.strip().startswith(f"**Warning:** {backtest.NO_JOINS}.")
    assert "Warning" not in backtest.markdown(result)


def _fake_result() -> dict:
    holds = [1, 3]
    names = list(backtest.rules(_bt_cfg()))
    picks = [_pick("value", C1, 0.30), _pick("value", C2, 0.0)]
    summary = backtest.summarize(picks, BENCH, [1], names=names)
    for name in names:
        summary[name][3] = backtest.summarize([], BENCH, [3], names=[name])[name][3]
    return {"generated": "2026-10-24", "cohorts": [C1, C2], "holds": holds, "universe_size": 900,
            "with_data": 850, "rows_per_cohort": {C1: 600, C2: 640}, "picks": picks,
            "summary": summary,
            "bar_verdicts": {name: backtest.bar_verdict(summary[name][1], summary[name][3],
                                                        summary["magic_formula"][1], rule=name)
                             for name in backtest.BAR_CANDIDATES},
            "sensitivity": [{"rule": "value", "bar": "mos", "value": 0.3, "n": 2, "beat_spy": 1,
                             "cohorts": 2, "mean_excess_spy": 0.025},
                            {"rule": "value", "bar": "mos", "value": 0.4, "n": 1, "beat_spy": 1,
                             "cohorts": 1, "mean_excess_spy": 0.15}],
            "bound": backtest.summarize([], BENCH, holds, names=[backtest.BOUND]),
            "survivorship": {C1: 40, C2: 30}, "joins_known": True,
            "defaults": {"value": {"top_n": 30, "mos": 0.3}},
            "skipped": {"X": "no EDGAR statements", "Y": "no EDGAR statements",
                        "Z": "no price history"}}


def test_markdown_sections_in_order():
    text = backtest.markdown(_fake_result())
    heads = [line for line in text.splitlines() if line.startswith(("# ", "## "))]
    assert heads == ["# Lane-rule backtest", "## What was tested", "## Verdict",
                     "## Buy-bar candidates", "## By cohort", "## Sensitivity", "## The bar that bound", "## Coverage"]
    assert ("today's members only: N of today's S&P 500 names joined the S&P 500 after this "
            "date (some from the S&P 400, where they were already in the universe) and are "
            "left out of this cohort; names that left either index since are not in the sample "
            "and cannot be counted from free data; the S&P 400's join dates are not published, "
            "so all its members stay") in text
    assert f"{C1}: 40" in text
    assert ("value: beat SPY in 1 of 2 cohorts with picks (picked in 2 of 2 cohort dates; "
            "1-year hold, equal weight), median excess +5.0 points; 2 picks in all.") in text
    assert "dislocation: no picks in any cohort." in text
    verdict = text.split("## Verdict")[1].split("## By cohort")[0]
    assert verdict.index("1-year") < verdict.index("3-year")
    assert "| Cohort |" in text and "+15.0" in text            # C1's excess over SPY
    assert "| mos | 0.3 \\* | 2 | 1 of 2 | +2.5 |" in text      # the default, marked
    assert "| mos | 0.4 | 1 | 1 of 1 | +15.0 |" in text
    assert "- premium: price at most premium × worksheet base" in text
    assert "- mos: formula margin of safety at least this" in text
    assert "no EDGAR statements: 2" in text and "900" in text and "850" in text
    assert backtest.verdict(_fake_result()) in text
    empty = backtest.markdown({**_fake_result(), "survivorship": {}, "joins_known": False})
    assert backtest.NO_JOINS in empty and "**Warning:**" in empty


def test_write_and_latest_report(tmp_path):
    paths = Paths(tmp_path / "data")
    assert backtest.latest_report(paths) is None
    result = _fake_result()
    md, csv = backtest.write(paths, result, "2026-10-24")
    assert md == paths.reports / "backtest" / "backtest_2026-10-24.md"
    assert csv == paths.reports / "backtest" / "backtest_2026-10-24_picks.csv"
    assert md.read_text(encoding="utf-8") == backtest.markdown(result)
    assert len(pd.read_csv(csv)) == 2
    assert sorted(p.name for p in md.parent.iterdir()) == [md.name, csv.name]   # no temp file
    backtest.write(paths, result, "2026-01-02")
    assert backtest.latest_report(paths) == ("2026-10-24", backtest.markdown(result))


def test_cli_backtest_no_fetch(tmp_path, monkeypatch, capsys):
    from functools import partial

    from value_pipeline import cli

    paths, _ = _e2e_setup(tmp_path)
    monkeypatch.setattr(backtest, "added_dates", _no_wiki)
    monkeypatch.setattr(backtest, "run", partial(backtest.run, universe_fn=_e2e_universe))
    cli.main(["--data-dir", str(paths.root), "backtest", "--no-fetch", "--tickers", "AAA,BBB",
              "--from", "2022", "--to", "2023", "--holds", "1"])
    out = capsys.readouterr().out
    assert "Verdict" in out and "magic_formula: beat SPY" in out
    reports = sorted((paths.reports / "backtest").iterdir())
    assert [p.name for p in reports] == [f"backtest_{date.today()}.md",
                                         f"backtest_{date.today()}_picks.csv"]
    assert "backtest_" in out
    text = reports[0].read_text(encoding="utf-8")
    assert "2022-06-30" in text and "2023-06-30" in text and "2024-06-30" not in text
    assert "3-year" not in text.split("## Verdict")[1].split("## By cohort")[0]


# --- buy-bar candidates --------------------------------------------------------------------

def _row(t, **kw) -> dict:
    """A passing, BASING row with no lane hint and no record, unless ``kw`` says otherwise."""
    row = _lane_row(t, trend="BASING", expected_return=None, momentum_12_1=None, fcf_yield=None)
    row.update(kw)
    return row


def _tickers(rule_name, rows, **bars) -> list[str]:
    rule = backtest.rules(_bt_cfg())[rule_name]
    return [r["ticker"] for r in rule.picks(rows, {**rule.bars, **bars})]


@pytest.mark.unit
def test_the_six_candidates_follow_the_existing_eight_with_their_default_bars():
    rules = backtest.rules(_bt_cfg())
    assert list(rules)[8:] == list(backtest.BAR_CANDIDATES) == [
        "er_hurdle", "er_momentum", "trending_value", "value_graded", "value_no_downtrend",
        "compounder_fcf"]
    assert rules["er_hurdle"].bars == {"min_er": 0.12, "top_n": 20}
    assert rules["er_momentum"].bars == {"min_er": 0.12, "top_n": 20}
    assert rules["trending_value"].bars == {"pool": 60, "top_n": 20}
    assert rules["value_graded"].bars == {"top_n": 30, "mos": 0.20}
    assert rules["value_no_downtrend"].bars == {"top_n": 30, "mos": 0.30}
    assert rules["compounder_fcf"].bars == {"min_fcf_yield": 0.04, "top_n": 20}
    grid = backtest.SENSITIVITY_GRID
    assert grid["min_er"] == (0.10, 0.12, 0.15) and grid["pool"] == (40, 60, 100)
    assert grid["min_fcf_yield"] == (0.03, 0.04, 0.05) and grid["mos"] == (0.10, 0.20, 0.30, 0.40)
    assert all(bar in grid for rule in rules.values() for bar in rule.bars)


@pytest.mark.unit
def test_er_hurdle_takes_the_best_expected_returns_above_the_bar():
    rows = [_row("B", expected_return=0.13), _row("A", expected_return=0.20),
            _row("C", expected_return=0.11), _row("D", expected_return=None),
            _row("E", expected_return=0.30, trend="FREEFALL"),
            _row("F", expected_return=0.25, passes_filters=False),
            _row("G", expected_return=0.12)]
    assert _tickers("er_hurdle", rows) == ["A", "B", "G"]            # sorted, the bar included
    assert _tickers("er_hurdle", rows, top_n=1) == ["A"]
    assert _tickers("er_hurdle", rows, min_er=0.15) == ["A"]


@pytest.mark.unit
def test_er_momentum_keeps_picks_at_or_above_the_passing_rows_median_momentum():
    rows = [_row("A", expected_return=0.20, momentum_12_1=0.30),
            _row("B", expected_return=0.15, momentum_12_1=0.10),
            _row("C", expected_return=0.05, momentum_12_1=-0.05),    # under the hurdle, in the median
            _row("G", expected_return=0.40, momentum_12_1=None),     # no momentum: dropped
            _row("E", expected_return=0.40, momentum_12_1=9.0, trend="FREEFALL"),   # not counted
            _row("F", expected_return=0.40, momentum_12_1=9.0, passes_filters=False)]
    assert _tickers("er_momentum", rows) == ["A", "B"]   # median of .30, .10, -.05
    rows[1]["momentum_12_1"] = 0.09
    rows.append(_row("D", expected_return=0.01, momentum_12_1=0.50))   # median of four: 0.195
    assert _tickers("er_momentum", rows) == ["A"]
    assert _tickers("er_momentum", rows[:1]) == []                   # one row has no median
    assert _tickers("er_momentum", [rows[0], _row("H", momentum_12_1=None)]) == []


@pytest.mark.unit
def test_trending_value_takes_the_pool_by_rank_then_the_strongest_momentum():
    rows = [_row(f"T{i}", mf_position=i, momentum_12_1=m)
            for i, m in enumerate((0.0, 0.4, 0.1, 0.5, 0.9), start=1)]
    rows += [_row("N", mf_position=2, momentum_12_1=None),
             _row("X", mf_position=3, momentum_12_1=2.0, trend="FREEFALL"),
             _row("Y", mf_position=1, momentum_12_1=2.0, passes_filters=False)]
    assert _tickers("trending_value", rows, pool=3, top_n=2) == ["T2", "T3"]
    assert _tickers("trending_value", rows, pool=3, top_n=1) == ["T2"]
    assert _tickers("trending_value", rows, pool=5, top_n=2) == ["T5", "T4"]


@pytest.mark.unit
def test_value_graded_and_value_no_downtrend():
    rows = [_row("IN", mf_position=3, mos_formula=0.20), _row("OUT", mf_position=3, mos_formula=0.19),
            _row("RANK", mf_position=31, mos_formula=0.9), _row("NONE", mf_position=4, mos_formula=None),
            _row("FF", mf_position=5, mos_formula=0.9, trend="FREEFALL"),
            _row("NOPASS", mf_position=6, mos_formula=0.9, passes_filters=False)]
    assert _tickers("value_graded", rows) == ["IN"]
    assert _tickers("value_graded", rows, mos=0.10) == ["IN", "OUT"]
    down = [_row("A", mf_position=1, mos_formula=0.30), _row("D", mf_position=2, mos_formula=0.40,
                                                            trend="DOWNTREND"),
            _row("B", mf_position=3, mos_formula=0.29)]
    assert _tickers("value", down) == ["A", "D"]
    assert _tickers("value_no_downtrend", down) == ["A"]
    assert _tickers("value_no_downtrend", down, mos=0.25) == ["A", "B"]


@pytest.mark.unit
def test_compounder_fcf_needs_the_record_and_the_yield():
    rec = {"lane_hints": ["compounder"], "pm_record_ok": True}
    rows = [_row("A", **rec, fcf_yield=0.05, expected_return=0.10),
            _row("B", **rec, fcf_yield=0.04, expected_return=0.20),
            _row("LOW", **rec, fcf_yield=0.03, expected_return=0.90),
            _row("NOREC", lane_hints=["compounder"], pm_record_ok=False, fcf_yield=0.09,
                 expected_return=0.90),
            _row("NOHINT", pm_record_ok=True, fcf_yield=0.09, expected_return=0.90),
            _row("NOER", **rec, fcf_yield=0.09, expected_return=None),
            _row("NOFCF", **rec, fcf_yield=None, expected_return=0.90),
            _row("FF", **rec, fcf_yield=0.09, expected_return=0.90, trend="FREEFALL"),
            _row("NOPASS", **rec, fcf_yield=0.09, expected_return=0.90, passes_filters=False)]
    assert _tickers("compounder_fcf", rows) == ["B", "A"]
    assert _tickers("compounder_fcf", rows, top_n=1) == ["B"]
    assert _tickers("compounder_fcf", rows, min_fcf_yield=0.03) == ["LOW", "B", "A"]
    assert _tickers("compounder_fcf", rows, min_fcf_yield=0.05) == ["A"]


_ONE = {"cohorts_min5": 30, "beat_universe_min5": 20, "mean_excess_universe_min5": 0.03,
        "mean_excess_spy_min5": 0.01}
_THREE = {"cohorts_min5": 20, "mean_excess_universe_min5": 0.01}
_BASE = {"mean_excess_universe_min5": 0.01}


@pytest.mark.unit
def test_bar_verdict_conditions():
    one, three, base = _ONE, _THREE, _BASE
    assert backtest.bar_verdict(one, three, base)["supports"] is True
    assert backtest.bar_verdict({**one, "cohorts_min5": 20, "beat_universe_min5": 12},
                                three, base)["supports"] is True        # exactly 20 and 60%
    assert backtest.bar_verdict({**one, "mean_excess_universe_min5": 0.02, "mean_excess_spy_min5": 0.0},
                                three, base)["supports"] is True        # exactly +2 and 0
    assert backtest.bar_verdict(one, {**three, "mean_excess_universe_min5": 0.0},
                                base)["supports"] is True
    assert backtest.bar_verdict({**one, "cohorts_min5": 19}, three, base)["supports"] is False      # 1
    assert backtest.bar_verdict({**one, "beat_universe_min5": 17}, three, base)["supports"] is False  # 2
    assert backtest.bar_verdict({**one, "mean_excess_universe_min5": 0.019}, three, base)["supports"] is False
    assert backtest.bar_verdict({**one, "mean_excess_spy_min5": -0.001}, three, base)["supports"] is False
    assert backtest.bar_verdict(one, {**three, "mean_excess_universe_min5": -0.001}, base)["supports"] is False
    assert backtest.bar_verdict(one, three, {"mean_excess_universe_min5": 0.03})["supports"] is False  # 5
    assert backtest.bar_verdict(one, three, {"mean_excess_universe_min5": None})["supports"] is False
    assert backtest.bar_verdict(one, None, base)["supports"] is False
    assert backtest.bar_verdict(one, {**three, "cohorts_min5": 0, "mean_excess_universe_min5": None},
                                base)["supports"] is False
    assert backtest.bar_verdict({**one, "mean_excess_universe_min5": None}, three, base)["supports"] is False
    assert backtest.bar_verdict({**one, "mean_excess_spy_min5": None}, three, base)["supports"] is False


@pytest.mark.unit
def test_bar_verdict_values_and_sentence():
    v = backtest.bar_verdict(_ONE, _THREE, _BASE, rule="er_hurdle")
    assert set(v) == {"supports", "sentence", "cohorts", "beat_share", "mean_excess_universe",
                      "mean_excess_spy", "mean_excess_universe_3y", "baseline"}
    assert v["cohorts"] == 30 and v["beat_share"] == pytest.approx(20 / 30)
    assert v["mean_excess_universe"] == 0.03 and v["mean_excess_spy"] == 0.01
    assert v["mean_excess_universe_3y"] == 0.01 and v["baseline"] == 0.01
    assert v["sentence"] == (
        "er_hurdle: 30 cohorts with five or more picks; beat the universe in 67% of them; mean "
        "one-year excess +3.0 points over the universe and +1.0 over SPY; three-year +1.0; the "
        "plain screen +1.0: supports a live bar change.")
    no = backtest.bar_verdict({**_ONE, "cohorts_min5": 19}, _THREE, _BASE)
    assert no["sentence"].endswith("does not support a live bar change.")
    empty = backtest.bar_verdict({"cohorts_min5": 0, "beat_universe_min5": 0,
                                  "mean_excess_universe_min5": None,
                                  "mean_excess_spy_min5": None}, None, {"mean_excess_universe_min5": None})
    assert empty["supports"] is False and empty["beat_share"] == 0
    assert ("0 cohorts with five or more picks; beat the universe in 0% of them; mean one-year "
            "excess n/a points over the universe and n/a over SPY; three-year n/a; the plain "
            "screen n/a: does not support") in empty["sentence"]
    assert empty["mean_excess_universe_3y"] is None


@pytest.mark.unit
def test_the_report_prints_one_sentence_per_candidate():
    result = _fake_result()
    text = backtest.markdown(result)
    section = text.split("## Buy-bar candidates")[1].split("\n## ")[0]
    assert text.index("## Verdict") < text.index("## Buy-bar candidates") < text.index("## By cohort")
    assert "overlap" in section and "not independent" in section
    for name in backtest.BAR_CANDIDATES:
        assert f"- {result['bar_verdicts'][name]['sentence']}" in section
        assert result["bar_verdicts"][name]["sentence"].startswith(f"{name}: ")
    assert "at least 20 such cohorts" in section and "60%" in section
