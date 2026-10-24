"""Lane-rule backtest: prices, the cohort snapshot, rules, returns and summary (no network; data faked)."""

import copy
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from tradingagents.agents import decision_math as dm
from tradingagents.dataflows.value_metrics import EQUITY_PREMIUM, MIN_DISCOUNT_RATE
from value_pipeline import backtest, metrics, pit_statements as pit
from value_pipeline.config import CONFIG_PATH, DEFAULTS, Paths, load_config, validate_config

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


# --- cohorts -------------------------------------------------------------------------------

def _bt_cfg(**over) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    cfg["backtest"].update(over)
    return cfg


def test_cohort_dates_stop_at_completed_holds():
    cfg = _bt_cfg()
    assert backtest.cohort_dates(cfg, date(2025, 6, 30)) == [f"{y}-06-30" for y in range(2015, 2025)]
    assert backtest.cohort_dates(cfg, date(2025, 6, 29)) == [f"{y}-06-30" for y in range(2015, 2024)]
    assert backtest.cohort_dates(_bt_cfg(first_year=2018, last_year=2030, cohort_day="03-31"),
                                 date(2021, 3, 30)) == ["2018-03-31", "2019-03-31"]
    assert backtest.cohort_dates(cfg, date(2016, 6, 1)) == []


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
    assert list(rules) == ["value", "magic_formula", "compounder", "compounder_overweight",
                           "compounder_record", "compounder_hint", "inflection", "dislocation"]
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
    assert a["sentence"] == ("a: beat SPY in 1 of 2 cohorts (1-year hold, equal weight), "
                             "median excess -5.0 points; 3 picks in all.")
    for name in ("b", "c"):
        s = out[name][1]
        assert s["sentence"] == f"{name}: no picks in any cohort."
        assert s["cohorts"] == [] and s["picks"] == 0 and s["beat_spy"] == 0
        assert s["median_excess_spy"] is None and s["worst_cohort"] is None
    plus = backtest.summarize([_pick("a", C1, 0.30)], BENCH, [1])
    assert plus["a"][1]["sentence"] == ("a: beat SPY in 1 of 1 cohorts (1-year hold, equal "
                                        "weight), median excess +15.0 points; 1 picks in all.")


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
    assert len(rows) == 30
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

def _changes_html(rows) -> str:
    body = "".join(
        f"<tr><td>{d}</td><td>{at}</td><td>{an}</td><td>{rt}</td><td>{rn}</td><td>r</td></tr>"
        for d, at, an, rt, rn in rows)
    return (
        "<html><body>"
        "<table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>"
        "<tr><td>AAA</td><td>Aaa Inc</td><td>Industrials</td></tr></table>"
        "<table><thead>"
        "<tr><th rowspan='2'>Date</th><th colspan='2'>Added</th><th colspan='2'>Removed</th>"
        "<th rowspan='2'>Reason</th></tr>"
        "<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>"
        f"</thead><tbody>{body}</tbody></table></body></html>")


def test_survivorship_counts_removals_after_each_cohort(tmp_path):
    html = _changes_html([
        ("October 1, 2025", "NEW", "New Co", "YYY", "Y Co"),
        ("March 20, 2023", "", "", "ZZZ", "Z Co"),
        ("June 1, 2021", "ADD", "Add Co", "", ""),
        ("January 5, 2021", "", "", "VVV", "V Co"),
        ("June 30, 2020", "", "", "WWW", "W Co"),      # on the cohort date: not after it
    ])
    urls = []

    def fetch(url):
        urls.append(url)
        return html

    paths = Paths(tmp_path / "data")
    cohorts = ["2020-06-30", "2022-06-30", "2024-06-30", "2026-06-30"]
    assert backtest.survivorship(paths, cohorts, fetch=fetch) == {
        "2020-06-30": 3, "2022-06-30": 2, "2024-06-30": 1, "2026-06-30": 0}
    assert urls == [backtest.market_data.WIKI["sp500"]]

    def boom(url):
        raise RuntimeError("offline")

    assert backtest.survivorship(paths, cohorts, fetch=boom) == {}
    no_table = "<table><tr><th>Symbol</th></tr><tr><td>AAA</td></tr></table>"
    assert backtest.survivorship(paths, cohorts, fetch=lambda url: no_table) == {}


# --- config --------------------------------------------------------------------------------

def test_backtest_defaults_in_config():
    assert DEFAULTS["backtest"] == {"first_year": 2015, "last_year": 2024, "cohort_day": "06-30",
                                    "holds": [1, 3], "value_top_n": 30, "benchmark": "SPY"}
    cfg = load_config()
    assert cfg["backtest"] == DEFAULTS["backtest"]
    validate_config(cfg)
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for key in DEFAULTS["backtest"]:
        assert f"#   {key}:" in text
