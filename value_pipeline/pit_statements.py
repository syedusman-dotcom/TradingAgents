"""Company statements as they stood on a past date, from SEC EDGAR, for the backtest.

A backtest that reads today's statements credits the past with figures nobody had yet:
a fiscal year is public only once its 10-K is filed, and a restated figure is not what
investors saw. EDGAR stamps every fact with its filing date, so ``years_as_of`` and
``quarters_as_of`` rebuild the ``years`` / ``quarters`` dicts the screen and the value
metrics already consume from the facts filed on or before a date, through
``sec_edgar._as_of``.

``extract`` keeps only the tags those dicts need, in companyfacts' own shape
(``{tag: {"units": {unit: [fact, ...]}}}``), so the store ``facts_for`` caches per ticker
is a few hundred KB instead of several MB and ``_as_of`` reads it unchanged. Nothing here
trades; a failed fetch is logged and answered with None.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.value_metrics import YEAR_FIELDS, fiscal_year_label
from tradingagents.dataflows.vendors import sec_edgar
from value_pipeline.config import Paths

log = logging.getLogger(__name__)

# Per field, the us-gaap tags filers use for it, best first; a period keeps the first tag
# that reports it and values are never added across tags (as in ``_as_of``).
TAGS: dict[str, tuple[str, ...]] = {
    # SalesRevenueGoodsNet / SalesRevenueServicesNet: the usual total before ASC 606 (2018).
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenuesNetOfInterestExpense", "SalesRevenueGoodsNet",
                "SalesRevenueServicesNet"),
    "gross_profit": ("GrossProfit",),
    "ebit": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    "interest_expense": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                         "InterestExpenseBorrowings", "InterestPaidNet"),
    "total_assets": ("Assets",),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "cash": ("CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "net_ppe": ("PropertyPlantAndEquipmentNet",
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),  # noqa: E501
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                       "LongTermDebt"),
    "equity": ("StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "retained_earnings": ("RetainedEarningsAccumulatedDeficit",),
    "total_liabilities": ("Liabilities",),
    "shares": ("CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsForCapitalImprovements"),
    "dividends_paid": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
    "buybacks": ("PaymentsForRepurchaseOfCommonStock",),
    "stock_based_comp": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
    "receivables": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    "depreciation": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                     "DepreciationAmortizationAndAccretionNet", "Depreciation"),
    "sga": ("SellingGeneralAndAdministrativeExpense",),
    "acquisitions": ("PaymentsToAcquireBusinessesNetOfCashAcquired",
                     "PaymentsToAcquireBusinessesGross"),
    "goodwill": ("Goodwill",),
    "rnd": ("ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
}

# Short-term debt, added to long-term debt for ``total_debt``; not fields of their own.
DEBT_PARTS = ("LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings", "CommercialPaper")
# Cost of revenue, for ``gross_profit = revenue - cost`` when ``GrossProfit`` is not filed
# (about half of filers); first found per period wins.
COST_OF_REVENUE = ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold",
                   "CostOfServices")
# Pre-tax income from continuing operations, for ``ebit = pre-tax income + interest expense``
# when ``OperatingIncomeLoss`` is not filed (H&R Block, Fox); the second is the older name.
PRETAX_INCOME = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",  # noqa: E501
)
# The cover-page share count, stored beside the us-gaap tags under this key.
SHARES_DEI = "dei:EntityCommonStockSharesOutstanding"
# A cover count this far from the newest annual count (as a ratio) is a placeholder.
DEI_BAND = (0.5, 2.0)
# Added to goodwill when both are filed, to match Yahoo's "Goodwill And Other Intangible Assets".
INTANGIBLES = "IntangibleAssetsNetExcludingGoodwill"

# EDGAR reports payments positive; the Yahoo vendor, whose dicts the metrics read, negative.
PAYMENTS = ("capex", "dividends_paid", "buybacks", "acquisitions")
QUARTER_BALANCE = ("current_assets", "current_liabilities", "cash", "net_ppe", "goodwill",
                   "total_assets")
UNITS = ("USD", "shares")
FACT_KEYS = ("start", "end", "val", "filed", "form")
MIN_YEAR_VALUES = 6       # the Yahoo vendor's rule: fewer means a padded, near-empty year
MAX_QUARTERS = 8
Q4_GAP_DAYS = (80, 100)   # a nine-month figure ends about three months before the year end
MIN_YEAR_GAP_DAYS = 300   # annual ends closer than this overlap: one restates or replaces the other

# SEC asks for no more than ten requests a second; eight leaves room.
MIN_REQUEST_GAP = 0.125
_last_fetch = 0.0

_ANNUAL = (sec_edgar._SPANS["annual"],)
_QUARTER = (sec_edgar._SPANS["quarterly"],)
_NINE_MONTHS = (sec_edgar._YEAR_TO_DATE[1][:2],)

Values = dict[tuple[str, int], float]


def _compact(entry: dict) -> dict | None:
    units = {unit: [{k: fact[k] for k in FACT_KEYS if k in fact} for fact in facts]
             for unit, facts in (entry.get("units") or {}).items() if unit in UNITS}
    return {"units": units} if units else None


def extract(facts: dict) -> dict:
    """The mapped tags of one companyfacts JSON, each fact cut to ``start?, end, val, filed,
    form`` and its units to USD and shares, in companyfacts' shape so ``_as_of`` reads it."""
    body = facts.get("facts") or {}
    us_gaap = body.get("us-gaap") or {}
    wanted = ({tag for tags in TAGS.values() for tag in tags} | set(DEBT_PARTS) | {INTANGIBLES}
              | set(COST_OF_REVENUE) | set(PRETAX_INCOME))
    out = {}
    for tag in sorted(wanted):
        compact = _compact(us_gaap.get(tag) or {})
        if compact:
            out[tag] = compact
    compact = _compact((body.get("dei") or {}).get("EntityCommonStockSharesOutstanding") or {})
    if compact:
        out[SHARES_DEI] = compact
    return out


def _throttle() -> None:
    global _last_fetch
    wait = MIN_REQUEST_GAP - (time.monotonic() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    _last_fetch = time.monotonic()


def facts_for(ticker: str, paths: Paths, fetch: bool = True, refresh: bool = False,
              stale_before: str | None = None) -> dict | None:
    """``{"cik", "name", "fetched_at", "facts"}`` for the ticker (``name``: EDGAR's entity name),
    cached under
    ``paths.cache/backtest/facts``; fetched when absent, on ``refresh``, or when the cache was
    fetched before ``stale_before`` (an ISO date: the newest cohort date, so a later cohort
    sees the filings made since), never with ``fetch=False``. A failed refetch keeps the
    cached store. None for a non-filer, a failed fetch with nothing cached, or nothing cached
    and no fetch."""
    path = paths.cache / "backtest" / "facts" / f"{ticker}.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("unreadable EDGAR fact store %s; refetching", path)
    stale = (cached is not None and stale_before is not None
             and str(cached.get("fetched_at", ""))[:10] < stale_before)
    if not fetch or (cached is not None and not refresh and not stale):
        return cached
    try:
        cik = sec_edgar.cik_for(ticker)
        if cik is None:
            log.info("%s: not a US SEC filer", ticker)
            return cached
        _throttle()
        raw = sec_edgar._fetch_json(sec_edgar._FACTS_URL.format(cik=cik))
    except (VendorUnavailableError, OSError) as exc:
        log.warning("%s: EDGAR companyfacts unavailable (%s)", ticker, exc)
        return cached
    if not isinstance(raw, dict):
        log.warning("%s: EDGAR companyfacts unreadable (%s)", ticker, type(raw).__name__)
        return cached
    store = {"cik": cik, "name": raw.get("entityName"),
             "fetched_at": datetime.now().isoformat(timespec="seconds"), "facts": extract(raw)}
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(store), encoding="utf-8"))
    return store


def _first_filed(facts: dict, tags: tuple[str, ...], as_of_date: str,
                 spans: tuple[tuple[int, int], ...],
                 forms: tuple[str, ...] = ()) -> dict[tuple[str, int], dict]:
    """``_as_of``, except a period reported more than once keeps its **earliest** filing on or
    before the date: the original figure, never a later restatement (a share count recast
    after a split would otherwise mix split bases across years). Each period maps to that
    fact, so the caller has its ``val`` and the ``filed`` date its basis is as of."""
    chosen: dict[tuple[str, int], dict] = {}
    for tag in tags:
        for unit_values in ((facts.get(tag) or {}).get("units", {})).values():
            first: dict[tuple[str, int], dict] = {}
            covered: set[tuple[str, int]] = set()
            for fact in unit_values:
                index = sec_edgar._span_index(fact, spans)
                key = (fact["end"], index)
                if fact["filed"] > as_of_date or index is None or key in chosen:
                    continue
                if not forms or fact.get("form", "").startswith(forms):
                    covered.add(key)
                seen = first.get(key)
                if seen is None or fact["filed"] < seen["filed"]:
                    first[key] = fact
            chosen.update({key: fact for key, fact in first.items() if key in covered})
    return dict(sorted(chosen.items()))


def _values(store: dict, tags: tuple[str, ...], as_of: str, spans, forms=()) -> Values:
    return sec_edgar._as_of(store, tags, as_of, spans, forms)[0]


def _distinct_ends(ends) -> list[str]:
    """Annual period ends, newest first, without overlapping duplicates: an end fewer than
    ``MIN_YEAR_GAP_DAYS`` before the last kept one is a restated or changed-year-end period
    and the later one wins. A 52/53-week filer's years (2022-01-01, then 2022-12-31) are
    about 364 days apart and all kept, which a one-per-calendar-year rule would not do."""
    kept: list[str] = []
    for end in sorted(set(ends), reverse=True):
        if not kept or _days_between(end, kept[-1]) >= MIN_YEAR_GAP_DAYS:
            kept.append(end)
    return kept


def _fiscal_year_ends(store: dict, as_of: str) -> list[str]:
    """Annual period ends of the revenue, EBIT and total-assets facts filed in annual reports,
    newest first, overlapping periods removed by ``_distinct_ends``. The union, so a filer
    that moves to a tag the map lacks keeps the years its other lines still report."""
    ends: set[str] = set()
    for tags in (TAGS["revenue"], TAGS["ebit"], TAGS["total_assets"]):
        ends |= {end for end, _ in _values(store, tags, as_of, _ANNUAL, sec_edgar._ANNUAL_FORMS)}
    return _distinct_ends(ends)


def _goodwill(goodwill: float | None, intangibles: float | None) -> float | None:
    if goodwill is None or intangibles is None:
        return goodwill
    return goodwill + intangibles


def _first_present(*values: float | None) -> float:
    return next((v for v in values if v is not None), 0.0)


def years_as_of(store: dict, as_of: str,
                split_factor: Callable[[str], float] | None = None) -> list[dict]:
    """The fiscal years filed on or before ``as_of`` as ``YEAR_FIELDS`` dicts, newest first,
    each keyed by ``fiscal_year_end``; ``split_factor(filed)`` brings a year's share count to
    today's basis when given, called with the filing date of the count (its first filing),
    since splits between the year end and that filing are already in it. Years with fewer
    than six values are dropped."""
    annual = sec_edgar._ANNUAL_FORMS
    fields = {field: _values(store, tags, as_of, _ANNUAL, annual)
              for field, tags in TAGS.items() if field != "shares"}
    shares = _first_filed(store, TAGS["shares"], as_of, _ANNUAL, annual)
    fields["shares"] = {key: fact["val"] for key, fact in shares.items()}
    parts = {tag: _values(store, (tag,), as_of, _ANNUAL, annual)
             for tag in (*DEBT_PARTS, INTANGIBLES)}
    cost = _values(store, COST_OF_REVENUE, as_of, _ANNUAL, annual)
    pretax = _values(store, PRETAX_INCOME, as_of, _ANNUAL, annual)

    years = []
    for end in _fiscal_year_ends(store, as_of):
        key = (end, 0)
        year = {"fiscal_year_end": end}
        year.update(dict.fromkeys(YEAR_FIELDS))
        year.update({field: values.get(key) for field, values in fields.items()})
        year["goodwill"] = _goodwill(year["goodwill"], parts[INTANGIBLES].get(key))
        if year["long_term_debt"] is not None:
            year["total_debt"] = (
                year["long_term_debt"]
                + _first_present(parts["LongTermDebtCurrent"].get(key),
                                 parts["DebtCurrent"].get(key))
                + _first_present(parts["ShortTermBorrowings"].get(key),
                                 parts["CommercialPaper"].get(key)))
        if year["gross_profit"] is None and year["revenue"] is not None and key in cost:
            year["gross_profit"] = year["revenue"] - cost[key]
        if (year["ebit"] is None and key in pretax
                and year["interest_expense"] is not None):
            year["ebit"] = pretax[key] + abs(year["interest_expense"])
        if year["operating_cash_flow"] is not None and year["capex"] is not None:
            year["free_cash_flow"] = year["operating_cash_flow"] - abs(year["capex"])
        if (year["total_liabilities"] is None and year["total_assets"] is not None
                and year["equity"] is not None):
            year["total_liabilities"] = year["total_assets"] - year["equity"]
        for field in PAYMENTS:
            if year[field] is not None:
                year[field] = -abs(year[field])
        if split_factor is not None and year["shares"] is not None:
            # From the filing date: a 10-K filed after a split already restates the count.
            year["shares"] = year["shares"] * split_factor(shares[key]["filed"])
        if sum(v is not None for k, v in year.items() if k != "fiscal_year_end") >= MIN_YEAR_VALUES:
            years.append(year)
    return years


def _days_between(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def quarters_as_of(store: dict, as_of: str) -> list[dict]:
    """Up to eight quarters filed on or before ``as_of``, newest first: revenue, EBIT and the
    balance fields ``_invested_capital`` reads. A fourth quarter is never filed on its own; it
    is derived as the annual figure less the nine-month one when both are on file, flagged
    ``derived``, and left out otherwise."""
    flows = {field: _values(store, TAGS[field], as_of, _QUARTER) for field in ("revenue", "ebit")}
    nine = {field: _values(store, TAGS[field], as_of, _NINE_MONTHS) for field in flows}
    annual = {field: _values(store, TAGS[field], as_of, _ANNUAL, sec_edgar._ANNUAL_FORMS)
              for field in flows}
    balance = {field: _values(store, TAGS[field], as_of, _QUARTER) for field in QUARTER_BALANCE}
    intangibles = _values(store, (INTANGIBLES,), as_of, _QUARTER)

    quarters: dict[str, dict] = {}
    for end in {end for end, _ in flows["revenue"] or flows["ebit"]}:
        quarters[end] = {"fiscal_year_end": end, "revenue": flows["revenue"].get((end, 0)),
                         "ebit": flows["ebit"].get((end, 0)), "derived": False}
    for end in _fiscal_year_ends(store, as_of):
        if end in quarters:
            continue      # a filer that reports the fourth quarter on its own
        q4 = {"fiscal_year_end": end, "derived": True}
        for field in flows:
            year = annual[field].get((end, 0))
            nine_end = next((e for e, _ in nine[field]
                             if Q4_GAP_DAYS[0] <= _days_between(e, end) <= Q4_GAP_DAYS[1]), None)
            q4[field] = (None if year is None or nine_end is None
                         else year - nine[field][(nine_end, 0)])
        if q4["revenue"] is not None or q4["ebit"] is not None:
            quarters[end] = q4

    out = []
    for end in sorted(quarters, reverse=True)[:MAX_QUARTERS]:
        quarter = quarters[end]
        key = (end, 0)
        quarter.update({field: balance[field].get(key) for field in QUARTER_BALANCE})
        quarter["goodwill"] = _goodwill(quarter["goodwill"], intangibles.get(key))
        out.append({k: quarter[k] for k in ("fiscal_year_end", "revenue", "ebit",
                                             *QUARTER_BALANCE, "derived")})
    return out


def shares_as_of_dated(store: dict, as_of: str) -> tuple[str, float] | None:
    """The share count known on ``as_of`` and the date it is counted at (its fact's ``end``), on
    the filing's own basis: the latest cover-page count, else the latest annual shares
    outstanding, else diluted weighted average shares. The date is what a split adjustment
    starts from: splits after it are not in the count.

    The cover count is trusted only when it is dated no earlier than the newest fiscal year
    end known and lies within ``DEI_BAND`` of the newest annual count (when there is one): a
    registration statement's placeholder (FOX's ``1``) or a stale cover would otherwise price
    the company at a few dollars."""
    annual = None
    for tag in ("CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding"):
        values = _values(store, (tag,), as_of, _ANNUAL, sec_edgar._ANNUAL_FORMS)
        if values:
            (end, _), count = max(values.items())
            annual = end, float(count)
            break
    dei = _values(store, (SHARES_DEI,), as_of, _ANNUAL)
    if dei:
        (end, _), count = max(dei.items())
        ends = _fiscal_year_ends(store, as_of)
        fresh = not ends or end >= ends[0]
        sane = annual is None or (annual[1] > 0
                                  and DEI_BAND[0] <= count / annual[1] <= DEI_BAND[1])
        if fresh and sane:
            return end, float(count)
    return annual


def shares_as_of(store: dict, as_of: str) -> float | None:
    """``shares_as_of_dated``'s count alone."""
    dated = shares_as_of_dated(store, as_of)
    return None if dated is None else dated[1]


def revenue_history(store: dict, as_of: str) -> dict[str, float]:
    """Fiscal year -> annual revenue in dollars, filed on or before ``as_of``, for
    ``record``'s ten-year CAGR. Overlapping periods keep the later one (``_distinct_ends``)
    and a year is labelled by ``value_metrics.fiscal_year_label`` (as ``record`` labels the
    years it merges with), so 52/53-week years stay distinct."""
    values, unit = sec_edgar._as_of(store, TAGS["revenue"], as_of, _ANNUAL,
                                    sec_edgar._ANNUAL_FORMS)
    if unit != "USD":
        return {}
    by_end = {end: value for (end, _), value in values.items()}
    return {fiscal_year_label(end): by_end[end] for end in _distinct_ends(by_end)}
