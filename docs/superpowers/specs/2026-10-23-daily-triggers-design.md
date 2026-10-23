# Daily triggers — design

**Date:** 2026-10-23
**Status:** the user asked "can we have daily runs to check whether to analyse the current stocks?" and said "sure" to Claude designing it with automatic re-reviews; calls Claude made are marked *(my call)*.

## Goal

Every weekday morning, check the holdings and the watched ideas against a small set of triggers, and re-run the agents only on the names where a trigger fired, within a daily cap, so a stock reaching its entry price, reaching its value, reporting earnings, falling hard or moving a lot gets a fresh decision the same day instead of waiting for the monthly review.

### What the user said

- "can we have daily runs to check whether to analyse the current stocks?" and "sure" (design it, auto re-review).
- Context from the night's reviews: thirteen of fifteen candidates were deferred "until the Q3 print" with explicit gates; the new decisions carry an entry price, kill criteria and the next earnings date, which are exactly the inputs a daily check needs.

### Non-goals

- Trading. Nothing changes a position; a re-review writes a normal review folder and ACTIONS, as today.
- Evaluating kill criteria automatically (they stay a checklist for the user).
- Replacing the Saturday jobs: the first-Saturday review and the weekly watch stay as they are.
- Intraday checks: one run per weekday morning on the previous close.

## Verified facts (from the code on 2026-10-23)

- `value_pipeline/watch.py:run` loads the portfolio, each ticker's newest `results.json` row (`_latest_results`), prices (`market_data.latest_price`) and trends (`metrics.trend(market_data.closes(t))`), builds `Alert`s (REACHED VALUE, FREEFALL, BUY RANGE, ENTRY PRICE), writes `reports/watch_<date>.md`, and notifies via `osascript`.
- `review.run(paths, cfg, *, yes, dry_run, only=[tickers])` runs the agents on named tickers under the `review` lock, writes a review folder `reports/<date>[-N]/` with `ACTIONS.md`, `results.json`, `committee.json` (≥ 2 tickers), and updates held positions' ratings.
- `results.json` rows carry `price` at review time, `entry_price`, `kill_criteria`, `intrinsic_value`, `rating`, `name`, `currency`; the market snapshot and instrument context know the next earnings date (`next_earnings_date(symbol)` in the Yahoo fundamentals vendor), but the pipeline does not store it.
- `scripts/monthly.sh` is started by `~/Library/LaunchAgents/local.value-pipeline.plist` on Saturdays at 09:00 and decides review vs watch by the date; it logs to `value_pipeline/data/logs/monthly_<date>.log` and notifies.
- The website dashboard shows the latest weekly watch markdown (`views.latest_watch`).

## Design

### Triggers

`value_pipeline/daily.py` evaluates, for each holding and each watched idea (a reviewed, not-held ticker whose latest rating is not Sell and whose review is at most `watch.max_rating_age_days` old):

| Trigger | Fires when | Applies to |
|---|---|---|
| ENTRY PRICE | price ≤ the latest review's `entry_price` | ideas, and holdings with an entry price (an add) |
| REACHED VALUE | price ≥ the latest intrinsic value | holdings |
| FREEFALL | the trend is FREEFALL and the last review's trend was not | holdings and ideas |
| EARNINGS | the earnings date stored at the last review is on or before yesterday, and no review has run since that date | holdings and ideas |
| BIG MOVE | price moved 15% or more from the last review's `price` *(my call: ±15%)* | holdings and ideas |

Each trigger yields a `Trigger(ticker, kind, message)`; the message states the numbers (price, level, date). A ticker can fire several triggers; it is reviewed once.

### Earnings date stored at review time

`review._run` records `next_earnings_date(ticker)` (fail-open) in each `results.json` row as `earnings_date`, and `_record_ratings` stores it on the `Position` as `next_earnings_date`. The daily check compares that stored date with today; after the re-review the stored date moves to the next report, so EARNINGS fires once per report.

### What the daily run does

`value_pipeline daily [--dry-run] [--no-notify] [--max N]`:

1. Prices and trends as the watch does (one Yahoo call per ticker; the same `latest_price`/`closes`).
2. Evaluate the triggers above.
3. Select tickers to re-review: those with ≥ 1 trigger, excluding any reviewed within `daily.cooldown_days` (default 5 *(my call)*) unless the trigger is EARNINGS or REACHED VALUE, ordered ENTRY PRICE, REACHED VALUE, EARNINGS, FREEFALL, BIG MOVE, capped at `daily.max_reviews` (default 3 *(my call)*; the rest are listed as "deferred to tomorrow").
4. If the `review` lock is held (a Saturday job running), skip the reviews and say so.
5. Run `review.run(paths, cfg, yes=True, only=selected)` once for all selected tickers (so the committee sees them together), unless `--dry-run`.
6. Write `reports/daily_<date>.md`: what was checked (ticker, price, change since review, trend), the triggers that fired, the reviews run with links to their ACTIONS and reports, the deferred ones, and the kill criteria to check for holdings whose trigger fired. Notify via macOS: `N trigger(s): HRB EARNINGS; DNLM.L ENTRY PRICE — 2 review(s) run` or `Nothing fired`.
7. Exit 0 when nothing fired or everything ran; exit 1 only when a review failed, so launchd's log shows it.

Config, under a new `daily:` block in `config.yaml` with DEFAULTS: `max_reviews: 3`, `cooldown_days: 5`, `big_move: 0.15`, `enabled: true`.

### Scheduling

- `scripts/daily.sh`: like `monthly.sh` (PATH, log to `value_pipeline/data/logs/daily_<date>.log`, notify on failure), runs `uv run python -m value_pipeline daily`, with `--check` to prove reachability.
- `~/Library/LaunchAgents/local.value-pipeline-daily.plist`: `StartCalendarInterval` for Weekday 1–5 at Hour 7 Minute 30 *(my call: after the US close, before the UK open, so prices are yesterday's closes)*. Installation stays manual, as the Saturday job's was: the plan prints the `launchctl load` command and the `pmset repeat wakeorpoweron MTWRF 07:25:00` the user must run (pmset accepts one repeat set, so the Saturday wake must be combined: `wakeorpoweron MTWRFS 07:25:00` and the Saturday job moved to 07:30 *(my call: one wake time for both jobs)*).

### Website

The dashboard's watch card becomes "Latest check" and shows the newest of the weekly watch and the daily check (by file date); `views.latest_watch` globs both `watch_*.md` and `daily_*.md`. The Run page's job list gains `daily` next to `watch`.

## Testing

- Unit tests for every trigger with hand-built rows (fires / does not fire / missing data), the cooldown and cap selection order, EARNINGS firing once (stored date before today, review after → no fire).
- `daily.run` with patched prices/trends/review: dry run writes the report and runs no review; a fired trigger calls `review.run` with the selected tickers; the lock held → no review and the report says so; a failing review → exit 1 path.
- `results.json` and `Position` carry `earnings_date` / `next_earnings_date`; old files load.
- Website: the card shows a daily file when it is newer than the watch.
- `scripts/daily.sh --check` and the plist parse (`plutil -lint`).

## Rollout

1. Build in a worktree, merge, restart the website.
2. Run `value_pipeline daily --dry-run` once by hand to see which triggers would fire today.
3. The user loads the plist and sets the wake schedule.
