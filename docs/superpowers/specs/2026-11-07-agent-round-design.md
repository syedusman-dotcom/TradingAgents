# The independent agent round — design

**Date:** 2026-11-07 (Saturday 09:00 CET)
**Status:** decided by Claude under the user's delegation of 2026-11-07 08:28 ("Any you decide based on the goal"); item 4 of the decisions in the weekend ledger. Implements section 6 of `docs/superpowers/specs/2026-11-08-best-process-design.md`.
**Evidence:** `~/Desktop/ValueTrading/reports/Reliable LLM equity analysis.md` (practices 1, 4, 5, 7) and the Base Rate Book (Mauboussin & Callahan, Credit Suisse, September 2016; sales growth 1950–2015, top 1,000 global companies by market capitalisation, inflation-adjusted to 2015 dollars, dead companies included).

## Intent

The review's eleven agents share one context and talk in a fixed order, so their probabilities move together and the Portfolio Manager's value sits where the price is (median margin of safety −5% over 153 reviews). The published evidence says what helps a process like this is independence and scoring, not more debate: average independent views instead of exposing them to each other, keep the valuer away from the price, give the bear its own evidence, let the judge read the cases in random order, start scenarios from base rates, and log every probability for Brier scoring. This design adds an **independent arm** of the review next to the **current arm**, assigns each ticker review to one arm at random, records the arm in the results, and lets the calibration page compare the two by Brier skill under a rule fixed here. Nothing about rating bars, sizing or candidates changes.

## Goals, non-goals, anti-goals

- Goals: (1) the independent arm exists behind one config key and runs in the scheduled reviews without any manual step; (2) every result row says which arm produced it and carries the new fields the comparison needs; (3) the calibration page and the `calibration` command print the per-arm comparison and the fixed adoption rule; (4) the current arm's prompts, outputs and parsing are byte-for-byte what they were, except the three changes that apply to both arms (exact base-rate table, kill criteria in the portfolio context, kill-criterion check).
- Non-goals: a second model family for the judge; tool-using bull/bear subgraphs; changing the committee; any rating bar; scoring sells.
- Anti-goals: an arm that cannot be told apart in the data afterwards; a prompt change that breaks `parse.scenarios` (the Brier rows would silently vanish); a Blind Valuer that sees the price through any route.

## Constraints

- Nothing trades; no agent review is started this weekend (first live run: Monday 2026-11-09 07:30, then Saturday 2026-11-14 09:00).
- The arm is chosen per ticker and date, deterministically, so a resumed run gets the same arm.
- Checkpoint signatures (`_run_signature`) may change; that is acceptable (no checkpoints are kept across runs).
- Added cost per independent-arm review: one extra structured LLM call (Blind Valuer) and two vendor fetches for the bear; no extra tool rounds.
- Tests never touch the network. `uv run pytest -q` and `uv run ruff check .` green; line length 100.
- The public fork: no portfolio data in any committed file.

## The design

### 1. The arm

- `value_pipeline/config.py` `DEFAULTS["review"]["agent_round"] = "current"`; allowed values `"current"`, `"independent"`, `"ab"`; `validate_config` rejects anything else. `config.yaml` ships `agent_round: ab` with a comment pointing to this spec.
- `value_pipeline/review.py` `arm_for(ticker: str, date: str, setting: str) -> str`: `"current"` → `"current"`; `"independent"` → `"independent"`; `"ab"` → `"independent"` when `int(hashlib.sha256(f"{ticker}:{date}".encode()).hexdigest(), 16) % 2 == 0`, else `"current"`.
- `review.run_agents` passes `agent_round=arm` into `ta.propagate(...)`; `TradingAgentsGraph.propagate` gains `agent_round: str = "current"` and `create_initial_state` sets the new `AgentState` field `agent_round: str`. Every node reads `state.get("agent_round", "current")`; a missing field means the current arm, so every existing test and caller is unchanged.
- The results row gains `"agent_round"`. `fake_run` (dry run) records the arm too.

### 2. Price-blind valuation (independent arm only)

- New node **"Blind Valuer"**, `tradingagents/agents/analysts/blind_valuer.py`, `create_blind_valuer(llm)`. It starts from START in parallel with the analysts and joins the same `first_steps` edge into "Bull Researcher". In the current arm it returns `{"blind_valuation": ""}` without calling the model.
- It makes no tool calls. Its input is `blind_fundamentals_text(ticker, date)` (new, `tradingagents/dataflows/blind.py`), built from the same vendor data as `get_value_metrics` but rendering only price-free facts: company name and sector, the statement currency and the quote unit note, and a per-year table of revenue, gross profit, EBIT, net income, operating cash flow, capex, free cash flow, dividends paid, shares outstanding, net debt, ROIC including goodwill, and margins, for every available year (**no buyback cash and no repurchase lines**: cash spent divided by shares retired is the price paid, so the share count is shown but not the money); then the trailing growth rates the worksheet already computes (revenue and FCF CAGR), the reinvestment rate and the sustainable growth, the accounting checks (F-Score, Altman Z without the market-cap term is not possible, so Z is omitted; Beneish M), the current discount rate, and the base-rate block of section 5 for the company's size. It must not contain: the price, market capitalisation, enterprise value, any yield, P/E, P/B, EV/EBIT, the 52-week range, the consensus target, the formula value, the worksheet base, buyback cash or buyback prices, or the pipeline note. The company name stays: the valuer cannot reason about a business it cannot identify, and what the model may recall from training is an accepted limit; the test is independence from today's price and from the worksheet. Test: the rendered text for a fixture company contains none of the strings `price`, `Price`, `market cap`, `enterprise value`, `EV/`, `P/E`, `P/B`, `yield`, `52-week`, `target`, `formula value`, `worksheet`, `margin of safety`, `buyback`, `repurchase`, and the fixture's price formatted with two decimals; and does contain `Revenue`, `Free cash flow` and `Shares outstanding`.
- Structured output `BlindValuation` (`schemas.py`): `value_per_share: float`, `low: float`, `high: float`, `growth_5y: float` (the revenue or owner-earnings CAGR assumed for five years, as a fraction), `discount_rate: float`, `method: str` (≤ 400 characters), `reasoning: str`. Rendered by `render_blind_valuation` as markdown with a first line `**Value per share**: {value:.2f} (range {low:.2f}–{high:.2f}) in the quote unit`, then `**Growth assumed**`, `**Discount rate**`, `**Method**`, `**Reasoning**`. Saved as `1_analysts/blind_valuation.md` (`reporting._SECTIONS`). `parse.blind_value(text) -> float | None` reads the first line; results row `"blind_value"`.
- **Per-share basis.** The valuer works in the statement currency with the statement share count (`statement_shares`); the code converts `value_per_share`, `low` and `high` to the quote unit afterwards with the same price-independent cross rate the worksheet uses (`cross`, F1). When the worksheet has no cross rate, or the quoted and statement share bases differ (ADRs, `same_basis` false), the valuer is skipped: `blind_valuation` is `skipped: <reason>`, `blind_value` is `None`, and the Portfolio Manager's block reads `No price-blind valuation (<reason>).` The valuer's text says which currency its per-share figures are in and never mentions the quote.
- The prompt: the valuer is told it is valuing the business from its accounts alone, that it will not be shown and must not guess the price, to start growth from the base-rate median for the company's size and to justify any higher rate, to use the discount rate given, and to give a range at least ±20% wide. It is told the per-share figures are in the statement currency.
- The Portfolio Manager in the independent arm gets, after the worksheet block: `**Price-blind valuation** (made without the price): {value} per share, range {low}–{high}, growth {g}, discount {d}. The worksheet base is {base}; the price is {price}. Treat the blind value and the worksheet base as two independent estimates: your base scenario value must lie between them or you must say which one you reject and why.`

### 3. Independent cases and a randomised judge (independent arm only)

- **Bull and Bear openings.** In the independent arm both researchers write an opening from the analyst reports without the other's text: the prompt shows the opening marker from `opponent_argument_or_opening` and omits the debate history. Routing is unchanged (Bull, then Bear, then Research Manager); `should_continue_debate` sends the independent arm to the Research Manager as soon as `count >= 2`, whatever `max_debate_rounds` says.
- **The bear's own evidence.** In the independent arm the bear's prompt carries an **Adverse evidence pack** fetched before the call by `adverse_pack(ticker, date)` (new, `tradingagents/agents/researchers/adverse.py`): the insider transactions text and the news for the 90 days to the review date, each through the same vendor routing the tools use, each truncated to 6,000 characters, each replaced by `not retrieved: {reason}` on failure. The bull does not see the pack. The bear is told the pack is its own evidence and that it must cite from it or say it found nothing adverse.
- **The Research Manager** in the independent arm reads `bull_history` and `bear_history` as two separate cases, each under its own heading (`**The Bull case**`, `**The Bear case**`), in an order drawn from `random.Random(f"{ticker}:{date}:research").random() < 0.5` (bear first when true). It records `judge_order` in the state (`"bear-first"` or `"bull-first"`); results row `"judge_order"`. The current arm keeps the interleaved history exactly as today.
- **Risk analysts.** In the independent arm Aggressive, Conservative and Neutral each speak once without the others' text (opening marker, no history); `should_continue_risk_analysis` sends the independent arm to "Pre-mortem" as soon as `count >= 3`, whatever `max_risk_discuss_rounds` says. The Portfolio Manager in the independent arm reads the three statements under their own headings in an order drawn from `random.Random(f"{ticker}:{date}:risk")` (a shuffle of the three), instead of the interleaved history.

### 4. Base-rate scenarios

- **Exact table (both arms).** `value_metrics.BASE_RATE_COHORTS` is replaced by the book's Exhibit 4 (ten-year real sales CAGR by base-year sales, 2015 dollars; cumulative shares computed from the 5-point bands):

| Sales (2015 $) | 10y median | ≥0% | ≥5% | ≥10% | ≥15% | ≥20% |
|---|---|---|---|---|---|---|
| < $325M | 9.8% | 95% | 78% | 49% | 29% | 18% |
| $325–700M | 6.7% | 81% | 57% | 25% | 10% | 5% |
| $700M–1.25B | 5.7% | 87% | 55% | 23% | 8% | 4% |
| $1.25–2B | 5.1% | 87% | 51% | 19% | 7% | 3% |
| $2–3B | 4.5% | 85% | 45% | 16% | 6% | 2% |
| $3–4.5B | 4.1% | 81% | 41% | 13% | 4% | 1% |
| $4.5–7B | 3.7% | 79% | 38% | 12% | 4% | 1% |
| $7–12B | 3.2% | 75% | 35% | 10% | 3% | 1% |
| $12–25B | 2.7% | 72% | 30% | 10% | 3% | 1% |
| $25–50B | 1.8% | 65% | 28% | 7% | 1% | 0% |
| > $50B | 1.1% | 59% | 21% | 4% | 1% | 0% |

  Revenue in today's dollars is deflated by `DOLLARS_2015 = 1.35` (US CPI 2015 → 2026, rounded) before the lookup. `growth_base_rates` keeps its signature and return shape (`cohort`, `share_achieving` with keys 0.0, 0.05, 0.10, 0.15, 0.20, `cap`) and gains `"median"`. **`cap` becomes the rate that `BASE_RATE_CAP_SHARE` (25%) of the cohort reached, interpolated linearly between the 5-point band edges** (the 75th percentile of ten-year real growth), instead of the highest tabulated key at or above 25%; without interpolation the > $50B cohort would get 0% and the < $325M cohort 15%. Resulting caps: < $325M 16.8%, $325–700M 10.0%, $700M–1.25B 9.7%, $1.25–2B 9.1%, $2–3B 8.4%, $3–4.5B 7.9%, $4.5–7B 7.5%, $7–12B 7.0%, $12–25B 6.3%, $25–50B 5.7%, > $50B 4.5% (old table: < $1B 10%, $1–5B 7%, $5–25B 7%, > $25B 5%). Test: those eleven caps to one decimal. `base_rate_note` keeps working with the new keys. Ruling (in the ledger): a data correction from the source table; the caps move by at most two points except below $325M of sales, which the screen's size floors make rare; applied to both arms.
- **The block (independent arm only).** `base_rate_block(revenue_usd) -> str` (in `value_metrics.py`) renders: the cohort, the ten-year median, the five shares, the sentence "23% of companies shrank in real terms over three years and 20% over five", and "the return-on-capital spread between the top and bottom quintile halves within ten years (18 to 9 points); operating margin's five-year correlation is 0.72". The Portfolio Manager's prompt in the independent arm carries it with the rule: *Start the base scenario's growth at the cohort median (add about two points for inflation). The bull scenario may not assume a rate that fewer than 10% of the cohort reached unless the basis names the reason. The bear scenario's probability may not be below the cohort's share that shrank. Name the base rate used in each scenario's basis.* The Blind Valuer gets the same block.
- The `Scenario` schema and `render_pm_decision` are unchanged, so `parse.scenarios` keeps matching.

### 5. Kill-criterion check (both arms, holdings only)

- The agents' pydantic `Position` (`tradingagents/portfolio.py`) gains `kill_criteria: list[str] = []`; `value_pipeline/portfolio.py` `to_context` passes each holding's stored criteria; `PortfolioContext.render` writes them under the holding as `Kill criteria on record: 1. … 2. …`. This text reaches every agent that reads the portfolio context, in both arms; it is the third change both arms share (with the exact base-rate table and the check itself).
- `PortfolioDecision` gains `kill_criteria_check: list[KillCheck] = []`, `KillCheck(criterion: str, status: Literal["met", "not met", "unknown"], note: str)`. The prompt: *If kill criteria are on record for this holding, check each one: met, not met or unknown. A met criterion means Underweight or Sell unless the note is a rebuttal that says why the thesis survives; a rebuttal holds for one review cycle only.* `render_pm_decision` writes a `**Kill Criteria Check**` section (`n. criterion — status: note`), only when the list is non-empty; `parse.kill_criteria_check` reads it; results row `"kill_criteria_check"`. `decision_checks` adds a problem when a check says `met` and the rating is above Underweight and the note is empty.
- `watch` and ACTIONS are unchanged; the user still checks criteria against the news weekly.

### 6. Measurement

- Results row additions: `agent_round`, `blind_value`, `judge_order`, `kill_criteria_check`, `missing_data_lines` (the count of matches of `not provided|not retrieved|returned nothing|could not retrieve|unavailable` in the decision text, case-insensitive).
- `calibration.brier(rs, horizon, arm=None)` filters by `agent_round` when given; **rows without the field (every review before 2026-11-09) are excluded from any per-arm figure**, so the comparison never mixes the arms with the old base-rate table or an earlier period. The per-arm scores apply the same row selection and de-duplication as the overall `brier` does today. The overall Brier (no arm) keeps counting every row. `score()` gains `"arms"`: for each arm, `n`, the Brier skills at 365 and 180 days, the median margin of safety, the median `missing_data_lines`, the rating mix, and for the independent arm the median of `|blind_value − price| / price` and of `|intrinsic_value − price| / price`.
- The calibration page and the `calibration` command print the arm table and this rule, fixed now:
  **Adoption rule.** Counting only rows that carry an arm, with at least 30 resolved decisions per arm at 180 days (early read) or 365 days (verdict): the independent arm becomes the default (`agent_round: independent`) if both its bear and bull Brier skills are at or above the current arm's and its median `missing_data_lines` is not higher; it is retired (`agent_round: current`) if both skills are lower by 0.05 or more; otherwise the A/B continues. The reading is made by Claude in the Saturday review after the count is reached and recorded in the ledger before any config change.
- The HTML page shows the arm table under the Brier section; `tests/test_calibration.py` covers the per-arm scores and the rule text.

### 7. Documentation
- `value_pipeline/README.md`: `review.agent_round`, the arm assignment, the new result fields, the adoption rule.
- `CHANGELOG.md`: one entry.

## What is left to the builder
- `RunResult` needs fields for `blind_value`, `judge_order` and `agent_round` (they live in the final state).
- Where exactly in `value_metrics.py` the per-year rows are assembled, so `blind_fundamentals_text` reuses them rather than re-deriving them.
- The precise wording of the new prompt paragraphs, within the rules above, and the i18n coverage test's expectations (`tests/test_i18n_coverage.py`).
- Whether the Blind Valuer node needs its own `_run_signature` consideration (it should not).
