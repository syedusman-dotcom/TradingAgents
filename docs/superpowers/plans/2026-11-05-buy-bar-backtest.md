# Buy-Bar Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test six candidate Buy bars on quarterly point-in-time cohorts and print, for each, a verdict from a pass rule fixed before the run.

**Architecture:** Everything lives in the existing lane backtest (`value_pipeline/backtest.py`): `cohort_dates` yields four dates a year, six `Rule`s are added to `rules(cfg)`, `_summary` gains cohort counts at a five-pick floor, and `bar_verdict` turns two summaries and a baseline into a sentence the report prints.

**Tech Stack:** Python 3.14, pandas, pytest, ruff (line length 100). Run through `uv run`; `python` is not on PATH.

**Spec:** `docs/superpowers/specs/2026-11-05-buy-bar-backtest-design.md`

## Global Constraints

- No live setting changes: the Buy bar, the agents' prompts, `decision_checks` and `sizing` are untouched. This round only measures.
- The pass rule in the spec is fixed: implement its five conditions exactly as written, with the five-pick floor, and tune nothing after seeing results.
- Every new rule requires `passes_filters` and a trend other than FREEFALL; "top n by x" sorts descending and drops rows where x is missing.
- A config that still has `backtest.cohort_day` (a string) keeps working and means one date a year.
- The eight existing rules, their default bars and their sentences are unchanged.
- No network in tests; mark tests `@pytest.mark.unit`. `uv run ruff check .` and `uv run pytest -q` pass at every commit.
- Commit messages end with `Co-Authored-By:` naming the model that wrote the commit, then `Claude-Session: https://claude.ai/code/session_015r4hMD2DRWvm5AXmWo8KRV`.

## Review Focus

1. A cohort where a rule picks fewer than five names: it must not count toward the verdict's cohorts, shares or means, but its picks still appear in the pick list.
2. Rows with a missing `expected_return`, `momentum_12_1` or `fcf_yield`: dropped from that rule, never ranked as zero.
3. Quarter-end dates on which a ticker has no close within the allowed days, and cohorts whose three-year hold has not ended: no crash, the three-year condition uses only cohorts that have one.
4. An old config with `cohort_day` and no `cohort_days`; a config with both (the list wins).
5. The momentum median when fewer than two passing rows have momentum: `er_momentum` picks nothing instead of raising.

---

### Task 1: Quarterly cohorts and the five-pick summary fields

**Files:**
- Modify: `value_pipeline/backtest.py` (`cohort_dates`, `_summary`)
- Modify: `value_pipeline/config.py` (`DEFAULTS["backtest"]`), `value_pipeline/config.yaml` (the commented `backtest` block)
- Test: `tests/test_lane_backtest.py`

**Interfaces:**
- Produces:
  - `DEFAULTS["backtest"]["cohort_days"] = ["03-31", "06-30", "09-30", "12-31"]` (the key `cohort_day` is removed from the defaults).
  - `backtest.cohort_days(cfg: dict) -> list[str]`: `backtest.cohort_days` when present, else `[backtest.cohort_day]`.
  - `backtest.cohort_dates(cfg, today) -> list[str]`: every `f"{year}-{day}"` for the years `first_year..last_year` and the days above, in date order, keeping those whose one-year hold has ended by `today`.
  - `_summary(...)` gains `cohorts_min5: int`, `beat_universe_min5: int`, `mean_excess_universe_min5: float | None`, `mean_excess_spy_min5: float | None`, computed over the cohort lines with `n ≥ MIN_BASKET` (`MIN_BASKET = 5`) and a universe return.

- [ ] **Step 1: Write the failing tests**

```python
def test_cohort_dates_are_quarterly_and_in_order():
    cfg = _bt_cfg(first_year=2023, last_year=2024, cohort_days=["03-31", "06-30", "09-30", "12-31"])
    assert backtest.cohort_dates(cfg, date(2025, 7, 1)) == [
        "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30"]

def test_an_old_config_with_cohort_day_still_means_one_date_a_year():
    cfg = {"backtest": {"first_year": 2022, "last_year": 2023, "cohort_day": "06-30", "holds": [1, 3]}}
    assert backtest.cohort_days(cfg) == ["06-30"]
    assert backtest.cohort_dates(cfg, date(2030, 1, 1)) == ["2022-06-30", "2023-06-30"]

def test_cohort_days_wins_over_cohort_day():
    cfg = {"backtest": {"first_year": 2022, "last_year": 2022, "cohort_day": "06-30",
                        "cohort_days": ["03-31"], "holds": [1]}}
    assert backtest.cohort_dates(cfg, date(2030, 1, 1)) == ["2022-03-31"]

def test_summary_counts_only_cohorts_with_five_picks_for_the_verdict_fields():
    # three cohorts: A with 5 picks (basket +20%, universe +10%, spy +12%),
    # B with 6 picks (basket +5%, universe +8%, spy +9%), C with 2 picks (basket +50%)
    s = backtest._summary("r", 1, picks, bench)
    assert s["cohorts_with_picks"] == 3
    assert s["cohorts_min5"] == 2 and s["beat_universe_min5"] == 1
    assert s["mean_excess_universe_min5"] == pytest.approx((0.10 - 0.03) / 2)
    assert s["mean_excess_spy_min5"] == pytest.approx((0.08 - 0.04) / 2)

def test_summary_min5_fields_are_empty_without_such_a_cohort():
    # one cohort with two picks: cohorts_min5 == 0, both means None
```

Update `tests/test_lane_backtest.py:735` (the `DEFAULTS["backtest"]` equality) and the `_bt_cfg` helper for the new key.

- [ ] **Step 2: Run them and see them fail**: `uv run pytest -q tests/test_lane_backtest.py`
- [ ] **Step 3: Implement.** Everything downstream of `cohort_dates` is already per cohort date. Update the `config.yaml` comment block (`cohort_days`, one line on why quarterly, and that overlapping one-year holds are not independent).
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: quarterly backtest cohorts and five-pick summary fields`)

---

### Task 2: Six candidate rules, the pass rule and the report section

**Files:**
- Modify: `value_pipeline/backtest.py` (`rules`, six pick functions, `SENSITIVITY_GRID`, new `bar_verdict`, `run`'s result, the report)
- Modify: `value_pipeline/README.md`
- Test: `tests/test_lane_backtest.py`

**Interfaces:**
- Consumes: Task 1's `_summary` fields.
- Produces:
  - Rules appended to `rules(cfg)` in this order, after the existing eight: `er_hurdle` (`{"min_er": 0.12, "top_n": 20}`), `er_momentum` (same bars), `trending_value` (`{"pool": 60, "top_n": 20}`), `value_graded` (`{"top_n": 30, "mos": 0.20}`), `value_no_downtrend` (`{"top_n": 30, "mos": 0.30}`), `compounder_fcf` (`{"min_fcf_yield": 0.04, "top_n": 20}`).
  - `BAR_CANDIDATES = ("er_hurdle", "er_momentum", "trending_value", "value_graded", "value_no_downtrend", "compounder_fcf")`.
  - `SENSITIVITY_GRID` gains `"min_er": (0.10, 0.12, 0.15)`, `"pool": (40, 60, 100)`, `"min_fcf_yield": (0.03, 0.04, 0.05)`; `"mos"` becomes `(0.10, 0.20, 0.30, 0.40)`.
  - `bar_verdict(one_year: dict, three_year: dict | None, baseline: dict) -> dict` with keys `supports, sentence, cohorts, beat_share, mean_excess_universe, mean_excess_spy, mean_excess_universe_3y, baseline`.
  - `run`'s result gains `"bar_verdicts": {rule: bar_verdict(...)}` for the six candidates (baseline = `magic_formula`'s one-year summary).
  - The report gains `## Buy-bar candidates` after the summary section: one line stating the pass rule and that overlapping cohorts are not independent, then each candidate's sentence.

- [ ] **Step 1: Write the failing tests**

```python
def _row(t, **kw):      # passes_filters True, trend "BASING", lane_hints [], pm_record_ok False unless given
    ...

def test_er_hurdle_takes_the_best_expected_returns_above_the_bar():
    rows = [_row("A", expected_return=0.20), _row("B", expected_return=0.13), _row("C", expected_return=0.11),
            _row("D", expected_return=None), _row("E", expected_return=0.30, trend="FREEFALL"),
            _row("F", expected_return=0.25, passes_filters=False)]
    rule = backtest.rules(CFG)["er_hurdle"]
    assert [r["ticker"] for r in rule.picks(rows, {"min_er": 0.12, "top_n": 20})] == ["A", "B"]
    assert [r["ticker"] for r in rule.picks(rows, {"min_er": 0.12, "top_n": 1})] == ["A"]

def test_er_momentum_keeps_picks_at_or_above_the_passing_rows_median_momentum():
    # passing rows' momentum: A 0.30, B 0.10, C −0.05 (C fails the hurdle but counts in the median), G None
    # median of (0.30, 0.10, −0.05) = 0.10: A and B stay; a pick with momentum None is dropped;
    # with fewer than two passing rows that have momentum the rule picks nothing

def test_trending_value_takes_the_pool_by_rank_then_the_strongest_momentum():
    # mf_position 1..5 with momentum 0.0, 0.4, 0.1, 0.5, 0.9; pool 3, top_n 2 -> the tickers ranked 2 and 3

def test_value_graded_and_value_no_downtrend():
    # value_graded: mf_position ≤ 30 and mos_formula ≥ 0.20 (0.19 out, 0.20 in)
    # value_no_downtrend: the value rule's picks minus trend "DOWNTREND"

def test_compounder_fcf_needs_the_record_and_the_yield():
    # lane_hints ["compounder"], pm_record_ok True, fcf_yield 0.05 -> in; 0.03 -> out; record False -> out;
    # then top_n by expected_return, missing expected_return dropped

def test_bar_verdict_conditions():
    one = {"cohorts_min5": 30, "beat_universe_min5": 20, "mean_excess_universe_min5": 0.03,
           "mean_excess_spy_min5": 0.01}
    three = {"cohorts_min5": 20, "mean_excess_universe_min5": 0.01}
    base = {"mean_excess_universe_min5": 0.01}
    assert backtest.bar_verdict(one, three, base)["supports"] is True
    assert backtest.bar_verdict({**one, "cohorts_min5": 19}, three, base)["supports"] is False            # 1
    assert backtest.bar_verdict({**one, "beat_universe_min5": 17}, three, base)["supports"] is False      # 2: 17/30 < 60%
    assert backtest.bar_verdict({**one, "mean_excess_universe_min5": 0.019}, three, base)["supports"] is False   # 3
    assert backtest.bar_verdict({**one, "mean_excess_spy_min5": -0.001}, three, base)["supports"] is False       # 3
    assert backtest.bar_verdict(one, {**three, "mean_excess_universe_min5": -0.001}, base)["supports"] is False  # 4
    assert backtest.bar_verdict(one, three, {"mean_excess_universe_min5": 0.03})["supports"] is False            # 5: not higher
    assert backtest.bar_verdict(one, None, base)["supports"] is False          # no three-year cohorts: condition 4 unmet
    v = backtest.bar_verdict(one, three, base)
    assert "supports a live bar change" in v["sentence"] and v["beat_share"] == pytest.approx(20 / 30)
    assert "does not support a live bar change" in backtest.bar_verdict({**one, "cohorts_min5": 19}, three, base)["sentence"]

def test_run_result_and_report_carry_the_bar_verdicts(...):
    # the existing small offline run fixture: result["bar_verdicts"] has the six names;
    # the report contains "## Buy-bar candidates" and one sentence per candidate; the sensitivity
    # table lists min_er, pool and min_fcf_yield

def test_existing_rules_and_bars_are_unchanged():
    r = backtest.rules(CFG)
    assert list(r)[:8] == ["value", "magic_formula", "compounder", "compounder_overweight",
                           "compounder_record", "compounder_hint", "inflection", "dislocation"]
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement.** `bar_verdict` reads the `_min5` fields; a missing mean counts as failing its condition; condition 4 needs a three-year summary with at least one five-pick cohort; the sentence names the rule's numbers (cohorts, share beaten, the three means, the baseline) and ends with "supports a live bar change" or "does not support a live bar change". README: a subsection "Buy-bar candidates" under Backtest with the six rules, the pass rule and the overlap caveat; update the cohort description to quarterly.
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: buy-bar candidate rules and their verdicts in the backtest`)

---

### Task 3: Run on real data (controller, after the merge)

- [ ] `uv run python -m value_pipeline backtest` (cached statements and prices; about 40 minutes with four times the cohorts).
- [ ] Read the six verdict sentences, the cohort tables and the sensitivity rows; write the findings and a recommendation for the user. No live setting changes.
