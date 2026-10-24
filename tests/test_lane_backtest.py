"""Lane-rule backtest: price helpers and the cohort snapshot (no network; prices and filings faked)."""

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from tradingagents.agents import decision_math as dm
from tradingagents.dataflows.value_metrics import EQUITY_PREMIUM, MIN_DISCOUNT_RATE
from value_pipeline import backtest, metrics, pit_statements as pit
from value_pipeline.config import DEFAULTS, Paths

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
