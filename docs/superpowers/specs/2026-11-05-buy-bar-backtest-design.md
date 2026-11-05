# Buy-bar backtest — design

**Date:** 2026-11-05
**Status:** the user said "Your goal is to make as much money as possible" after a research report found that the pipeline's price rule, not its screen, is why nothing gets bought. Claude proposed backtesting the report's top candidates overnight with pass rules written down first; the user said "ok". Every choice below is Claude's call.

## Why

The record: 99 agent reviews, no Buy. The Buy bar is a 30% discount to a no-growth perpetuity of five-year average earnings. The research report (`~/Desktop/ValueTrading/reports/Successful investor strategies and gaps.md`) found no successful quality investor using such a bar: they require an expected return (Buffett stops near 10% a year) or a cash-flow yield above bond yields, and Europe's only clearly paying factor since 2015 was momentum. The lane backtest already replays the screen on what was filed at each past date and records each row's `expected_return`, `fcf_yield`, `momentum_12_1`, `trend`, `mos_formula` and the Magic Formula position. Three candidate bars can therefore be tested with new rules and no new data.

The existing run has two weaknesses for this purpose: ten yearly cohorts, and a `value` rule that picks 2 to 7 names a year, which is noise.

## Goals

1. More cohorts: quarterly cohort dates instead of one a year.
2. Six new rules that each pick a basket large enough to mean something.
3. A cohort-level pass rule, fixed here before any run, that says whether a rule supports changing the live Buy bar.
4. A report section that prints each new rule's verdict.

### Non-goals

Changing the live Buy bar, the agents' prompts or `decision_checks` (a later round, only for a rule that passes, and only on the user's word). A non-US backtest (EDGAR covers US filers). Removing survivorship bias (today's members only; the pass rule therefore compares with the equal-weight universe of the same members, not only with SPY).

## Verified facts (main at the merge of the deployment-and-trend round)

- `backtest.cohort_dates(cfg, today)` yields one date per year on `backtest.cohort_day` ("06-30") from `first_year` to `last_year`, keeping those whose one-year hold has ended. `backtest.run` snapshots every ticker on every cohort date through `screener.evaluate` (`snapshot`), ranks each cohort (`finish_cohort`), attaches `ret_1`, `ret_3` (`attach_returns`) and computes `benchmarks` per `(cohort, hold)`: SPY's total return and the equal-weight mean of every row with a return (`universe`).
- A `Rule(name, bars, picks)` takes a cohort's rows and returns the rows it picks; `rules(cfg)` lists eight; `picks_for`, `summarize` and `sensitivity` work per rule; `SENSITIVITY_GRID` maps a bar name to its test values. `_summary` already gives cohort-level lines (`basket`, `spy`, `universe`, `excess_spy`, `excess_universe`), `beat_spy`, `mean_excess_spy`, `median_excess_spy`, `mean_excess_universe`.
- Rows carry `passes_filters`, `trend`, `mf_position`, `mos_formula`, `expected_return` (shareholder yield + capped sustainable growth + reversion of EV/EBIT to the sector median, per year over seven years), `fcf_yield`, `momentum_12_1`, `lane_hints`, `pm_record_ok`, `fscore`, `roic_avg`.
- `_ranked(row, top_n)`: passes the filters, not FREEFALL, `mf_position ≤ top_n`. `_record(row)`: compounder hint and the PM's record bars.
- The full run took about 11 minutes with 10 cohorts on cached statements and prices.

## Design

### 1. Quarterly cohorts

`backtest.cohort_days: ["03-31", "06-30", "09-30", "12-31"]` replaces `cohort_day` (a config that still has `cohort_day` is read as a one-item list). `cohort_dates` yields every (year, day) from `first_year` to `last_year` in date order, keeping those whose one-year hold has ended. Everything downstream is per cohort date already. Overlapping one-year holds are not independent: the report says so wherever it counts cohorts.

### 2. New rules

All require `passes_filters` and a trend other than FREEFALL. "Top n by x" sorts descending and drops rows where x is missing.

| Rule | Picks | Bars (default; sensitivity) |
|---|---|---|
| `er_hurdle` | `expected_return ≥ min_er`, then the top `top_n` by `expected_return` | `min_er` 0.12 (0.10, 0.12, 0.15); `top_n` 20 (10, 20, 30) |
| `er_momentum` | `er_hurdle`'s picks whose `momentum_12_1` is at or above the median `momentum_12_1` of the cohort's passing, non-FREEFALL rows | same bars |
| `trending_value` | the Magic Formula's best `pool`, then the top `top_n` of those by `momentum_12_1` | `pool` 60 (40, 60, 100); `top_n` 20 |
| `value_graded` | `_ranked(row, top_n)` and `mos_formula ≥ mos` | `top_n` 30; `mos` 0.20 (0.10, 0.20, 0.30) |
| `value_no_downtrend` | the existing `value` rule's picks whose trend is not DOWNTREND | `top_n` 30; `mos` 0.30 |
| `compounder_fcf` | `_record(row)` and `fcf_yield ≥ min_fcf_yield`, then the top `top_n` by `expected_return` | `min_fcf_yield` 0.04 (0.03, 0.04, 0.05); `top_n` 20 |

`SENSITIVITY_GRID` gains `min_er`, `pool`, `min_fcf_yield`, and `mos` gains 0.10.

### 3. The pass rule (fixed before any run; amended 2026-11-05 after the whole-branch review, still before any run)

For a rule, over the one-year hold, a cohort counts when the rule picked at least 5 names in it. The rule **supports a live bar change** when all hold:

1. it has at least 20 such cohorts, falling in at least 7 different calendar years;
2. its basket beat the equal-weight universe in at least 60% of them;
3. the mean of its cohort excess returns over the universe is at least +2 points, and the mean excess over SPY is at least 0;
4. over the three-year hold the mean cohort excess over the universe is at least 0;
5. its mean one-year excess over the universe is higher than `magic_formula`'s **over the same cohorts** (the plain screen measured on exactly the cohorts the candidate counted in).

Otherwise it **does not support** one. `backtest.bar_verdict(one_year, three_year, baseline_matched, rule="") -> dict` returns `{"supports", "sentence", "failed", "cohorts", "years", "beat", "beat_share", "mean_excess_universe", "mean_excess_spy", "mean_excess_universe_3y", "baseline", "year_mean", "year_se"}`; the report prints one sentence per new rule under `## Buy-bar candidates`. The sentence gives counts ("26 of 40"), means to two decimals, and ends with the verdict and, when it fails, `Failed: (1) ..., (5) ...`. The existing rules keep their sentences.

`_summary` gains `cohorts_min5`, `years_min5` (distinct calendar years among those cohorts), `beat_universe_min5`, `mean_excess_universe_min5`, `mean_excess_spy_min5`, `cohort_excess_universe_min5` (`{cohort: excess}`), and, as information only, `year_mean_excess_universe` and `year_se_excess_universe`: the mean and standard error of the calendar-year averages of the cohort excesses.

**The survivorship check, also fixed before the run.** The S&P 400 publishes no join dates, so every current member is in every cohort, including names that entered after rising; a 20-name basket ranked on momentum can hold several while the universe holds each at about 1/600. The run is therefore made twice: on the S&P 500 and 400 together (the verdict above) and on the S&P 500 alone (`backtest --indexes sp500`, report `backtest_<date>_sp500.md`), where membership is point-in-time by join date. A candidate is **recommended** to the user for a live change only when it supports on the full universe and, on the S&P 500 alone, its mean one-year excess over the universe is above zero and above the matched plain screen's. The report says the bias is shared but not equal.

**What the report must say wherever it counts cohorts** (in "What was tested"): one-year holds on quarterly dates overlap, so the cohorts are not independent and amount to about eleven separate years; three-year holds overlap far more and amount to fewer than four separate periods; six candidates were tried against one threshold, so one passing by luck is plausible; returns are before trading costs; a pass is evidence to look closer, not proof.

**Other points settled before the run.** `er_momentum`'s median is taken over the cohort's rows that pass the filters, are not FREEFALL and have a momentum value (the same population every rule draws from). `backtest.last_year` becomes 2025 (cohorts whose one-year hold has not ended are dropped anyway). The `backtest` command screens the US lists only (`sp500`, `sp400`) unless `--indexes` says otherwise, since EDGAR covers US filers.

### 4. Report and command

`backtest` runs as before; the report gains the section above after the summary table, and the sensitivity table lists the new bars. The page `/backtest` shows the newest report unchanged. README: the six rules, the pass rule and the overlap caveat.

## Testing

- `cohort_dates`: four days a year in order; an old `cohort_day` config still works; a cohort whose one-year hold has not ended is dropped.
- Each rule on a hand-built cohort: the hurdle, the top-n cut, missing values dropped, the momentum median taken over passing rows only, the pool-then-momentum order, FREEFALL and failing rows excluded.
- `bar_verdict`: one case per condition failing alone, one passing, and the sentence text.
- `_summary`'s new fields on a three-cohort example with one cohort under five picks.
- The report prints the section and one sentence per new rule.

## After the build

Run `backtest` on the cached data (about 40 minutes with four times the cohorts), read the six verdicts, and give the user each rule's numbers and a recommendation. No live setting changes in this round.
