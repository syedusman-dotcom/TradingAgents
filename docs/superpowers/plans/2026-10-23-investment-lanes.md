# Investment Lanes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compounder lane and an inflection lane to the review: evidence metrics in code (record, inflection, two-stage run-rate value), lane rules the Portfolio Manager picks and `decision_checks` enforces, lane-aware sizing caps and committee, a theme scan that finds inflection candidates, and the screen/ACTIONS/website showing lanes.

**Architecture:** Metrics live in `tradingagents/dataflows/value_metrics.py` (pure, None-tolerant) fed by quarterly statements from the Yahoo vendor and by `value_pipeline/market_data.fundamentals`; the lane is a field on `PortfolioDecision` rendered as `**Lane**:` and parsed by `value_pipeline/parse.py`; `actions.plan` applies lane caps and size factors; `value_pipeline/themes.py` runs the monthly theme scan with one quick-tier structured call per theme and feeds hinted candidates into the review; the committee payload carries lanes.

**Tech Stack:** Python 3.11+, pydantic v2, LangChain structured output (`bind_structured`/`invoke_structured`), yfinance 1.7, pandas, pytest (`@pytest.mark.unit`, no network), ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-10-23-investment-lanes-design.md`

## Global Constraints

- Nothing trades. The value lane's Buy rule is unchanged (weighted MoS ≥ 30%, upside/downside ≥ 3).
- No lane buys in FREEFALL; price trend stays a timing filter only.
- Every new metric tolerates None and short histories (returns None / `quarters_available`), never raises.
- No network in tests; patch yfinance and the model calls.
- Old `results.json`, `portfolio.json` and decisions without `lane` load as lane "value".
- Thresholds, copied from the spec: compounder Buy = ROIC ≥ 20% in each of the last 5 years, 10-year revenue CAGR ≥ 8% (5-year when 10 unavailable, flagged), sustainable growth ≥ 10%, implied growth ≤ sustainable growth, expected return ≥ 12%/yr at reversion ≤ 0, upside/downside ≥ 1.5, price ≤ 1.15 × base; Overweight = expected return ≥ 10%, price ≤ 1.25 × base. Inflection Buy = business and management pass, no red flag, `ttm_vs_5y_ebit ≥ 1.5`, `revenue_acceleration ≥ 0.10`, incremental ROIC ≥ 20%, verified demand evidence, run-rate weighted MoS ≥ 20%, upside/downside ≥ 2, bear probability ≥ 0.30, a demand kill criterion.
- Config: `lanes: {themes: [], tickers: [], inflection_candidates: 3, compounder_candidates: 2}`; `sizing.lane_caps: {compounder: 0.30, inflection: 0.20}`; `sizing.lane_size_factor: {compounder: 0.5, inflection: 0.5}`.
- ruff clean; full suite green after each task; conventional commits ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A company with only 3 years of statements and 4 quarters: `record`/`inflection` return partial dicts with `years_available`/`quarters_available`, no lane hint, no crash. Test in Task 1.
2. A quarterly frame with a missing latest quarter (Yahoo often lags): TTM uses the last four available quarters and says which. Test in Task 1.
3. A PM that claims lane "inflection" without verified demand evidence or with `ttm_vs_5y_ebit` below 1.5: `decision_checks` sends it back once; on the retry still failing, the rendered decision carries `**Unresolved Checks**:` and the pipeline treats the rating as Hold for sizing (no lane Buy on an unresolved lane claim). Test in Task 3.
4. Lane caps across existing holdings: two inflection holdings already at 18% of the portfolio and a new inflection Buy → the buy is cut to fit 20% and the reason says so. Test in Task 4.
5. A theme scan returning a ticker the screen cannot fetch: skipped with a note, the review proceeds. Test in Task 5.

---

### Task 1: Record, inflection and run-rate value in code, quarterly statements in both fetch paths

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (new `record`, `inflection`, `run_rate_value`; `company_metrics(..., quarters=None)`; `render_report` "Record and inflection" section), `tradingagents/dataflows/vendors/yahoo/value_metrics.py` (fetch `quarterly_income_stmt`/`quarterly_balance_sheet`/`quarterly_cashflow` via `yf_retry`, `statement_years` works on them; pass `quarters`), `value_pipeline/market_data.py` (`fundamentals` stores `quarters`; `CACHE_SCHEMA` bump), `value_pipeline/screener.py` (columns `revenue_cagr_10y, sustainable_growth, ttm_vs_5y_ebit, revenue_acceleration`)
- Test: `tests/test_value_metrics.py`, `tests/test_value_pipeline.py`

**Interfaces:**
- `record(years: list[dict]) -> dict` keys `roic_min_5y, roic_years_above_20, revenue_cagr_10y, revenue_cagr_5y, fcf_cagr_5y, reinvestment_rate, sustainable_growth, years_available`.
- `inflection(years: list[dict], quarters: list[dict]) -> dict` keys `ttm_revenue, ttm_ebit, ttm_vs_5y_revenue, ttm_vs_5y_ebit, revenue_acceleration, ebit_margin_change_4q, incremental_roic, quarters_available, quarters_used` (the four quarter-end dates used for TTM).
- `run_rate_value(ttm_owner_earnings, ttm_ebit, net_debt, shares, price, g3, rate, decay_to) -> dict | None` keys `base, low, high, methods, implied_g3` (two-stage: three years at `g3`, then `perpetuity_multiple(decay_to, rate)`; `implied_g3` by bisection on the owner-earnings method).
- `company_metrics(...)["record"]`, `["inflection"]`, `["run_rate"]` (at `g3 = min(max(revenue_acceleration-adjusted TTM growth, 0), 0.40)` *(builder's call on the exact g3 rule; document it)*, `decay_to = growth_base_rates(revenue_usd)["cap"]`).

- [ ] **Step 1: Write the failing tests** (`_year`/`_quarter` helpers): `test_record_counts_roic_years_and_cagrs`, `test_record_with_three_years_is_partial`, `test_inflection_ttm_and_acceleration`, `test_inflection_uses_last_four_available_quarters`, `test_run_rate_value_two_stage_and_implied_g3` (hand computation), `test_report_has_record_and_inflection_lines`, `test_screen_rows_carry_lane_columns`, Yahoo quarterly mapping test.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Suite and ruff.**
- [ ] **Step 5: Commit** — `feat(metrics): record, inflection and two-stage run-rate value from annual and quarterly statements`

### Task 2: Lane on the decision: schema, prompt, render, parse, results

**Files:**
- Modify: `tradingagents/agents/schemas.py` (`lane: Literal["value","compounder","inflection"] = "value"`, `lane_evidence: list[str] = []`, `**Lane**:` rendering after `**Checklist**:`), `tradingagents/agents/managers/portfolio_manager.py` (the three-lane rubric paragraph; "pick the lane the evidence supports; value first"), `tradingagents/agents/decision_math.py` (`worksheet_record(excerpt) -> dict`, `worksheet_inflection(excerpt) -> dict`, lane checks in `decision_checks`), `value_pipeline/parse.py` (`lane`, `lane_evidence`), `value_pipeline/review.py` (results.json `lane`, `lane_evidence`), `value_pipeline/actions.py` (`Decision.lane`), `tradingagents/agents/analysts/news_analyst.py` (demand-evidence brief: customer capex, backlog, pricing, with sources, labelled verified/unverified), `tradingagents/agents/analysts/fundamentals_analyst.py` (quote the Record and inflection lines)
- Test: `tests/test_structured_agents.py`, `tests/test_decision_math.py`, `tests/test_value_pipeline.py`

**Interfaces:**
- `decision_checks(decision, formula_value, price, worksheet: str = "")` adds: lane "compounder" requires `record.roic_years_above_20 == 5`, `revenue_cagr_10y ≥ 0.08` (or 5y flagged), `sustainable_growth ≥ 0.10`, implied growth ≤ sustainable growth, price ≤ 1.15 × base for a Buy; lane "inflection" requires `ttm_vs_5y_ebit ≥ 1.5`, `revenue_acceleration ≥ 0.10`, `incremental_roic ≥ 0.20`, bear probability ≥ 0.30, at least one kill criterion mentioning orders/backlog/capex/demand, and `lane_evidence` naming a verified demand source. Problems are plain English.
- Rendered line `**Lane**: compounder — ROIC ≥ 20% 5/5 years; revenue 11.2%/yr 10y; sustainable growth 14%; implied growth 6%; expected return 13%; price 1.08× base` (evidence items joined by `; `).

- [ ] **Step 1: Failing tests**: round-trip of lane through render/parse; old decision text without `**Lane**:` parses as "value"; `test_compounder_claim_without_the_record_is_sent_back`; `test_inflection_claim_needs_verified_demand_and_a_demand_kill_criterion`; PM prompt contains the three-lane paragraph; news prompt contains the demand brief.
- [ ] **Step 2–5** as above. **Commit** — `feat(decision): investment lane with its evidence, enforced by the checks`

### Task 3: Lane-aware sizing, positions, ACTIONS and the committee

**Files:**
- Modify: `value_pipeline/config.py` + `config.yaml` (`lanes`, `sizing.lane_caps`, `sizing.lane_size_factor`), `value_pipeline/actions.py` (`plan`: lane size factor, lane caps like the sector cap, reason text `inflection lane at its 20% cap`; `Action.lane`), `value_pipeline/portfolio.py` (`Position.lane: str = "value"`), `value_pipeline/review.py` (`_record_ratings` stores lane; unresolved lane claim → size as Hold), `value_pipeline/render.py` (Lane column in New buys and Candidates; holdings table), `value_pipeline/committee.py` (payload `lane`, `lane_evidence`; prompt sentence on lane caps and cutting inflection first), `value_pipeline/watch.py` + `daily.py` (inflection holdings' demand criteria listed first)
- Test: `tests/test_value_pipeline.py`, `tests/test_committee.py`, `tests/test_daily.py`

- [ ] **Step 1: Failing tests**: `test_lane_buy_is_sized_at_half_and_capped_by_the_lane_cap` (two inflection holdings at 18% + a new inflection Buy → cut to fit 20% with the reason), `test_unresolved_lane_claim_sizes_as_hold`, `test_position_and_results_carry_the_lane`, `test_committee_payload_carries_lanes`, `test_watch_lists_demand_criteria_first`.
- [ ] **Step 2–5.** **Commit** — `feat(pipeline): lane caps and sizes, lanes on positions, ACTIONS and the committee`

### Task 4: Theme scan and lane-hinted candidates

**Files:**
- Create: `value_pipeline/themes.py`, `tests/test_themes.py`
- Modify: `value_pipeline/screener.py` (`lane_hint` column; `top_candidates(paths, owned, n, compounders=0)`), `value_pipeline/review.py` (`run-all` path: theme scan before the review; hinted candidates appended up to `lanes.inflection_candidates`; `lanes.tickers` always included), `value_pipeline/cli.py` (`themes` sub-command to run the scan alone; `run-all` runs it), `value_pipeline/web/templates/screen.html` (Lane hint column and filter), `value_pipeline/README.md` (Lanes section: what each lane is, thresholds, caps, how to name themes)

**Interfaces:**
- `themes.scan(llm, themes: list[str], today: str) -> list[ThemeIdea]` with `ThemeIdea(theme, ticker, name, evidence, verified: bool)` via a structured call per theme (`ThemeScan(ideas: list[ThemeIdea])`); `themes.candidates(paths, cfg, ideas) -> list[dict]` fetches and evaluates each ticker (fail-open, skipped tickers noted), keeps those with `ttm_vs_5y_ebit ≥ 1.3` or `revenue_acceleration ≥ 0.10`, sets `lane_hint = "inflection"`, caps at `lanes.inflection_candidates`; writes `reports/<date>/themes.md`.
- `screener.lane_hint(row) -> str` (`compounder` when the record bar passes, `inflection` when the inflection numbers pass, else "").

- [ ] **Step 1: Failing tests**: `test_theme_scan_extracts_ideas_with_a_stub_model`, `test_theme_candidates_skip_unfetchable_tickers_and_cap`, `test_lane_hint_column`, `test_run_all_includes_theme_and_compounder_candidates` (dry run with patched scan), web filter test.
- [ ] **Step 2–5.** **Commit** — `feat(discovery): theme scan feeds inflection candidates; compounder hints join the review`

### Task 4b: Dislocation scan and the watch's New ideas section

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` or `value_pipeline/metrics.py` (`dislocation(closes, ttm, five_year_avg, flags) -> {drawdown_from_high, move_1m, move_3m, fundamentals_intact}` over the screen's 420-day closes), `value_pipeline/screener.py` (columns + `dislocation` hint, sector-scare tagging when ≥ 3 names in one industry carry it; `industry` stored in the fundamentals cache), `value_pipeline/review.py` (`lanes.dislocation_candidates`, default 3, best by expected return, hint carried to the Decision/ACTIONS), `value_pipeline/themes.py` (the themes report lists sector scares with members), `tradingagents/agents/analysts/news_analyst.py` (classify the cause of a fall: company-specific / sector-wide sentiment / macro, with sources), `value_pipeline/watch.py` (`## New ideas`: names newly carrying a dislocation, compounder or inflection hint since the previous watch file), `value_pipeline/web/templates/screen.html` (hint filter), `value_pipeline/README.md`
- Test: `tests/test_value_pipeline.py`, `tests/test_value_web.py`

**Interfaces:**
- `drawdown_from_high = price / max(closes[-252:]) − 1`; `move_1m = price / closes[-22] − 1`; `move_3m = price / closes[-64] − 1`; `fundamentals_intact = ttm_revenue ≥ 5y avg revenue and ttm_ebit ≥ 5y avg EBIT and no accounting flags`; hint when the row passes the hard filters and (`drawdown_from_high ≤ −0.25` or `move_1m ≤ −0.20`) and `fundamentals_intact`.
- The watch compares the newest screen's hinted tickers against the previous watch's stored list (`reports/watch_<date>.json` sidecar, `{"hinted": [...]}`).

- [ ] **Step 1: Failing tests**: `test_dislocation_metrics_and_hint`, `test_sector_scare_tags_three_names_in_one_industry`, `test_review_adds_dislocation_candidates`, `test_watch_new_ideas_lists_only_new_hints`, web filter test.
- [ ] **Step 2–5.** **Commit** — `feat(discovery): dislocation scan with sector-scare detection; New ideas in the weekly watch`

### Task 5: Verification and docs

- [ ] Full suite and ruff; `review --dry-run` shows the Lane column; real run `review --tickers ADBE,CLS` into a scratch data dir (the Claude CLI on the plan; ~16 min); read both decisions: the lane line with numbers, the bridge, the inflection numbers for CLS, the demand evidence in the news report; list defects as findings.
- [ ] README updated in Task 4; finish the branch with superpowers:finishing-a-development-branch.
