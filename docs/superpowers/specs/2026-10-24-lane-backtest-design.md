# Lane-rule backtest — design

**Date:** 2026-10-24
**Status:** the user asked "Is the analysis improving with each feature?"; Claude answered that reasoning is measurably better but returns are unmeasured and recommended a backtest of the lane rules before any new gate; the user said "Add the lens", chose "Lane-rule backtest" and then "Design it" / "Skip questions". Every design call below is Claude's and is marked *(my call)* where a reasonable builder might have chosen otherwise.

## Goal

Answer, with history rather than argument, whether the mechanical rules the pipeline applies to each investment lane would have beaten the index. The report must be able to say, for each lane, a sentence of the form *"compounders bought under 1.15× base beat SPY in 7 of 10 years (equal-weight, one-year hold), by a median of +3.1 points"*, and show how that sentence changes when each bar moves.

What is tested is the **rules**, not the agents: the screen's hard filters, the Magic Formula rank, the formula value and its margin of safety, the compounder record bars and price bars, the inflection bars and the dislocation hint — the code in `value_pipeline/screener.py`, `value_pipeline/metrics.py`, `tradingagents/dataflows/value_metrics.py` and `tradingagents/agents/decision_math.py`, run on the numbers a filer had published by each cohort date.

### Non-goals and anti-goals

- Not a portfolio simulator: no position sizing, cash ledger, fees, rebalancing or equity curve. Cohorts are independent; each pick is one equal-weight slot held for a fixed term. (`tradingagents/backtest.py` is a different tool: it runs the agents over a grid and scores their ratings. This design runs no agents.)
- Not a new rule: the backtest reports; it changes no bar. Moving a bar after reading it is a separate decision for the user.
- Nothing trades.
- Anti-goal: a flattering number from look-ahead. Every fundamental is the value **filed on or before** the cohort date (an amendment counts from its filing date), every price is the close on or after the cohort date, and the one bias that cannot be removed with free data (survivorship of today's index members) is quantified and printed on the report, never hidden.

## Verified facts (code and data on 2026-10-24)

- `sec_edgar._as_of(facts, tags, as_of_date, spans, forms)` is point-in-time: it drops facts with `filed > as_of_date`, keeps the latest filing per period and never adds across tags. `_SPANS["annual"] = (300, 400)` days, `_ANNUAL_FORMS = ("10-K", "20-F", "40-F")`. `cik_for(ticker)` maps a ticker to a CIK via SEC's ticker file. `_fetch_json(url)` carries the SEC user agent and raises `VendorUnavailableError`.
- EDGAR companyfacts (`_FACTS_URL`) serves one JSON per filer, 3–7 MB each (DECK 3.3, HRB 5.1, ADBE 4.8, CAT 6.5), with `facts["us-gaap"]` and `facts["dei"]`; each fact has `start` (duration facts only), `end`, `val`, `filed`, `form`, `fy`, `fp`. Tag names vary by filer: DECK reports `RevenueFromContractWithCustomerExcludingAssessedTax` and no `Revenues`; CAT has no `StockholdersEquity` (it reports the "including noncontrolling interest" variant); ADBE has no `ResearchAndDevelopmentExpense` under that tag; HRB has no `GrossProfit`. Every tag therefore needs an ordered list of alternatives, and some fields must be derived (total debt from its parts, free cash flow from operating cash flow and capex, total liabilities from assets minus equity).
- `dei:EntityCommonStockSharesOutstanding` exists for all four probed filers (instant facts, the 10-K/10-Q cover page).
- `YEAR_FIELDS` (28 keys) is what `screener.evaluate(data)` and every `value_metrics` function consume; the Yahoo vendor stores capex, dividends paid, buybacks and acquisitions **negative** (payments) and the code takes `abs()`. `data` needs `ticker, price, market_cap, shares, years, quarters, closes` (plus optional `enterprise_value, industry, sector`). `evaluate` raises `DataUnavailable` with fewer than two years.
- `screener.failed_filters(row, cfg["filters"])` applies the hard filters (min market cap 2 bn USD, F-Score ≥ 6, Altman Z ≥ 1.8, positive FCF, interest coverage ≥ 4, ROIC ≥ 8% in every year, dilution ≤ 3%/yr). `screener.lane_hints(row)` returns `compounder` / `inflection` / `dislocation` from the row's record, TTM and drawdown keys, only when `row["passes_filters"]` is true. `metrics.magic_formula_rank(rows, EY_KEY, ROIC_KEY)` ranks on the five-year averages.
- The Portfolio Manager's lane bars live in `decision_math`: compounder Buy at ≤ `COMPOUNDER_BUY_PREMIUM` (1.15) × worksheet base, Overweight ≤ 1.25; expected return (yield + earned growth + negative reversion only) ≥ `COMPOUNDER_BUY_RETURN` 0.12 (Overweight 0.10); implied growth ≤ max(earned, sustainable); upside/downside ≥ 1.5 (needs agent scenarios, not testable mechanically). Inflection: TTM EBIT ≥ 1.5× five-year average, revenue acceleration ≥ 10 points, incremental ROIC ≥ 20%, plus agent-only tests (bear probability, demand kill criterion, verified evidence, weighted MOS). The watch alerts a Buy-rated name at a margin of safety ≥ `watch.buy_margin_of_safety` (0.30).
- `metrics.trend(closes)` needs 220+ closes (oldest first); `metrics.dislocation(closes, ttm_revenue, ttm_ebit, avg_rev, avg_ebit, flags)` needs 252 for the drawdown. `metrics.inflection(years, quarters)` needs quarters with `fiscal_year_end, revenue, ebit` and balance fields for `_invested_capital`; it returns `incremental_roic`, which `evaluate` does not copy into the row.
- `value_metrics.record(years, revenue_history)` accepts a longer `{fiscal year: revenue}` history for the ten-year CAGR; `sec_filings.annual_revenue` builds that from XBRL today.
- Prices: `market_data.closes_between` uses `yf.Ticker(...).history(auto_adjust=False)`; Yahoo's `Close` is split-adjusted but not dividend-adjusted, `Adj Close` is both, and the frame carries a `Stock Splits` column (ratio on the split date, 0 otherwise). `^TNX` is the 10-year Treasury yield in percent. `current_discount_rate` is `max(0.09, treasury + 0.045)`.
- Universe: `market_data.universe(paths, indexes)` reads today's S&P 500 and 400 members from Wikipedia (first table of each page); the S&P 500 page also carries a "Selected changes" table (date, added, removed). There is no membership history for past dates in the project.
- Website: `create_app` renders Jinja templates; the `markdown` filter (markdown-it, tables enabled) renders Markdown text; `/calibration` is the pattern for a read-only page that never calls the network. `config.DEFAULTS` has one dict per section; `config.yaml` overrides.
- Caches: `paths.cache` (`value_pipeline/data/cache/`, gitignored) holds the screen's fundamentals and `prices/` for calibration. EDGAR's per-filer JSON is cached by `sec_edgar._cached_json` under tradingagents' `data_cache_dir` for 24 hours only.

## Design

### 1. Point-in-time statements from EDGAR — `value_pipeline/pit_statements.py`

A compact, permanent per-ticker fact store and the functions that turn it into the `years` and `quarters` dicts the metrics already consume, as they stood on a date.

**Tag map.** `TAGS: dict[str, tuple[str, ...]]` keyed by `YEAR_FIELDS` names, each an ordered tuple of us-gaap tags (first found per period wins, as in `_as_of`). The map below is the starting point *(my call; the builder may add alternatives found in the data, never remove these)*:

| field | tags (in order) |
|---|---|
| revenue | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`, `RevenuesNetOfInterestExpense`, `SalesRevenueGoodsNet`, `SalesRevenueServicesNet` (the last two: the usual pre-ASC 606 totals, added in the final fix wave) |
| gross_profit | `GrossProfit` |
| ebit | `OperatingIncomeLoss` |
| net_income | `NetIncomeLoss`, `ProfitLoss`, `NetIncomeLossAvailableToCommonStockholdersBasic` |
| interest_expense | `InterestExpense`, `InterestExpenseNonoperating`, `InterestExpenseDebt`, `InterestExpenseBorrowings`, `InterestPaidNet` |
| total_assets | `Assets` |
| current_assets | `AssetsCurrent` |
| current_liabilities | `LiabilitiesCurrent` |
| cash | `CashCashEquivalentsAndShortTermInvestments`, `CashAndCashEquivalentsAtCarryingValue`, `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` |
| net_ppe | `PropertyPlantAndEquipmentNet`, `PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization` |
| long_term_debt | `LongTermDebtNoncurrent`, `LongTermDebtAndCapitalLeaseObligations`, `LongTermDebt` |
| equity | `StockholdersEquity`, `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` |
| retained_earnings | `RetainedEarningsAccumulatedDeficit` |
| total_liabilities | `Liabilities` |
| shares | `CommonStockSharesOutstanding`, `WeightedAverageNumberOfDilutedSharesOutstanding` (dei `EntityCommonStockSharesOutstanding` first; see shares below) |
| operating_cash_flow | `NetCashProvidedByUsedInOperatingActivities`, `NetCashProvidedByUsedInOperatingActivitiesContinuingOperations` |
| capex | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets`, `PaymentsToAcquireOtherPropertyPlantAndEquipment`, `PaymentsForCapitalImprovements` |
| dividends_paid | `PaymentsOfDividendsCommonStock`, `PaymentsOfDividends` |
| buybacks | `PaymentsForRepurchaseOfCommonStock` |
| stock_based_comp | `ShareBasedCompensation`, `AllocatedShareBasedCompensationExpense` |
| receivables | `AccountsReceivableNetCurrent`, `ReceivablesNetCurrent` |
| depreciation | `DepreciationDepletionAndAmortization`, `DepreciationAndAmortization`, `DepreciationAmortizationAndAccretionNet`, `Depreciation` |
| sga | `SellingGeneralAndAdministrativeExpense` |
| acquisitions | `PaymentsToAcquireBusinessesNetOfCashAcquired`, `PaymentsToAcquireBusinessesGross` |
| goodwill | `Goodwill` (plus `IntangibleAssetsNetExcludingGoodwill` added when both exist, to match Yahoo's "Goodwill And Other Intangible Assets") |
| rnd | `ResearchAndDevelopmentExpense`, `ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost` |

Debt parts (not fields): `LongTermDebtCurrent`, `ShortTermBorrowings`, `DebtCurrent`, `CommercialPaper`.

**Derived fields** (after the per-tag pick, per period): `total_debt = long_term_debt + (LongTermDebtCurrent or DebtCurrent or 0) + (ShortTermBorrowings or CommercialPaper or 0)` (None when long_term_debt is None); `free_cash_flow = operating_cash_flow − |capex|` (None when either is None); `total_liabilities = total_assets − equity` when `Liabilities` is missing and both exist; `gross_profit = revenue − cost` (`CostOfGoodsAndServicesSold`, `CostOfRevenue`, `CostOfGoodsSold`, `CostOfServices`, first found) when `GrossProfit` is missing; `ebit = pre-tax income from continuing operations (`IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest`, else the older `…MinorityInterestAndIncomeLossFromEquityMethodInvestments`) + |interest_expense|` when `OperatingIncomeLoss` is missing and both exist (final fix wave; the quarters still read `OperatingIncomeLoss` only). **Signs follow the Yahoo vendor:** capex, dividends_paid, buybacks and acquisitions are stored negative (EDGAR reports payments positive).

**Compact store.** `extract(facts: dict) -> dict`: from one companyfacts JSON keep, for every tag in `TAGS`, the debt parts and `dei:EntityCommonStockSharesOutstanding`, the list of `{start?, end, val, filed, form}` for USD (or `shares`) units — nothing else. Written as `paths.cache / "backtest" / "facts" / f"{ticker}.json"` (a few hundred KB), with `{"cik", "fetched_at", "facts": {...}}`. `facts_for(ticker, paths, fetch=True, refresh=False) -> dict | None`: the cached compact store; fetches with `sec_edgar._fetch_json` (SEC rate: sleep so that no more than 8 requests start per second *(my call)*) when absent or `refresh`; None for a non-filer or a failed fetch (logged, never raised).

**As-of views.** Reuse `sec_edgar._as_of` on the compact store (it has the same fact shape):
- `years_as_of(store, as_of: str) -> list[dict]`: one dict per fiscal year, newest first, `fiscal_year_end` = period end, every `YEAR_FIELDS` key (None when unreported), from annual spans and `_ANNUAL_FORMS`; the fiscal years are the union of the annual period ends over the revenue, EBIT and `Assets` facts (so a revenue-tag switch never deletes history); balance (instant) facts take the value at the fiscal year end from the latest filing on or before `as_of`. Duplicate fiscal years after a changed year-end: the later period of that calendar year wins (as `annual_revenue` does).
- `quarters_as_of(store, as_of) -> list[dict]`: quarter dicts (newest first, up to 8) with `fiscal_year_end`, `revenue`, `ebit` and the balance fields `_invested_capital` reads (`current_assets, current_liabilities, cash, net_ppe, goodwill, total_assets`); duration facts from `_SPANS["quarterly"]` (60–115 days, any form). **A fourth quarter is not filed on its own**: when a fiscal year's annual figure and a nine-month year-to-date figure (span 240–290 days ending about three months before the year end) are both known by `as_of`, the quarter is derived as annual − nine-month and flagged `"derived": True`; otherwise it is left out *(my call — the live Yahoo vendor hands the metrics four discrete quarters; without Q4 the TTM would be three quarters)*.
- `shares_as_of(store, as_of) -> float | None`: the latest-dated `dei:EntityCommonStockSharesOutstanding` filed on or before `as_of`, else the latest annual `CommonStockSharesOutstanding`, else `WeightedAverageNumberOfDilutedSharesOutstanding`. The cover count is used only when it is dated no earlier than the newest fiscal year end known and lies within 0.5×–2× of the newest annual count when there is one (a registration statement's placeholder — FOX's `1` — or a stale cover falls through to the annual tags; final fix wave). Raw EDGAR basis; the caller brings it to today's split basis (§2).
- `revenue_history(store, as_of) -> dict[str, float]`: fiscal year → revenue over the whole store, for `record(years, revenue_history)`'s ten-year CAGR. A year is labelled by `value_metrics.fiscal_year_label(end)` (the year of the end less 14 days), the same rule `record`'s `_revenue_by_year` and `sec_filings.annual_revenue` use, so a 52/53-week year ending in early January is the year before in every series (final fix wave).

**Share counts on one split basis** *(builder-check ruling)*. Yahoo restates past share counts after a split; EDGAR facts are on the basis of their own filing, and a later 10-K can restate a prior year. So the per-year `shares` field is taken from the **first** filing that reported the period (the original, never a restatement; a small `_first_filed` variant beside `_as_of`, same look-ahead rule), and `years_as_of(store, as_of, split_factor=None)` multiplies each year's `shares` by `split_factor(fiscal_year_end)` when the callable is given (§2's `split_factor_after`). Every count the metrics see — the yearly counts behind `dilution_per_year`, the cohort count behind market cap — is then on today's basis, as Yahoo's would be. Without the callable the counts stay raw (tests, non-price uses).

### 2. Prices — in `value_pipeline/backtest.py`

`price_history(ticker, paths, fetch=True, refresh=False) -> pd.DataFrame | None`: one `yf.Ticker(ticker).history(period="max", auto_adjust=False)` call, kept as `paths.cache / "backtest" / "prices" / f"{ticker}.csv"` with columns `date, close, adj_close, split` (split = the `Stock Splits` ratio, 0 when none); refreshed only on `--refresh` or when the file's last date is older than 400 days before the latest hold end needed *(my call)*. Fail-open: None, logged.

Helpers (pure, on the frame): `close_on_or_after(hist, day) -> tuple[date, float] | None` (first trading day ≥ day, within 10 days); `closes_before(hist, day, n=420) -> list[float]` (the last n `close` values on or before day, oldest first — the screen's trend and dislocation input); `split_factor_after(hist, day) -> float` (product of split ratios dated after `day`; 1.0 when none); `total_return(hist, start, end) -> dict | None` with `{"start_date", "end_date", "ret", "ended_early": bool}` from `adj_close` at `close_on_or_after(start)` to `adj_close` at the last date ≤ end when the series ends before `end` (`ended_early` true; a delisting or data gap), else at `close_on_or_after(end)`; None when the start is missing.

Market cap on a date: `close × shares_as_of × split_factor_after(day)` — Yahoo's close is already split-adjusted, EDGAR's share count is not, so the shares are brought forward to today's split basis *(my call)*.

Discount rate on a date: `^TNX` history through the same `price_history` (ticker `^TNX`), `rate = max(MIN_DISCOUNT_RATE, close/100 + EQUITY_PREMIUM)` on the last trading day ≤ cohort date; the floor alone when `^TNX` is unavailable.

### 3. Snapshot, rules, cohorts — `value_pipeline/backtest.py`

**Snapshot.** `snapshot(ticker, as_of: str, store, hist, rate, cfg, sector) -> tuple[dict, tuple] | None`: builds `data = {ticker, price, market_cap, shares, years, quarters, closes}` from §1 and §2, calls `screener.evaluate(data, ...)`, adds `sector`, `passes_filters` (empty `screener.failed_filters`) and `fail_reasons`, `lane_hints` (`screener.lane_hints`), `incremental_roic` from `metrics.inflection(years, quarters)`, and the Portfolio Manager's record verdict (below); returns the row and the `(years, price, market_cap, shares)` tuple `apply_sector_targets` needs; None when there are fewer than two fiscal years by `as_of`, no price, or `evaluate` raises `DataUnavailable`. Nothing in the row may come from a fact filed after `as_of` or a close after `as_of` (the one invariant a test must pin).

**Which years the metrics see** *(builder-check ruling)*. The live screen and worksheet see Yahoo's four fiscal years, and the lane bars were set for that window (`COMPOUNDER_CAGR_YEARS = 3` in the screen is "a 3-year span"). So `evaluate` gets the **newest four** fiscal years known by `as_of` (`SCREEN_YEARS = 4`), which replays the screen's filters, averages, rank and hints as they would have run. The Portfolio Manager's compounder record bars are the worksheet's, which also sees four Yahoo years plus the EDGAR revenue history for the long CAGR: `pm_record = metrics.record(years[:4], revenue_history(store, as_of))`, and the row carries `pm_record_ok` = ROIC including goodwill ≥ `COMPOUNDER_ROIC_INCL_GOODWILL` in every counted year with at least `COMPOUNDER_MIN_ROIC_YEARS` counted, revenue CAGR ≥ `COMPOUNDER_REVENUE_CAGR` over at least `COMPOUNDER_MIN_CAGR_YEARS`, FCF CAGR ≥ `COMPOUNDER_FCF_CAGR` over at least `COMPOUNDER_MIN_FCF_YEARS` (the `_compounder_problems` tests, constants imported). The `compounder` rule requires both the screen's hint and `pm_record_ok`, which is the pipeline as it runs: the screen flags, the PM checks.

**Per-cohort steps after the snapshots** *(builder-check ruling)*. The screen excludes `universe.exclude_sectors` and re-runs every row's expected return against its sector's median EV/EBIT (`apply_sector_targets`) before ranking; `compounder_return`, a bar under test, depends on it. The backtest does the same per cohort: the universe drops the excluded sectors before any snapshot, and `finish_cohort(rows, inputs)` calls `screener.apply_sector_targets(rows, inputs)` then ranks. Sectors come from today's Wikipedia list (EDGAR has none) — a mild look-ahead, named in the report's *What was tested*.

**Rules** — `RULES: dict[str, Rule]`; a `Rule` has a `name`, a `bars: dict[str, float]` (its default bar values) and `picks(rows: list[dict], bars) -> list[dict]`. The default bars are **imported from the code they mirror**, never retyped:

| rule | picks a cohort row when |
|---|---|
| `value` | passes the filters; `trend != "FREEFALL"`; Magic Formula rank (`metrics.magic_formula_rank(rows, EY_KEY, ROIC_KEY)` over the cohort's passing rows) ≤ `top_n` (30 *(my call: the screen shows 10 of ~900; 30 gives a readable sample per year)*); `mos_formula ≥ mos` (`watch.buy_margin_of_safety`, 0.30) |
| `magic_formula` | passes the filters; `trend != "FREEFALL"`; rank ≤ `top_n` — the reference the value rule is measured against (what the margin-of-safety bar adds) |
| `compounder` | `"compounder" in lane_hints`; `pm_record_ok`; `price ≤ premium × value_base` (`COMPOUNDER_BUY_PREMIUM`); `compounder_return ≥ min_return` (`COMPOUNDER_BUY_RETURN`); `implied_growth ≤ max(sustainable_growth, earned_growth)` |
| `compounder_overweight` | as `compounder` with `COMPOUNDER_OVERWEIGHT_PREMIUM` and `COMPOUNDER_OVERWEIGHT_RETURN` |
| `compounder_record` | `"compounder" in lane_hints` and `pm_record_ok` — the reference for the price bars |
| `compounder_hint` | `"compounder" in lane_hints` alone — the reference for the PM's record bars |
| `inflection` | `"inflection" in lane_hints`; `incremental_roic ≥ INFLECTION_INCREMENTAL_ROIC`; `mos_formula ≥ 0` is **not** required (the lane values on the run rate) *(my call)* |
| `dislocation` | `"dislocation" in lane_hints` (fundamentals intact, price down 25%+ from the high or 20%+ in a month) |

**Cohorts.** `cohort_dates(cfg) -> list[str]`: one date per year from `backtest.first_year` to `backtest.last_year` on `backtest.cohort_day` (default `06-30` *(my call: most December fiscal years have filed the 10-K by then; the as-of filter makes any day valid, this one keeps the data fresh)*). A cohort is evaluated only when its one-year hold has ended by today; three-year results only where the three-year hold has ended.

**Returns.** For every pick: `total_return(hist, cohort_date, cohort_date + hold years)` per hold in `backtest.holds` (default `[1, 3]`). Benchmarks per cohort and hold: `SPY` (same function on its history) and the **universe** — the equal-weight mean of `total_return` over every cohort row with a price (passing or not). Excess = pick return − SPY return (points, not annualised; the hold is fixed).

**Aggregation.** `summarize(picks, benchmarks) -> dict`:
- per rule × hold × cohort: `n`, `basket` (equal-weight mean return), `median`, `spy`, `universe`, `excess_spy`, `excess_universe`, `hit_rate` (share of picks beating SPY), `ended_early` count;
- per rule × hold: `cohorts` with picks, `beat_spy` (count of cohorts where `basket > spy`), `mean_excess_spy`, `median_excess_spy`, `mean_excess_universe`, `all_picks_median`, `worst_cohort`, and the **sentence**: `"{rule}: beat SPY in {k} of {n} cohorts ({hold}-year hold, equal weight), median excess {±x.x} points; {m} picks in all."`, or `"{rule}: no picks in any cohort."`;
- **sensitivity**: for each rule and each of its bars, one bar moved at a time over a fixed grid — `premium` {1.00, 1.15, 1.25, 1.50}, `min_return` {0.08, 0.10, 0.12, 0.15}, `mos` {0.20, 0.30, 0.40}, `top_n` {10, 20, 30, 50}, `incremental_roic` {0.15, 0.20, 0.30} — reporting `n`, `beat_spy`, `mean_excess_spy` for the one-year hold *(my call: one-at-a-time, so the table stays readable and no bar is tuned to the sample)*;
- **the bar that bound**: for `compounder`, the rows that cleared the hint and the PM's record bars but failed the price bars (`compounder_record` minus `compounder`), with their own basket and excess — what the premium bar cost or saved.

**Survivorship note** *(revised during the build: Wikipedia's "Selected changes" table no longer exists on the S&P 500 page, checked live on 2026-10-24; the constituents table carries a `Date added` column for every member, the S&P 400 table carries none)*. `added_dates(fetch=None) -> dict[str, str]`: ticker → ISO date from the S&P 500 constituents table's `Date added` (dashes as Yahoo spells them; unparseable rows dropped; `{}` on any failure). `survivorship(paths, cohorts, fetch=None) -> dict[str, int | None]`: for each cohort date, the count of today's S&P 500 members added after it. `run` **excludes** those names from that cohort (the one half of the bias free data can remove: a name that was not in the index then is not in the cohort's universe), and the report prints *"today's members only: N of today's S&P 500 names joined after this date and are left out of this cohort; names that left either index since are not in the sample and cannot be counted from free data; the S&P 400's join dates are not published, so all its members stay"*. The removals half is stated, never corrected.

**Run.** `run(paths, cfg, today=None, tickers=None, refresh=False, progress=None) -> dict`: universe (`market_data.universe` less `exclude_sectors`; `tickers` narrows it for smoke runs), fetch or read every store and history (progress callback per ticker; `market_data.throttle(cfg["screen"]["throttle_seconds"])` between Yahoo calls), snapshots per cohort, picks, returns, summary, survivorship. Returns `{"generated", "cohorts", "holds", "universe_size", "with_data", "picks": [...], "summary", "sensitivity", "bound", "survivorship", "skipped": {ticker: reason}, "missing_fields": {cohort: {"revenue": n, "ebit": n, "free_cash_flow": n}}}`.

**Output.** `markdown(result) -> str` — the report, sections in this order: *What was tested* (one paragraph: rules, cohorts, holds, point-in-time statement, the survivorship sentence); *Verdict* (the per-rule sentences, one-year first, then three-year); *By cohort* (one table per rule and hold: cohort, n, basket, median, SPY, universe, excess, hit rate); *Sensitivity* (one table per rule); *The bar that bound* (compounder); *Coverage* (universe size, tickers with data, skipped reasons counted, and per cohort the rows whose newest year lacks revenue / EBIT / free cash flow — `result["missing_fields"]`). `write(paths, result, today) -> tuple[Path, Path]` writes `paths.reports / "backtest" / f"backtest_{today}.md"` and `backtest_{today}_picks.csv` (one row per pick and hold: cohort, ticker, rule, price, value_base, mos_formula, market_cap, hold, start_date, end_date, ret, spy, universe, excess_spy, ended_early).

### 4. Command, config, website, docs

- CLI `backtest [--from YEAR] [--to YEAR] [--holds 1,3] [--tickers A,B] [--refresh] [--no-fetch]` (`--no-fetch` reads caches only); prints the Verdict section and the two paths. The run is long on the first pass (≈ 900 EDGAR + 900 Yahoo requests; later runs read the caches); the command prints a progress line every 50 tickers.
- `config.DEFAULTS["backtest"] = {"first_year": 2015, "last_year": 2024, "cohort_day": "06-30", "holds": [1, 3], "value_top_n": 30, "benchmark": "SPY"}`; documented in `config.yaml`.
- Website: `/backtest` renders the newest `reports/backtest/backtest_*.md` through the `markdown` filter (template `backtest.html`; "No backtest yet — run `python -m value_pipeline backtest`." when none); nav link after Calibration. Read-only; never calls the network.
- README: a `## Backtest` section after Calibration — what it tests, what it cannot (survivorship, today's sectors, agent-only bars, nominal 2 bn market-cap floor across years, Q4 derived, four-year window), how to run, how to read the sentence and the sensitivity table.

## Testing (no network; `@pytest.mark.unit`)

- `pit_statements`: `extract` keeps only mapped tags and the fact shape; `years_as_of` drops a fact filed after the date and takes an amendment from its filing date; alternative tags (a filer with `RevenueFromContractWithCustomerExcludingAssessedTax` only) fill `revenue`; derived `total_debt`, `free_cash_flow`, `total_liabilities`; signs negative for capex/dividends/buybacks/acquisitions; `quarters_as_of` derives Q4 as annual − nine months and flags it, and omits it when the nine-month figure is missing; `shares_as_of` precedence; `facts_for` writes the compact file and returns None for a non-filer and on `VendorUnavailableError`.
- prices: `close_on_or_after` over a weekend; `closes_before` length and order; `split_factor_after` with one split after and one before the date; `total_return` normal, `ended_early`, missing start; market cap uses the split factor.
- snapshot: a synthetic store with a fact filed one day after the cohort date — the row equals the row built without that fact (the look-ahead invariant); `DataUnavailable` → None.
- rules: each rule's pick and non-pick at the bar (boundary equal passes); `value` versus `magic_formula` differ only by the MOS bar; `compounder_hint` ⊇ `compounder`.
- summarize: two cohorts × two rules by hand (basket, excess, beat_spy, hit_rate, the sentence text, "no picks" sentence); sensitivity grid shape; bound set = hint minus rule.
- markdown and write: sections present in order, CSV columns; CLI `backtest --no-fetch --tickers X` with patched caches; `/backtest` page empty and populated.

## Rollout

Worktree → subagent-driven implementation → final whole-branch review → merge, suite, website restart, push. Then the real run: `backtest --tickers DECK,ADBE,HRB,CAT,SPY` as a smoke test, then the full run in the background (first pass ≈ 20–40 minutes), and the report read with the user. Memory notes the sentence the report produced for each lane.
