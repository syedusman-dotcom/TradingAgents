# Investment lanes: compounder and inflection — design

**Date:** 2026-10-23
**Status:** the user asked whether the method should take "the potential of a business even if slightly overpriced" and "data center businesses when models were showing potential" into account, and said "Go ahead and build it" to Claude's proposal of two additional lanes. Calls Claude made are marked *(my call)*.

## Goal

Let the pipeline buy two kinds of business the pure value lane structurally rejects, each under its own numeric evidence bar and its own position cap, while the value lane stays the core:

- **Compounder lane** — a wonderful business at a fair price: a long record of high returns and reinvestment, bought at no more than a modest premium to the base value, because the growth is earned.
- **Inflection lane** — a business whose demand is accelerating now (the 2023 data-centre suppliers): trailing numbers understate it, so the value rests on the next three years with the growth then decaying to the base rate, sized smaller, with mandatory kill criteria and daily monitoring.

And a small discovery step so inflection candidates are found, not only analysed when named.

### What the user said

- "Do we need to take any other point of view like the potential of business even if they are slightly overpriced?"
- "Like data center businesses when models were showing potential."
- "Go ahead and build it."
- Context: Celestica on 2026-10-21 passed every quality filter and failed only on "the price implies 9.1% growth forever"; ADBE, BKNG and DPZ were "top-tier business at fair value, Hold" on 2026-10-22; the non-US screen listed a dozen names with ROIC above 100% at a negative margin of safety.

### Non-goals

- Loosening the value lane. Its Buy rule (weighted margin of safety ≥ 30%, upside/downside ≥ 3) is unchanged.
- Momentum investing. Price trend stays a timing filter (no Buy in FREEFALL) in every lane; no lane buys on price action.
- Trading. Unchanged.
- The wider non-US universe in the pipeline proper (the discovery spec); the theme step here works on the existing screen plus user-named tickers.

## Verified facts (from the code on 2026-10-23)

- The worksheet (`tradingagents/dataflows/value_metrics.py`) values on up-to-five-year averages of owner earnings and after-tax EBIT at 0/3/5% perpetual growth; `expected_return` caps growth at the cohort base rate (≤ 10%) and the company's own ROIC × reinvestment rate; `growth_base_rates` gives the share of companies sustaining each growth rate for ten years.
- `PortfolioDecision` carries verdicts, red flags, kill criteria, entry price, scenarios (bear/base/bull with probabilities), a value bridge and a pre-mortem answer; `decision_checks` enforces sums and presence; the PM prompt holds the rubric.
- `actions.plan` sizes positions equal-weight under `max_position_weight` (10%) and `max_sector_weight` (30%), Overweight at half size; the committee ranks and allocates across reviewed stocks and never overrides a Sell.
- The agents' statement tools fetch annual and quarterly statements (`get_income_statement(freq="quarterly")`); the pipeline's own `market_data.fundamentals` fetches annual statements only.
- The daily triggers (branch `daily-triggers`) re-review names on entry price, reached value, freefall, earnings and ±15% moves, with kill criteria shown.

## Design

### 1. Evidence metrics in code

New functions in `value_metrics.py`, None-tolerant, with per-year inputs plus up to eight quarters of `quarters: list[dict]` (same `YEAR_FIELDS` keys, newest first) that the Yahoo vendor supplies from `quarterly_income_stmt` / `quarterly_balance_sheet` / `quarterly_cashflow`:

- `record(years)` → `{roic_min_5y, roic_years_above_20, revenue_cagr_10y, revenue_cagr_5y, fcf_cagr_5y, reinvestment_rate, sustainable_growth, years_available}`.
- `inflection(years, quarters)` → `{ttm_revenue, ttm_ebit, ttm_vs_5y_revenue, ttm_vs_5y_ebit, revenue_acceleration (yoy growth of the latest quarter minus yoy growth four quarters earlier), ebit_margin_change_4q, incremental_roic (Δ EBIT after tax over the last 8 quarters ÷ Δ invested capital), quarters_available}`.
- `run_rate_value(ttm_owner_earnings, ttm_ebit, net_debt, shares, price, g3: float, rate, decay_to: float)` → per-share value where earnings grow at `g3` for three years then at `decay_to` in perpetuity (two-stage), with the same two methods as the worksheet, returned as `{base, implied_g3, methods}`; `implied_g3` is the three-year growth the price assumes with the decay fixed.

`render_report` gains a **Record and inflection** section with those numbers and two plain-English lines: `Record: ROIC ≥ 20% in 5/5 years, revenue 11.2%/yr over 10 years, reinvesting 62% at those returns → sustainable growth 14%.` and `Inflection: TTM EBIT is 2.4× the 5-year average; revenue growth accelerated from 8% to 31% y/y over four quarters; incremental ROIC 38%.` plus the run-rate valuation at g3 = the evidenced growth, decaying to the base-rate cap.

The screen adds columns `revenue_cagr_10y, sustainable_growth, ttm_vs_5y_ebit, revenue_acceleration, lane_hint` (see §3). The screen's fundamentals fetch gains quarterly statements (one extra Yahoo call per ticker, cached with the record; `CACHE_SCHEMA` bump).

### 2. Lane rules (the Portfolio Manager's rubric, enforced in code where numeric)

`PortfolioDecision` gains `lane: Literal["value", "compounder", "inflection"]` (required in the prompt; default "value" in the schema for old decisions) and `lane_evidence: list[str]` (the numbers the lane rests on, quoted).

- **Value lane**: unchanged.
- **Compounder lane** *(my calls on thresholds)*: Buy when all three verdicts pass, no red flag, ROIC ≥ 20% in every one of the last five years, 10-year revenue CAGR ≥ 8% (or 5-year when ten are unavailable, flagged), sustainable growth ≥ 10%, `implied_growth` (reverse DCF) ≤ sustainable growth, expected return ≥ 12%/yr with reversion set to 0 or lower, upside/downside ≥ 1.5, and price ≤ 1.15 × the worksheet base. Overweight at expected return ≥ 10% and price ≤ 1.25 × base.
- **Inflection lane** *(my calls)*: Buy when the business and management verdicts pass, no red flag, `ttm_vs_5y_ebit ≥ 1.5`, `revenue_acceleration ≥ 10 points`, incremental ROIC ≥ 20%, the news analyst's demand evidence is marked verified (customer capex or backlog or pricing, with a source), the run-rate value at the evidenced g3 decaying to the base-rate cap gives a weighted margin of safety ≥ 20% and upside/downside ≥ 2, and the price is not in FREEFALL. Scenario probabilities must give the bear case (growth stops now) at least 30%. Kill criteria must include a demand indicator (orders, backlog, customer capex) and the daily triggers watch it.
- `decision_checks` adds the numeric parts of each lane's bar from the worksheet's Record/inflection lines (parsed like the base and the price) so a lane claim without the numbers is a problem sent back once.

The PM prompt describes the three lanes, tells the manager to pick the lane the evidence supports (value first; compounder when the record qualifies; inflection only with verified demand evidence), and to state the lane's numbers under `**Lane**:` in the rendered decision (`Lane: compounder — ROIC ≥ 20% 5/5 years; revenue 11.2%/yr 10y; sustainable growth 14%; implied growth 6%; expected return 13%; price 1.08× base`). `parse.py` reads `lane` and `lane_evidence`; `results.json` and `Decision` carry them.

### 3. Discovery of inflection candidates

- Config `lanes.themes: [...]` — user-named themes (free text) *(my call: a list in config.yaml, empty by default)* and `lanes.tickers: [...]` for named candidates outside the screen.
- A monthly **theme scan** step in `run-all`, before the review, one quick-tier call per theme: "Where is demand inflecting for this theme, and which listed suppliers with pricing power benefit? Name up to 8 tickers with one line of evidence each; label unverified claims." The output goes to `reports/<date>/themes.md`; its tickers are screened (the fundamentals fetch and `evaluate`) and those with `ttm_vs_5y_ebit ≥ 1.3` or `revenue_acceleration ≥ 10` join the candidate list with `lane_hint = "inflection"`, up to `lanes.inflection_candidates` (default 3) per review, in addition to the Magic Formula candidates.
- The screen's `lane_hint` column: `compounder` when `record` passes the compounder record bar, `inflection` when the inflection numbers pass, else blank; the Screen page shows it and filters on it. `top_candidates` adds up to `lanes.compounder_candidates` (default 2) of the best compounder-hinted names by expected return that are not already candidates.

### 3b. Dislocation scan: quality names knocked down by a scare

The user's second example: "when SaaS company stocks fell on AI worries, but they have now mostly recovered", and "see opportunities in these big movements of stocks". That is the value lane's best source of entries and the pipeline does not look for it today; it only ranks what is cheap now on five-year averages. A **dislocation** is a quality business whose price fell hard while its numbers did not.

- Metrics in code, from the screen's cached closes and the statements already fetched: `drawdown_from_high` (price vs the 52-week high), `move_1m` and `move_3m`, and `fundamentals_intact` = TTM revenue and EBIT at or above the five-year average (reusing `inflection`'s TTM figures) with no accounting flag.
- Screen columns `drawdown_from_high, move_1m, move_3m, fundamentals_intact` and a `dislocation` hint when a name passes the quality filters, is ≥ 25% below its 52-week high or fell ≥ 20% in a month *(my calls)*, and `fundamentals_intact` is true.
- **Sector-wide scare detection** *(my call)*: when three or more names in the same Yahoo industry carry the hint in the same screen, the hint is tagged `sector scare` and the themes report lists the industry with its members, so a sentiment-driven selloff (the AI-worry SaaS case) is visible as one event rather than a dozen tickers.
- Candidates: up to `lanes.dislocation_candidates` (default 3) of the best dislocation-hinted names by expected return join the review as value-lane candidates with the hint; the news analyst is briefed to classify the cause of the fall (company-specific deterioration, sector-wide sentiment, macro) with sources, and the PM must say which in the thesis; a company-specific cause with intact numbers still needs the value lane's bar, a sector-wide scare with intact numbers is the classic entry.
- The weekly watch gains a **New ideas** section: names that newly carry a dislocation, compounder or inflection hint since the last watch, so a scare that starts mid-month is seen that Saturday, and the daily BIG MOVE trigger already re-reviews watched names that fall 15%.

Timing honesty: the SaaS names "have now mostly recovered", so the scan is for the next scare, not this one; the watch's weekly cadence plus the daily trigger is what makes it catchable.

### 4. Sizing and the committee

- `config.sizing` gains `lane_caps: {compounder: 0.30, inflection: 0.20}` (share of total portfolio value) and `lane_size_factor: {compounder: 0.5, inflection: 0.5}` *(my calls)*: a lane Buy is sized at half the target weight, Overweight at a quarter; `actions.plan` enforces the lane caps like the sector cap and records the lane on the `Action`.
- ACTIONS shows the lane in the New buys and Candidates tables; holdings carry `lane` on the `Position`.
- The committee payload includes each stock's lane and lane evidence; its prompt says the lanes' caps and that inflection positions are the first to be cut when evidence fades.
- The weekly watch and the daily check list inflection holdings' demand kill criteria first.

## Testing

- Unit tests for `record`, `inflection`, `run_rate_value` (two-stage maths against a hand computation; implied g3), with None-tolerance and fewer-than-needed quarters.
- Yahoo quarterly mapping with synthetic frames; the screen's columns and `lane_hint`.
- Schema/prompt/parse round-trip for `lane` and `lane_evidence`; `decision_checks` lane bars (a compounder claim without the ROIC record → problem).
- `actions.plan` lane caps and size factors; the committee payload carries lanes.
- Theme scan with a stub model: tickers extracted, screened, hinted, capped.
- Real run after merge: one compounder-looking name (ADBE) and one inflection-looking name (CLS) through `review --tickers`, read end to end.

## Rollout

1. Build in a worktree after the daily-triggers merge; merge; restart the website.
2. Real-run ADBE and CLS; read the lane lines and bridges.
3. The November review is the first with lanes; the user names any themes in `config.yaml` beforehand.
