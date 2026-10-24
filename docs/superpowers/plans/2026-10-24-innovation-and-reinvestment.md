# Innovation and Reinvestment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put innovation and reinvestment numbers on the worksheet, the 10-K's Business section (with product and pipeline paragraphs) in the annual-report tool, a fourth `Still investing` checklist verdict on the decision (required for the compounder lane), and a disruption question in the pre-mortem.

**Architecture:** `value_metrics.innovation(years)` and a `## Innovation and reinvestment` section in `render_report`, fed by a new `rnd` year field from the Yahoo vendor; `sec_filings.SECTIONS` gains Item 1 and `get_annual_report` prints it first with the innovation paragraphs kept whole; `PortfolioDecision.still_investing` is rendered, checked, parsed and shown like the other three verdicts; two prompt paragraphs (fundamentals analyst, Portfolio Manager) and one pre-mortem question.

**Tech Stack:** Python 3.11+, pandas/yfinance (vendor), pydantic v2, Jinja (website), pytest (`@pytest.mark.unit`, no network), ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-10-24-innovation-and-reinvestment-design.md`

## Global Constraints

- Nothing trades. Every new metric is None-tolerant: a missing field yields `None`/`n/a`, never an exception; old cached `years` rows without `rnd` load.
- No network in tests: synthetic `years` dicts, synthetic 10-K text, patched fetches.
- Capex is stored negative; use `abs()` like the existing code (`value_metrics.py:295`, `:760`).
- Worksheet labels are fixed strings, one number each, `n/a` when None (exact labels in the spec §1).
- `assert set(ROWS) == set(YEAR_FIELDS)` in the Yahoo vendor must hold.
- ruff clean; full suite green after each task; conventional commits ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A 10-K whose table of contents lists "Item 1. Business" and "Item 1A. Risk Factors": the Business span must be the body and must stop at Item 1A (not swallow it, not match "Item 1A" as its own start). Test in Task 2 (`test_business_section_is_the_body_and_stops_at_1a`).
2. A company that does not report R&D (DECK, HRB): every R&D key None, `rnd_reported` False, growth capex and growth spend still computed, the prose line says R&D is not reported. Test in Task 1 (`test_innovation_without_rnd`).
3. Harvesting signal with only capex data (no R&D): capex below depreciation and EBIT margin up → True. Test in Task 1 (`test_harvesting_signal`).
4. An older decision (results.json without `still_investing`) must load, parse and render on the stock page. Test in Task 3 (`test_checklist_without_still_investing_still_renders`).
5. An inflection-lane Overweight whose `still_investing` is FAIL must raise no lane problem (only the compounder lane requires it). Test in Task 3 (`test_still_investing_required_only_for_compounder`).

---

### Task 1: `innovation()` and the worksheet section; the `rnd` field

**Files:**
- Modify: `tradingagents/dataflows/value_metrics.py` (`YEAR_FIELDS` + `"rnd"`; new `innovation(years) -> dict`; `render_report` section after `## Record and inflection`), `tradingagents/dataflows/vendors/yahoo/value_metrics.py` (`ROWS["rnd"] = ("Research And Development",)`)
- Test: `tests/test_value_metrics.py`

**Interfaces:**
- Produces: `innovation(years: list[dict]) -> dict` with keys `rnd_to_revenue, rnd_to_revenue_avg, rnd_cagr, rnd_cagr_years, revenue_cagr_same_span, gross_profit_per_rnd, growth_capex_to_revenue, growth_spend_to_cfo, roic_incremental_3y, harvesting, rnd_reported` (definitions in spec §1; reuse `_invested_capital`, `TAX_RATE`, the CAGR span rule of `record`'s `revenue_cagr` with `MIN_CAGR_SPAN`).
- Produces: worksheet lines (exact labels): `- R&D / revenue`, `- R&D / revenue 5-year average`, `- R&D growth {x}%/yr over {n} years`, `- Revenue growth over the same span`, `- Gross profit added per R&D dollar`, `- Growth capex / revenue (3-year average)`, `- Growth spend / operating cash flow (3-year)`, `- Return on incremental capital (3-year)`, `- Harvesting signal yes|no|n/a`; plus the prose line (spec §1).

- [ ] **Step 1: Failing tests** `test_innovation_full_data` (synthetic 5 years with R&D growing 10%/yr, revenue 8%/yr: `rnd_cagr` ≈ 0.10 over 4 years, `revenue_cagr_same_span` ≈ 0.08, `gross_profit_per_rnd` equals (GP0 − GP4)/sum(rnd[1:5]), `growth_spend_to_cfo` and `roic_incremental_3y` by hand), `test_innovation_without_rnd`, `test_innovation_needs_four_years_for_incremental_return`, `test_harvesting_signal` (three cases), `test_render_innovation_section_labels_and_na`, `test_yahoo_rows_cover_rnd` (`ROWS["rnd"]` and `"rnd" in YEAR_FIELDS`).
- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Suite and ruff.** — [ ] **Step 5: Commit** — `feat(worksheet): innovation and reinvestment section with R&D from Yahoo`

### Task 2: The Business section and innovation lines in `get_annual_report`

**Files:**
- Modify: `tradingagents/dataflows/vendors/sec_filings.py` (`SECTIONS` first entry `("Business (Item 1)", _ITEM + r"1" + _SEP + r"business\.?\s*$", _ITEM + r"1a\b")`; `BUSINESS_CHARS = 6_000`; `INNOVATION_WORDS` and a compiled regex like `_OUTLOOK`; `get_annual_report` prints Business (cut to `BUSINESS_CHARS`), then `## Products and innovation lines` (paragraphs matching, capped at `OUTLOOK_CHARS`, "None found."), then 1A and 7 as today; `_sections` cache path `10k_{cik}_{accession}_v2.json`)
- Test: `tests/test_sec_filings.py`

**Interfaces:**
- Consumes: `section_spans`, `_cut`, `OUTLOOK_CHARS` as they exist.
- Produces: `get_annual_report` output order Business → innovation lines → Risk factors → MD&A; `SECTIONS[0][0] == "Business (Item 1)"`.

- [ ] **Step 1: Failing tests** `test_business_section_is_the_body_and_stops_at_1a` (TOC lines "Item 1. Business" / "Item 1A. Risk Factors" / "Item 7. ..." then bodies; Business span contains the body sentence and not the 1A body), `test_business_heading_does_not_match_item_1a`, `test_innovation_lines_kept_beyond_the_cut` (a paragraph with "new product" beyond `BUSINESS_CHARS` appears under the heading), `test_innovation_lines_none_found`, `test_sections_cache_name_is_versioned` (patched fetch; the written file name ends `_v2.json`).
- [ ] **Steps 2–5.** **Commit** — `feat(tools): the 10-K Business section and its product lines in the annual report`

### Task 3: `Still investing` on the decision; prompts; pre-mortem; parse; website

**Files:**
- Modify: `tradingagents/agents/schemas.py` (`still_investing: Verdict | None` with the spec's description; render fourth line after Good management), `tradingagents/agents/decision_math.py` (missing-verdict loop includes it, message "the checklist needs all four tests"; `_compounder_problems` passes `("good_business", "good_management", "still_investing")`; inflection unchanged; `labels` dict gains `still_investing: "Still investing"`), `tradingagents/agents/managers/portfolio_manager.py` (rubric: four tests; the spec §3 sentences; Output line for Checklist: four lines), `tradingagents/agents/analysts/fundamentals_analyst.py` (spec §3 innovation paragraph), `tradingagents/agents/managers/premortem.py` (question 4; "about 300 words"), `value_pipeline/parse.py` (`_CHECKS["still investing"] = "still_investing"`; regex label alternation), `value_pipeline/web/templates/stock.html` (fourth label), `value_pipeline/review.py` (dry-run fake sets `still_investing`)
- Test: `tests/test_structured_agents.py`, `tests/test_decision_math.py`, `tests/test_structured_agent_prompts.py`, `tests/test_value_pipeline.py`, `tests/test_value_web.py`

**Interfaces:**
- Produces: `PortfolioDecision.still_investing`; rendered `Still investing — PASS: note`; `parse.checklist(...)["still_investing"]`; results.json `checklist.still_investing`.

- [ ] **Step 1: Failing tests** `test_still_investing_round_trip` (render → parse), `test_missing_still_investing_is_a_problem_for_non_sell` (message contains "all four tests"), `test_still_investing_required_only_for_compounder` (compounder Overweight with FAIL → problem; inflection Overweight with FAIL → no lane problem about it), `test_checklist_without_still_investing_still_renders` (old three-line decision parses to three keys; stock page renders), `test_stock_page_lists_still_investing`, `test_prompts_carry_innovation_lines` (fundamentals prompt contains "Innovation and reinvestment"; pre-mortem prompt contains "disruption exposure"; PM prompt contains "Still investing must PASS for the compounder lane"), `test_dry_run_results_carry_still_investing`.
- [ ] **Steps 2–5.** **Commit** — `feat(decision): the Still investing verdict, required for compounders; disruption in the pre-mortem`

### Task 4: Docs and verification

- [ ] README: the worksheet section, the Business section in the annual report, the fourth checklist line and the compounder rule, the pre-mortem question.
- [ ] Full suite and ruff. After the merge (controller's step): `review --tickers ADBE` and `review --tickers DECK` into the real data dir; read the new worksheet section (ADBE with R&D, DECK without), the Business lines quoted in the fundamentals reports, the fourth checklist line and the pre-mortem's fourth answer.
- [ ] Finish with superpowers:finishing-a-development-branch.
