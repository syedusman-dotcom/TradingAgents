# Evidence and Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the analysts the company's own filings (10-Q discussion and earnings release) as verified evidence, put an eight-quarter trend table and Yahoo's consensus on the worksheet with the Portfolio Manager stating where the thesis differs, and start a calibration record with a page that scores past decisions against later prices.

**Architecture:** Two new SEC vendor functions and tools beside `get_annual_report`; `quarter_trend` and a consensus section in `value_metrics`/the Yahoo vendor rendered by `render_report`; a `consensus_view` field on the decision; `value_pipeline/calibration.py` reading every review folder's results and pricing horizons through `market_data.closes_between`, with a CLI command and a website page.

**Tech Stack:** Python 3.11+, requests (EDGAR), yfinance 1.7, pandas, pydantic v2, FastAPI/Jinja for the page, pytest (`@pytest.mark.unit`, no network), ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-10-24-evidence-and-calibration-design.md`

## Global Constraints

- Nothing trades. Every new network path is fail-open and respects the look-ahead guard (`is_historical` / `as_of`): a past-dated run never sees a filing, an estimate or a price after its date.
- No network in tests: patch `requests`/`sec_edgar` helpers, `yfinance` and `market_data.closes_between`.
- EDGAR requests carry the configured user agent (`SEC_EDGAR_USER_AGENT`) through the existing helpers; cap each section at `SECTION_CHARS` (12,000) unless the spec says otherwise (earnings release 10,000).
- Worksheet additions use one number per fixed label where a check will read them (consensus); the eight-quarter table is a Markdown table.
- Old `results.json` rows without the new keys load; the calibration page renders empty.
- ruff clean; full suite green after each task; conventional commits ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A 10-Q whose MD&A heading is "Item 2" followed by a page break or table of contents entry: the extractor must pick the real section, not the TOC line. Test in Task 1 (`test_mdna_skips_the_table_of_contents_entry`).
2. An 8-K without Item 2.02 (e.g. a dividend announcement) must be skipped in favour of the latest earnings 8-K. Test in Task 1.
3. A review folder with an errored row or a dry-run folder must not enter calibration. Test in Task 3.
4. A ticker whose price history has a gap at a horizon date: use the last close on or before the horizon. Test in Task 3.
5. Yahoo returning empty estimate frames: the consensus section prints `n/a` lines and the PM's soft check does not fire. Test in Task 2.

---

### Task 1: Quarterly report and earnings release from EDGAR

**Files:**
- Modify: `tradingagents/dataflows/vendors/sec_filings.py` (reuse `_plain_text`, `_sections`-style extraction, the submissions fetch in `_latest_annual_filing`), `tradingagents/agents/tools.py` (two tools), `tradingagents/dataflows/router.py` (route like `get_annual_report`), `tradingagents/agents/analysts/fundamentals_analyst.py` and `news_analyst.py` (TOOLS + prompt lines)
- Test: `tests/test_sec_filings.py` (or the existing SEC test module; synthetic submissions JSON and HTML documents)

**Interfaces:**
- `latest_10q_mdna(ticker: str, as_of_date: str | None = None) -> str`: header `# Quarterly report (10-Q) for {T}, filed {date}, period ending {date}`; body = Item 2 (MD&A) text from "Results of Operations" through "Liquidity and Capital Resources" when those sub-headings exist, else the whole item, cut to `SECTION_CHARS` with the "more characters" note; non-filer → `NOT_A_US_FILER`; none filed by as-of → `NoMarketDataError`.
- `latest_earnings_release(ticker, as_of_date=None) -> str`: header `# Earnings release (8-K exhibit 99.1) for {T}, filed {date}`; the exhibit text cut to `RELEASE_CHARS = 10_000`, except that paragraphs containing any of `outlook, guidance, expect, backlog, bookings, book-to-bill, remaining performance obligations, RPO, demand` are appended whole under `## Outlook and demand lines` even when beyond the cut; selection = most recent 8-K whose items include `2.02` filed on or before as-of.
- Tools `get_quarterly_report(ticker: InjectedState("company_of_interest"), trade_date: InjectedState("trade_date") = "")` and `get_earnings_release(...)`, docstrings naming them as verified sources; both in the fundamentals analyst's TOOLS (after `get_annual_report`) and the news analyst's TOOLS.
- Prompts: fundamentals — "Call `get_quarterly_report` for the latest quarter's drivers and management's guidance; quote it with the filing date." News — "`get_earnings_release` and `get_quarterly_report` are verified sources: a quoted sentence with its filing date counts as verified demand evidence and as evidence for the cause of a fall."

- [ ] **Step 1: Failing tests**: `test_latest_10q_picks_the_newest_on_or_before_as_of`, `test_mdna_skips_the_table_of_contents_entry`, `test_mdna_keeps_results_and_liquidity`, `test_earnings_release_requires_item_2_02`, `test_outlook_lines_are_kept_beyond_the_cut`, `test_non_filer_sentence_for_both_tools`, `test_tools_are_offered_to_both_analysts`.
- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Suite and ruff.** — [ ] **Step 5: Commit** — `feat(tools): quarterly report and earnings release from EDGAR as verified evidence`

### Task 2: Eight-quarter trend table and consensus on the worksheet; the PM's consensus view

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (`quarter_trend`, `render_report` sections), `tradingagents/dataflows/vendors/yahoo/value_metrics.py` or a new `vendors/yahoo/consensus.py` (`consensus(symbol) -> dict`), `tradingagents/agents/schemas.py` (`consensus_view: str = ""`, rendered `**Consensus**:` after `**Pre-mortem Answer**:`), `tradingagents/agents/managers/portfolio_manager.py` (prompt line; soft check), `tradingagents/agents/decision_math.py` (`worksheet_consensus(excerpt) -> dict`; soft problem when the section exists and the field is empty), `value_pipeline/parse.py` (`consensus_view`), `value_pipeline/review.py` (results.json), `value_pipeline/committee.py` (payload `consensus_growth`, `consensus_target`), `tradingagents/agents/analysts/fundamentals_analyst.py` (trend sentence)
- Test: `tests/test_value_metrics.py`, `tests/test_structured_agents.py`, `tests/test_decision_math.py`, `tests/test_value_pipeline.py`

**Interfaces:**
- `quarter_trend(quarters: list[dict]) -> list[dict]` rows (newest first, ≤ 8): `period_end, revenue, revenue_yoy, gross_margin, ebit_margin, operating_cash_flow, free_cash_flow, cash_conversion, receivables_to_revenue` (y/y by the date-matched year-before quarter; None-tolerant); `render_report` section `## Last eight quarters` (table) + prose `Over the span: gross margin {up|flat|down} {x} points, EBIT margin …, cash conversion …`.
- `consensus(symbol) -> dict` keys `revenue_growth_next, revenue_growth_following, eps_growth_next, analysts, target_mean, target_median, target_high, target_low` (fractions/floats or None; `{}` on failure); worksheet `## Consensus (Yahoo estimates)` lines `- Consensus revenue growth next year {x.x%}`, `- Consensus revenue growth following year {x.x%}`, `- Consensus EPS growth next year {x.x%}`, `- Consensus analysts {n}`, `- Consensus price target median {v.vv}` (`n/a` when None) + one prose line vs implied and earned growth.
- `PortfolioDecision.consensus_view`; prompt "State where your value differs from consensus and why; a value far below consensus with no stated reason is a problem."; `decision_checks` problem text `The worksheet shows consensus estimates; say where your thesis differs from them and why (Consensus field)` only when the consensus section has at least one number and the field is empty.

- [ ] **Step 1: Failing tests** per interface (empty estimates → `n/a` and no problem; partial; full; trend y/y; rendering; round-trip).
- [ ] **Steps 2–5.** **Commit** — `feat(worksheet): eight-quarter trend and Yahoo consensus; the PM states its consensus view`

### Task 3: Calibration record, scores, CLI and page

**Files:**
- Create: `value_pipeline/calibration.py`, `tests/test_calibration.py`, `value_pipeline/web/templates/calibration.html`
- Modify: `value_pipeline/market_data.py` (price cache helper `close_on_or_before(ticker, date) -> float | None` using `closes_between` and a JSON cache under `cache/prices/`), `value_pipeline/cli.py` (`calibration` command), `value_pipeline/web/app.py` + `views.py` + `base.html` nav (page `/calibration`), `value_pipeline/README.md`

**Interfaces:**
- `rows(paths, today: date | None = None, price_fn=None) -> list[dict]` with the keys in the spec (date, ticker, name, lane, rating, price, value, formula_value, bear, base, bull, entry_price, expected_return, and per horizon `h30, h90, h180, h365` dicts `{price, ret, reached_value, below_bear, below_entry}` or None when the horizon is in the future); dry-run folders and errored rows excluded.
- `score(rows) -> dict`: per rating and per lane `n, median_ret_180, hit_value_365, sells_fell_180`; overall `median_value_gap` (agents' value / formula value − 1); `calibrated_probabilities` only with ≥ 10 rows at 180 days: realised share below bear / above bull vs mean stated probabilities; `verdicts: list[str]` one line per lane.
- CLI `calibration [--horizon 180]` prints the table and scores; page shows the sortable table, scores, verdicts; empty state "No decisions old enough yet".

- [ ] **Step 1: Failing tests**: `test_rows_read_every_review_and_skip_dry_runs_and_errors`, `test_horizons_use_the_last_close_on_or_before`, `test_future_horizons_are_none`, `test_scores_by_rating_and_lane_and_value_gap`, `test_probability_calibration_needs_ten_rows`, `test_calibration_page_renders_empty_and_populated`, `test_cli_calibration`.
- [ ] **Steps 2–5.** **Commit** — `feat(calibration): record and score past decisions against later prices; page and command`

### Task 4: Verification and docs

- [ ] README: sections for the filings tools, the trend and consensus lines, the calibration page.
- [ ] Full suite and ruff. Real run after the merge (controller's step): `review --tickers HRB` into a scratch data dir; read the quarterly report and earnings release tool outputs in the fundamentals and news reports, the `**Consensus**:` line, and the eight-quarter table; `calibration` prints rows for every review so far.
- [ ] Finish with superpowers:finishing-a-development-branch (merge after the Saturday weekly run has completed).
