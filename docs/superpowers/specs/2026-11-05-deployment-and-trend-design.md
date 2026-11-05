# Deployment and trend round — design

**Date:** 2026-11-05
**Status:** the user asked "where can we improve, missing something, how can we increase a bit of risk taking, can we also invest with short-term goals like the Turtles approach"; Claude gave an assessment with a recommendation; the user said "you decide". Every choice below is Claude's call unless marked otherwise.

## Why

The record since 2026-10-11: 99 agent reviews on 53 companies, no Buy rating, nine Overweights, 94% of the portfolio in cash, measured against SPY. Four findings drive this round:

1. **Recording a trade outside dollars is broken.** `apply_buy` subtracts `shares × price` from dollar cash and `portfolio_view` values `shares × price` as dollars. `buy DNLM.L 84 900` would cost "75,600 dollars". The screen and review became currency-aware on 2026-11-05; the trade log and the dashboard did not. The first UK or European purchase would hit this.
2. **Idle cash has no rule.** The lane backtest found 2 to 7 value buys a year in the US. Waiting in cash against an index benchmark is the largest cost in the process.
3. **Reviews are spent on peak-cycle names.** The formula values the up-to-five-year average EBIT. Across 33 reviewed names the agents' value over the formula's value tracks trailing-twelve-month EBIT over the five-year average (Spearman 0.69). THO, PPC and BYD (ratio 0.50 to 0.55) showed 39% to 51% formula margins of safety and were all rejected.
4. **A short-term trend sleeve is untested.** The user's rule is no new risk without a backtest. The price cache already holds daily closes back to 1999 for 659 US tickers.

## Goals

- Part 0: buys, sells and holdings in any quote currency, with dollars as the portfolio's base.
- Part 1: a rule that parks a share of idle money in an index fund and funds buys from it; the tool only suggests.
- Part 2: Magic Formula candidates whose profit runs well below the average give their review slot to the next name.
- Part 3: a backtest of Turtle-style breakout rules and a momentum alternative, after costs, with a verdict rule written down before the run.

### Non-goals

Live trend signals (a later round, only if the verdict supports it). Shorting, leverage, pyramiding. A non-US trend universe. A formula sleeve or starter positions on Hold ratings (wait for the calibration page's first rows after 2026-11-21). Any change to a rating bar or to `sizing` for stocks. Trading: the tool never places an order.

## Verified facts (code on 2026-11-05, main at 8a44fb2)

- `portfolio.Position` has `ticker, shares, avg_price, first_bought, thesis, intrinsic_value_estimate, last_rating, last_review_date, consecutive_underweight, kill_criteria, entry_price, next_earnings_date, lane`. `apply_buy` / `apply_sell` work in one unit; `operations.record_buy` / `record_sell` append `date,ticker,action,shares,price,fees,note` to `trades.csv`; `read_trades` is a `csv.DictReader`.
- `operations.portfolio_view(paths, price_fn, benchmark_fn)` values `shares × price`; `benchmark.compare` replays `shares × price` from the trade log into SPY; `operations.realised_gains` reads the same rows.
- `market_data.quote_fx(ticker) -> (currency | None, usd_per_unit | None)`: `('GBp', 0.01348)` for DNLM.L, `('EUR', 1.1238)` for SXR8.DE, `(None, 1.0)` when unreadable, `(currency, None)` when the rate is unknown. `market_data.latest_price` returns the quote-unit price (SXR8.DE 719.88).
- `review._holding_value(p, price, fx)` already values an unreviewed holding at `shares × price × rate`, falling back to `shares × avg_price`.
- `actions.plan(decisions, cash, sizing, others)` sizes buys against `cash + proceeds`; `total_value = cash + held + others`; `summary` has `total_value, cash, target_weight, cash_after, over_cap`.
- `screener.top_candidates(paths, owned, n, compounders, dislocations, inflections)` takes the `n` best `mf_position` rows that are not owned and not FREEFALL, then the hinted lanes. The screen CSV has `ttm_vs_5y_ebit` (median 1.18; 7% of passing rows below 0.9; missing for names without quarterly statements, such as DNLM.L).
- `backtest.price_history(ticker, paths, fetch, refresh, stale_before)` returns a frame `date, close, adj_close, split` from `data/cache/backtest/prices/`; `backtest.run` builds point-in-time rows per 30 June cohort (2015 to 2024) through `screener.evaluate`, with S&P 500 join dates from `added_dates`; the rows are not returned today. `backtest._universe` reads `cfg["universe"]["indexes"]`, which is now all 23 markets (non-US names are skipped for want of EDGAR statements).
- The website serves `data/reports/` at `/reports/{rel:path}`; `render.actions_html(md, title)` turns Markdown into a page.

## Design

### Part 0: trades and holdings outside dollars

The portfolio's base stays dollars. A price is always entered and shown in the stock's quote unit (pence for London).

- `Position` gains `currency: str | None = None` (None means dollars) and `avg_fx: float = 1.0` (dollars per quoted unit paid, weighted by quote cost). `avg_price` stays in the quote unit, fees included. Old `portfolio.json` files load unchanged.
- `apply_buy(pf, ticker, shares, price, fees=0.0, ..., currency=None, fx=1.0)`: `cost_usd = shares × price × fx + fees` (fees are dollars); the cash check and the debit use `cost_usd`; `avg_price` is `(shares × price + fees / fx) / shares`; on an add, `avg_price` and `avg_fx` are weighted by quote cost. `apply_sell(pf, ticker, shares, price, fees=0.0, fx=1.0)`: proceeds `shares × price × fx − fees` in dollars; returns the realised gain in dollars against `shares × avg_price × avg_fx`.
- `operations.record_buy` / `record_sell` take `fx: float | None = None`. When None they call `market_data.quote_fx(ticker)`; an unknown rate raises `TradeError` naming `--fx`. A sale of a held position uses the position's currency. `trades.csv` gains the columns `currency` and `fx`; a file with the old header is rewritten once with the new header (blank currency, fx 1) before the first new row; readers treat a missing or blank `fx` as 1.
- CLI `buy` and `sell` gain `--fx`. The confirmation line states both units: `BUY 84 DNLM.L @ 900.00 GBp (fx 0.01348) = $1,019.40`. The website's trade form gains an optional rate field prefilled from `quote_fx` and shows the dollar total.
- `portfolio_view(paths, price_fn, benchmark_fn, fx_fn=market_data.quote_fx)`: `value = shares × price × rate`; when the price or the rate is missing, the cost basis `shares × avg_price × avg_fx`. `gain` is the dollar gain `value / (shares × avg_price × avg_fx) − 1`. Each row carries `currency`; `status` and the dashboard print the price with its unit when it is not dollars. Margin of safety stays quote unit against quote unit.
- `benchmark.compare` and `realised_gains` multiply each trade's gross by its `fx`.
- `review._holding_value` falls back to `shares × avg_price × avg_fx`. `Portfolio.to_context` is unchanged (quantities and quote-unit prices).

### Part 1: index-fund parking

```yaml
parking:
  ticker: SXR8.DE        # iShares Core S&P 500 UCITS ETF (Acc), Xetra, quoted in euros; "" turns parking off
  share: 0.7             # of idle money: cash plus the fund, after this review's buys
  min_trade_amount: 250  # dollars; smaller moves into the fund are not suggested
```

The fund is a holding like any other in `portfolio.json` (recorded with `buy` / `sell`), recognised by its ticker. With `ticker: ""` every behaviour is exactly today's.

- **Never analysed.** The review, the daily check, the watch and the committee do not run agents or triggers on it. It is not a `Decision`, not in `others`, and does not count toward the number of positions, the 10% cap, the sector cap or the lane caps.
- **Counts as money.** `plan(decisions, cash, sizing, others, parked=0.0, parking=None)`: `total_value` includes `parked` (the fund's dollar value); buys are sized against `cash + proceeds + parked`.
- **One suggestion per review**, computed after the stock sizing, with `liquid = cash + proceeds`, `buys` = the dollars of BUY and ADD actions, `idle = liquid + parked − buys`, `target = share × idle`:
  - `buys > liquid`: **UNPARK** `buys − liquid` (sell that much of the fund to fund the buys; place it first).
  - else `target − parked ≥ min_trade_amount`: **PARK** `min(target − parked, liquid − buys)`.
  - else nothing. The fund is never sold merely for being above its share: selling happens only to fund a buy.
- `summary` gains `parked` (value before), `parking` (`{"action", "amount", "shares"}` or None; shares from the fund's dollar price, None without one) and `cash_after` becomes the cash left after the stock actions and the parking move.
- **ACTIONS.md**: the Portfolio block gains `Index fund (SXR8.DE): $X (N% of the portfolio)`; a section `## Index fund` after `## New buys` holds one line (`Buy $6,700.00 of SXR8.DE (about 8.3 shares)`, `Sell $1,000.00 of SXR8.DE to fund the buys; place it first`, or `No change.`) and the sentence `Idle money sits in the index fund so it moves with the market and can fall; sell it only to fund a buy.` The section is absent when parking is off.
- `status` and the dashboard show the fund as a holding with the rating text `index fund`, no margin of safety.
- The SPY comparison is unchanged: the fund's trades are in the trade log like any other.

### Part 2: run-rate filter on candidates

```yaml
review:
  min_run_rate: 0.8   # Magic Formula candidates need TTM EBIT at least this multiple of the 5-year average; 0 turns it off
```

`screener.top_candidates(..., min_run_rate=0.0)` drops, from the Magic Formula picks only, rows whose `ttm_vs_5y_ebit` is a number below `min_run_rate`; the place goes to the next row by `mf_position`. A missing ratio passes (no evidence either way). The hinted lanes are untouched: dislocations carry their own `fundamentals_intact` test. It returns the picks as today; a second function `screener.run_rate_skips(paths, owned, n, min_run_rate) -> list[dict]` (ticker, ratio) lists what was passed over above the last pick, and the review prints `Left out, profit running below the five-year average: THO 0.50, PPC 0.52` before the cost estimate. `review --tickers` and the daily triggers ignore the filter. It is not a rating bar: it decides which names an agent run is spent on.

### Part 3: trend-sleeve backtest

`value_pipeline/trend.py`, command `python -m value_pipeline trend-backtest [--from 2015-07-01] [--no-fetch]`, outputs `data/reports/backtest/trend_<date>.md`, `.html` and `trend_<date>_trades.csv`; the `/backtest` page links to the newest HTML. No agent is called, nothing trades.

```yaml
trend:
  start: "2015-07-01"
  equity: 10000
  risk_per_trade: 0.01       # of equity, lost if the stop is hit
  stop_n: 2.0                # stop this many N below the entry
  n_days: 20                 # N = mean absolute daily close change over this many days
  max_positions: 10
  max_position_weight: 0.20  # of equity
  cost_per_side: 0.0035      # commission plus the broker's currency fee, each buy and each sell
  market_filter: true        # new entries only while SPY closes above its 200-day average
```

- **Data.** `adj_close` from the backtest price cache (splits and dividends included). Universe: today's S&P 500 and S&P 400 members only (`indexes: [sp500, sp400]`, whatever `universe.indexes` holds); an S&P 500 name is eligible from its join date; the S&P 400 has no join dates, which the report states as survivorship bias.
- **Two universes.** `all`: every member. `quality`: names whose point-in-time cohort row (the lane backtest's 30 June snapshot) has `passes_filters` true, eligible from that cohort date until the next one. `backtest.run(..., keep_rows=False)` returns the rows and price histories when asked, so the cohort machinery is reused, not copied.
- **Calendar.** SPY's trading days from `start`. A signal read at the close of day t is traded at the close of day t+1 (the ticker's next close; none within 5 days cancels the order). Nothing uses a price after t to decide at t.
- **Systems.**
  - `breakout_55_20` (the primary): enter when the close exceeds the highest close of the prior 55 days; exit when the close falls below the lowest close of the prior 20 days or to `entry − stop_n × N` or lower.
  - `breakout_20_10`: the same with 20 and 10.
  - `momentum_12_1`: on the first trading day of each month hold the `max_positions` eligible names with the highest return from t−252 to t−21 among those closing above their 200-day average, equal weight at entry; names that stay are left alone.
- **Sizing (breakouts).** `shares = risk_per_trade × equity / (stop_n × N)`, capped at `max_position_weight × equity` and by cash. One unit per name, no adds. When entries outnumber the free slots or the cash, the highest 126-day return goes first. The market filter blocks entries, never exits.
- **Costs.** `cost_per_side × trade value` on every buy and sell. Cash earns nothing.
- **Measures**, per system and universe: CAGR, total return, maximum drawdown, annualised daily volatility, return over volatility, average share of equity invested, trades, trades per year, win rate, average win, average loss, payoff ratio, average holding days, costs per year as a share of equity, share of the profit from the five best trades, and a calendar-year table against SPY. Benchmarks: SPY bought and held, and SPY held at the system's average exposure with the rest in cash.
- **Sensitivity**, one setting at a time on the primary system and the quality universe: `max_positions` 5 / 10 / 20, `cost_per_side` 0 / 0.0035 / 0.006, `market_filter` on / off, `risk_per_trade` 0.005 / 0.01 / 0.02.
- **Verdict**, fixed here before any run. The primary result (breakout_55_20, quality universe, default settings) *supports a live sleeve* when all three hold: (a) CAGR at least SPY's plus 1 point, **or** CAGR at least SPY's minus 2 points with a maximum drawdown no more than two thirds of SPY's; (b) the same test still passes at `cost_per_side` 0.006; (c) the system gained in at least 60% of the calendar years. Otherwise it *does not support* one. The report prints the verdict sentence with the three numbers, and applies the same test to `momentum_12_1` as the alternative. A supporting verdict starts a design for live signals; it does not turn anything on.

### Settled details (from the builder check; all Claude's calls)

- **The fund's currency.** The fund holds US stocks; its euro price times the euro's dollar rate is a dollar value that tracks the S&P 500, so in the dollar base there is no added euro exposure. Value is `shares × price × quote_fx`. With no dollar rate or no price the fund is valued at its cost basis (`shares × avg_price × avg_fx`) and the section says `No suggestion: no dollar price for SXR8.DE today.`
- **Sizing against the fund.** `plan` already caps every buy by `available`; with parking `available = cash + proceeds + parked`, so `buys` never exceeds `liquid + parked` and an UNPARK is always coverable. UNPARK is not subject to `min_trade_amount` (it funds a buy) and is rounded to cents.
- **One helper.** `config.parking_ticker(cfg) -> str | None` (upper-cased, None when blank) is the only test of "is this the fund". Skipped for the fund: the review's agent runs (a `--tickers` list naming it is refused with a message), the daily triggers, the watch alerts, the committee's holdings list. Shown as `index fund`: `status`, the dashboard, the ACTIONS Portfolio block.
- **Trade-log upgrade.** Lazily, on the first write after the change, written whole to a temporary file and renamed, after the backup `save` already makes. No non-dollar position exists today (the only holding is HRB), so old positions are dollars.
- **Trend simulation.** Equity compounds and every size is taken from the equity at the signal's close. Fractional shares everywhere. N is fixed at entry; the stop is read on closes. A name may re-enter on a new breakout from the day after its exit fill. `momentum_12_1` buys `equity / max_positions` per new name, capped by cash and by `max_position_weight`. Prices are loaded from 300 trading days before `start` so every signal exists on the first day. The last cohort's quality list runs to the end of the data; a name with no cohort row is outside `quality` and inside `all`.
- **Verdict terms.** SPY is measured on `adj_close` over the identical span. "Gained in a calendar year" means a positive return in that year; the part-years at both ends count as years.

### Documentation

`value_pipeline/README.md`: buying outside dollars (`--fx`), the index-fund section, the run-rate filter, the trend backtest with its limits (close-only N, next-close fills, survivorship, no shorting). `config.yaml` and `config.DEFAULTS` carry the three new sections and `review.min_run_rate`.

## Testing

- Part 0: a pence buy debits dollars and stores pence; an add reweights `avg_price` and `avg_fx`; a sale returns the dollar gain; an unknown rate raises and `--fx` resolves it; an old `trades.csv` and an old `portfolio.json` load and upgrade; `portfolio_view` values pence holdings in dollars and falls back to cost basis; `benchmark.compare` replays a pence trade at its dollar amount; the CLI line shows both units.
- Part 1: parking off reproduces today's `plan` output exactly; PARK when idle money exceeds the share; no PARK below `min_trade_amount`; UNPARK when buys exceed liquid cash and the buy is sized in full; the fund is absent from positions, caps and agent runs in a dry-run review; ACTIONS shows the section and the three line forms.
- Part 2: a low-ratio row is skipped and the next takes its place; a missing ratio passes; hinted lanes are unaffected; `min_run_rate: 0` is today's behaviour; the review prints the left-out names.
- Part 3: on synthetic closes — entry on the day after the breakout, exit on the day after the 20-day low, the stop, the sizing arithmetic and both caps, slot priority, the market filter, costs on both sides, the monthly momentum pick, eligibility windows, maximum drawdown and CAGR on a known series, and a no-lookahead test (changing closes after day t leaves every trade decided by t unchanged); the verdict sentence for a passing and a failing result.
- After the merge: `trend-backtest` on the cached prices; `review --dry-run` to read the index-fund line; `buy --help`.

## Rollout

Worktree, subagent-driven implementation (Part 0, then Part 1; Parts 2 and 3 independent), final review, merge to main, push, website restart, the trend backtest run and its verdict read to the user.
