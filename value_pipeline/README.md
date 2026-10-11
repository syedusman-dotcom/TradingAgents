# Value pipeline

A monthly routine on top of the value-investing TradingAgents agents (see `VALUE_INVESTING.md`):

1. **Screen** the S&P 500 + S&P 400 for good, cheap, financially healthy businesses (Magic Formula ranked).
2. **Review**: run the agents on every stock you own and on the top new candidates.
3. **Act**: get a short action list (`ACTIONS.md` / `ACTIONS.html`) with amounts and share counts.

**It never places trades.** You place orders at your broker, then record each fill with `buy` / `sell`. Positions and cash change only through those commands.

All commands run from the repo root:

```bash
uv run python -m value_pipeline <command>
```

## Monthly routine

```bash
uv run python -m value_pipeline run-all                 # screen + review (asks before spending)
open value_pipeline/data/reports/$(date +%F)/ACTIONS.html

# place the orders at your broker, sells first, then record each fill:
uv run python -m value_pipeline sell XYZ 4 87.10 --fees 1
uv run python -m value_pipeline buy ABC 2.5 140.20 --fees 1
uv run python -m value_pipeline status
```

## Commands

| Command | What it does |
|---|---|
| `init --cash 1000 [--currency USD] [--date D]` | Start a portfolio with cash. |
| `deposit AMOUNT [--date D]` | Add cash (negative withdraws). Counted in the SPY comparison. |
| `buy TICKER SHARES PRICE [--fees X] [--date D] [--note T]` | Record a buy: updates shares, average price (fees included) and cash; appends to `trades.csv`. Fractional shares work. A new position takes its thesis and intrinsic value from the latest review of that ticker. |
| `sell TICKER SHARES PRICE [--fees X] [--date D] [--note T]` | Record a sale and print the realised gain. Selling everything closes the position. |
| `status` | Each holding's price, value, gain, weight, last rating and days held; total value and return vs SPY. |
| `screen [--top N]` | Rank the universe; writes `data/screens/screen_YYYY-MM-DD.csv`. |
| `review [--yes] [--dry-run] [--candidates N]` | Run the agents and write the action list. |
| `run-all [--yes] [--dry-run]` | `screen`, then `review`. |

Use `--data-dir PATH` before the command (or `VALUE_PIPELINE_DATA`) to keep data somewhere else, and `-v` to log every skipped ticker.

## The screen

- **Universe**: S&P 500 + S&P 400 from Wikipedia (cached in `data/universe.csv` when Wikipedia is down). Financials, real estate and utilities are excluded, because EBIT/EV, ROIC and the Z-Score don't describe them; they are listed in `screen_<date>_excluded.csv`.
- **Metrics**, from Yahoo annual statements (cached per ticker for 7 days in `data/cache/`):
  - Earnings yield = EBIT / EV; ROIC = EBIT / (net working capital + net PP&E). Working capital excludes cash and is floored at zero, as in Greenblatt.
  - FCF yield = free cash flow / market cap.
  - Piotroski F-Score, all nine points recorded (`f_*` columns). Missing data scores 0 for that point and is counted in `fscore_missing`.
  - Altman Z-Score (original formula), debt/equity, interest coverage. No reported interest expense counts as unlimited coverage.
  - Trend: price vs the 200-day SMA and the 12-month return excluding the last month, classified as UPTREND / BASING / DOWNTREND / FREEFALL.
  - Formula value (`value_base`, `value_low`, `value_high`), its margin of safety (`mos_formula`) and the growth today's price implies (`implied_growth`). These are the same figures the agents see; see `VALUE_INVESTING.md`.
- **Hard filters** (`config.yaml`): market cap > $2B, F-Score ≥ 6, Altman Z > 1.8, positive FCF, interest coverage > 4.
- **Rank**: Magic Formula (rank by earnings yield + rank by ROIC, lower is better), ties broken by FCF yield. FREEFALL stocks are kept out of the top list and shown as "watch: falling knives".
- Tickers with missing data are skipped and listed with the reason in `screen_<date>_skipped.csv`.

The first screen of a week fetches every ticker (roughly 20-40 minutes); later runs that week read the cache.

## The review

1. Scores past decisions in the memory log (`settle_all_pending`).
2. Runs TradingAgents (market, news, fundamentals analysts) on every holding, then on the top `review.candidates` (default 5) screen picks you don't own. Every run sees your holdings and cash.
3. Prints the number of runs and the estimated cost (`cost_per_run`, default $2) and asks before starting, unless you pass `--yes`. It refuses to start above `max_cost_per_review` (default $40).
4. Saves each report to `data/reports/YYYY-MM-DD/TICKER/`. A failed run is retried once, then reported.
5. Turns ratings into actions:

   | Situation | Action |
   |---|---|
   | Held, Buy / Overweight | KEEP; ADD up to the target weight if cash allows |
   | Held, Hold | KEEP |
   | Held, Underweight | TRIM half the first time; SELL if Underweight twice in a row |
   | Held, Sell | SELL all |
   | Held, price above the agents' intrinsic value | flag **REACHED VALUE: consider selling** |
   | Not held, Buy | BUY (full size) |
   | Not held, Overweight | BUY (half size) |
   | Not held, anything else | skip |
   | FREEFALL | never buy or add, whatever the rating |

6. Sizing: equal weight, `1 / max(min_holdings, positions)`, capped at `max_position_weight` (10%); Overweight gets half. Buys never exceed cash. Cash includes the proceeds of the sells and trims on the same list, so place those first. Amounts are in dollars plus share counts at the latest price.
7. Writes `ACTIONS.md` and `ACTIONS.html` (one self-contained page that works on a phone) and prints the list.
8. Updates each holding's `last_rating`, `last_review_date`, `consecutive_underweight` and `intrinsic_value_estimate` in `portfolio.json`. It does not touch positions or cash.

The agents read financial statements from Yahoo (`review.statement_vendor`): about 5 years with every line (debt, interest, PP&E, buybacks, dividends). The repo's default, SEC EDGAR, has about 20 years but only top-line rows, and stays the default for the `tradingagents` CLI.

The intrinsic value comes from the Portfolio Manager's **Price Target** field (its prompt asks for the per-share intrinsic value there), or else from an "intrinsic value … $X" phrase in the decision.

The new-buys table shows both values (agents / formula). If the agents' value is more than 30% away from the formula's, the ticker is flagged so you can read their reasons before acting.

`--dry-run` skips the agents and the memory log, uses made-up ratings, writes to `data/reports/<date>-dry-run/`, and leaves `portfolio.json` untouched.

## Files

```
value_pipeline/data/            (gitignored: your holdings)
  portfolio.json                cash, deposits, positions
  trades.csv                    date, ticker, action, shares, price, fees, note
  backups/<timestamp>/          copy of both files before every write
  cache/<TICKER>.json           Yahoo data, refreshed after 7 days
  universe.csv                  last index membership fetched
  screens/screen_<date>.csv     every screened ticker with all metrics
  reports/<date>/ACTIONS.md|html, results.json, <TICKER>/complete_report.html
  logs/monthly_<date>.log       output of scheduled runs
```

The SPY comparison replays your deposits and trades into SPY on the same dates and with the same dollars. It uses prices without dividends, so it reads slightly in SPY's favour if you don't record your dividends as deposits.

## Scheduling (macOS, first Saturday of each month)

`scripts/monthly.sh` runs `run-all --yes` and logs to `value_pipeline/data/logs/`. launchd can't express "first Saturday", so the job starts every Saturday and the script exits unless the date is between the 1st and the 7th. To run it by hand on any day: `scripts/monthly.sh --now`.

1. Save this as `~/Library/LaunchAgents/local.value-pipeline.plist`, with your repo path:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>local.value-pipeline</string>
     <key>ProgramArguments</key>
     <array><string>/Users/syedusman/Developer/ValueTrading/scripts/monthly.sh</string></array>
     <key>StartCalendarInterval</key>
     <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
     <key>StandardOutPath</key><string>/Users/syedusman/Library/Logs/value-pipeline.log</string>
     <key>StandardErrorPath</key><string>/Users/syedusman/Library/Logs/value-pipeline.log</string>
   </dict>
   </plist>
   ```

2. Load it: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.value-pipeline.plist`
   (remove with `launchctl bootout gui/$(id -u)/local.value-pipeline`).
3. Test it now: `launchctl kickstart gui/$(id -u)/local.value-pipeline` (on a day after the 7th, edit the script or run `scripts/monthly.sh --now` instead).

**The Mac must be awake.** launchd runs a missed job when the Mac wakes from sleep, but not if it was shut down. To wake it automatically: `sudo pmset repeat wakeorpoweron S 08:55:00`. Keep the repo out of `~/Desktop`, `~/Documents` and `~/Downloads`: macOS blocks launchd jobs from those folders ("Operation not permitted", exit 126). `scripts/monthly.sh --check` tests access without spending anything.

## Tests

```bash
uv run pytest tests/test_value_pipeline.py
```

The portfolio maths, action rules, F-Score, Z-Score, Magic Formula, trend classification and the HTML page are tested on fixed sample data, with no network.
