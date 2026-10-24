# Lane-Rule Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `backtest` command that applies the pipeline's mechanical lane rules to point-in-time EDGAR statements and Yahoo price history over yearly cohorts, and writes a report that says, per lane, in how many years the rule beat SPY and how that changes when each bar moves.

**Architecture:** `value_pipeline/pit_statements.py` turns a compact, permanent per-ticker EDGAR fact store into the `years`/`quarters` dicts the existing metrics consume, as of a date (reusing `sec_edgar._as_of`). `value_pipeline/backtest.py` holds the price history cache and its pure helpers, the cohort snapshot (built through `screener.evaluate`, `failed_filters`, `lane_hints`), the rules with bars imported from `decision_math` and config, the returns, the summary, sensitivity and survivorship note, the Markdown/CSV writers and `run`. A CLI command, a config section, a read-only `/backtest` page and a README section complete it.

**Tech Stack:** Python 3.11+, requests (EDGAR via `sec_edgar._fetch_json`), yfinance 1.7, pandas 3, FastAPI/Jinja, pytest (`@pytest.mark.unit`, no network), ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-10-24-lane-backtest-design.md`

## Global Constraints

- Nothing trades; no agent is called. The backtest reports and changes no bar.
- Look-ahead: a row for cohort date D uses only facts with `filed <= D` and closes dated `<= D`; returns start at the first close `>= D`.
- Signs follow the Yahoo vendor: `capex`, `dividends_paid`, `buybacks`, `acquisitions` stored negative.
- Rule bars are imported from `tradingagents.agents.decision_math` (`COMPOUNDER_BUY_PREMIUM`, `COMPOUNDER_OVERWEIGHT_PREMIUM`, `COMPOUNDER_BUY_RETURN`, `COMPOUNDER_OVERWEIGHT_RETURN`, `INFLECTION_INCREMENTAL_ROIC`) and `cfg["watch"]["buy_margin_of_safety"]`; never retyped as literals.
- The compact store keeps the companyfacts shape `{tag: {"units": {unit: [ {start?, end, val, filed, form} ]}}}` so `sec_edgar._as_of` reads it unchanged.
- Caches under `paths.cache / "backtest" / {"facts", "prices"}`; fail-open (None + log) on any network failure; `fetch=False` never touches the network.
- No network in tests: synthetic stores and frames; `sec_edgar._fetch_json`, `yf.Ticker` and `market_data.universe` patched.
- ruff clean; full suite green after each task; conventional commits ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_015r4hMD2DRWvm5AXmWo8KRV`.

## Review Focus

1. A fact filed the day after the cohort date must not change the snapshot (look-ahead). Test in Task 2 (`test_snapshot_ignores_facts_filed_after_the_date`).
2. A filer whose revenue is tagged only `RevenueFromContractWithCustomerExcludingAssessedTax` and whose equity is only the "including noncontrolling interest" tag must still produce `revenue` and `equity`. Test in Task 1 (`test_alternative_tags_fill_the_field`).
3. A fiscal year with the annual revenue known but no nine-month figure yet: no fourth quarter is invented (`quarters_as_of` leaves it out). Test in Task 1 (`test_q4_omitted_without_nine_month_figure`).
4. A stock delisted during the hold: `total_return` ends at the last close and flags `ended_early`; the basket still counts it. Test in Task 2 (`test_total_return_ended_early`) and Task 3 (summary counts it).
5. A split after the cohort date: market cap uses EDGAR shares × the split factor, so the 2 bn floor and the formula value per share are on the same basis as Yahoo's split-adjusted close. Test in Task 2 (`test_market_cap_uses_split_factor`).

---

### Task 1: Point-in-time statements from EDGAR

**Files:**
- Create: `value_pipeline/pit_statements.py`, `tests/test_pit_statements.py`

**Interfaces:**
- Consumes: `sec_edgar._as_of`, `sec_edgar._SPANS`, `sec_edgar._ANNUAL_FORMS`, `sec_edgar._YEAR_TO_DATE`, `sec_edgar.cik_for`, `sec_edgar._fetch_json`, `sec_edgar._FACTS_URL`, `VendorUnavailableError`; `value_metrics.YEAR_FIELDS`.
- Produces:
  - `TAGS: dict[str, tuple[str, ...]]` (spec §1 table, keys ⊆ `YEAR_FIELDS` minus the derived `total_debt`, `free_cash_flow`), `DEBT_PARTS = ("LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings", "CommercialPaper")`, `SHARES_DEI = "dei:EntityCommonStockSharesOutstanding"`, `INTANGIBLES = "IntangibleAssetsNetExcludingGoodwill"`.
  - `extract(facts: dict) -> dict`: `{tag: {"units": {unit: [fact...]}}}` for every tag in `TAGS` values, `DEBT_PARTS`, `INTANGIBLES` (from `facts["facts"]["us-gaap"]`) and `SHARES_DEI` (from `facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]`), each fact reduced to `start?`, `end`, `val`, `filed`, `form`; units limited to `USD` and `shares`.
  - `facts_for(ticker: str, paths: Paths, fetch: bool = True, refresh: bool = False) -> dict | None`: reads/writes `paths.cache / "backtest" / "facts" / f"{ticker}.json"` = `{"cik", "fetched_at", "facts": extract(...)}`; None for a non-filer (`cik_for` None), a `VendorUnavailableError`, or `fetch=False` with no file. Module-level `MIN_REQUEST_GAP = 0.125` seconds enforced between fetches.
  - `years_as_of(store: dict, as_of: str, split_factor: Callable[[str], float] | None = None) -> list[dict]`: newest first; each dict has `fiscal_year_end` (the period end) and every `YEAR_FIELDS` key; fiscal years are the ends of the annual-span `revenue` facts (fallback `ebit`); a calendar year reported twice keeps the later period end; derived `total_debt`, `free_cash_flow`, `total_liabilities`, goodwill + intangibles per spec §1; signs flipped for the four payment fields; `shares` taken by `_first_filed(facts, tags, as_of, spans, forms)` (same signature and return as `_as_of`, but the **earliest** filing per period wins) and multiplied by `split_factor(fiscal_year_end)` when given; years with fewer than 6 non-None values dropped (the Yahoo vendor's rule).
  - `quarters_as_of(store, as_of) -> list[dict]`: newest first, at most 8, keys `fiscal_year_end, revenue, ebit, current_assets, current_liabilities, cash, net_ppe, goodwill, total_assets, derived`; discrete quarters from `_SPANS["quarterly"]`, any form; Q4 derived as annual − nine-month (`_YEAR_TO_DATE[1]` span, end 80–100 days before the year end) when both exist, else omitted.
  - `shares_as_of(store, as_of) -> float | None`: latest-ended `SHARES_DEI` fact filed on or before `as_of`; else latest annual `CommonStockSharesOutstanding`; else `WeightedAverageNumberOfDilutedSharesOutstanding`.
  - `revenue_history(store, as_of) -> dict[str, float]`: `{"2019": value, ...}` from the annual `revenue` facts (as `sec_filings.annual_revenue`).

- [ ] **Step 1: Failing tests** in `tests/test_pit_statements.py` with a `_store()` helper building a synthetic companyfacts dict (three fiscal years ending 12-31 for 2021–2023, 10-K filed the following 02-20; quarters; a 10-K/A for 2022 filed 2023-06-01 with a changed revenue; `dei` shares): `test_extract_keeps_only_mapped_tags_and_fact_shape`, `test_years_as_of_respects_filing_dates` (as of 2023-03-01 sees 2022 with the original value; as of 2023-07-01 sees the amended value; as of 2022-01-01 sees no 2021 year), `test_alternative_tags_fill_the_field`, `test_derived_fields_and_signs` (`total_debt`, `free_cash_flow = ocf − |capex|`, `total_liabilities = assets − equity`, capex/dividends/buybacks/acquisitions negative), `test_quarters_derive_q4_and_flag_it`, `test_q4_omitted_without_nine_month_figure`, `test_shares_as_of_precedence`, `test_year_shares_take_the_first_filing_and_the_split_factor` (a 2022 count restated in the 2023 10-K is ignored; `split_factor=lambda end: 2.0 if end < "2023-01-01" else 1.0` doubles the older years only), `test_revenue_history_by_fiscal_year`, `test_facts_for_caches_and_fails_open` (patched `cik_for` and `_fetch_json`: file written; second call no fetch; `VendorUnavailableError` → None; non-filer → None; `fetch=False` without a file → None).
- [ ] **Step 2: Run to verify they fail.** `pytest tests/test_pit_statements.py -q`
- [ ] **Step 3: Implement** `value_pipeline/pit_statements.py` per the Interfaces block.
- [ ] **Step 4: Suite and ruff.** `pytest -q -m unit` and `ruff check value_pipeline tests`.
- [ ] **Step 5: Commit** — `feat(backtest): point-in-time statements from EDGAR companyfacts`

### Task 2: Price history, helpers and the cohort snapshot

**Files:**
- Create: `value_pipeline/backtest.py`, `tests/test_lane_backtest.py`

**Interfaces:**
- Consumes: Task 1's `years_as_of`, `quarters_as_of`, `shares_as_of`, `revenue_history`; `screener.evaluate`, `screener.failed_filters`, `screener.lane_hints`; `metrics.inflection`, `metrics.record`; `value_metrics.MIN_DISCOUNT_RATE`, `EQUITY_PREMIUM`; `decision_math.COMPOUNDER_ROIC_INCL_GOODWILL`, `COMPOUNDER_MIN_ROIC_YEARS`, `COMPOUNDER_REVENUE_CAGR`, `COMPOUNDER_MIN_CAGR_YEARS`, `COMPOUNDER_FCF_CAGR`, `COMPOUNDER_MIN_FCF_YEARS`; `market_data.DataUnavailable`.
- Produces:
  - `price_history(ticker: str, paths: Paths, fetch: bool = True, refresh: bool = False) -> pd.DataFrame | None`: columns `date` (datetime64, ascending), `close`, `adj_close`, `split`; CSV at `paths.cache / "backtest" / "prices" / f"{ticker}.csv"` (`^TNX` saved as `TNX.csv`); from `yf.Ticker(ticker).history(period="max", auto_adjust=False)` (`Close`, `Adj Close`, `Stock Splits`); None on empty/failure.
  - `close_on_or_after(hist, day: str, within: int = 10) -> tuple[date, float] | None` (uses `close`); `adj_close_on_or_after(hist, day, within=10)` likewise on `adj_close`; `closes_before(hist, day, n: int = 420) -> list[float]`; `split_factor_after(hist, day) -> float`; `total_return(hist, start: str, end: str) -> dict | None` = `{"start_date", "end_date", "ret", "ended_early"}` on `adj_close`.
  - `discount_rate_on(tnx: pd.DataFrame | None, day: str) -> float` = `max(MIN_DISCOUNT_RATE, close/100 + EQUITY_PREMIUM)` at the last row `<= day`; `MIN_DISCOUNT_RATE` when `tnx` is None or has no row.
  - `SCREEN_YEARS = 4`.
  - `pm_record_ok(rec: dict) -> bool`: `rec` from `metrics.record`; true when `roic_years_counted >= COMPOUNDER_MIN_ROIC_YEARS` and `roic_gw_years_above_15 == roic_years_counted` and `roic_gw_min >= COMPOUNDER_ROIC_INCL_GOODWILL − 1e-9`, `revenue_cagr >= COMPOUNDER_REVENUE_CAGR` with `revenue_cagr_years >= COMPOUNDER_MIN_CAGR_YEARS`, `fcf_cagr >= COMPOUNDER_FCF_CAGR − 1e-9` with `fcf_cagr_years >= COMPOUNDER_MIN_FCF_YEARS` (constants imported from `decision_math`).
  - `snapshot(ticker: str, as_of: str, store: dict, hist: pd.DataFrame, rate: float, cfg: dict, sector: str | None = None) -> tuple[dict, tuple] | None`: `years = years_as_of(store, as_of, split_factor=lambda end: split_factor_after(hist, end))[:SCREEN_YEARS]`; `data = {"ticker", "price" (`closes_before(hist, as_of)[-1]`), "shares" (`shares_as_of × split_factor_after(hist, as_of)`), "market_cap" (price × shares), "years", "quarters", "closes"}`; row = `screener.evaluate(data, cfg["screen"]["normalize_years"], cfg["screen"]["roic_cap"], rate)` plus `sector`, `fail_reasons`, `passes_filters`, `lane_hints`, `lane_hint`, `incremental_roic` (`metrics.inflection(years, quarters)["incremental_roic"]`), `pm_record_ok` (`pm_record_ok(metrics.record(years, revenue_history(store, as_of)))`), `as_of`; returns `(row, (years, price, market_cap, shares))`; None when years < 2, no price, or `DataUnavailable`.

- [ ] **Step 1: Failing tests**: `_hist()` helper (a DataFrame of ~500 business days with a 2:1 split row), `test_price_history_caches_csv_and_fails_open` (patched `yf.Ticker`), `test_close_on_or_after_skips_a_weekend`, `test_closes_before_is_oldest_first_and_capped`, `test_split_factor_after`, `test_total_return_normal`, `test_total_return_ended_early`, `test_total_return_missing_start`, `test_discount_rate_on_floor_and_tnx`, `test_market_cap_uses_split_factor`, `test_snapshot_feeds_four_years_and_the_pm_record` (a store with six fiscal years: `evaluate` sees four — `roic_years` ≤ 4 — and `pm_record_ok` is set from `record(years[:4], revenue_history)`), `test_pm_record_ok_at_each_bar`, `test_snapshot_ignores_facts_filed_after_the_date` (same store ± one fact filed as_of + 1 day → identical rows), `test_snapshot_none_without_two_years`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Suite and ruff.**
- [ ] **Step 5: Commit** — `feat(backtest): price history cache, return helpers and the cohort snapshot`

### Task 3: Rules, cohorts, returns, summary, sensitivity, survivorship

**Files:**
- Modify: `value_pipeline/backtest.py`, `tests/test_lane_backtest.py`; `value_pipeline/config.py` (`DEFAULTS["backtest"]`), `value_pipeline/config.yaml` (commented section)

**Interfaces:**
- Consumes: Task 2's helpers and `snapshot`; `decision_math` bars; `metrics.magic_formula_rank`, `screener.EY_KEY`, `ROIC_KEY`.
- Produces:
  - `DEFAULTS["backtest"] = {"first_year": 2015, "last_year": 2024, "cohort_day": "06-30", "holds": [1, 3], "value_top_n": 30, "benchmark": "SPY"}`.
  - `cohort_dates(cfg: dict, today: date) -> list[str]`: `f"{year}-{cohort_day}"` for each year, keeping only dates whose one-year hold ended by `today`.
  - `finish_cohort(rows: list[dict], inputs: dict[str, tuple]) -> None`: `screener.apply_sector_targets(rows, inputs)` then sets `mf_position` (1-based, among passing non-FREEFALL rows ordered by `metrics.magic_formula_rank(rows, EY_KEY, ROIC_KEY)`), None elsewhere.
  - `@dataclass Rule(name: str, bars: dict[str, float], picks: Callable[[list[dict], dict], list[dict]])`; `rules(cfg) -> dict[str, Rule]` with the eight rules of spec §3 (`value`, `magic_formula`, `compounder`, `compounder_overweight`, `compounder_record`, `compounder_hint`, `inflection`, `dislocation`), bars `{"top_n", "mos"}`, `{"top_n"}`, `{"premium", "min_return"}`, `{"premium", "min_return"}`, `{}`, `{}`, `{"incremental_roic"}`, `{}`; comparisons `>=`/`<=` inclusive with a 1e-9 tolerance.
  - `attach_returns(rows, hist_by_ticker, cohort: str, holds: list[int]) -> None`: for each row sets `ret_{h}`, `end_date_{h}`, `ended_early_{h}`, `start_date`.
  - `benchmarks(spy_hist, rows_by_cohort, holds) -> dict[tuple[str, int], dict]` = `{(cohort, hold): {"spy": float | None, "universe": float | None}}` (universe = mean of `ret_{h}` over rows with one).
  - `picks_for(rule: Rule, rows_by_cohort, holds, bench, bars=None) -> list[dict]`: one pick dict per pick and hold: `cohort, ticker, rule, price, value_base, mos_formula, market_cap, hold, start_date, end_date, ret, spy, universe, excess_spy, ended_early` (picks without `ret` are kept with `ret=None` and excluded from averages).
  - `summarize(picks, bench, holds) -> dict`: `{rule: {hold: {"cohorts": [{cohort, n, basket, median, spy, universe, excess_spy, excess_universe, hit_rate, ended_early}], "beat_spy", "cohorts_with_picks", "mean_excess_spy", "median_excess_spy", "mean_excess_universe", "all_picks_median", "worst_cohort", "picks", "sentence"}}}` with the exact sentence texts of spec §3.
  - `SENSITIVITY_GRID = {"premium": (1.0, 1.15, 1.25, 1.5), "min_return": (0.08, 0.10, 0.12, 0.15), "mos": (0.20, 0.30, 0.40), "top_n": (10, 20, 30, 50), "incremental_roic": (0.15, 0.20, 0.30)}`; `sensitivity(rules, rows_by_cohort, bench, hold=1) -> list[dict]` rows `{rule, bar, value, n, beat_spy, cohorts, mean_excess_spy}`.
  - `bound_by_price(rules, rows_by_cohort, bench, holds) -> dict` = `summarize` of the `compounder_record` picks minus `compounder` picks, under the key `"compounder_price_bar"`.
  - `survivorship(paths, cohorts: list[str], fetch=lambda url: requests.get(...).text) -> dict[str, int | None]`: removals after each cohort date from the S&P 500 page's "Selected changes" table (the table with a `Date` column and a `Removed` column group); `{}` on any failure.

- [ ] **Step 1: Failing tests**: `test_cohort_dates_stop_at_completed_holds`, `test_finish_cohort_applies_sector_targets_and_ranks` (patched `apply_sector_targets` call recorded; FREEFALL and failing rows get no position), `test_rules_pick_at_the_bar` (one row per rule at, above and below each bar; `value` ⊆ `magic_formula`; `compounder` ⊆ `compounder_record` ⊆ `compounder_hint`), `test_attach_returns_and_benchmarks` (two tickers, SPY, universe mean), `test_summarize_by_hand` (two cohorts × two rules: basket, excess, beat_spy, hit_rate, ended_early count, sentence text and the "no picks" sentence), `test_sensitivity_grid_rows`, `test_bound_by_price_is_hint_minus_rule`, `test_survivorship_counts_removals_after_each_cohort` (synthetic HTML; failure → `{}`), `test_backtest_defaults_in_config`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Suite and ruff.**
- [ ] **Step 5: Commit** — `feat(backtest): lane rules, cohorts, returns, summary and sensitivity`

### Task 4: `run`, report, command, page, docs

**Files:**
- Modify: `value_pipeline/backtest.py`, `value_pipeline/cli.py`, `value_pipeline/web/app.py`, `value_pipeline/web/templates/base.html` (nav link after Calibration), `value_pipeline/README.md` (`## Backtest` after `## Calibration`), `tests/test_lane_backtest.py`, `tests/test_value_web.py`
- Create: `value_pipeline/web/templates/backtest.html`

**Interfaces:**
- Produces:
  - `run(paths, cfg, today: date | None = None, tickers: list[str] | None = None, refresh=False, fetch=True, progress=None, universe_fn=market_data.universe) -> dict` per spec §3 (`generated, cohorts, holds, universe_size, with_data, picks, summary, sensitivity, bound, survivorship, skipped`); the universe less `cfg["universe"]["exclude_sectors"]`, each row's `sector` from the universe frame; `added = added_dates()` once per run (Task 3) and a ticker whose date added is after the cohort date is skipped for that cohort; per cohort: snapshots → `finish_cohort` → `attach_returns`; `summarize(..., names=list(rules))` so zero-pick rules get their sentence; the survivorship sentence in `markdown` reads: "today's members only: N of today's S&P 500 names joined after this date and are left out of this cohort; names that left either index since are not in the sample and cannot be counted from free data; the S&P 400's join dates are not published, so all its members stay"; `progress(i, n, ticker)` called per ticker; `market_data.throttle(cfg["screen"]["throttle_seconds"])` after each Yahoo fetch.
  - `markdown(result) -> str` sections in order: `# Lane-rule backtest`, `## What was tested`, `## Verdict`, `## By cohort`, `## Sensitivity`, `## The bar that bound`, `## Coverage`.
  - `write(paths, result, today: str) -> tuple[Path, Path]` → `paths.reports / "backtest" / f"backtest_{today}.md"` and `backtest_{today}_picks.csv`; `latest_report(paths) -> tuple[str, str] | None` (date, text).
  - CLI `backtest [--from YEAR] [--to YEAR] [--holds 1,3] [--tickers A,B] [--refresh] [--no-fetch]` (`cmd_backtest`): prints the Verdict section and both paths; progress every 50 tickers.
  - `/backtest` page: `latest_report` rendered through the `markdown` filter; empty state text per spec §4.

- [ ] **Step 1: Failing tests**: `test_run_end_to_end_with_patched_caches` (two tickers + SPY with pre-written cache files and `fetch=False`; `universe_fn` and `added_dates` patched — one ticker added after the first cohort is absent from that cohort's rows and present in the next; result keys; picks CSV columns), `test_markdown_sections_in_order`, `test_write_and_latest_report`, `test_cli_backtest_no_fetch` (in `tests/test_lane_backtest.py`); `test_backtest_page_empty_and_populated` (in `tests/test_value_web.py`).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**, README section included.
- [ ] **Step 4: Suite and ruff.**
- [ ] **Step 5: Commit** — `feat(backtest): run, report, command and page`

### Task 5: Verification (controller's steps after the merge)

- [ ] `python -m value_pipeline backtest --tickers DECK,ADBE,HRB,CAT` as a smoke test; read the report.
- [ ] Full run in the background; read the Verdict and sensitivity with the user; note the sentences in memory.
- [ ] Finish with superpowers:finishing-a-development-branch.
