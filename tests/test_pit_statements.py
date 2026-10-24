"""Point-in-time statements from EDGAR companyfacts (no network; a synthetic filer)."""

import json
from datetime import date, timedelta

import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.value_metrics import YEAR_FIELDS
from value_pipeline import pit_statements as pit
from value_pipeline.config import Paths

pytestmark = pytest.mark.unit


def _f(end, val, filed, form="10-K", start=None):
    fact = {"end": end, "val": val, "filed": filed, "form": form, "fy": 2023, "fp": "FY",
            "accn": "0000000000-00-000000"}
    if start:
        fact["start"] = start
    return fact


def _annual(values, filed=None, unit="USD"):
    """{year: val} -> 10-K duration facts for calendar fiscal years, filed the next 02-20."""
    return {"units": {unit: [_f(f"{y}-12-31", v, filed or f"{y + 1}-02-20", start=f"{y}-01-01")
                             for y, v in values.items()]}}


def _balance(values, unit="USD"):
    """{year: val} -> 10-K instant facts at the fiscal year end, filed the next 02-20."""
    return {"units": {unit: [_f(f"{y}-12-31", v, f"{y + 1}-02-20") for y, v in values.items()]}}


def _raw() -> dict:
    """Three fiscal years (2021-2023, Dec year end), 2023 quarters, a 2022 10-K/A, dei shares."""
    g = {
        # 2021 revenue under an older tag; 2022 and 2023 under Revenues.
        "SalesRevenueNet": _annual({2021: 1000.0}),
        "Revenues": _annual({2022: 1100.0, 2023: 1300.0}),
        "OperatingIncomeLoss": _annual({2021: 200.0, 2022: 220.0, 2023: 260.0}),
        "ProfitLoss": _annual({2021: 150.0, 2022: 160.0, 2023: 190.0}),
        "Assets": _balance({2021: 2000.0, 2022: 2100.0, 2023: 2300.0}),
        "Liabilities": _balance({2021: 850.0}),
        "StockholdersEquity": _balance({2021: 1200.0, 2022: 1250.0, 2023: 1400.0}),
        "AssetsCurrent": _balance({2021: 800.0, 2022: 850.0, 2023: 900.0}),
        "LiabilitiesCurrent": _balance({2021: 400.0, 2022: 420.0, 2023: 450.0}),
        "CashAndCashEquivalentsAtCarryingValue": _balance({2021: 300.0, 2022: 310.0, 2023: 320.0}),
        "PropertyPlantAndEquipmentNet": _balance({2021: 500.0, 2022: 510.0, 2023: 520.0}),
        "Goodwill": _balance({2022: 100.0, 2023: 100.0}),
        "IntangibleAssetsNetExcludingGoodwill": _balance({2023: 50.0}),
        "LongTermDebtNoncurrent": _balance({2021: 400.0, 2022: 400.0, 2023: 400.0}),
        "LongTermDebtCurrent": _balance({2023: 50.0}),
        "DebtCurrent": _balance({2023: 30.0}),
        "CommercialPaper": _balance({2023: 20.0}),
        "NetCashProvidedByUsedInOperatingActivities": _annual({2021: 300.0, 2022: 320.0, 2023: 350.0}),
        "PaymentsToAcquirePropertyPlantAndEquipment": _annual({2021: 100.0, 2022: 110.0, 2023: 120.0}),
        "PaymentsOfDividendsCommonStock": _annual({2023: 40.0}),
        "PaymentsForRepurchaseOfCommonStock": _annual({2023: 60.0}),
        "PaymentsToAcquireBusinessesNetOfCashAcquired": _annual({2023: 25.0}),
        "CommonStockSharesOutstanding": {"units": {"shares": [
            _f("2021-12-31", 100.0, "2022-02-20"),
            _f("2022-12-31", 100.0, "2023-02-20"),
            _f("2022-12-31", 200.0, "2024-02-20"),     # restated after a 2:1 split
            _f("2023-12-31", 200.0, "2024-02-20"),
        ]}},
        "WeightedAverageNumberOfDilutedSharesOutstanding": _annual({2023: 198.0}, unit="shares"),
        # Not mapped: dropped by extract.
        "EarningsPerShareDiluted": _annual({2023: 1.5}, unit="USD/shares"),
        "SomethingElse": _annual({2023: 7.0}),
    }
    # 2022 10-K/A changes revenue.
    g["Revenues"]["units"]["USD"].append(
        _f("2022-12-31", 1150.0, "2023-06-01", "10-K/A", start="2022-01-01"))
    # A non-dollar unit on a mapped tag: dropped by extract.
    g["Revenues"]["units"]["EUR"] = [_f("2023-12-31", 1.0, "2024-02-20", start="2023-01-01")]
    # 2023 quarters (10-Q), a six-month and a nine-month year to date, and a lone Q3 2022.
    q = [("2023-01-01", "2023-03-31", "2023-05-01", 300.0, 60.0),
         ("2023-04-01", "2023-06-30", "2023-08-01", 310.0, 62.0),
         ("2023-07-01", "2023-09-30", "2023-11-01", 320.0, 64.0),
         ("2023-01-01", "2023-06-30", "2023-08-01", 610.0, 122.0),
         ("2023-01-01", "2023-09-30", "2023-11-01", 930.0, 186.0),
         ("2022-07-01", "2022-09-30", "2022-11-01", 280.0, 55.0)]
    for start, end, filed, rev, ebit in q:
        g["Revenues"]["units"]["USD"].append(_f(end, rev, filed, "10-Q", start=start))
        g["OperatingIncomeLoss"]["units"]["USD"].append(_f(end, ebit, filed, "10-Q", start=start))
    for end, filed, val in (("2023-03-31", "2023-05-01", 810.0), ("2023-06-30", "2023-08-01", 820.0),
                            ("2023-09-30", "2023-11-01", 830.0)):
        g["AssetsCurrent"]["units"]["USD"].append(_f(end, val, filed, "10-Q"))
    dei = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        _f("2023-02-10", 101.0, "2023-02-20"),
        _f("2023-04-28", 102.0, "2023-05-01", "10-Q"),
        _f("2024-02-10", 205.0, "2024-02-20"),
    ]}}, "EntityPublicFloat": {"units": {"USD": [_f("2023-06-30", 9.0, "2024-02-20")]}}}
    return {"cik": 1, "entityName": "Synthetic", "facts": {"us-gaap": g, "dei": dei}}


def _store() -> dict:
    return pit.extract(_raw())


def _year(years, end):
    return next(y for y in years if y["fiscal_year_end"] == end)


def test_extract_keeps_only_mapped_tags_and_fact_shape():
    store = _store()
    assert "EarningsPerShareDiluted" not in store and "SomethingElse" not in store
    assert "EntityPublicFloat" not in store and "dei:EntityPublicFloat" not in store
    assert store[pit.SHARES_DEI]["units"]["shares"][0] == {
        "end": "2023-02-10", "val": 101.0, "filed": "2023-02-20", "form": "10-K"}
    assert set(store["Revenues"]["units"]) == {"USD"}
    fact = store["Revenues"]["units"]["USD"][0]
    assert fact == {"start": "2022-01-01", "end": "2022-12-31", "val": 1100.0,
                    "filed": "2023-02-20", "form": "10-K"}
    assert set(store["Assets"]["units"]["USD"][0]) == {"end", "val", "filed", "form"}
    for tag in ("LongTermDebtCurrent", "DebtCurrent", "CommercialPaper", pit.INTANGIBLES,
                "CommonStockSharesOutstanding"):
        assert tag in store
    mapped = {t for tags in pit.TAGS.values() for t in tags} | set(pit.DEBT_PARTS)
    assert set(store) <= mapped | {pit.INTANGIBLES, pit.SHARES_DEI}
    assert set(pit.TAGS) <= set(YEAR_FIELDS) - {"total_debt", "free_cash_flow"}


def test_years_as_of_respects_filing_dates():
    store = _store()
    early = pit.years_as_of(store, "2023-03-01")
    assert [y["fiscal_year_end"] for y in early] == ["2022-12-31", "2021-12-31"]
    assert _year(early, "2022-12-31")["revenue"] == 1100.0
    assert set(early[0]) == {"fiscal_year_end", *YEAR_FIELDS}
    later = pit.years_as_of(store, "2023-07-01")
    assert _year(later, "2022-12-31")["revenue"] == 1150.0
    # The 2021 10-K was filed 2022-02-20; nothing about 2021 is known on 2022-01-01.
    assert pit.years_as_of(store, "2022-01-01") == []
    # A quarter's balance (10-Q) never makes a fiscal year.
    assert all(y["fiscal_year_end"].endswith("12-31") for y in pit.years_as_of(store, "2024-03-01"))


def test_alternative_tags_fill_the_field():
    years = pit.years_as_of(_store(), "2024-03-01")
    y2021 = _year(years, "2021-12-31")
    assert y2021["revenue"] == 1000.0            # SalesRevenueNet
    assert y2021["net_income"] == 150.0          # ProfitLoss
    assert y2021["cash"] == 300.0                # CashAndCashEquivalentsAtCarryingValue
    assert _year(years, "2023-12-31")["revenue"] == 1300.0
    assert y2021["gross_profit"] is None and y2021["rnd"] is None


def test_derived_fields_and_signs():
    years = pit.years_as_of(_store(), "2024-03-01")
    y23, y22, y21 = (_year(years, f"{y}-12-31") for y in (2023, 2022, 2021))
    assert y23["total_debt"] == 400.0 + 50.0 + 20.0   # LongTermDebtCurrent over DebtCurrent
    assert y22["total_debt"] == 400.0
    assert y23["free_cash_flow"] == 350.0 - 120.0
    assert y23["total_liabilities"] == 2300.0 - 1400.0
    assert y21["total_liabilities"] == 850.0           # reported Liabilities wins
    assert y23["capex"] == -120.0 and y23["dividends_paid"] == -40.0
    assert y23["buybacks"] == -60.0 and y23["acquisitions"] == -25.0
    assert y23["goodwill"] == 150.0 and y22["goodwill"] == 100.0 and y21["goodwill"] is None


def test_changed_year_end_keeps_the_later_period():
    store = _store()
    store["Revenues"]["units"]["USD"].append(
        {"start": "2022-04-01", "end": "2023-03-31", "val": 1.0, "filed": "2023-05-20", "form": "10-K"})
    ends = [y["fiscal_year_end"] for y in pit.years_as_of(store, "2024-03-01")]
    assert ends == ["2023-12-31", "2022-12-31", "2021-12-31"]


def test_52_53_week_years_are_all_kept():
    """A 52/53-week filer: two fiscal years end in calendar 2022 and both are real years."""
    years = (("2020-01-05", "2021-01-02", "2020-10-03", 1000.0, 700.0),
             ("2021-01-03", "2022-01-01", "2021-10-02", 1100.0, 780.0),
             ("2022-01-02", "2022-12-31", "2022-10-01", 1200.0, 850.0))
    store = {tag: {"units": {"USD": []}} for tag in (
        "Revenues", "OperatingIncomeLoss", "Assets", "StockholdersEquity", "AssetsCurrent",
        "LiabilitiesCurrent")}

    def add(tag, fact):
        store[tag]["units"]["USD"].append(fact)

    for start, end, nine_end, rev, nine_rev in years:
        filed = (date.fromisoformat(end) + timedelta(days=60)).isoformat()
        nine_filed = (date.fromisoformat(nine_end) + timedelta(days=40)).isoformat()
        for tag, annual, nine in (("Revenues", rev, nine_rev),
                                  ("OperatingIncomeLoss", rev / 10, nine_rev / 10)):
            add(tag, {"start": start, "end": end, "val": annual, "filed": filed, "form": "10-K"})
            add(tag, {"start": start, "end": nine_end, "val": nine, "filed": nine_filed,
                      "form": "10-Q"})
        for tag, val in (("Assets", 2000.0), ("StockholdersEquity", 1200.0),
                         ("AssetsCurrent", 800.0), ("LiabilitiesCurrent", 400.0)):
            add(tag, {"end": end, "val": val, "filed": filed, "form": "10-K"})

    as_of = "2023-06-01"
    assert [y["fiscal_year_end"] for y in pit.years_as_of(store, as_of)] == [
        "2022-12-31", "2022-01-01", "2021-01-02"]
    quarters = pit.quarters_as_of(store, as_of)
    assert [q["fiscal_year_end"] for q in quarters] == ["2022-12-31", "2022-01-01", "2021-01-02"]
    assert all(q["derived"] for q in quarters)
    assert [q["revenue"] for q in quarters] == [350.0, 320.0, 300.0]
    assert pit.revenue_history(store, as_of) == {"2022": 1200.0, "2021": 1100.0, "2020": 1000.0}


def test_quarters_derive_q4_and_flag_it():
    quarters = pit.quarters_as_of(_store(), "2024-03-01")
    assert [q["fiscal_year_end"] for q in quarters] == [
        "2023-12-31", "2023-09-30", "2023-06-30", "2023-03-31", "2022-09-30"]
    q4, q3 = quarters[0], quarters[1]
    assert set(q4) == {"fiscal_year_end", "revenue", "ebit", "current_assets",
                       "current_liabilities", "cash", "net_ppe", "goodwill", "total_assets",
                       "derived"}
    assert q4["derived"] is True
    assert q4["revenue"] == 1300.0 - 930.0 and q4["ebit"] == 260.0 - 186.0
    assert q4["current_assets"] == 900.0 and q4["goodwill"] == 150.0
    assert q3["derived"] is False and q3["revenue"] == 320.0 and q3["current_assets"] == 830.0


def test_q4_omitted_without_nine_month_figure():
    store = _store()
    # 2022 has an annual figure but no nine-month one: no 2022 Q4.
    assert [q["fiscal_year_end"] for q in pit.quarters_as_of(store, "2023-03-01")] == ["2022-09-30"]
    # Before the 2023 10-K, the nine-month figure alone derives nothing.
    assert [q["fiscal_year_end"] for q in pit.quarters_as_of(store, "2023-12-01")] == [
        "2023-09-30", "2023-06-30", "2023-03-31", "2022-09-30"]


def test_quarters_keep_at_most_eight():
    store = _store()
    usd = store["Revenues"]["units"]["USD"]
    for year in range(2010, 2020):
        usd.append({"start": f"{year}-07-01", "end": f"{year}-09-30", "val": 1.0,
                    "filed": f"{year}-11-01", "form": "10-Q"})
    quarters = pit.quarters_as_of(store, "2024-03-01")
    assert len(quarters) == 8 and quarters[0]["fiscal_year_end"] == "2023-12-31"


def test_shares_as_of_precedence():
    store = _store()
    assert pit.shares_as_of(store, "2023-03-01") == 101.0
    assert pit.shares_as_of(store, "2023-06-01") == 102.0
    assert pit.shares_as_of(store, "2024-03-01") == 205.0
    del store[pit.SHARES_DEI]
    assert pit.shares_as_of(store, "2024-03-01") == 200.0
    del store["CommonStockSharesOutstanding"]
    assert pit.shares_as_of(store, "2024-03-01") == 198.0
    assert pit.shares_as_of(store, "2022-01-01") is None


def test_shares_as_of_dated_names_the_count_date():
    store = _store()
    assert pit.shares_as_of_dated(store, "2023-03-01") == ("2023-02-10", 101.0)
    assert pit.shares_as_of_dated(store, "2024-03-01") == ("2024-02-10", 205.0)
    del store[pit.SHARES_DEI]
    assert pit.shares_as_of_dated(store, "2024-03-01") == ("2023-12-31", 200.0)
    assert pit.shares_as_of_dated(store, "2022-01-01") is None


def test_year_shares_take_the_first_filing_and_the_split_factor():
    store = _store()
    years = pit.years_as_of(store, "2024-03-01")
    assert _year(years, "2022-12-31")["shares"] == 100.0     # the restatement is ignored
    assert _year(years, "2023-12-31")["shares"] == 200.0
    split = pit.years_as_of(store, "2024-03-01",
                            split_factor=lambda end: 2.0 if end < "2023-01-01" else 1.0)
    assert _year(split, "2023-12-31")["shares"] == 200.0
    assert _year(split, "2022-12-31")["shares"] == 200.0
    assert _year(split, "2021-12-31")["shares"] == 200.0


def test_revenue_history_by_fiscal_year():
    store = _store()
    assert pit.revenue_history(store, "2024-03-01") == {"2023": 1300.0, "2022": 1150.0,
                                                        "2021": 1000.0}
    assert pit.revenue_history(store, "2023-03-01") == {"2022": 1100.0, "2021": 1000.0}


def test_facts_for_caches_and_fails_open(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    calls = []

    def fetch(url):
        calls.append(url)
        if "CIK0000000002" in url:
            raise VendorUnavailableError("down")
        return _raw()

    ciks = {"SYN": "0000000001", "DOWN": "0000000002"}
    monkeypatch.setattr(pit.sec_edgar, "cik_for", lambda t: ciks.get(t))
    monkeypatch.setattr(pit.sec_edgar, "_fetch_json", fetch)
    monkeypatch.setattr(pit.time, "sleep", lambda s: None)

    assert pit.facts_for("SYN", paths, fetch=False) is None
    assert calls == []
    got = pit.facts_for("SYN", paths)
    assert got["cik"] == "0000000001" and got["fetched_at"]
    assert got["facts"] == _store()
    assert calls == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"]
    path = paths.cache / "backtest" / "facts" / "SYN.json"
    assert json.loads(path.read_text())["facts"] == _store()
    assert pit.facts_for("SYN", paths) == got and len(calls) == 1
    assert pit.facts_for("SYN", paths, fetch=False) == got and len(calls) == 1
    pit.facts_for("SYN", paths, refresh=True)
    assert len(calls) == 2
    assert pit.facts_for("DOWN", paths) is None
    assert not (paths.cache / "backtest" / "facts" / "DOWN.json").exists()
    assert pit.facts_for("NOTAFILER", paths) is None
    assert len(calls) == 3
    # A response that is not a companyfacts object is a failed fetch.
    monkeypatch.setattr(pit.sec_edgar, "_fetch_json", lambda url: ["not", "facts"])
    assert pit.facts_for("SYN", paths, refresh=True) is None


def test_facts_for_unreadable_cache_fails_open(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    (paths.cache / "backtest" / "facts" / "SYN.json").mkdir(parents=True)   # read raises OSError
    monkeypatch.setattr(pit.sec_edgar, "_fetch_json", lambda url: pytest.fail("fetched"))
    assert pit.facts_for("SYN", paths, fetch=False) is None


def _switching_store(goods_tag="SalesRevenueGoodsNet") -> dict:
    """A filer on a pre-ASC 606 revenue tag through FY2017 and the new one from FY2018."""
    years = range(2013, 2019)
    raw = {
        goods_tag: _annual({y: 1000.0 + y for y in years if y <= 2017}),
        "RevenueFromContractWithCustomerExcludingAssessedTax": _annual({2018: 3018.0}),
        "OperatingIncomeLoss": _annual(dict.fromkeys(years, 200.0)),
        "NetIncomeLoss": _annual(dict.fromkeys(years, 150.0)),
        "Assets": _balance(dict.fromkeys(years, 2000.0)),
        "StockholdersEquity": _balance(dict.fromkeys(years, 1200.0)),
        "AssetsCurrent": _balance(dict.fromkeys(years, 800.0)),
        "LiabilitiesCurrent": _balance(dict.fromkeys(years, 400.0)),
    }
    return pit.extract({"facts": {"us-gaap": raw}})


def test_pre_asc_606_revenue_tags_keep_every_year():
    years = pit.years_as_of(_switching_store(), "2019-06-30")
    assert [y["fiscal_year_end"][:4] for y in years] == [str(y) for y in range(2018, 2012, -1)]
    assert all(y["revenue"] is not None for y in years)
    assert _year(years, "2015-12-31")["revenue"] == 3015.0
    assert _year(years, "2018-12-31")["revenue"] == 3018.0
    services = pit.years_as_of(_switching_store("SalesRevenueServicesNet"), "2019-06-30")
    assert _year(services, "2016-12-31")["revenue"] == 3016.0
    assert pit.revenue_history(_switching_store(), "2019-06-30")["2014"] == 3014.0


def test_fiscal_years_are_the_union_over_revenue_ebit_and_assets():
    """A tag switch to a revenue tag the map lacks never deletes the years EBIT and the balance
    sheet still report."""
    store = _switching_store("SomeUnmappedRevenueTag")
    years = pit.years_as_of(store, "2019-06-30")
    assert [y["fiscal_year_end"][:4] for y in years] == [str(y) for y in range(2018, 2012, -1)]
    assert _year(years, "2016-12-31")["revenue"] is None
    assert _year(years, "2016-12-31")["ebit"] == 200.0
    # A year known only from the balance sheet still counts when it has six values.
    del store["OperatingIncomeLoss"]
    assert len(pit.years_as_of(store, "2019-06-30")) == 6


def test_a_stale_or_placeholder_cover_count_is_not_used():
    store = _store()
    # A registration statement's placeholder count of 1, dated before the newest fiscal year.
    store[pit.SHARES_DEI] = {"units": {"shares": [
        {"end": "2019-03-18", "val": 1.0, "filed": "2019-03-18", "form": "10-12B"}]}}
    assert pit.shares_as_of_dated(store, "2024-03-01") == ("2023-12-31", 200.0)
    # Dated after the newest year end but far from the annual count: still a placeholder.
    store[pit.SHARES_DEI]["units"]["shares"].append(
        {"end": "2024-02-10", "val": 1.0, "filed": "2024-02-20", "form": "10-K"})
    assert pit.shares_as_of_dated(store, "2024-03-01") == ("2023-12-31", 200.0)
    # Within 0.5x-2x of the annual count and not stale: the cover count wins.
    store[pit.SHARES_DEI]["units"]["shares"].append(
        {"end": "2024-02-11", "val": 390.0, "filed": "2024-02-21", "form": "10-K"})
    assert pit.shares_as_of_dated(store, "2024-03-01") == ("2024-02-11", 390.0)
    # Stale: dated before the newest year end known, though its size is fine.
    stale = _store()
    stale[pit.SHARES_DEI] = {"units": {"shares": [
        {"end": "2023-11-01", "val": 199.0, "filed": "2024-02-25", "form": "10-K"}]}}
    assert pit.shares_as_of_dated(stale, "2024-03-01") == ("2023-12-31", 200.0)
    # No fiscal year and no annual count known: the cover count stands alone.
    alone = {pit.SHARES_DEI: {"units": {"shares": [
        {"end": "2019-03-18", "val": 5.0, "filed": "2019-03-18", "form": "10-12B"}]}}}
    assert pit.shares_as_of_dated(alone, "2020-01-01") == ("2019-03-18", 5.0)
