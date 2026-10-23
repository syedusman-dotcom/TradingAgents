# Methodology Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anchor every review to a valuation worksheet computed in code, put accounting and capital-allocation judgments into numbers, give the agents the facts they guess at today (earnings date, peers, news for foreign listings), add one pre-mortem call, make the Portfolio Manager's decision a parsed checklist with scenarios, a value bridge and an entry price, rank candidates by expected return, and add a cross-stock investment committee.

**Architecture:** New metrics live in `tradingagents/dataflows/value_metrics.py` (pure functions over `YEAR_FIELDS` dicts) and are rendered into the fundamentals analyst's verified report; new facts arrive through existing tools and the instrument context; one new LangGraph node sits between the risk debate and the Portfolio Manager; the decision schema (`PortfolioDecision`) grows, with `value_pipeline/parse.py`, `actions.py`, `render.py`, `watch.py` and the website reading the new fields; the committee is one structured deep-tier call in `value_pipeline/committee.py` after the per-ticker loop.

**Tech Stack:** Python 3.11+, pydantic v2, LangChain/LangGraph, yfinance 1.7, pandas, pytest (`@pytest.mark.unit`), ruff (line length 100). Tests must not touch the network: patch `yfinance` objects.

**Spec:** `docs/superpowers/specs/2026-10-23-methodology-upgrade-design.md`

## Global Constraints

- Nothing trades. No new code places or simulates orders.
- Every new metric tolerates `None` inputs and returns `None` (or an empty list) instead of raising; `num()`/`div()` from `value_metrics` are the helpers.
- Fields added to `PortfolioDecision` have defaults, so the free-text fallback and old tests keep working; the prompt asks for them and `decision_checks` enforces them.
- Rendered report section headers already parsed downstream stay byte-identical: `**Rating**:`, `**Executive Summary**:`, `**Investment Thesis**:`, `**Price Target**:`, `**Time Horizon**:`.
- The user's hard filters and rating thresholds do not change except where the spec says (Buy: weighted MoS ≥ 30% and upside/downside ≥ 3; Overweight: ≥ 15% and ≥ 2). New screen filters are optional config keys, absent from the shipped `config.yaml`.
- `ruff check .` clean; the full suite (`.venv/bin/python -m pytest -q`) passes after every task.
- Commit after every task with a conventional message and the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- The deep/quick tiers are unchanged: pre-mortem is quick tier; the committee is deep tier.

## Review Focus

1. A company with no buybacks, no dividends and negative owner earnings (a loss-making grower): `expected_return`, `capital_allocation` and `beneish_m` must return `None`/empty, never divide by zero, and the worksheet must still render. Test in Task 4 (`test_expected_return_is_none_without_positive_owner_earnings`) and Task 2 (`test_capital_allocation_with_no_returns_reports_zero_returned`).
2. A London quote in pence with statements in USD (Diageo): the peer table converts the quote currency correctly and never mixes pence with pounds. Test in Task 6 (`test_peer_row_divides_pence_quotes_by_100`).
3. The Portfolio Manager returns a bridge that does not sum to the target, or probabilities that do not sum to 1: one corrective retry, then the decision is kept with the problems listed in the rendered text, never a crash. Test in Task 8 (`test_pm_retries_once_on_failed_checks_then_keeps_the_decision`).
4. A holding rated Sell by its own review but ranked "BUY" by the committee: the Sell wins and ACTIONS says the committee disagreed. Test in Task 10 (`test_committee_never_overrides_a_sell`).
5. An old `portfolio.json` without `kill_criteria`/`entry_price` and old `results.json` without the new keys: the website, watch and review load them with empty defaults. Tests in Task 9 (`test_old_portfolio_file_loads_with_empty_kill_criteria`, `test_watch_handles_results_without_entry_price`).

---

### Task 1: Statement fields and accounting checks

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (YEAR_FIELDS L18-24; new functions after `interest_coverage` L167; `company_metrics` L289-334; `render_report` L364+)
- Modify: `tradingagents/dataflows/vendors/yahoo/value_metrics.py:26-50` (`ROWS`)
- Modify: `value_pipeline/screener.py` (`evaluate` L24-71, `failed_filters` L74-99), `value_pipeline/config.yaml` (commented optional filters), `value_pipeline/web/views.py:87-89` (`_SCREEN_COLUMNS`), `value_pipeline/web/templates/screen.html` (one column)
- Test: `tests/test_value_metrics.py`, `tests/test_value_pipeline.py`, `tests/test_value_web.py`

**Interfaces:**
- Produces: `YEAR_FIELDS` gains `"receivables", "depreciation", "sga", "acquisitions"`.
- Produces: `accruals_ratio(cur: dict, prev: dict | None) -> float | None`; `cash_conversion(years: list[dict], n: int = 3) -> float | None`; `receivables_vs_revenue(cur: dict, prev: dict | None) -> float | None`; `beneish_m(cur: dict, prev: dict | None) -> float | None`; `accounting_checks(years: list[dict]) -> dict` with keys `accruals_ratio, cash_conversion, receivables_vs_revenue, beneish_m, flags: list[str]`.
- Produces: `company_metrics(...)["accounting"]` = `accounting_checks(years)`; a `## Accounting checks` section in `render_report`.
- Produces: screen columns `accruals_ratio`, `beneish_m`, `accounting_flags` (flags joined with "; "); optional filters `max_accruals_ratio`, `max_beneish_m` in `failed_filters` (reason strings "accruals" and "M-score").

- [ ] **Step 1: Write the failing tests** in `tests/test_value_metrics.py` (extend `_year` with `receivables=120, depreciation=40, sga=150, acquisitions=0`):

```python
def test_accruals_ratio_is_net_income_less_cfo_over_average_assets():
    assert vm.accruals_ratio(_year(net_income=150, operating_cash_flow=220, total_assets=2000),
                             _year(total_assets=1800)) == pytest.approx((150 - 220) / 1900)

def test_beneish_m_for_identical_years_is_the_intercept_plus_tata():
    y = _year()
    m = vm.beneish_m(y, y)       # every index is 1.0; TATA = (150-220)/2000
    assert m == pytest.approx(-4.84 + 0.920 + 0.528 + 0.404 + 0.892 + 0.115 - 0.172 - 0.327 + 4.679 * (-70 / 2000))

def test_beneish_m_is_none_when_receivables_are_missing():
    assert vm.beneish_m(_year(receivables=None), _year()) is None

def test_accounting_flags_name_each_breach():
    bad = [_year(net_income=400, operating_cash_flow=100, receivables=300), _year(receivables=100, revenue=1000)]
    flags = vm.accounting_checks(bad)["flags"]
    assert any("accruals" in f for f in flags) and any("receivables" in f for f in flags)
    assert vm.accounting_checks([_year(), _year()])["flags"] == []

def test_report_has_an_accounting_section():
    m = vm.company_metrics([_year(), _year()], price=20, market_cap=2000, shares=100)
    assert "## Accounting checks" in vm.render_report("X", "2026-10-23", m)
```

In `tests/test_value_pipeline.py`:

```python
def test_screen_rows_carry_accounting_columns_and_optional_filters():
    row = screener.evaluate(_data())            # the existing helper that builds a fetched record
    assert {"accruals_ratio", "beneish_m", "accounting_flags"} <= set(row)
    filters = {**DEFAULT_FILTERS, "max_accruals_ratio": -1.0}
    assert "accruals" in screener.failed_filters({**row, "accruals_ratio": 0.0}, filters)
    assert "accruals" not in screener.failed_filters({**row, "accruals_ratio": 0.0}, DEFAULT_FILTERS)
```

In `tests/test_value_web.py`: extend `test_screen_page_shows_the_latest_screen` with an `accounting_flags` column value `"accruals 12%"` and assert it appears.

Yahoo mapping test in `tests/test_value_metrics.py` (follow the existing `statement_years` tests that build frames): a frame with rows "Accounts Receivable", "Reconciled Depreciation", "Selling General And Administration", "Purchase Of Business" maps to the four fields.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_value_metrics.py tests/test_value_pipeline.py -k "accruals or beneish or accounting" -q`
Expected: FAIL (AttributeError / KeyError).

- [ ] **Step 3: Implement**

- `YEAR_FIELDS` += the four names; `ROWS` gets `"receivables": ("Accounts Receivable", "Receivables")`, `"depreciation": ("Reconciled Depreciation", "Depreciation And Amortization", "Depreciation Amortization Depletion")`, `"sga": ("Selling General And Administration",)`, `"acquisitions": ("Purchase Of Business",)`. Bump `CACHE_SCHEMA` in `value_pipeline/market_data.py` to 3 so cached screen records refetch.
- Beneish: M = −4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI with DSRI = (rec_t/rev_t)/(rec_p/rev_p), GMI = gm_p/gm_t, AQI = (1 − (ca_t+ppe_t)/ta_t)/(1 − (ca_p+ppe_p)/ta_p), SGI = rev_t/rev_p, DEPI = (dep_p/(dep_p+ppe_p))/(dep_t/(dep_t+ppe_t)), SGAI = (sga_t/rev_t)/(sga_p/rev_p), LVGI = ((ltd_t+cl_t)/ta_t)/((ltd_p+cl_p)/ta_p), TATA = (ni_t − cfo_t)/ta_t. Use absolute values for depreciation and SGA (Yahoo signs vary).
- Flags (exact wording, numbers formatted): `accruals ratio {x:.0%} above 10%`, `cash conversion {x:.2f} below 0.8 over 3 years`, `receivables grew {x:+.0%} faster than revenue`, `Beneish M-score {m:.2f} above -1.78`, `capex below half of depreciation for 2 years`.
- `render_report`: after the Altman line, `## Accounting checks` with one line of numbers and `Flags: …`/`Flags: none`.
- `screener.evaluate`: compute `accounting_checks(years)` and set the three columns; `failed_filters`: `if filters.get("max_accruals_ratio") is not None and row.get("accruals_ratio") is not None and row["accruals_ratio"] > cap: reasons.append("accruals")`, same for `max_beneish_m` → "M-score". Add both keys, commented, under `filters:` in `config.yaml`.

- [ ] **Step 4: Run the tests and the suite** — `.venv/bin/python -m pytest -q` passes; `.venv/bin/ruff check .` clean.
- [ ] **Step 5: Commit** — `feat(metrics): accounting checks in code (accruals, cash conversion, receivables, Beneish M) with screen columns`

### Task 2: Capital allocation history

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (`company_metrics` signature, `render_report`), `tradingagents/dataflows/vendors/yahoo/value_metrics.py:81-135` (`get_value_metrics`), `tradingagents/agents/analysts/fundamentals_analyst.py:34-64` (GOOD MANAGEMENT wording)
- Test: `tests/test_value_metrics.py`, `tests/test_structured_agent_prompts.py` (or wherever the fundamentals prompt is asserted; grep `GOOD MANAGEMENT`)

**Interfaces:**
- Produces: `capital_allocation(years: list[dict], avg_prices: dict[str, float] | None = None, price: float | None = None, n: int = NORMALIZE_YEARS) -> dict` with `per_year: [{fiscal_year_end, free_cash_flow, dividends, buybacks, acquisitions, net_debt_change, shares_change, shares_retired, buyback_price, avg_price}]` (newest first, up to n years; `buyback_price = buybacks / shares_retired` when both are positive) and `totals: {generated, dividends, buybacks, returned, acquisitions, net_debt_change, unexplained, avg_buyback_price}` where `unexplained = generated − returned − acquisitions + net_debt_change`.
- Produces: `yearly_average_closes(history: pd.DataFrame) -> dict[str, float]` in the Yahoo vendor (keys "YYYY"); `company_metrics(..., avg_prices: dict | None = None)` → `m["capital_allocation"]`.
- Produces: `## Capital allocation` section in `render_report`.

- [ ] **Step 1: Write the failing tests**

```python
def test_capital_allocation_totals_and_buyback_price():
    y1 = _year(free_cash_flow=180, dividends_paid=-50, buybacks=-30, shares=100, total_debt=500, cash=100, acquisitions=-20)
    y0 = _year(shares=102, total_debt=520, cash=90)
    ca = vm.capital_allocation([y1, y0], avg_prices={"2025": 16.0}, price=20.0)
    row = ca["per_year"][0]
    assert row["shares_retired"] == 2 and row["buyback_price"] == pytest.approx(15.0)
    assert row["net_debt_change"] == pytest.approx((500 - 100) - (520 - 90))
    t = ca["totals"]
    assert t["returned"] == 80 and t["acquisitions"] == 20
    assert t["unexplained"] == pytest.approx(180 - 80 - 20 + t["net_debt_change"])

def test_capital_allocation_with_no_returns_reports_zero_returned():
    ca = vm.capital_allocation([_year(dividends_paid=None, buybacks=None, free_cash_flow=-10), _year()])
    assert ca["totals"]["returned"] == 0 and ca["per_year"][0]["buyback_price"] is None

def test_yearly_average_closes_groups_by_calendar_year():
    hist = pd.DataFrame({"Close": [10, 20, 30]}, index=pd.to_datetime(["2024-03-01", "2024-09-01", "2025-02-01"]))
    assert yahoo_vm.yearly_average_closes(hist) == {"2024": 15.0, "2025": 30.0}

def test_report_has_a_capital_allocation_section_with_the_totals_line():
    m = vm.company_metrics([_year(), _year()], price=20, market_cap=2000, shares=100)
    text = vm.render_report("X", "2026-10-23", m)
    assert "## Capital allocation" in text and "Over 2 years" in text
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_value_metrics.py -k capital -q` → FAIL.
- [ ] **Step 3: Implement** — `get_value_metrics` fetches `t.history(period="10y", auto_adjust=True)` once (through `yf_retry`), passes `yearly_average_closes(hist)`; `fiscal_year_end[:4]` keys the lookup. Totals line copy: `Over {n} years: generated {g}, returned {r} (dividends {d}, buybacks {b} at an average of {p} per share vs {price} today), acquisitions {a}, net debt {nd:+}, unexplained {u}.` Prompt: replace the GOOD MANAGEMENT sentence with "Start from the Capital allocation table in `get_value_metrics`: where the cash went over five years, the prices paid for buybacks against today's price, acquisitions and debt; then insider buying vs selling and stock-based compensation as % of revenue."
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(metrics): capital allocation history with buyback prices in the verified report`

### Task 3: Valuation worksheet: EPV, sensitivity grid, growth base rates

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (`formula_value` L250-286, new `growth_base_rates`, `render_report`), `tradingagents/agents/analysts/fundamentals_analyst.py` (one sentence)
- Test: `tests/test_value_metrics.py`

**Interfaces:**
- Produces: `formula_value(...)` adds `epv` (per share: `(mean(ebits) * (1 − TAX_RATE) / rate − net_debt) / shares`, None without positive EBIT) and `grid: {"r-1": [v0, v3, v5], "r": [...], "r+1": [...]}` where each value is the mean across available methods at that rate and growth.
- Produces: `BASE_RATE_COHORTS: tuple[tuple[str, float | None, dict[float, float]], ...]` and `growth_base_rates(revenue: float | None) -> dict | None` → `{"cohort": "$5–25B", "share_achieving": {0.0: .., 0.03: .., 0.05: .., 0.07: .., 0.10: .., 0.15: ..}, "cap": g}` where `cap` is the highest tabulated growth that at least 25% of the cohort achieved.
- Produces: `base_rate_note(implied_growth: float | None, revenue: float | None) -> str | None` e.g. `Implied growth 7.9%/yr: about 22% of companies with revenue $5–25B sustained 7% or more for 10 years.`
- Produces: `## Valuation worksheet` section (replaces the `## Formula valuation` header; keeps every existing line, adds EPV, the grid as a Markdown table, and the base-rate note).

- [ ] **Step 1: Write the failing tests**

```python
def test_epv_is_after_tax_normalized_ebit_capitalized_at_the_rate_less_net_debt():
    years = [_year(), _year(ebit=180)]
    v = vm.formula_value(years, price=20.0, shares=100, net_debt=400, rate=0.10)
    assert v["epv"] == pytest.approx((190 * (1 - vm.TAX_RATE) / 0.10 - 400) / 100)

def test_sensitivity_grid_moves_with_rate_and_growth():
    v = vm.formula_value([_year(), _year()], price=20.0, shares=100, net_debt=400, rate=0.10)
    assert set(v["grid"]) == {"r-1", "r", "r+1"} and len(v["grid"]["r"]) == 3
    assert v["grid"]["r-1"][1] > v["grid"]["r"][1] > v["grid"]["r+1"][1]
    assert v["grid"]["r"][1] == pytest.approx(v["base"])

def test_growth_base_rates_pick_the_cohort_and_cap():
    br = vm.growth_base_rates(8e9)
    assert br["cohort"] == "$5–25B" and 0 < br["share_achieving"][0.10] < br["share_achieving"][0.05]
    assert br["cap"] in br["share_achieving"] and vm.growth_base_rates(None) is None

def test_worksheet_renders_epv_grid_and_base_rate_note():
    m = vm.company_metrics([_year(), _year()], price=20, market_cap=2000, shares=100)
    text = vm.render_report("X", "2026-10-23", m)
    assert "## Valuation worksheet" in text and "Earnings-power value" in text and "base rate" in text
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** — cohorts by revenue: `<$1B`, `$1–5B`, `$5–25B`, `>$25B`, with approximate shares achieving ≥ 0/3/5/7/10/15% ten-year sales CAGR taken from the Credit Suisse "Base Rate Book" (Mauboussin, 2016) charts; cite that in the docstring and say the values are read off the charts. Suggested values (builder may refine from the source): `<$1B: {0: .80, .03: .60, .05: .48, .07: .38, .10: .27, .15: .15}`, `$1–5B: {0: .85, .03: .62, .05: .47, .07: .35, .10: .22, .15: .10}`, `$5–25B: {0: .88, .03: .62, .05: .45, .07: .30, .10: .17, .15: .06}`, `>$25B: {0: .90, .03: .60, .05: .40, .07: .24, .10: .11, .15: .03}`. Fundamentals prompt: after "quote them and do not recompute", add "The worksheet's base value is the formula value every later agent refers to; start your valuation from it and state each adjustment."
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(metrics): valuation worksheet with earnings-power value, rate/growth grid and growth base rates`

### Task 4: Expected-return model and candidate ranking

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (new `expected_return`, `company_metrics`, `render_report`), `value_pipeline/screener.py` (`evaluate`, `rank`), `value_pipeline/actions.py` (`Decision.expected_return`), `value_pipeline/review.py:268-281`, `value_pipeline/render.py` (candidate lines and the New buys table), `value_pipeline/web/views.py` + `screen.html` (column)
- Test: `tests/test_value_metrics.py`, `tests/test_value_pipeline.py`

**Interfaces:**
- Produces: `expected_return(years: list[dict], price, market_cap, shares, avg_prices: dict | None = None, horizon: int = 7) -> dict | None` with keys `expected_return, yield, growth, reversion, growth_cap, sustainable_growth, ev_ebit_now, ev_ebit_target, bear, base, bull` (annual fractions). `yield` = 3-year mean of (dividends + buybacks − issuance)/market_cap; `sustainable_growth = roic_avg × reinvestment_rate`, reinvestment = clamp(1 − (dividends+buybacks)/owner_earnings, 0, 1); `growth = min(sustainable_growth, growth_base_rates(revenue)["cap"], 0.10)` floored at 0; `ev_ebit_target` = median of the company's own historical EV/EBIT (yearly average close × shares + net debt, over EBIT, for years with avg prices) when ≥ 3 years, else 12.0; `reversion = clamp((target/now)^(1/horizon) − 1, −0.05, 0.05)`; `bear/base/bull` = yield + {0, growth, growth+0.03} + reversion to {0.8, 1.0, 1.2}×target. None when owner earnings ≤ 0 or EV/EBIT not computable.
- Produces: `company_metrics(...)["expected_return"]`; a worksheet line `Expected return (7-year): X%/yr = yield Y% + growth G% + multiple reversion R% (bear B% / base X% / bull U%)`.
- Produces: screen columns `expected_return, er_yield, er_growth, er_reversion, er_rank` (rank among passers by expected return, descending); `Decision.expected_return: float | None`; ACTIONS shows `expected return X%/yr` in the New buys table (new column) and in each "Candidates not bought" line.

- [ ] **Step 1: Write the failing tests**

```python
def test_expected_return_sums_yield_growth_and_reversion_and_orders_scenarios():
    years = [_year(dividends_paid=-50, buybacks=-30, free_cash_flow=180)] * 3 + [_year()] * 2
    er = vm.expected_return(years, price=20.0, market_cap=2000, shares=100,
                            avg_prices={"2021": 20, "2022": 20, "2023": 20, "2024": 20, "2025": 20})
    assert er["expected_return"] == pytest.approx(er["yield"] + er["growth"] + er["reversion"])
    assert er["bear"] <= er["base"] <= er["bull"] and er["growth"] <= 0.10

def test_expected_return_caps_growth_at_the_base_rate_and_reversion_at_five_points():
    rich = [_year(ebit=2000, revenue=30e9)] * 5         # ROIC huge, revenue in the >$25B cohort
    er = vm.expected_return(rich, price=1.0, market_cap=100, shares=100)
    assert er["growth"] <= vm.growth_base_rates(30e9)["cap"] and abs(er["reversion"]) <= 0.05

def test_expected_return_is_none_without_positive_owner_earnings():
    assert vm.expected_return([_year(free_cash_flow=-50)] * 3, price=20, market_cap=2000, shares=100) is None
```

In `tests/test_value_pipeline.py`: `test_screen_ranks_passers_by_expected_return_in_er_rank` (two passing rows, higher `expected_return` gets `er_rank` 1; a failing row has `er_rank` None) and `test_actions_show_expected_return_for_candidates` (a SKIP candidate line contains `expected return 9%/yr`).

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** per the interface; `review.py` sets `expected_return=_float(row.get("expected_return")) if row else None`.
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(metrics): 7-year expected-return model, er_rank in the screen, expected return in ACTIONS`

### Task 5: Facts for the agents: earnings date, foreign news, SEC-only tools, company name downstream

**Files:**
- Modify: `tradingagents/dataflows/vendors/yahoo/fundamentals.py` (new `next_earnings_date`), `tradingagents/dataflows/vendors/yahoo/snapshot.py:66-120` (one line), `tradingagents/agents/context.py:107-170` (`build_instrument_context(..., earnings_date: str | None = None)`), `tradingagents/graph/trading_graph.py:117-128` (pass it), `tradingagents/dataflows/vendors/yahoo/news.py:87-136` (name fallback), `tradingagents/dataflows/vendors/sec_filings.py` (non-filer message), `tradingagents/agents/analysts/fundamentals_analyst.py` (one sentence), `value_pipeline/review.py` (`RunResult.name`, results.json `name`), `value_pipeline/render.py` (ticker cells)
- Test: `tests/test_yahoo_snapshot.py`, a new `tests/test_instrument_facts.py`, `tests/test_value_pipeline.py`

**Interfaces:**
- Produces: `next_earnings_date(symbol: str) -> str | None` (ISO date from `yf.Ticker(symbol).calendar["Earnings Date"][0]`, fail-open).
- Produces: snapshot line `- Next earnings: 2026-11-05 (13 days)` or `- Next earnings: unknown`; instrument context sentence `Next earnings report: 2026-11-05 (13 days away).` when known.
- Produces: `get_news_yfinance` tries `yf.Search(company_name, news_count=limit).news` when the tagged search is empty and a `longName` exists, prefixing the report with `Matched by company name "…" (Yahoo tags no articles with this listing): verify each item is about the company.`
- Produces: `NOT_A_US_FILER = "Not a US SEC filer: use get_fundamentals, get_income_statement, get_balance_sheet and get_cashflow (Yahoo) and the annual report on the company's investor site."` returned by both SEC tools for a non-filer.
- Produces: `RunResult.name: str | None`; `results.json` key `name`; ACTIONS ticker cells render `Kia Corporation (000270.KS)` when the name is known.

- [ ] **Step 1: Write the failing tests** — `test_snapshot_shows_days_to_next_earnings` (patch `next_earnings_date` to return as_of + 13 days; assert `(13 days)`), `test_instrument_context_names_the_earnings_date`, `test_news_falls_back_to_company_name_search` (patch `yf.Ticker.get_news` → [], `yf.Search` → first call no tagged articles, second call (by name) returns one article; assert the prefix and the headline), `test_sec_tools_say_not_a_us_filer_for_a_foreign_ticker` (patch the vendor's filer lookup to raise the existing "not a US SEC filer" condition; assert the exact sentence), `test_actions_table_shows_the_company_name`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Fundamentals prompt: add "`get_financial_history` and `get_annual_report` answer only for US SEC filers; if they say so, do not retry them." `review.py` gets the name from `resolve_instrument_identity(ticker).get("company_name")` (already cached by the graph's own lookup).
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(facts): earnings date in snapshot and context, news by company name for foreign listings, clear SEC-only answers, names in ACTIONS`

### Task 6: Peer comparison tool

**Files:**
- Create: `tradingagents/dataflows/vendors/yahoo/peers.py`
- Modify: `tradingagents/dataflows/vendors/yahoo/common.py` (new `usd_rate`), `tradingagents/agents/tools.py` (new `get_peer_comparison`), `tradingagents/agents/analysts/fundamentals_analyst.py` (`TOOLS` and the CHEAP sentence), `tradingagents/dataflows/router.py` if a vendor registration is needed for the tool name (follow `get_value_metrics`)
- Test: `tests/test_peers.py`

**Interfaces:**
- Produces: `usd_rate(currency: str | None) -> float | None` (1.0 for USD; `GBp`/`GBX` → GBP rate / 100 applied by the caller; cached per process; None on failure).
- Produces: `peer_comparison(symbol: str, limit: int = 6, as_of: str | None = None) -> str` — Markdown table with columns `Ticker | Name | Mkt cap (USD) | EV/EBIT | Gross margin | EBIT margin | ROIC | FCF yield | Revenue CAGR 5y | Share count 5y`, the subject first, peers from `yf.Industry(info["industryKey"]).top_companies` excluding the subject, then a `Median (peers)` row; cached as JSON per symbol per day under `get_config()["data_cache_dir"]/peers/`.
- Produces: tool `get_peer_comparison(ticker: InjectedState("company_of_interest"), trade_date: InjectedState("trade_date") = "") -> str`, listed in the fundamentals analyst's `TOOLS`.

- [ ] **Step 1: Write the failing tests** (patch `yfinance.Ticker` and `yfinance.Industry` with fakes carrying `.info`, `.income_stmt/.balance_sheet/.cashflow` frames and `.top_companies`): `test_peer_table_lists_subject_first_then_peers_then_median`, `test_peer_row_divides_pence_quotes_by_100` (a `.L` peer with `currency: "GBp"`, price 1789.5, marketCap 39.8e9 → Mkt cap `$53.3B` at GBP 1.34), `test_peer_table_says_why_when_no_industry_is_known` (no `industryKey` → one-line reason), `test_peer_table_is_cached_for_the_day`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Prompt: in CHEAP add "Call `get_peer_comparison`: say whether it is cheap relative to peers and why (quote the table)."
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(tools): peer comparison from Yahoo's industry listing, in USD, for the fundamentals analyst`

### Task 7: Pre-mortem node

**Files:**
- Create: `tradingagents/agents/managers/premortem.py`
- Modify: `tradingagents/agents/__init__.py`, `tradingagents/agents/state.py:45-73` (`premortem`), `tradingagents/graph/propagation.py:30-65` (`"premortem": ""`), `tradingagents/graph/setup.py:35-45,109-187`, `tradingagents/graph/conditional_logic.py:23-33`, `tradingagents/graph/trading_graph.py:140-165,449-487` (signature `premortem=1`, `_log_state`), `tradingagents/reporting.py:60-76`, `tradingagents/report_html.py` if sections are listed there, `cli/display.py:31,220,428`, `cli/run.py` status handling, `tradingagents/agents/managers/portfolio_manager.py` (prompt gets `**Pre-mortem:**`)
- Test: `tests/test_graph_end_to_end.py` (follow its fake-LLM pattern), `tests/test_reporting.py`, `tests/test_structured_agents.py`

**Interfaces:**
- Produces: `create_premortem(llm) -> Callable[[state], dict]` returning `{"premortem": "Pre-mortem: " + text}`; prompt copy: "It is three years on and this position has lost 40%. Write about 250 words: 1. The single most likely cause. 2. The evidence visible today that supports it. 3. The one metric that would show it first, and the level that confirms it." with the fundamentals report, the research manager's plan, the trader's plan and the risk debate history as context.
- Produces: `RISK_ANALYSIS_PATH_MAP["Pre-mortem"]`; `should_continue_risk_analysis` returns `"Pre-mortem"` at the end of the risk round; edge `Pre-mortem → Portfolio Manager`; `_run_signature` includes `"premortem=1"`; report section `("IV. Risk Management Team Decision", "4_risk", … + ("Pre-mortem", "premortem.md", ("premortem",)))`.

- [ ] **Step 1: Write the failing tests** — `test_risk_round_ends_at_the_premortem_then_the_portfolio_manager` (compiled graph with fake LLMs: node visit order ends `..., "Neutral Analyst", "Pre-mortem", "Portfolio Manager"`), `test_premortem_text_reaches_the_pm_prompt_and_the_report` (state with `premortem` set → PM prompt contains `**Pre-mortem:**` and the report tree writes `4_risk/premortem.md`), `test_run_signature_mentions_premortem`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** CLI: add "Pre-mortem" to the Risk Management agent lists in `cli/display.py` and the status transitions in `cli/run.py` next to "Neutral Analyst".
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(graph): pre-mortem node between the risk debate and the Portfolio Manager`

### Task 8: The decision as a checklist with scenarios and a value bridge

**Files:**
- Modify: `tradingagents/agents/schemas.py:221-290`, `tradingagents/agents/managers/portfolio_manager.py:23-110`, `value_pipeline/parse.py`, `value_pipeline/review.py` (results.json), `value_pipeline/actions.py` (`Decision` fields, `value_gap_flag`)
- Create: `tradingagents/agents/decision_math.py`
- Test: `tests/test_structured_agents.py`, `tests/test_value_pipeline.py`, `tests/test_decision_math.py`

**Interfaces:**
- Produces, in `schemas.py`: `class Verdict(BaseModel): passed: bool; note: str`; `class Adjustment(BaseModel): reason: str; amount: float`; `class Scenario(BaseModel): value: float; probability: float; basis: str`; `class Scenarios(BaseModel): bear: Scenario; base: Scenario; bull: Scenario`. `PortfolioDecision` gains `good_business: Verdict | None = None`, `good_management: Verdict | None = None`, `cheap: Verdict | None = None`, `red_flags: list[str] = []`, `kill_criteria: list[str] = []`, `entry_price: float | None = None` (same nullish coercion as `price_target`), `value_bridge: list[Adjustment] = []`, `scenarios: Scenarios | None = None`, `premortem_answer: str = ""`. Field descriptions carry the instructions (bridge starts at the formula value and sums to the price target; probabilities sum to 1; entry price required unless Sell; 3–6 kill criteria).
- Produces, in `decision_math.py`: `scenario_stats(scenarios: Scenarios, price: float | None) -> dict` → `{expected_value, upside_downside, weighted_mos}` (upside_downside None unless bear < price < bull); `decision_checks(decision: PortfolioDecision, formula_value: float | None, price: float | None) -> list[str]` returning plain-English problems: bridge sum off by more than 2% of the target, probabilities not summing to 1 ± 0.02, expected value off the target by more than 2%, entry price missing on a non-Sell, fewer than 3 kill criteria.
- Produces: `render_pm_decision` appends, after `**Time Horizon**:`, the sections `**Checklist**:` (three lines `Good business — PASS/FAIL: note`), `**Red Flags**:` (bullets or `none`), `**Kill Criteria**:` (numbered), `**Entry Price**:` (number or `not provided`), `**Scenarios**:` (three lines `Bear 25%: 42.00 — basis`; then `Expected value: 56.00; upside/downside 2.4x`), `**Value Bridge**:` (`Formula value 80.33` then one line per adjustment `−12.00 reason`, then `= Price Target 56.00`), `**Pre-mortem Answer**:`.
- Produces, in `parse.py`: `entry_price(decision: str) -> float | None`, `kill_criteria(decision: str) -> list[str]`, `checklist(decision: str) -> dict[str, dict]` (`{"good_business": {"passed": True, "note": "..."}, ...}`), `scenarios(decision: str) -> dict | None` (`{"bear": {"value", "probability"}, ..., "expected_value", "upside_downside"}`), `value_bridge(decision: str) -> list[dict]`.
- Produces: `results.json` keys `price, formula_value, entry_price, kill_criteria, checklist, scenarios, value_bridge, red_flags`; `Decision` gains `entry_price, kill_criteria, value_bridge`; `value_gap_flag` text ends with `; bridge: 80.33 → −12.00 (reason) → … = 56.00` when a bridge exists.
- Produces, PM prompt: the fundamentals report's worksheet section (`worksheet_excerpt(report: str) -> str`, the text from `## Valuation worksheet` to the next `## ` or end) under `**Valuation worksheet (formula value):**`; the rubric lines replaced by: Buy = all three pass, probability-weighted margin of safety ≥ 30% and upside/downside ≥ 3, no serious red flag; Overweight = all three pass, ≥ 15% and ≥ 2, or DOWNTREND; Hold on a name not held must give the entry price at which both are met; "Express entry conditions as prices and verifiable facts. Use the earnings date only when the print is genuinely binary and say why. If you would buy at today's price after a clean print, say so and give the entry price. Start the value bridge from the worksheet's formula value and justify any growth above the base rate. The bear case may not assume more than 0.8× the target multiple and zero growth unless you say why."; one corrective retry when `decision_checks` is non-empty: re-invoke with `Fix these problems and return the full decision again: …` appended; if problems remain, keep the decision and append `**Unresolved Checks**:` lines to the rendered text.

- [ ] **Step 1: Write the failing tests**

```python
def test_scenario_stats_expected_value_and_upside_downside():
    s = Scenarios(bear=Scenario(value=40, probability=0.25, basis="b"), base=Scenario(value=56, probability=0.5, basis="m"),
                  bull=Scenario(value=70, probability=0.25, basis="u"))
    st = scenario_stats(s, price=50)
    assert st["expected_value"] == pytest.approx(55.5) and st["upside_downside"] == pytest.approx(2.0)
    assert st["weighted_mos"] == pytest.approx((55.5 - 50) / 55.5)

def test_decision_checks_catch_bridge_probabilities_entry_price_and_kill_criteria():
    d = _decision(rating="Hold", price_target=56, value_bridge=[Adjustment(reason="x", amount=-10)], scenarios=_scen(0.3, 0.5, 0.3),
                  entry_price=None, kill_criteria=["a"])
    problems = decision_checks(d, formula_value=80.33, price=50)
    assert len(problems) == 4 and decision_checks(_good_decision(), formula_value=80.33, price=50) == []

def test_rendered_decision_round_trips_through_parse():
    text = render_pm_decision(_good_decision())
    assert parse.entry_price(text) == 46.0 and len(parse.kill_criteria(text)) == 3
    assert parse.checklist(text)["good_business"]["passed"] is True
    assert parse.scenarios(text)["expected_value"] == pytest.approx(56.0)
    assert parse.value_bridge(text)[0]["amount"] == -12.0
    assert parse.intrinsic_value(text) == 56.0 and parse.margin_of_safety(text) is not None   # old parsers still work

def test_pm_retries_once_on_failed_checks_then_keeps_the_decision():
    # fake structured llm returns a bad decision twice; node calls it exactly twice and renders "**Unresolved Checks**:"
```

Plus `test_value_gap_flag_prints_the_bridge` in `tests/test_value_pipeline.py` and `test_results_json_carries_the_checklist_fields`.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Keep `render_pm_decision`'s first five sections byte-identical. `parse.section` already reads `**Label**:` blocks; the new parsers build on it.
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(decision): checklist, kill criteria, entry price, scenarios and value bridge in the Portfolio Manager's decision, parsed by the pipeline`

### Task 9: Entry price and kill criteria in ACTIONS, the portfolio, the watch and the website

**Files:**
- Modify: `value_pipeline/render.py:24-100`, `value_pipeline/portfolio.py:25-35` (`Position.kill_criteria: list[str] = field(default_factory=list)`, `Position.entry_price: float | None = None`) and its load/save, `value_pipeline/review.py:312-330` (`_record_ratings`), `value_pipeline/operations.py` (buy records kill criteria from the latest result), `value_pipeline/watch.py:24-56` (`Alert` kind `ENTRY PRICE`, holdings' kill criteria in the markdown), `value_pipeline/web/views.py:55-70`, `value_pipeline/web/templates/stock.html`
- Test: `tests/test_value_pipeline.py`, `tests/test_value_web.py`

**Interfaces:**
- Produces: ACTIONS "Candidates not bought" lines: `- 000270.KS: Rated Overweight — buy below 115,000.00; expected return 11%/yr (report)`; after the Holdings table a `Kill criteria` list per holding (`- HRB: 1. …; 2. …`).
- Produces: `alerts(holdings, ideas, prices, trends, buy_mos, today, max_age_days)` adds `Alert(t, "ENTRY PRICE", f"{t} at {price:,.2f} is at or below the agents' entry price {entry:,.2f} (rated {rating} on {date})")` for ideas with an `entry_price`, any non-Sell rating, age ≤ max_age_days, not FREEFALL.
- Produces: the stock page shows `Checklist`, `Kill criteria`, `Entry price` and `Scenarios` from the newest result.

- [ ] **Step 1: Write the failing tests** — `test_actions_list_entry_price_for_candidates_and_kill_criteria_for_holdings`, `test_watch_alerts_on_entry_price`, `test_watch_handles_results_without_entry_price` (old results.json rows → no ENTRY PRICE alert, no error), `test_old_portfolio_file_loads_with_empty_kill_criteria` (JSON without the keys → `[]`/`None`; save writes them), `test_recording_a_buy_stores_kill_criteria_and_entry_price`, `test_stock_page_shows_checklist_and_kill_criteria` (web).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(pipeline): entry price and kill criteria flow into ACTIONS, the portfolio, the weekly watch and the stock page`

### Task 10: Investment committee

**Files:**
- Create: `value_pipeline/committee.py`, `tests/test_committee.py`
- Modify: `value_pipeline/review.py` (after `plan(...)`, before rendering; `results.json`), `value_pipeline/render.py` (new section), `value_pipeline/config.py` + `config.yaml` (`review.committee: true`), `value_pipeline/web/templates/reviews.html` (show the committee summary if present)

**Interfaces:**
- Produces, in `committee.py`: pydantic `RankedIdea(ticker: str, rank: int, action: Literal["BUY","ADD","HOLD","TRIM","SELL","SKIP"], weight: float, expected_return: float | None, downside: str, rationale: str)`, `Swap(sell: str, buy: str, reason: str)`, `Committee(ranking: list[RankedIdea], swaps: list[Swap], concentration: list[str], summary: str)`.
- Produces: `build_payload(decisions: list[Decision], results: list[dict], portfolio, summary: dict, sizing: dict) -> dict` (JSON-serialisable: per ticker rating, checklist, expected_return, scenarios, upside_downside, entry_price, red_flags, sector, held weight, kill_criteria; cash; the sizing rules).
- Produces: `run_committee(llm, payload: dict) -> Committee | None` using `bind_structured`/`invoke_structured` with the prompt: rank by expected return adjusted for the probability-weighted downside and red flags; propose the allocation under the 10% position and 30% sector caps; propose a swap only when the candidate's expected return exceeds the holding's by at least 5 points; name concentration and correlation (same sector, end market, macro factor); one paragraph per action. Returns None on failure (the review continues).
- Produces: `reconcile(committee: Committee, actions: list[Action]) -> tuple[Committee, list[str]]` — drops any committee BUY/ADD on a ticker whose own review rated Sell (note "committee wanted to buy X but its review rated Sell: the review stands"), and lists every ticker where the committee's action differs from the deterministic one.
- Produces: ACTIONS section `## Investment committee` (ranking table `Rank | Ticker | Action | Weight | Expected return | Rationale`, then `Swaps`, `Concentration`, `Where the committee and the rules disagree`) placed before `## Holdings`; `results.json` top-level is unchanged (a list), so the committee goes to a sibling file `committee.json` in the report folder and the reviews page reads it.

- [ ] **Step 1: Write the failing tests** — `test_payload_is_json_serialisable_and_carries_every_ticker`, `test_committee_never_overrides_a_sell`, `test_reconcile_lists_disagreements`, `test_actions_render_the_committee_section_before_holdings`, `test_review_skips_the_committee_when_disabled_or_dry_run` (config `committee: false` or `--dry-run` → no `committee.json`), `test_a_failed_committee_call_does_not_fail_the_review`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** The deep LLM is `ta.deep_thinking_llm` from `make_graph`; the committee call counts in the same `UsageTally`.
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(review): investment committee ranks and allocates across all reviewed stocks after the per-ticker reviews`

### Task 11: Docs, real-run verification and branch finish

**Files:**
- Modify: `value_pipeline/README.md` (sections: Valuation worksheet, Accounting checks, Capital allocation, Expected return, Pre-mortem, the decision checklist, Investment committee, what changes in ACTIONS and the watch), `docs/` prompt notes if a prompts doc exists (grep "Portfolio Manager" in docs/)
- Test: none new; the real run below

- [ ] **Step 1: Write the README sections** (one short paragraph each, in the README's existing voice).
- [ ] **Step 2: Full verification** — `.venv/bin/python -m pytest -q` (all pass), `.venv/bin/ruff check .` (clean), `uv run python -m value_pipeline review --dry-run --yes --tickers HRB,ADBE` writes ACTIONS with the new sections populated from the fake decisions (extend `fake_run` so the dry run exercises the checklist, scenarios and entry price).
- [ ] **Step 3: Real run** (requires the Claude CLI login; skip in CI): from the worktree, `uv run python -m value_pipeline review --yes --tickers HRB,000270.KS` into a scratch data dir (`--data-dir /tmp/…` with a copied `portfolio.json`), then read both `5_portfolio/decision.md` files end to end: the worksheet is quoted, the bridge starts at the formula value, scenarios sum to 1, an entry price is given, the pre-mortem is answered, the peer table appears in the fundamentals report, and the Korean ticker's report names the company and used the name-based news fallback.
- [ ] **Step 4: Commit** — `docs: methodology upgrade in the value_pipeline README`
- [ ] **Step 5: Finish the branch** with superpowers:finishing-a-development-branch (merge to `main`, restart the website).
