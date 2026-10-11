# Value pipeline website: design

Date: 2026-10-11. Status: approved in conversation; this file records it.

## Purpose

A local website that does everything the `value_pipeline` command line does, so the owner can run the monthly routine, record trades and read the history from a browser. It is for one person, on one Mac. It never places trades.

**Success:** every CLI capability is usable from the browser; the website and the CLI read and write the same files, so they always agree; nothing is reachable from another device.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Where it runs | This Mac only, `127.0.0.1` | The agents, API keys and data live here; no login needed |
| How it starts | On demand: `uv run python -m value_pipeline web` opens the browser; Ctrl+C stops it | No always-on service |
| Stack | FastAPI + Jinja2 server-rendered pages, served by uvicorn | Calls the pipeline functions directly; small, testable; no build step |
| New dependencies | `fastapi`, `uvicorn`, `jinja2`, `python-multipart` | Forms need `python-multipart` |
| Not included | Placing trades, logins, editing `.env` or keys, editing the launchd schedule | Out of scope or unsafe |

## Pages

| Route | Content and actions |
|---|---|
| `/` Dashboard | Total value, cash, return vs SPY (with an inline SVG chart of value over time vs the SPY shadow), and holdings with price, gain %, weight, last rating, intrinsic value and days held. Also this week's watch alerts and the next scheduled review date (first Saturday). |
| `/trade` | Buy, sell and deposit/withdraw forms: ticker, shares, price, fees, date, note. Shows the live price, the latest agent rating, value and margin of safety for the ticker, and the cash left after the trade. Errors are those of `portfolio.apply_*` (`TradeError`). A buy fills the thesis and value from the latest review, as the CLI does. |
| `/trades` | All trades and deposits, newest first, with realised gain per sale. "Undo last entry" restores `portfolio.json` and `trades.csv` from the backup taken just before it. |
| `/reviews` | Every review folder (`review_dirs`), newest first: date, tickers, cost, buys and sells. Each opens its `ACTIONS.html` and per-stock reports. |
| `/reviews/<folder>/<path>` | Serves the files of a review folder (reports, `ACTIONS.html`). Paths are confined to `data/reports`. |
| `/stock/<ticker>` | Every rating, value and margin of safety for the ticker across reviews (with a small SVG chart), its latest screen metrics, trades in it, and links to its reports. |
| `/screen` | The latest screen as a table that sorts and filters in the browser (sector, trend, passes filters, margin of safety, F-Score), plus falling knives and the excluded and skipped tickers. Button: run a new screen. |
| `/run` | Start a screen, a review (default, or named tickers), a dry run, or a watch. A review shows the run count and estimated cost and needs a Confirm click; the `max_cost_per_review` cap still applies. Shows the current job's live log and the past jobs. |
| `/settings` | Edit `config.yaml` as text. It is validated by loading it before saving; the previous version is backed up. |

## Architecture

- **Package:** `value_pipeline/web/`
  - `app.py`: routes;
  - `jobs.py`: the background-job runner;
  - `views.py`: data assembled for pages;
  - `templates/`: Jinja templates;
  - `static.css`: inlined into each page.
  - CLI: a new `web` subcommand starts uvicorn on `127.0.0.1:8765` and opens the browser.
- **One source of logic:**
  - The pages call the existing functions: `portfolio.load/save/apply_buy/apply_sell/apply_deposit/append_trade`, `screener.run/top_candidates/latest_screen`, `review.run/review_dirs/latest_result`, `watch.run`, `benchmark.compare`, `market_data.latest_price`.
  - The value/gain/weight calculation in `cli.cmd_status` moves to a shared `portfolio_view()` function used by both the CLI and the dashboard.
  - The "record a trade" steps in `cli.cmd_buy/cmd_sell` (load, apply, save, append, fill thesis) move to shared functions so the CLI and the website record a trade identically.
- **Jobs:**
  - One job at a time, run in a `threading.Thread`.
  - Its stdout and log output go to `data/logs/job_<timestamp>_<kind>.log`.
  - A small JSON file, `data/logs/jobs.json`, records each job's kind, start, end and status.
  - The Run page polls a `/run/status` endpoint every 3 seconds.
  - Before a review, a new `review.preview(paths, cfg, only)` returns the tickers, run count, estimated cost and whether the cap blocks it, without running anything. The CLI's own printout uses the same function.
  - The web starts `review.run(..., yes=True)` only after the user has confirmed that preview.
  - Jobs cannot be cancelled. Closing the browser does not stop them.
- **Undo:**
  - `portfolio.save` already backs up both files before each write.
  - Undo restores the newest backup taken before the last trade entry, after first backing up the current state.
  - Only the last entry can be undone.
- **Safety:**
  - Binds to `127.0.0.1` only.
  - Each page embeds a per-process random token, and every POST must carry it. This stops other sites open in the browser from submitting forms (cross-site request forgery).
  - Review file serving resolves paths and refuses anything outside `data/reports`.
- **Look:**
  - Server-rendered HTML with one inline stylesheet and light/dark colour tokens.
  - Charts are inline SVG generated in Python.
  - No external requests from pages; works offline.
  - Readable on narrow windows.

## Errors

- **Form errors** (a `TradeError` or bad input) re-show the form with the message and the entered values.
- **Market data:** a missing price shows "no price" as the CLI does; pages never fail because Yahoo is down.
- **A failed job** is marked failed, with its log kept and linked.

## Testing

Pytest with FastAPI's `TestClient`, a temporary `--data-dir`, no network (the repo's conftest blocks it) and faked prices and agent runs:

- Every page returns 200 on an empty portfolio and on a sample one.
- Buy, sell and deposit through the forms leave `portfolio.json` and `trades.csv` identical to the same CLI commands.
- Refused trades change nothing and show the error. Undo restores the prior state.
- A POST without the token is rejected (403).
- Review file serving refuses paths outside `data/reports`.
- A review job does not start without confirmation, respects the cost cap, and a second job is refused while one runs (agents faked).

After the automated tests pass, a manual check: start the site on the real data folder and visit every page.
