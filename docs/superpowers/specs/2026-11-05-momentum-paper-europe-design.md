# Momentum test, paper Buys and European candidates — design

**Date:** 2026-11-05
**Status:** the user asked what the strategy lacks to beat the S&P 500. Claude replayed the 43 past cohorts row by row (31 traits) and proposed four steps; the user answered "That sounds like a plan, lets do that". Step one (buy the index fund when a review suggests it) is the user's own action and needs no code. This design covers the other three. Every choice below is Claude's call unless marked as the user's.

## Why

What the row-by-row replay showed (S&P 500 members at the time, one-year holds, 2015 to 2025):

- No trait the screen measures separates winners from losers. Names passing the filters, equal weight, ended 0.1 points behind SPY; every trait's top-minus-bottom spread is inside the noise. Tuning thresholds cannot help.
- The top 30 by 12-1 momentum with no quality filter ended 4.7 points a year ahead of SPY (8 of 11 years). With the pipeline's filters: 0.8 behind. 62% of the momentum names fail the filters. This is after the fact and flattered by missing delisted stocks: a lead, not a rule.
- The agents have never rated a Buy in 109 reviews. The live bar needs a 30% discount to their own probability-weighted value and an upside/downside of 3. Their value already includes growth (base case about 3% for ever, a bull case above it), so the bar pays nothing for it. Whether the agents can pick is unmeasured.
- Value paid in Europe recently (Fama-French Europe HML +13.4% a year 2021 to 2025) and not in US large caps (-2.3% a year 2015 to 2026). EDGAR cannot backtest Europe.

## Goals

1. A momentum sleeve backtest with its pass rule fixed here, before any run, including a ten-year period nobody has looked at, and the real record of momentum index funds next to it.
2. Paper Buys: looser, growth-crediting buy rules applied in code to what each review already records, scored against SPY on the calibration page. No money, no prompt change, no change to the live rating.
3. Value-lane candidates drawn from European markets.

### Non-goals

Changing the live Buy bar, any prompt, sizing, or `decision_checks`. Trading anything. A live momentum sleeve (a later round, only if the test passes and the user says so). A European momentum test (no join dates for European indexes; today's members only would be badly inflated). Broader Nordic index lists. Showing trading costs per market.

### Anti-goals

- A paper Buy that reads as a suggestion to buy. Every place it appears says it is tracked only.
- A pass rule adjusted after the run. If a number turns out badly chosen, the report says so and the rule stands.
- Slower or costlier reviews: nothing here calls an agent.

## Verified facts (main at 212e988)

- `trend.simulate("momentum_12_1", closes, spy, settings, eligible)` already runs monthly 12-1 momentum: on the first trading row of each month it ranks the eligible names that close above their 200-day average by `close[t-21] / close[t-252] - 1`, sells holdings outside the top `max_positions`, and (while SPY is above its own 200-day average, if `market_filter`) buys the missing ones with `equity / max_positions` each, filled at the next close, `cost_per_side` on every buy and sale. `trend.metrics(result, spy)` gives CAGR, drawdown, calendar years against SPY, costs. `trend.load_closes(paths, tickers, start, fetch, progress)` returns adjusted closes on SPY's trading days from 300 rows before `start`, and SPY. `trend._eligibility(added)` is "on or after the S&P 500 join date".
- The backtest price cache holds every S&P 500 member's full Yahoo history (`period="max"`); SPY from 1993. `cache/backtest/added_dates.json` has a join date for all 503 members: 210 joined by 2005-01-03, 320 by 2015-07-01.
- Each `results.json` row carries `rating`, `price`, `intrinsic_value`, `margin_of_safety` (probability-weighted, at the review price), `checklist` (`good_business`, `good_management`, `cheap`, and since 2026-10-24 `still_investing`, each `{"passed": bool, "note": str}`), `scenarios`, `currency` (None on older rows, which were all US). 58 of 99 rows carry a checklist.
- `calibration.rows(paths, today, price_fn)` builds one row per decision with `h30`, `h90`, `h180`, `h365` (`price`, `ret` in the quote currency, not dividend-adjusted); `price_fn(ticker, day)` is the cached close on or before a day; `_prefetch` fills the cache with one request per ticker; `score(rs)` groups by rating and lane; `markdown` and `web/templates/calibration.html` show them. `/calibration` reads cached closes only.
- Yahoo carries `GBPUSD=X`-style pairs (`usd_rate` uses them); `MINOR_UNITS` maps `GBp` to `GBP`.
- `screener.top_candidates(paths, owned, n, compounders, dislocations, inflections, min_run_rate)` takes Magic Formula names in rank order, then hinted names; `run_rate_skips` lists names dropped for a low run rate; `review.preview` calls both. The screen CSV has a `country` column: US, UK, Canada, Germany, France, Switzerland, Netherlands, Sweden, Norway, Italy, Finland, Denmark, Spain, Ireland, Portugal, Austria, Belgium.
- Yahoo has `MTUM` (2013-04), `IS3R.DE` and `EUNL.DE` (2014-10, 2009-09; EUR, accumulating), `QDVA.DE` and `SXR8.DE` (2016-10, 2010-05; EUR, accumulating).

## Design

### Part 1: momentum sleeve backtest

New module `value_pipeline/momentum.py`, command `momentum-backtest [--no-fetch]`, report `reports/backtest/momentum_<date>.md`, `.html` and `_trades.csv`, linked from `/backtest` beside the trend report. It reuses `trend.simulate`, `trend.metrics`, `trend.load_closes`; it does not change them.

**Universe.** Today's S&P 500 members that have a join date, each eligible from that date. No S&P 400 names (they have no join dates, and names that grew into the index are the survivorship the earlier run showed).

**Two periods.** `recent`: 2015-07-01 to the last close. `holdout`: 2005-01-03 to 2015-06-30, which no earlier run or analysis in this project has touched. The hold-out run sees closes up to its end date only.

**Settings (config `momentum:`).** `equity` 10000, `positions` 10 (primary), `check_positions` 20, `cost_per_side` 0.005 (Nordnet Mini outside the Nordics: 0.25% commission plus 0.25% currency fee), `market_filter` true, `recent_start`, `holdout_start`, `holdout_end`, `funds` (below).

**Equal-weight universe.** For each period, the daily mean return of every name eligible the day before with a close on both days, compounded, no costs. It shares the sleeve's survivorship, so the sleeve's lead over it is the cleaner measure of momentum; SPY has no such bias.

**The pass rule (fixed before any run).** The test **supports a small live sleeve** only when all six hold, for the top 10 at 0.5% a side:

1. recent period: CAGR at least SPY's plus 2 points;
2. recent period: CAGR at least the equal-weight universe's plus 1 point;
3. hold-out period: CAGR at least SPY's and at least the universe's;
4. in each period, maximum drawdown at most 1.25 times SPY's over the same days;
5. over both periods, ahead of SPY in at least 55% of the calendar years (2015 counts once per period, as two half years);
6. the top 20 at the same cost: recent CAGR at least SPY's plus 1 point, and hold-out CAGR at least SPY's.

Otherwise it **does not support** one. All bounds are inclusive. The report prints one sentence with the numbers and names every failed condition.

**Sensitivity (information only).** Positions 10, 20, 30; cost a side 0, 0.0035, 0.005, 0.0075; market filter off. Both periods.

**Fund record (information only).** For each `funds` pair `{fund, parent, label}`: CAGR of both on adjusted closes over their common dates, the difference, both drawdowns. Defaults: `MTUM`/`SPY`, `IS3R.DE`/`EUNL.DE`, `QDVA.DE`/`SXR8.DE`. These are real funds after their costs, with no survivorship, so they bound what an index version of the idea has delivered. A pair with no price data prints as unavailable; `--no-fetch` reads the cache only.

**Limits printed in the report.** Today's members only: names that left the index are missing, which flatters the sleeve and the universe but not SPY; fills at the next close; cash earns nothing; a Swedish investor's currency moves are not modelled; ten positions is a concentrated book.

### Part 2: paper Buys

New module `value_pipeline/paper.py`:

- `flags(row) -> list[str]`: the paper rules a `results.json` row meets, in the order below. Pure, no I/O.
- The gate, common to all rules: no `error`; `rating` is Buy, Overweight or Hold; `checklist.good_business.passed` and `checklist.good_management.passed` are both true; `margin_of_safety` is a number.
- `fair_price` (primary): the gate, and `margin_of_safety >= 0`: a good business with good management at or below the agents' own value.
- `fair_price_investing`: `fair_price`, and `checklist.still_investing.passed` is true: the same, and the company is still investing for growth.
- `discount_15`: the gate, and `margin_of_safety >= 0.15`.

On today's 99 reviews these flag 23, 7 and 3 rows (12, 6 and 2 names): enough to learn from, few enough to mean something. Only counts were looked at when choosing them, no returns.

**Scoring (`calibration`).**

- Each row gains `paper` (the flags) and `currency`. Each horizon gains `ret_usd` (the return in dollars: the quote-currency return times the change in the currency's dollar rate between the two days, from the `<CCY>USD=X` close; the plain return for a dollar or currency-less row; None when a rate is missing), `spy` (SPY's close-to-close return over the same days) and `excess` (`ret_usd - spy`). `_prefetch` also fills SPY and the currency pairs.
- A name counts once per rule per 90 days: rows oldest first, a flagged row counts unless a counted row of the same ticker and rule is less than 90 days older. The baseline is every row, counted the same way.
- Per rule and for the baseline, at each horizon: counted rows with a result, mean dollar return, mean and median excess over SPY, share ahead of SPY.
- **Verdict for `fair_price` at 180 days, fixed now.** Not enough yet: fewer than 20 counted paper Buys with a 180-day result, or from fewer than 3 calendar months. Otherwise it **supports a live test of the rule** when the mean excess is at least +3 points, at least 55% are ahead of SPY, and the mean excess is at least the baseline's plus 2 points; else it **does not support** one. The other two rules show their numbers and no verdict.
- `score(rs)["paper"]` holds all of it.

**Where it shows.**

- `calibration` (command and page): a "Paper Buys" section with what each rule means, the verdict sentence, a table of rule by horizon, and the counted paper Buys (date, ticker, rules, price then, dollar return and excess at the latest horizon reached). The Decisions table gains a Paper column.
- `ACTIONS.md`: a "Paper Buys" section listing this review's flagged names and rules, opening with: "Tracked on the calibration page only; not a suggestion to buy." Left out when nothing is flagged.

Nothing else reads the flags: not sizing, not the committee, not `watch`, not `daily`.

### Part 3: European candidates

- Config `review.candidate_countries`: the screen `country` values the Magic Formula and dislocation candidates may come from. Empty or absent means every country. `config.yaml` sets Sweden, Norway, Denmark, Finland, Germany, France, Netherlands, Switzerland, Italy, Spain, Ireland, Portugal, Austria, Belgium, UK. `DEFAULTS` keeps it empty.
- Compounder-hinted and inflection-hinted candidates, `lanes.tickers`, theme discoveries, holdings and `--tickers` ignore the list: the strict compounder rule is the one rule ahead of SPY in both US tests, and holdings are always reviewed.
- `screener.top_candidates(..., countries=None)` and `screener.run_rate_skips(..., countries=None)` filter on it; a screen without a `country` column is not filtered.
- A review (and `review.preview`'s result, key `candidate_countries`) says which countries the value candidates come from, and names any listed country with no row in the latest screen (a typo guard).
- `load_config` rejects a `candidate_countries` that is not a list of strings.

## Testing

- Momentum: the universe keeps only names with a join date; eligibility respects the date; the hold-out run reads no close after its end; the equal-weight universe on a hand-built frame; each of the six conditions failing alone, all passing, bounds inclusive, and the sentence naming the failures; the fund record on two hand-built series, a missing fund, and unequal date ranges; the report prints the verdict, both periods, the sensitivity rows, the fund table and the limits; the command with `--no-fetch` on a tiny cache.
- Paper: `flags` for each rule's boundary (0 and 0.15 exactly), a failed checklist item, a missing checklist, an Underweight, an errored row, a missing `still_investing`; the 90-day count (same ticker at 0, 60 and 95 days); dollar returns for a pence quote, a missing rate, a currency-less row; SPY excess; each verdict branch and its sentence; markdown and page sections; ACTIONS with and without flagged names; a dry run flags nothing.
- Candidates: the filter on Magic Formula and dislocation picks; compounder picks unfiltered; an empty list; a screen without `country`; `run_rate_skips` agreeing with the picks; the typo line; `load_config` validation; the default yaml list loads.

## After the build

Run `momentum-backtest` once and report the verdict as it falls. Run `calibration` to fill the closes and show the paper Buys so far. Tell the user the momentum verdict, the fund record, how many paper Buys exist and when the first 180-day results arrive (late April 2027 for the October reviews), and that value candidates now come from Europe.
