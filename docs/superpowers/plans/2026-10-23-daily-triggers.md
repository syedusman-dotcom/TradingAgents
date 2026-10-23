# Daily Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekday-morning `daily` command that applies five triggers to holdings and watched ideas, re-runs the agents only on the names that fired (capped, with a cooldown), writes a daily report, notifies, and is scheduled by launchd beside the Saturday job.

**Architecture:** `value_pipeline/daily.py` owns the trigger rules (pure functions over row dicts), the selection (cooldown, cap, order), the report and the orchestration that calls `review.run(..., only=selected)`. `review.py` stores each ticker's next earnings date at review time (`results.json` and `Position`). The CLI gains `daily`; `scripts/daily.sh` and a launchd plist schedule it; the dashboard card shows the newest of the weekly watch and the daily check.

**Tech Stack:** Python 3.11+, the existing `value_pipeline` modules (`watch`, `review`, `market_data`, `metrics`, `portfolio`, `lock`), pytest (`@pytest.mark.unit`, no network), ruff (line length 100), launchd plist.

**Spec:** `docs/superpowers/specs/2026-10-23-daily-triggers-design.md`

## Global Constraints

- Nothing trades. The daily run only re-runs the existing review path.
- No network in tests: patch `market_data.latest_price`, `market_data.closes`, `next_earnings_date`, `review.run`.
- Fail-open: a missing price, trend or earnings date skips that trigger; the daily report still writes.
- Old `results.json` rows and `portfolio.json` files without the new keys load (defaults None).
- Config block `daily: {enabled: true, max_reviews: 3, cooldown_days: 5, big_move: 0.15}` in DEFAULTS and `config.yaml`.
- ruff clean; full suite green after each task; conventional commits ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. EARNINGS must fire once per report: a stored date before today with a review after it must not fire again. Test in Task 2 (`test_earnings_fires_once_per_report`).
2. A ticker reviewed yesterday that fires BIG MOVE again must be held back by the cooldown, but REACHED VALUE / EARNINGS override it. Test in Task 2 (`test_cooldown_holds_back_except_value_and_earnings`).
3. The review lock held by a Saturday job: no review, exit 0, report says so. Test in Task 3 (`test_daily_skips_reviews_when_the_lock_is_held`).
4. A failing `review.run` must still write the daily report and exit 1. Test in Task 3 (`test_daily_reports_a_failed_review_and_exits_1`).
5. `next_earnings_date` returning None at review time must not break `results.json`/`Position`. Test in Task 1 (`test_results_and_position_carry_the_earnings_date_or_none`).

---

### Task 1: Store the next earnings date at review time

**Files:**
- Modify: `value_pipeline/review.py` (`RunResult`, `run_agents` or the loop in `_run`, the `results.json` row, `_record_ratings`), `value_pipeline/portfolio.py` (`Position.next_earnings_date: str | None = None`)
- Test: `tests/test_value_pipeline.py`

**Interfaces:**
- Produces: `results.json` row key `earnings_date` (ISO or None) from `tradingagents.dataflows.vendors.yahoo.fundamentals.next_earnings_date(ticker)` (fail-open; patch it in tests); `Position.next_earnings_date`; `Decision` unchanged.

- [ ] **Step 1: Write the failing test** `test_results_and_position_carry_the_earnings_date_or_none`: a dry-run review with `next_earnings_date` patched to return "2026-11-05" for HRB and None for ADBE → results rows carry those values; the held HRB position's `next_earnings_date == "2026-11-05"`; a `portfolio.json` without the key loads with None.
- [ ] **Step 2: Run it to verify it fails.**
- [ ] **Step 3: Implement** (dry run also records it, since the lookup is cheap and patched in tests).
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(review): store each ticker's next earnings date in results.json and on the position`

### Task 2: Trigger rules and selection

**Files:**
- Create: `value_pipeline/daily.py`, `tests/test_daily.py`
- Modify: `value_pipeline/config.py` (DEFAULTS `daily` block), `value_pipeline/config.yaml`

**Interfaces:**
- Produces: `@dataclass Trigger(ticker: str, kind: str, message: str)`; kinds exactly `ENTRY PRICE`, `REACHED VALUE`, `FREEFALL`, `EARNINGS`, `BIG MOVE`.
- Produces: `triggers(holdings: list[dict], ideas: list[dict], prices: dict, trends: dict, today: date, cfg: dict) -> list[Trigger]` where a holding dict has `ticker, intrinsic_value, entry_price, next_earnings_date, last_review_date, last_price, last_trend` and an idea dict is a `results.json` row plus `date` (from `watch._latest_results`), `last_trend` = the row's `trend` if present else None. Rules per the spec table; messages state the numbers, e.g. `DNLM.L at 740.00 is at or below its entry price 750.00`, `HRB at 56.10 is above its value 55.00`, `HRB reported on 2026-11-05 (last review 2026-10-23)`, `DECK moved -17% since its review at 85.28`.
- Produces: `select(triggers: list[Trigger], last_review: dict[str, str], today: date, cfg: dict) -> tuple[list[str], list[tuple[str, str]]]` → (tickers to review in order, deferred as (ticker, reason)); cooldown `cfg["daily"]["cooldown_days"]` except kinds `REACHED VALUE`/`EARNINGS`; order ENTRY PRICE, REACHED VALUE, EARNINGS, FREEFALL, BIG MOVE; cap `cfg["daily"]["max_reviews"]`.

- [ ] **Step 1: Write the failing tests**: one per trigger (fires / does not fire / missing data → no trigger), `test_earnings_fires_once_per_report`, `test_cooldown_holds_back_except_value_and_earnings`, `test_selection_order_and_cap`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `daily.py` (pure functions; no I/O in this task).
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(daily): trigger rules and review selection with cooldown and cap`

### Task 3: The daily run, report, notification and CLI

**Files:**
- Modify: `value_pipeline/daily.py` (add `run(paths, cfg, *, dry_run: bool = False, notify: bool = True, max_reviews: int | None = None, today: date | None = None) -> tuple[list[Trigger], Path, int]`), `value_pipeline/cli.py` (`daily` sub-command with `--dry-run`, `--no-notify`, `--max`), `value_pipeline/watch.py` (export `_latest_results` as `latest_results` and reuse `_notify`)
- Test: `tests/test_daily.py`

**Interfaces:**
- Produces: `daily.run` returns (triggers, report path, exit code). It builds holdings from the portfolio (using `Position.intrinsic_value_estimate, entry_price, next_earnings_date, last_review_date` and the latest results row's `price`/`trend` for `last_price`/`last_trend`), ideas from `watch.latest_results` (not held, rating ≠ Sell, age ≤ `watch.max_rating_age_days`), prices/trends as the watch does, calls `triggers`, `select`, then `review.run(paths, cfg, yes=True, only=selected)` unless `dry_run`, catching `lock.Locked` (report "a review is already running; triggers listed, nothing re-run", exit 0) and any exception from the review (report it, exit 1). Report at `paths.reports / f"daily_{today}.md"` with sections: `# Daily check <date>`, Checked (table ticker | price | since review | trend), Triggers, Reviews run (links to `<folder>/ACTIONS.md` and each report), Deferred, Kill criteria to check (holdings that fired). Notification text per the spec.
- Produces: CLI `daily` prints the report and the path; exit code propagated.

- [ ] **Step 1: Write the failing tests**: `test_daily_dry_run_writes_the_report_and_runs_nothing`, `test_daily_runs_the_review_on_fired_tickers`, `test_daily_skips_reviews_when_the_lock_is_held`, `test_daily_reports_a_failed_review_and_exits_1`, `test_cli_daily_exit_code`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite and ruff.**
- [ ] **Step 5: Commit** — `feat(daily): the daily check runs the agents on triggered names and writes a report`

### Task 4: Scheduling, website card and docs

**Files:**
- Create: `scripts/daily.sh`, `scripts/local.value-pipeline-daily.plist` (a template the user copies to `~/Library/LaunchAgents/`)
- Modify: `scripts/monthly.sh` only if a shared helper is extracted (optional), `value_pipeline/web/views.py:latest_watch` (newest of `watch_*.md` and `daily_*.md`), `value_pipeline/web/templates/dashboard.html` (card title "Latest check"), `value_pipeline/web/app.py` + `jobs` (a `daily` job next to `watch` on the Run page, if the Run page lists jobs by name), `value_pipeline/README.md` (Daily check section, scheduling, the pmset line)
- Test: `tests/test_value_web.py`, a shell check in the task

**Interfaces:**
- Produces: `scripts/daily.sh [--check]` (PATH setup, log `value_pipeline/data/logs/daily_<date>.log`, `uv run python -m value_pipeline daily`, notify on failure, exit code passthrough); plist Weekday 1–5 Hour 7 Minute 30 running `daily.sh`, StandardOut/Err to `~/Library/Logs/value-pipeline-daily.log`.

- [ ] **Step 1: Write the failing web test** `test_dashboard_shows_the_newer_of_watch_and_daily` (a `daily_2026-10-24.md` newer than `watch_2026-10-18.md` → the card shows the daily text and title "Latest check").
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the view/template, the Run-page job, the scripts and the README section (with the exact install commands: `cp scripts/local.value-pipeline-daily.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/local.value-pipeline-daily.plist`, `sudo pmset repeat wakeorpoweron MTWRFS 07:25:00`, and a note that the Saturday plist's hour should move to 7:30 to share the wake).
- [ ] **Step 4: Verify** `bash scripts/daily.sh --check` prints the check line; `plutil -lint scripts/local.value-pipeline-daily.plist` passes; suite and ruff green; `uv run python -m value_pipeline daily --dry-run` against a scratch data dir seeded with a copied `portfolio.json` and the real `reports/` folder writes a report listing today's triggers.
- [ ] **Step 5: Commit** — `feat(daily): weekday schedule, dashboard card and docs`
