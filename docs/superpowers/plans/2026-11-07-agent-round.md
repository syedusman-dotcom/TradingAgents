# The independent agent round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The review runs in one of two arms chosen per ticker (`current`, `independent`), records the arm and the new fields in every result row, and the calibration page compares the arms by Brier skill under the rule in the spec.

**Architecture:** The arm travels in the graph state (`agent_round`), so one graph serves both arms and every node branches on `state.get("agent_round", "current")`. The independent arm adds one node (Blind Valuer), prompt variants in six existing nodes, two routing short-cuts and a pre-fetched evidence pack; both arms get the exact base-rate table, kill criteria in the portfolio context and a kill-criterion check. `value_pipeline` chooses the arm, records it, and scores per arm.

**Tech Stack:** Python 3, LangGraph, pydantic, pandas, pytest. Tests `uv run pytest -q`, lint `uv run ruff check .` (line length 100).

**Spec:** `docs/superpowers/specs/2026-11-07-agent-round-design.md`

## Global Constraints

- Nothing trades; no agent review is started while building (`review`, `run-all` never run; tests use scripted models).
- Tests never touch the network (monkeypatch vendors and `yf.Ticker`).
- A state without `agent_round` behaves as the current arm; the current arm's prompts, rendered decisions and parsing are unchanged except: the base-rate table (Task 1), kill criteria in the portfolio context and the kill-criterion check (Task 3).
- `render_pm_decision`'s **Scenarios** lines keep the format `parse.scenarios` matches.
- Exact values: `DOLLARS_2015 = 1.35`; `BASE_RATE_CAP_SHARE = 0.25`; adverse-pack window 90 days, 6,000 characters per source; RNG seeds `f"{ticker}:{date}:research"` and `f"{ticker}:{date}:risk"`; arm hash `sha256(f"{ticker}:{date}")`, even → `independent`; `MIN_BRIER_ROWS = 30`; `missing_data_lines` pattern `not provided|not retrieved|returned nothing|could not retrieve|unavailable` (case-insensitive).
- Every commit message ends with the two trailer lines the dispatch gives. `git add` only the files named in the task.

## Review Focus

1. A results row written before this change (no `agent_round`, no `blind_value`) read by `calibration.rows`, `score` and the page: nothing raises; the row counts in the overall Brier and in no arm. (Task 7)
2. An independent-arm decision whose Blind Valuer was skipped (ADR): the Portfolio Manager prompt says "No price-blind valuation", the results row has `blind_value: None`, and the page's `|blind − price|/price` median ignores it. (Tasks 4, 6, 7)
3. A rendered independent-arm decision round-trips through `parse.scenarios`, `parse.kill_criteria_check` and `parse.blind_value` exactly as rendered. (Tasks 3, 4)
4. A checkpoint resume of an `ab` run assigns the same arm to the same ticker and date. (Task 2)
5. A current-arm state through the Research Manager and the Portfolio Manager builds prompts containing `Debate History` and no `Price-blind`, `Base rates` or `The Bull case` text; the risk history is interleaved. (Tasks 5, 6)

---

### Task 1: The exact base-rate table

**Files:** Modify `tradingagents/dataflows/value_metrics.py` (`BASE_RATE_COHORTS`, `growth_base_rates`, `base_rate_note`; add `DOLLARS_2015`, `base_rate_block`). Test: `tests/test_value_metrics.py` (extend).

**Interfaces:**
- `DOLLARS_2015: float = 1.35`.
- `BASE_RATE_COHORTS`: tuple of `(label, upper_bound_2015_usd | None, median, {0.0: s0, 0.05: s5, 0.10: s10, 0.15: s15, 0.20: s20})` with the eleven rows of the spec's table (bounds 325e6, 700e6, 1.25e9, 2e9, 3e9, 4.5e9, 7e9, 12e9, 25e9, 50e9, None; medians 0.098, 0.067, 0.057, 0.051, 0.045, 0.041, 0.037, 0.032, 0.027, 0.018, 0.011; shares as in the spec's table as fractions).
- `growth_base_rates(revenue: float | None) -> dict | None`: deflates `revenue / DOLLARS_2015`, picks the cohort, returns `{"cohort": label, "median": m, "share_achieving": {0.0.., 0.20..}, "cap": c}` where `cap` is the growth at which the cumulative share crosses `BASE_RATE_CAP_SHARE`, linearly interpolated between the band edges (cumulative share 1.0 at −infinity is not used: when `s0 < 0.25` the cap is 0.0; when `s20 >= 0.25` the cap is 0.20).
- `base_rate_block(revenue_usd: float | None) -> str | None`: the spec's section 4 block text (cohort, ten-year median, the five shares as percentages, the "23% … three years and 20% … five" sentence, the return-on-capital and margin sentences), `None` when `revenue_usd` is missing or non-positive.
- `base_rate_note` unchanged in signature and sentence shape; it uses the new keys.

Steps:
- [ ] Tests first: `growth_base_rates(1.35 * 10e9)` → cohort `$7–12B`, median 0.032, cap 0.070 ± 0.0005; the eleven caps of the spec to three decimals (`16.8, 10.0, 9.7, 9.1, 8.4, 7.9, 7.5, 7.0, 6.3, 5.7, 4.5` percent at representative revenues); `growth_base_rates(0)` and `(None)` → `None`; `base_rate_note(0.12, 1.35 * 1.5e9)` mentions `$1.25–2B` and `19%` and `10%`; `base_rate_block(1.35 * 1.5e9)` contains `$1.25–2B`, `5.1%`, `19%`, `23% of companies shrank`, `18 to 9 points`, `0.72`; `base_rate_block(None)` → `None`. Existing tests that assert old cohort labels (`<$1B`, `$1–5B`, `$5–25B`, `>$25B`) or old caps are updated to the new table.
- [ ] Implement.
- [ ] `uv run pytest -q tests/test_value_metrics.py && uv run ruff check .`; commit `feat: exact Base Rate Book sales-growth cohorts and the base-rate block`.

### Task 2: The arm

**Files:** Modify `value_pipeline/config.py` (`DEFAULTS["review"]["agent_round"]`, `validate_config`), `value_pipeline/config.yaml`, `tradingagents/agents/state.py`, `tradingagents/graph/propagation.py`, `tradingagents/graph/trading_graph.py` (`propagate`), `tradingagents/agents/context.py` (add `arm_of`), `value_pipeline/review.py` (`arm_for`, `missing_data_lines`, `RunResult`, `run_agents`, `fake_run`, the results row), `value_pipeline/README.md`. Tests: `tests/test_config.py` or the config tests that exist, `tests/test_value_pipeline.py` (results rows), `tests/test_graph_end_to_end.py` (state field present).

**Interfaces:**
- `DEFAULTS["review"]["agent_round"] = "current"`; `validate_config` raises `ValueError` unless the value is one of `("current", "independent", "ab")`. `config.yaml`: `agent_round: ab` with a comment naming the spec.
- `AgentState.agent_round: str`, `AgentState.blind_valuation: str`, `AgentState.base_rates: str`, `AgentState.judge_order: str` (all initialised: `agent_round` from the argument, the rest `""`).
- `TradingAgentsGraph.propagate(company_name, trade_date, asset_type="stock", portfolio=None, context_note="", agent_round="current")`; `create_initial_state(..., agent_round: str = "current")`.
- `context.arm_of(state) -> str`: `state.get("agent_round") or "current"`.
- `review.arm_for(ticker: str, date: str, setting: str) -> str` as in the spec; `review.missing_data_lines(text: str) -> int`.
- `RunResult` gains `agent_round: str = "current"`, `blind_value: float | None = None`, `judge_order: str | None = None` (filled from the final state by `run_agents`; `blind_value` via `parse.blind_value` once Task 4 adds it, until then `None`).
- Results row gains `"agent_round"`, `"missing_data_lines"`, `"blind_value"`, `"judge_order"`. `fake_run` records the arm from `arm_for`.

Steps:
- [ ] Tests first: `arm_for("AAPL", "2026-11-09", "current") == "current"`, `("…", "independent") == "independent"`, `"ab"` is deterministic across calls and gives between 40% and 60% `independent` over 1,000 tickers `T0..T999`; `validate_config` rejects `agent_round: "both"`; the shipped yaml loads and validates; `missing_data_lines("News not provided. Altman Z not retrieved.") == 2`; a results row from a run with `agent_round: ab` carries `agent_round` in `("current", "independent")` and `missing_data_lines` as an int; the end-to-end scripted run's final state has `agent_round == "current"` by default and `"independent"` when passed.
- [ ] Implement; README: the key, the assignment, the new row fields.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: review.agent_round assigns each review to an arm and records it`.

### Task 3: Kill criteria in the portfolio context and the kill-criterion check

**Files:** Modify `tradingagents/portfolio.py` (`Position.kill_criteria`, `PortfolioContext.render`), `value_pipeline/portfolio.py` (`to_context`), `tradingagents/agents/schemas.py` (`KillCheck`, `PortfolioDecision.kill_criteria_check`, `render_pm_decision`), `tradingagents/agents/managers/portfolio_manager.py` (prompt paragraph, both arms), `tradingagents/agents/decision_math.py` (`decision_checks`), `value_pipeline/parse.py` (`kill_criteria_check`), `value_pipeline/review.py` (row key). Tests: `tests/test_portfolio_context.py`, `tests/test_structured_agents.py`, `tests/test_decision_math.py`, `tests/test_value_pipeline.py`.

**Interfaces:**
- `tradingagents.portfolio.Position.kill_criteria: list[str] = []`; `PortfolioContext.render` writes, under a holding with criteria, `Kill criteria on record: 1. <c1> 2. <c2>` on one line.
- `KillCheck(BaseModel)`: `criterion: str`, `status: Literal["met", "not met", "unknown"]`, `note: str = ""`. `PortfolioDecision.kill_criteria_check: list[KillCheck] = []`.
- `render_pm_decision` writes, only when the list is non-empty, a section `**Kill Criteria Check**:` with lines `{n}. {criterion} — {status}: {note}` (an empty note renders as `{n}. {criterion} — {status}`), placed after **Kill Criteria**.
- `parse.kill_criteria_check(text) -> list[dict]` with keys `criterion`, `status`, `note`; `[]` when the section is absent.
- `decision_checks` adds the problem `"Kill criterion met without a rebuttal: <criterion>"` when a check is `met`, the rating is `Buy`, `Overweight` or `Hold`, and the note is empty.
- The prompt paragraph (both arms): *If kill criteria are on record for this holding, check each one: met, not met or unknown. A met criterion means Underweight or Sell unless the note is a rebuttal that says why the thesis survives; a rebuttal holds for one review cycle only.*

Steps:
- [ ] Tests first: `to_context` carries a holding's criteria and `render` shows the `Kill criteria on record:` line, and no such line for a holding without criteria; a `PortfolioDecision` with two checks renders the section and `parse.kill_criteria_check` returns both with status and note; a decision without checks renders no section and parses to `[]`; `decision_checks` flags a met check with an empty note at Hold and not at Underweight, and not when a note is present; the results row has `kill_criteria_check`; `tests/test_structured_agent_prompts.py` asserts the new paragraph is in the Portfolio Manager prompt.
- [ ] Implement.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: kill criteria reach the agents and the Portfolio Manager checks them`.

### Task 4: The Blind Valuer

**Files:** Create `tradingagents/dataflows/blind.py`, `tradingagents/agents/analysts/blind_valuer.py`, `tests/test_blind_valuer.py`. Modify `tradingagents/dataflows/vendors/yahoo/value_metrics.py` (factor `blind_inputs`), `tradingagents/agents/schemas.py` (`BlindValuation`, `render_blind_valuation`, `blind_values`), `tradingagents/graph/setup.py` (node and edges), `tradingagents/graph/trading_graph.py` (construct the node with the quick LLM), `tradingagents/reporting.py` (`_SECTIONS`), `value_pipeline/parse.py` (`blind_value`), `value_pipeline/review.py` (`RunResult.blind_value` from the final state). Tests also: `tests/test_graph_end_to_end.py`, `tests/test_reporting.py`, `tests/test_risk_router_path_map.py` if it enumerates nodes.

**Interfaces:**
- `yahoo.value_metrics.blind_inputs(ticker: str, as_of_date: str | None = None) -> dict`: `{"name", "sector", "years" (statement_years output), "reported" (statement currency), "quote", "cross" (float | None: the price-independent rate when converted, 1.0 when quote == reported, None otherwise), "same_basis" (bool), "statement_shares" (float | None), "revenue_usd" (float | None), "discount_rate" (float)}`. It never reads `price`, `marketCap` or the quote beyond the currency code; it reuses the module's existing fetch helpers.
- `blind.blind_fundamentals_text(inputs: dict) -> str`: the spec's price-free rendering; raises `ValueError("<reason>")` when `cross is None` or `same_basis` is false or `statement_shares` is missing.
- `schemas.BlindValuation`: `value_per_share: float`, `low: float`, `high: float`, `growth_5y: float`, `discount_rate: float`, `method: str` (max 400), `reasoning: str`. `render_blind_valuation(v: BlindValuation, cross: float) -> str`: first line `**Value per share**: {value*cross:.2f} (range {low*cross:.2f}–{high*cross:.2f}) in the quote unit`, then `**Growth assumed**: {growth_5y:.1%}`, `**Discount rate**: {discount_rate:.1%}`, `**Method**: …`, `**Reasoning**: …`. `schemas.blind_values(text: str) -> dict | None` with keys `value`, `low`, `high`, `growth`, `discount` (the quote-unit figures).
- `blind_valuer.create_blind_valuer(llm, inputs_fn=blind_inputs)`: node returning `{"blind_valuation": str, "base_rates": str}`; `base_rates = base_rate_block(inputs["revenue_usd"]) or ""` in both arms; in the current arm `blind_valuation = ""` with no LLM call; in the independent arm `blind_valuation` is the rendered valuation or `skipped: <reason>` (also when `inputs_fn` raises). Prompt per the spec, including the base-rate block and the statement currency.
- `setup.py`: node "Blind Valuer" added from START and into `first_steps`. `reporting._SECTIONS`: `("Blind Valuer", "blind_valuation.md", ("blind_valuation",))` under `1_analysts`, written only when non-empty if the existing writer skips empty sections, otherwise written as is.
- `parse.blind_value(text) -> float | None` reads the first line's value.

Steps:
- [ ] Tests first: `blind_fundamentals_text` on a fixture with price 123.45, buybacks and a 2-currency setup (cross 0.1) contains `Revenue`, `Free cash flow`, `Shares outstanding` and none of `price`, `Price`, `market cap`, `enterprise value`, `EV/`, `P/E`, `P/B`, `yield`, `52-week`, `target`, `formula value`, `worksheet`, `margin of safety`, `buyback`, `repurchase`, `123.45`; it raises for `same_basis=False` and for `cross=None`; `render_blind_valuation` + `blind_values` + `parse.blind_value` round-trip (value 10.0, cross 0.1 → `1.00`); the node in the current arm returns `blind_valuation == ""` and never calls the LLM (a scripted LLM that raises); in the independent arm with a scripted structured output it returns the rendered text and `base_rates` non-empty; with `inputs_fn` raising it returns `skipped: …`; the end-to-end scripted run writes `1_analysts/blind_valuation.md` in the independent arm; `blind_inputs` (monkeypatched `yf.Ticker`) returns the keys above and `cross == 1.0` for a USD/USD company.
- [ ] Implement.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: price-blind valuer in the independent arm`.

### Task 5: Independent cases, the adverse pack and the randomised judges

**Files:** Create `tradingagents/agents/researchers/adverse.py`, `tests/test_independent_debate.py`. Modify `tradingagents/agents/researchers/bull_researcher.py`, `bear_researcher.py`, `tradingagents/agents/managers/research_manager.py`, `tradingagents/agents/risk_mgmt/aggressive_debator.py`, `conservative_debator.py`, `neutral_debator.py`, `tradingagents/agents/managers/portfolio_manager.py` (risk section only), `tradingagents/graph/conditional_logic.py`, `tradingagents/agents/context.py` (`judge_order`, `shuffled`), `value_pipeline/review.py` (`RunResult.judge_order` from the state). Tests also: `tests/test_debate_opening.py`, `tests/test_risk_router_path_map.py`, `tests/test_structured_agent_prompts.py`.

**Interfaces:**
- `adverse.adverse_pack(ticker: str, date: str, fetch_news=None, fetch_insiders=None) -> str`: `**Adverse evidence pack**` with `## Insider transactions` and `## News, last 90 days` sections, each the routed tool text truncated to 6,000 characters, or `not retrieved: <reason>` on any exception; the defaults route through the same vendor functions the tools use (`get_insider_transactions`, `get_news` with start = date − 90 days, end = date).
- `context.judge_order(ticker, date, kind: str) -> list[str]`: for `kind == "research"` returns `["bear", "bull"]` when `random.Random(f"{ticker}:{date}:research").random() < 0.5` else `["bull", "bear"]`; for `kind == "risk"` returns `random.Random(f"{ticker}:{date}:risk").sample(["aggressive", "conservative", "neutral"], 3)`.
- Bull and Bear (independent arm): the prompt shows the opening marker, no history; the Bear's prompt also carries the adverse pack (fetched inside the node via an injectable `pack_fn`, default `adverse_pack`), with the sentence that it is the bear's own evidence and must be cited or declared empty. Current arm unchanged.
- `ConditionalLogic.should_continue_debate`: independent arm → `"Research Manager"` when `count >= 2`. `should_continue_risk_analysis`: independent arm → `"Pre-mortem"` when `count >= 3`.
- Research Manager (independent arm): prompt shows `**The Bull case**` / `**The Bear case**` sections from `bull_history` / `bear_history` in `judge_order(...,"research")` order and returns `judge_order` as `"bear-first"` or `"bull-first"` in its state update. Current arm unchanged (`**Debate History:**`).
- Risk debators (independent arm): opening marker, no history. Portfolio Manager (independent arm): `**Risk views**` with three `**<Name> view**` sections from the three `*_history` fields in `judge_order(...,"risk")` order; current arm keeps `**Risk Analysts Debate History:**`.

Steps:
- [ ] Tests first: `judge_order` is deterministic and both orders occur across 200 tickers; `adverse_pack` with fetchers that return text and one that raises gives both sections with the `not retrieved:` line; a bear node in the independent arm with a scripted LLM gets a prompt containing `Adverse evidence pack` and the opening marker and not the bull's text, and in the current arm the bull's text and no pack; the Research Manager prompt in the independent arm contains both case headings in the seeded order and no `Debate History`, and the state update has `judge_order`; the three risk prompts in the independent arm contain no other debator's text; `should_continue_debate` returns `"Research Manager"` at count 2 in the independent arm with `max_debate_rounds=2`, and `"Bull Researcher"` in the current arm; same for risk at count 3 with `max_risk_discuss_rounds=2`; the Portfolio Manager prompt in the independent arm has `Risk views` and three `view` headings, in the current arm `Risk Analysts Debate History`.
- [ ] Implement.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: independent cases, the bear's evidence pack and randomised judge order`.

### Task 6: The Portfolio Manager's independent-arm blocks

**Files:** Modify `tradingagents/agents/managers/portfolio_manager.py`. Tests: `tests/test_structured_agent_prompts.py` (extend) or `tests/test_independent_debate.py`.

**Interfaces:**
- In the independent arm, after the worksheet block: the `**Price-blind valuation**` paragraph of the spec (section 2) built from `schemas.blind_values(state["blind_valuation"])`, the worksheet base and the price the node already reads (`portfolio_manager.py` lines 62-76); when `blind_values` returns `None`, the line `No price-blind valuation (<the text after "skipped: ", or "not produced">).`
- In the independent arm, a `**Base rates**` block = `state["base_rates"]` followed by the scenario rule sentence of the spec (section 4). When `base_rates` is empty, the rule sentence alone with "cohort unknown".
- Current arm: neither block.

Steps:
- [ ] Tests first: an independent-arm state with a rendered blind valuation gives a prompt containing `Price-blind valuation`, the converted value, `must lie between them`, `Base rates`, `Name the base rate used`; with `blind_valuation = "skipped: ADR"` the prompt contains `No price-blind valuation (ADR)`; a current-arm state contains none of `Price-blind`, `Base rates`.
- [ ] Implement.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: the Portfolio Manager reconciles the blind value and starts scenarios from base rates`.

### Task 7: Per-arm calibration and the adoption rule

**Files:** Modify `value_pipeline/calibration.py` (`brier(rs, horizon, arm=None)`, `arms(rs) -> list[dict]`, `score`), `value_pipeline/web/templates/calibration.html`, `value_pipeline/cli.py` (`calibration` prints the arm table and the rule), `value_pipeline/README.md`, `CHANGELOG.md`. Tests: `tests/test_calibration.py`.

**Interfaces:**
- `brier(rs, horizon=365, arm=None)`: with `arm`, only rows whose `agent_round == arm` (rows without the key are excluded); otherwise unchanged.
- `ADOPTION_RULE: str` = the spec's rule sentence, verbatim. `arms(rs) -> list[dict]`: one dict per arm present (`"current"`, `"independent"`), keys `arm`, `n` (rows with the arm), `brier_365`, `brier_180` (the `brier` results or `None`), `median_mos`, `median_missing`, `ratings` (dict rating → count), `median_blind_gap` (median of `|blind_value − price| / price` over rows with both, else `None`), `median_value_gap` (same with `intrinsic_value`). `score()["arms"] = arms(rs)` and `score()["adoption_rule"] = ADOPTION_RULE`.
- The page: a table under the Brier section with one row per arm and the rule text; the CLI prints the same.

Steps:
- [ ] Tests first: rows with `agent_round` in both arms plus two legacy rows: `brier(rs, 365)` counts all, `brier(rs, 365, arm="current")` excludes the legacy rows; `arms(rs)` has two entries with the right `n`, medians and `median_blind_gap` `None` for the current arm; `score()["adoption_rule"]` is `ADOPTION_RULE`; the rendered page contains `independent` and `Adoption rule`; `calibration` CLI output contains the table header.
- [ ] Implement; README and CHANGELOG lines.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: per-arm Brier scores and the adoption rule on the calibration page`.
