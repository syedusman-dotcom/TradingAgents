# Value pipeline

A weekly routine on top of the value-investing TradingAgents agents (see `VALUE_INVESTING.md`):

1. **Screen** every Nordnet market (S&P 500 and 400, UK, Europe, Canada; see The screen) for good, cheap, financially healthy businesses (Magic Formula ranked).
2. **Review**: run the agents on every stock you own and on the top new candidates.
3. **Act**: get a short action list (`ACTIONS.md` / `ACTIONS.html`) with amounts and share counts.

**It never places trades.** You place orders at your broker, then record each fill with `buy` / `sell`. Positions and cash change only through those commands.

All commands run from the repo root:

```bash
uv run python -m value_pipeline <command>
```

## Weekly routine

```bash
uv run python -m value_pipeline run-all                 # screen + theme scan + review (asks before spending)
open value_pipeline/data/reports/$(date +%F)/ACTIONS.html

# place the orders at your broker, sells first, then record each fill:
uv run python -m value_pipeline sell XYZ 4 87.10 --fees 1
uv run python -m value_pipeline buy ABC 2.5 140.20 --fees 1
uv run python -m value_pipeline status
```

## Website

`uv run python -m value_pipeline web` opens http://127.0.0.1:8765. It does everything the commands do, on the same files, so the two always agree. It never trades, and it can only be reached from this Mac.

- **Dashboard**: value, cash, return vs SPY with a chart, holdings, the latest weekly watch, the next review date.
- **Trade**: record buys, sells and deposits; looking up a ticker shows its price and last rating.
- **History**: every trade and deposit with realised gains, and undo for the last entry. Undo is refused once something else, such as a review, has changed the files since.
- **Reviews** and **Stock**: past reviews and agent reports, and each ticker's ratings and values over time.
- **Screen**: the latest screen, sortable and filterable.
- **Calibration**: past decisions against the prices that followed (see Calibration below). Reads cached closes only.
- **Backtest**: the newest lane-rule backtest report (see Backtest below). Reads the saved report only.
- **Run**: start a screen, a review, a watch or the daily check. A review shows its run count and cost first and starts only when you confirm. One job runs at a time, and it keeps running if you close the page.
- **Settings**: edit `config.yaml`, validated and backed up on every save.

## Commands

| Command | What it does |
|---|---|
| `init --cash 1000 [--currency USD] [--date D]` | Start a portfolio with cash. |
| `deposit AMOUNT [--date D]` | Add cash (negative withdraws). Counted in the SPY comparison. |
| `buy TICKER SHARES PRICE [--fees X] [--date D] [--note T]` | Record a buy: updates shares, average price (fees included) and cash; appends to `trades.csv`. Fractional shares work. A new position takes its thesis and intrinsic value from the latest review of that ticker. |
| `sell TICKER SHARES PRICE [--fees X] [--date D] [--note T]` | Record a sale and print the realised gain. Selling everything closes the position. |
| `status` | Each holding's price, value, gain, weight, last rating and days held; total value and return vs SPY. |
| `superinvestors [--force]` | Refresh which tracked value investors (Buffett, Klarman, Li Lu, Pabrai, Russo, Akre; editable in `config.yaml`) hold what, from their latest 13F filings. Shown in the screen's Superinvestors column and on stock pages. A second opinion only: 13Fs are up to ~4.5 months old by the time you read them, and they never feed the agents or the action rules. The monthly screen refreshes it weekly at most. |
| `web [--port 8765] [--no-browser]` | Open the local website (this Mac only): dashboard, trades, history with undo, reviews and reports, stock pages, the screen, running jobs and settings. Stop with Ctrl+C. |
| `calibration [--horizon {30,90,180,365}]` | Every past decision against the closes that followed it, then the scores and one verdict per lane (see Calibration below). Fetches and caches the closes it has not seen. Default horizon 180 days. |
| `backtest [--from YEAR] [--to YEAR] [--holds 1,3] [--tickers A,B] [--refresh] [--no-fetch]` | The lane rules replayed on past cohort dates against SPY (see Backtest below). No agents. Writes `reports/backtest/backtest_<date>.md` and `_picks.csv` and prints the verdict. |
| `screen [--top N]` | Rank the universe (every market in `universe.indexes`); writes `data/screens/screen_YYYY-MM-DD.csv`. |
| `review [--yes] [--dry-run] [--candidates N]` | Run the agents and write the action list. |
| `run-all [--yes] [--dry-run]` | `screen`, then the theme scan (when `lanes.themes` names any), then `review` with the scan's inflection candidates. |
| `themes` | The theme scan alone (see Themes below): one quick model call per theme in `lanes.themes`, the named tickers fetched and checked, `reports/themes_<date>.md` written and printed. No review. |
| `watch [--no-notify]` | The free weekly check (prices only, no agents): a holding above its intrinsic value or in FREEFALL, a stock last rated Buy/Overweight now at least 30% below its value, or a stock now at or below the entry price its last review gave (ENTRY PRICE). Lists each holding's kill criteria for you to check against the news. A `New ideas` section lists names you don't own that newly carry a compounder, inflection or dislocation hint in the latest screen since the previous watch (the first watch lists them all), so a scare that starts mid-month is seen that Saturday. Writes `reports/watch_<date>.md` (and `watch_<date>.json`, the hinted list the next watch compares against) and shows a macOS notification when something needs a look. |

Use `--data-dir PATH` before the command (or `VALUE_PIPELINE_DATA`) to keep data somewhere else, and `-v` to log every skipped ticker.

## The screen

- **Universe**: the markets named in `universe.indexes`, by default every Nordnet market below, from Wikipedia (cached in `data/universe.csv` when Wikipedia is down or returns fewer than 10 tickers for a market). A company listed in two markets is kept once. The ATX has no usable Wikipedia table, so its 20 members are a static list in `market_data.py` (`ATX_MEMBERS`), to be checked yearly. Financials, real estate and utilities are excluded, because EBIT/EV, ROIC and the Z-Score don't describe them; they are listed in `screen_<date>_excluded.csv`.

| Key | Country | Wikipedia list | Yahoo suffix |
|---|---|---|---|
| `sp500` | US | List_of_S&P_500_companies |  |
| `sp400` | US | List_of_S&P_400_companies |  |
| `ftse100` | UK | FTSE_100_Index | `.L` |
| `ftse250` | UK | FTSE_250_Index | `.L` |
| `dax` | Germany | DAX | `.DE` |
| `mdax` | Germany | MDAX | `.DE` |
| `cac40` | France | CAC_40 | `.PA` |
| `cacnext20` | France | CAC_Next_20 | `.PA` |
| `aex` | Netherlands | AEX_index | `.AS` |
| `amx` | Netherlands | AMX_index | `.AS` |
| `bel20` | Belgium | BEL_20 | `.BR` |
| `psi` | Portugal | PSI-20 | `.LS` |
| `iseq20` | Ireland | ISEQ_20 | `.IR` |
| `ftsemib` | Italy | FTSE_MIB | `.MI` |
| `smi` | Switzerland | Swiss_Market_Index | `.SW` |
| `smim` | Switzerland | SMI_MID | `.SW` |
| `atx` | Austria | static list in `market_data.py` (check yearly) | `.VI` |
| `ibex35` | Spain | IBEX_35 | `.MC` |
| `omxs30` | Sweden | OMX_Stockholm_30 | `.ST` |
| `omxc25` | Denmark | OMX_Copenhagen_25 | `.CO` |
| `omxh25` | Finland | OMX_Helsinki_25 | `.HE` |
| `obx` | Norway | OBX_Index | `.OL` |
| `tsxcomp` | Canada | S&P/TSX_Composite_Index | `.TO` |

- **Currency**: each statement and price is converted to dollars at that day's FX, and the dollar figures drive the ranking, the filters, market cap and EV. Price, per-share values and the entry price stay in the stock's quote unit (`GBp` pence for London, `EUR`, `USD`, ...); screen rows carry `market`, `country`, `quote_currency` and `fx_quote`, and a review's note and the stock page name the quote currency. Prices refresh every `screen.price_days` (1) days; statements are cached `screen.cache_days` (7).
- **Metrics**, from Yahoo annual statements (cached per ticker for 7 days in `data/cache/`):
  - Earnings yield = EBIT / EV; ROIC = EBIT / (net working capital + net PP&E). Working capital excludes cash and is floored at zero, as in Greenblatt.
  - The same two averaged over up to 5 years (`earnings_yield_avg`, `roic_avg`), plus the worst year's ROIC (`roic_min`) and the yearly change in share count (`dilution_per_year`).
  - FCF yield = free cash flow / market cap.
  - Piotroski F-Score, all nine points recorded (`f_*` columns). Missing data scores 0 for that point and is counted in `fscore_missing`.
  - Altman Z-Score (original formula), debt/equity, interest coverage. No reported interest expense counts as unlimited coverage.
  - Trend: price vs the 200-day SMA and the 12-month return excluding the last month, classified as UPTREND / BASING / DOWNTREND / FREEFALL.
  - Formula value (`value_base`, `value_low`, `value_high`), its margin of safety (`mos_formula`) and the growth today's price implies (`implied_growth`). These are the same figures the agents see; see `VALUE_INVESTING.md`.
  - Accounting checks (accruals ratio, cash conversion, receivables vs revenue, Beneish M-score, and their flags) and the 7-year expected return with its rank (`expected_return`, `er_rank`); see below.
- **Hard filters** (`config.yaml`): market cap > $2B, F-Score ≥ 6, Altman Z > 1.8, positive FCF, interest coverage > 4, ROIC ≥ 8% in every year (stable quality), share count growing ≤ 3% a year (no heavy dilution).
- **Rank**: Magic Formula on the multi-year averages (rank by average earnings yield + rank by average ROIC, lower is better), ties broken by FCF yield. Averages keep a cyclical peak from ranking as a bargain, and ROIC is capped at 100% so asset-light outliers can't take the top on one ratio. FREEFALL stocks are kept out of the top list and shown as "watch: falling knives".
- Tickers with missing data are skipped and listed with the reason in `screen_<date>_skipped.csv`.

The first screen of a week fetches every ticker (roughly 20-40 minutes); later runs that week read the cache.

## The review

1. Scores past decisions in the memory log (`settle_all_pending`).
2. Runs TradingAgents (market, news, fundamentals analysts, with the filings tools for US filers) on every holding, then on the names in `lanes.tickers`, the top `review.candidates` (default 5) screen picks you don't own, up to `lanes.compounder_candidates` (default 2) compounder-hinted names, up to `lanes.dislocation_candidates` (default 3) dislocation-hinted names, and up to `lanes.inflection_candidates` (default 3) inflection candidates, in that order: the theme scan's names (`run-all`) come first and inflection-hinted screen names (best expected return, not owned, not in FREEFALL) fill the remainder. Every run sees your holdings and cash.
3. Prints the number of runs and the estimated cost (`cost_per_run`, default $2) and asks before starting, unless you pass `--yes`. Candidates are dropped from the end to stay under `max_cost_per_review` (default $40); holdings are never dropped, and if they alone exceed the cap the review refuses to start. Only one review runs at a time: the scheduled run, the website and a terminal share a lock in the data folder, and a second start is refused rather than spending twice.
   With `TRADINGAGENTS_LLM_PROVIDER=claude_code` the agents run on your Claude plan through Claude Code, so the cap is a usage budget rather than a bill; the ACTIONS report then shows the measured tokens and what the run would have cost at API prices. The website runs reviews in its own process: restart it after changing `.env`. Inside Claude Code, `/review HRB` runs one stock and relays the agents as they finish.
4. Saves each report to `data/reports/YYYY-MM-DD/TICKER/`. A failed run is retried once, then reported.
5. Turns ratings into actions:

   | Situation | Action |
   |---|---|
   | Held, Buy / Overweight | KEEP; ADD up to the target weight if cash allows |
   | Held, Hold | KEEP |
   | Held, Underweight | TRIM half the first time; SELL if Underweight twice in a row. An Underweight repeated within 21 days (a re-run, a `--tickers` check) is the same verdict, not a second strike |
   | Held, Sell | SELL all |
   | Held, above `max_position_weight` (10%) of the portfolio | TRIM back to the cap, whatever the rating (holdings left out of a `--tickers` review are named instead) |
   | Held, price above the agents' intrinsic value | flag **REACHED VALUE: consider selling** |
   | Not held, Buy | BUY (full size) |
   | Not held, Overweight | BUY (half size) |
   | Not held, Buy / Overweight but the price is above the agents' own value | skip: no margin of safety |
   | Not held, anything else | skip |
   | FREEFALL | never buy or add, whatever the rating |

6. Sizing: equal weight, `1 / max(min_holdings, positions)`, capped at `max_position_weight` (10%); Overweight gets half. Buys and adds also stop at `max_sector_weight` (30%) in one sector (GICS, from the screen, else from the fundamentals cache), counting the sells on the same list and the holdings not on it. Buys never exceed cash. Cash includes the proceeds of the sells and trims on the same list, so place those first. Amounts are in dollars plus share counts at the latest price.
7. Once every stock is done, the investment committee looks at all of them together (below).
8. Writes `ACTIONS.md` and `ACTIONS.html` (one self-contained page that works on a phone) and prints the list.
9. Updates each holding's `last_rating`, `last_review_date`, `consecutive_underweight`, `intrinsic_value_estimate` and kill criteria in `portfolio.json`. It does not touch positions or cash.

The agents read financial statements from Yahoo (`review.statement_vendor`): about 5 years with every line (debt, interest, PP&E, buybacks, dividends). The repo's default, SEC EDGAR, has about 20 years but only top-line rows, and stays the default for the `tradingagents` CLI.

The intrinsic value comes from the Portfolio Manager's **Price Target** field (its prompt asks for the per-share intrinsic value there), or else from an "intrinsic value … $X" phrase in the decision.

The new-buys table shows both values (agents / formula). If the agents' value is more than 30% away from the formula's, the ticker is flagged so you can read their reasons before acting.

`--dry-run` skips the agents, the memory log and the committee, uses made-up ratings and decisions (with every checklist section filled in, so the ACTIONS layout can be checked for free), writes to `data/reports/<date>-dry-run/`, and leaves `portfolio.json` untouched.

## Verified sources and consensus

Two filings tools give the fundamentals and news analysts the company's own words instead of a
summary of them. `get_quarterly_report` fetches management's discussion and analysis (results of operations and
liquidity) from the latest 10-Q on SEC EDGAR; `get_earnings_release` fetches the latest
earnings release (exhibit 99.1 of the 8-K reporting results). They cover US filers only: for a European, UK, Canadian, Japanese or Korean
name the tools say so and the analysts carry on with Yahoo's statements and news. The analysts
are told these are verified sources, to quote them with the filing date and to name a number's
source.

`get_annual_report` gives the fundamentals analyst the latest 10-K in the same way: the Business
section (Item 1, its first 6,000 characters), then `## Products and innovation lines` (the
paragraphs past that cut that mention new products, launches, innovation, research and
development, a pipeline, patents or a roadmap, kept whole, or "None past the cut."), then Risk factors and
management's discussion (MD&A). The analyst quotes the Business section for what the company is
building, with the filing date.

The worksheet gains two blocks. `## Last eight quarters (newest first)` shows revenue, margins
and cash flow quarter by quarter, so a turn in the trend is visible without opening the
filings. `## Consensus (Yahoo estimates)` shows what analysts expect for growth and the price
target. The Portfolio Manager answers it in a `**Consensus**:` line (agree, above or below
the street, and why) and a soft check flags a decision that skips it. `results.json` keeps the
view and the figures as `consensus_view`, `consensus_growth` and `consensus_target`.

## How a stock is judged

### Valuation worksheet

The fundamentals report starts from a worksheet computed in code, not from a guess: the formula value at 0%, 3% and 5% growth, the earnings-power value (today's after-tax EBIT with no growth at all, less net debt), a small grid of values at the discount rate ±1 point, and the growth the price already assumes. That last number comes with a base rate: how many companies of the same size actually sustained that growth for ten years. If the price needs growth only one company in ten has managed, the report says so.

### Accounting checks

Four checks look for profits that are not backed by cash: the accruals ratio (net income minus operating cash flow, over assets), cash conversion (operating cash flow over net income for three years), receivables growing faster than revenue, and the Beneish M-score, a classic test for earnings manipulation. Capex below half of depreciation for two years is flagged too, since a business that stops reinvesting looks more profitable than it is. A breach is a named flag in the report and in the screen columns; it is a reason to read closely, not an automatic no.

### Capital allocation

The report shows where five years of free cash went (dividends, buybacks, acquisitions, debt paid down) and what the company paid per share it bought back, next to the average share price that year. Management that buys back stock below its value is creating wealth for you; buying back near the top is not.

### Innovation and reinvestment

The worksheet's `## Innovation and reinvestment` section asks whether the company is building its next decade or harvesting the last one: R&D / revenue (latest and five-year average), R&D growth against revenue growth over the same span, gross profit added per R&D dollar, growth capex (capex beyond depreciation) / revenue over three years, growth spend (R&D + growth capex + acquisitions) / operating cash flow over three years, the return on incremental capital over three years, and a harvesting signal. R&D comes from Yahoo's income statement when the company reports it; when it does not (DECK, HRB) the R&D lines read `n/a` and the section says to judge reinvestment from growth capex, acquisitions and the filings. The harvesting signal is `yes` when the latest year cut R&D by a point of revenue or ran capex below half of depreciation (the accounting check's bar; Yahoo's depreciation includes acquired-intangible amortisation) while the EBIT margin rose, and the section names which cut fired. Any number that cannot be computed reads `n/a`. The fundamentals analyst sets these numbers against the product evidence in the 10-K's Business section and the latest 10-Q, and concludes "investing" or "harvesting".

### Expected return

Each stock gets a 7-year expected annual return: what it pays out (dividends and buybacks as a yield), plus the growth its returns on capital can fund (capped by the base rate and at 10%), plus or minus the multiple moving back to a normal EV/EBIT (at most ±5 points a year). The normal multiple is the sector's median in the screen and the company's own history in the agents' report, so the two figures can differ. The screen still picks candidates by Magic Formula rank; `er_rank` sits next to it, ACTIONS shows each stock's expected return, and the investment committee ranks the reviewed stocks by it.

### Pre-mortem

After the risk debate, one extra step imagines it is three years later and the position has lost 40%, and writes down the most likely cause, the evidence visible today, the one metric that would show it first, and the disruption exposure: who or what could make the product unnecessary, and the first visible sign that it is happening. The Portfolio Manager has to answer it before deciding.

### The decision checklist

The Portfolio Manager's decision is no longer just a rating and a thesis. It gives a PASS or FAIL with a reason for each test (good business, good management, still investing, cheap), any serious red flags, 3 to 6 kill criteria (verifiable facts that would prove the thesis wrong and mean selling), an entry price to buy at or below, bear/base/bull scenarios whose probabilities add up to 100% with the expected value and the upside against the downside, a value bridge that starts at the worksheet's formula value and shows every adjustment that takes it to the price target, and its answer to the pre-mortem. The decision is checked in code (the bridge adds up, the probabilities sum to 1, an entry price is there unless it is a Sell); if something is off, the Portfolio Manager gets one chance to fix it, and anything still wrong is printed under **Unresolved Checks**.

Still investing passes when R&D and growth capex are held or rising, the return on incremental capital is above the cost of capital, and the filings show products or a pipeline; a PASS needs a quoted filing or a number. It must PASS for the compounder lane. Elsewhere it is advisory: a FAIL does not stop the rating, but the thesis must say what is being harvested and for how long the margin can last, and one kill criterion names the reinvestment level (R&D or capex as a share of revenue) below which the thesis is wrong. Older decisions without the line still load and show their three tests.

### Investment committee

Each stock is reviewed on its own, so after the last one a committee looks at all of them together: it ranks them by expected return adjusted for the downside and the red flags, proposes weights under the 10% per stock and 30% per sector caps, proposes swaps only when a candidate beats a holding by at least 5 points a year, and names concentration risks (same sector, same customers, same macro bet). It can never buy a stock its own review rated Sell. Its answer is shown above Holdings, next to the rules' actions; wherever the two disagree, the difference is listed so you can decide. The rules still produce the amounts. It is skipped on a dry run and when fewer than two reviews succeeded (ACTIONS says so), can be turned off with `review.committee: false`, and is saved as `committee.json`.

### What changed in ACTIONS and the watch

ACTIONS now shows company names next to tickers, the expected return of each new buy, "buy below X" for candidates not bought, and each holding's kill criteria under Holdings, with the committee section on top. The stock page shows the latest entry price and kill criteria. The weekly watch adds an ENTRY PRICE alert when a stock reaches the price its review said to buy at, and lists the kill criteria of each holding so you can check them against the week's news; it does not evaluate them for you.

## Lanes

A decision names the lane its evidence supports; the Portfolio Manager picks value first, compounder when the record qualifies, inflection only with verified demand evidence. The numeric parts of each bar are checked in code.

- **Value**: the original rules (cheap on five-year averages, the good business, good management and cheap verdicts, margin of safety; still investing is judged but does not bar the rating).
- **Compounder**: a business that has earned high returns for years and keeps growing its cash. Buy needs ROIC including goodwill ≥ 15% in every counted year (at least four; acquisitions count as capital), revenue growth ≥ 8%/yr over 10 years (5 when 10 are unavailable, flagged), free cash flow growing ≥ 5%/yr over ≥ 3 years; the price's implied growth no higher than the earned growth (revenue CAGR capped at the cohort base rate) or the sustainable growth, whichever is higher; expected return = yield + earned growth + reversion (≤ 0) ≥ 12%; upside/downside ≥ 1.5 and a price ≤ 1.15× the worksheet base. A company that returns all its cash in buybacks has a sustainable growth of 0% but can still qualify on its free cash flow and earned growth.
- **Inflection**: a business whose earnings are stepping up now. Buy needs TTM EBIT ≥ 1.5× the five-year average, revenue growth in the latest quarter ≥ 10 points above the last fiscal year's (Yahoo serves five quarters, too few to compare two quarters' year-on-year growth), incremental ROIC ≥ 20%, verified demand evidence (customer capex, backlog or pricing, with a source), a weighted margin of safety ≥ 20% on the run-rate value, upside/downside ≥ 2, a bear case of at least 30%, no FREEFALL, and a demand indicator among the kill criteria. The worksheet shows the run-rate value (TTM earnings growing for three years, then the worksheet's 3% base growth) only when TTM EBIT is ≥ 1.3× the five-year average or the acceleration is ≥ 10 points; otherwise it says `not shown` and the stock is valued on the worksheet base.
- **Verdicts**: a compounder or inflection Buy or Overweight allows no red flag; both need good business and good management to pass, and a compounder also needs still investing to pass (an inflection is investing by definition, and its demand evidence covers it); the Cheap test is replaced by the lane's own price bars (for the compounder: price ≤ 1.15× base, expected return ≥ 12%, upside/downside ≥ 1.5), so Cheap may be FAIL. A `--tickers` or holding row built off the screen gets the same hard filters and lane hints, so its pipeline note names the hint. The inflection lane's margin-of-safety and upside/downside bars apply to an Overweight too. The value lane's `**Lane**:` line carries no numbers: its bars are the checklist and the scenarios.
- **Caps**: a lane Buy is sized at half the target weight (Overweight a quarter); compounders stop at 30% and inflection names at 20% of the portfolio (`sizing.lane_caps`, `sizing.lane_size_factor`). A lane named in `sizing.lane_buy_only` buys and adds on a Buy only: its Overweight is kept or watched, never sized (ACTIONS says "Overweight in a Buy-only lane: watch, no buy or add").
- **Since 2026-10-24 the shipped `config.yaml` runs value plus strict compounder Buys**: `lanes.inflection_candidates` and `dislocation_candidates` are 0, the inflection cap is 0 and `lane_buy_only: [compounder]`, because the lane-rule backtest found the compounder Buy bars beat SPY while the Overweight bars, inflection and dislocation did not (see Backtest). Restore the defaults (3 / 3, cap 0.20, `[]`) to run every lane again.

The screen's `Lane hint` column (and the Screen page's filter) points at names worth a look; it is a hint, not the lane:

- `compounder`: passes the hard filters, worst-year ROIC including goodwill ≥ 15% over at least 4 counted years, revenue growth ≥ 8%/yr over at least 3 years (Yahoo's four fiscal years; the review's SEC history then verifies the Portfolio Manager's bar of 5 or more), free cash flow growing ≥ 5%/yr over at least 3 years. The screen's `compounder_return` (yield + earned growth + reversion ≤ 0) ranks these candidates.
- `inflection`: passes the hard filters, TTM EBIT ≥ 1.5× the five-year average and latest-quarter revenue growth ≥ 10 points above the last fiscal year's.
- `dislocation`: passes the hard filters, the price is ≥ 25% below its 52-week high or fell ≥ 20% in a month, and the fundamentals are intact (TTM revenue and TTM EBIT at or above their five-year averages, no accounting flag).

When several apply, `Lane hint` shows the first in that order (a dislocated compounder is still a compounder) and the `hints` column lists all of them.

The review adds up to `lanes.compounder_candidates` (default 2) compounder-hinted names by compounder return, not owned and not in FREEFALL, after the Magic Formula picks. ACTIONS shows a candidate's hint as `(hint: inflection)`, and `results.json` stores it as `lane_hint`.

**Dislocations.** A quality business whose price fell hard while its numbers did not is the value lane's classic entry. The screen adds `drawdown_from_high` (price vs the 52-week high), `move_1m`, `move_3m`, `fundamentals_intact` and the Yahoo `industry` for every name, and the `dislocation` hint above. Up to `lanes.dislocation_candidates` (default 3) dislocation-hinted names, best expected return first, not owned and not in FREEFALL, join every review (plain `review` and `run-all`) after the compounder candidates, as value-lane candidates shown `(hint: dislocation)`. The review gives every agent the ticker's screen figures as a `Pipeline note` in its instrument context (lane hints and theme, the 3-month move, the drawdown from the 52-week high, TTM EBIT on the five-year average, the revenue acceleration), since the analysts run in parallel and the news analyst never sees the fundamentals report; the verified market snapshot also gives the 3-month move and the distance from the 52-week high. The news report always ends with a `## Demand evidence` section (customer capex, backlog or RPO, bookings, book-to-bill, lead times, pricing; each item verified or unverified, or "none found"). The news analyst classifies the cause of a 20%+ three-month fall or a 25%+ drawdown (company-specific deterioration, sector-wide sentiment or macro, with sources): a sector-wide scare with intact numbers is the classic entry; a company-specific cause still needs the value bar. When three or more names in one industry carry the hint in the same screen, each is tagged in the `sector_scare` column (the Screen page's `Scare` column), and `run-all` lists the industry and its members with their drawdowns under `## Sector scares` in `reports/themes_<date>.md`, written even when `lanes.themes` is empty (then without a model call), so a sentiment-driven selloff reads as one event.

## Themes

Name the themes and tickers you want looked at in `value_pipeline/config.yaml`:

```yaml
lanes:
  themes: ["AI infrastructure", "grid equipment"]   # free text, one quick model call each
  tickers: [CLS, VRT]                               # always reviewed (unless you hold them)
  inflection_candidates: 3                          # theme ideas added to a review, at most
  compounder_candidates: 2                          # compounder-hinted screen names added, at most
  dislocation_candidates: 3                         # dislocation-hinted screen names added, at most
```

For each theme the scan asks: "Where is demand inflecting for this theme, and which listed suppliers with pricing power benefit? Name up to 8 tickers with one line of evidence each; label unverified claims." Each named ticker you don't own is fetched and evaluated like a screen row and held to the screen's market-cap, Altman Z, positive-FCF and interest-coverage filters (not the F-Score or the ROIC floor, which measure the record an inflection breaks from); those with TTM EBIT ≥ 1.3× the five-year average or latest-quarter revenue growth ≥ 10 points above the last fiscal year's join the review hinted `inflection`, best expected return first, up to `lanes.inflection_candidates`. A ticker that cannot be fetched, fails a filter (`fails Altman Z`), isn't inflecting or is over the cap is listed under Skipped. `reports/themes_<date>.md` (beside the watch and daily reports; a dry run writes `themes_<date>-dry-run.md`) has each theme's ideas with their evidence (verified or unverified), the candidates added and the skipped ones.

`themes` runs the scan alone; `run-all` runs it before the review (a dry run makes no model call, so it finds no ideas). The scan's calls are not in the cost estimate; the candidates it adds are, and the cap drops them first. `lanes.tickers` join every review except a `--tickers` one.

## Calibration

Did the decisions work? `calibration` (and the website's Calibration page) reads every real
review's `results.json` (dry runs and failed analyses left out) and, for each decision, looks
at the close 30, 90, 180 and 365 days after the review day (the last close on or before that
day, not dividend-adjusted). A horizon still in the future stays blank. For each it shows the
return from the review price, whether the price was at or above the agents' value, below the
bear case and below the entry price. Older rows without a recorded price use the close on the
review day.

The scores, by rating and by lane: the median 180-day return; for Buy/Overweight, how many
reached the agents' value at a checkpoint within a year (30/90/180/365 days, not every day in
between); for Sell/Underweight, how many fell by 180 days. Overall: the median gap between the
agents' value and the formula value, and, once at least ten decisions are 180 days old, how
often the price ended below the bear case or above the bull case against the probabilities the
agents gave those cases. One verdict line per lane, such as `compounder: 3 decisions, none
past 90 days yet`.

The command fetches the closes it needs from Yahoo, one request per ticker, and caches them in
`data/cache/prices/`; the page reads only that cache, so it is cheap to reload and blank until
the command (or a later run of it) has fetched the closes. The Saturday script runs the command
after the watch, so the page stays current. It stays empty until a decision is a month old (the
30-day close does not exist before then). `--horizon` only picks which of the four horizons
the decisions table shows; the scores and verdicts always use 180 days (365 days for "reached
value"). A few decisions say little: read the counts before the shares.

## Backtest

Would the lane rules have picked winners? `backtest` replays the screen on one cohort date a
year (`backtest.first_year` to `last_year`, on `cohort_day`, default 30 June 2015 to 2024),
using only what was known then: the EDGAR statements filed on or before the date (an
amendment counts from its filing date) and the closes dated on or before it. It runs the
screen's hard filters, Magic Formula rank, formula value, the compounder, inflection and
dislocation hints and the Portfolio Manager's compounder record and price bars, and takes each
rule's picks: `value` (top 30 by the Magic Formula with a margin of safety of at least 30%),
`magic_formula` (the top 30 alone), `compounder` and `compounder_overweight` (the PM's Buy and
Overweight price bars), `compounder_record` (hint and record bars, no price bar),
`compounder_hint`, `inflection` and `dislocation`. Each pick is held 1 and 3 years, equal
weight, dividends reinvested, and compared with SPY and with the equal-weight universe of that
cohort. No agent is called, nothing trades and no bar is changed. The backtest covers the S&P 500 and 400 only: non-US companies have no point-in-time filings (EDGAR), so the UK and European markets are screened live but not replayed.

What it cannot test or correct:

- Survivorship. Only today's S&P 500 and 400 members are in the sample. A name that joined the
  S&P 500 after a cohort date is left out of that cohort (the constituents table dates every
  join), even one promoted from the S&P 400, which was already in the two-index universe then
  (so the exclusion removes a few names it need not), but names that left either index since are missing and cannot be counted from free
  data, and the S&P 400 publishes no join dates, so all its members stay. The report prints the
  count left out per cohort. Expect the results to read somewhat in the rules' favour.
- Sectors are today's (EDGAR has none), used to exclude Financials, Real Estate and Utilities
  and for the sector EV/EBIT targets.
- The agent-only bars (upside/downside, scenario probabilities, kill criteria, verified
  evidence) are not tested: the rules are the screen's and the PM's mechanical bars only.
- The 2 bn market-cap floor is nominal in every year (not deflated).
- The fourth quarter is derived as the annual figure less nine months.
- The metrics see the newest four fiscal years known at the date, as the live screen does
  (Yahoo's window); the PM's ten-year revenue CAGR uses the longer EDGAR history.
- The Magic Formula rank is taken among the passing rows that are not in FREEFALL, while the
  live screen ranks every passing row and then skips the falling knives, so a name's position
  can differ by one or two.
- Trend and drawdown read Yahoo's split-adjusted close; the live screen reads dividend-adjusted
  closes, so a high-yield name's trend can read slightly differently.
- EDGAR tags differ by filer. Gross profit is derived as revenue less cost of sales when a filer
  does not tag it, and the cost tag some filers use covers only part of their costs (UNH's
  products cost, not its medical costs), so a derived gross margin can read far too high; the
  Beneish and Piotroski checks read changes in it, which mostly cancels. EBIT is derived from
  pre-tax income plus interest when operating income is not tagged. `scripts/pit_probe.py`
  reports, from the cached EDGAR files, which fields the tag map reaches.

How to run: `uv run python -m value_pipeline backtest`. The first run fetches every ticker's
EDGAR company facts and Yahoo price history (about 900 + 900 requests, 20-40 minutes) and
caches them under `data/cache/backtest/facts/` and `prices/`; later runs read the caches and
take a minute or two, refetching only a price history that stops more than a week before the
newest hold end the run needs and a fact store fetched before the newest cohort date (so next
year's cohort is not silently dropped). Coverage prints where SPY's history ends and names any
cohort whose ended hold it does not reach. `--refresh` refetches, `--no-fetch` reads the caches only:
statements, prices and the S&P 500 join dates (`data/cache/backtest/added_dates.json`, rewritten
on every fetching run, and used when the fetch fails). The index lists are still read from
Wikipedia and fall back to `data/universe.csv` when that fails. If the join dates cannot be read
at all, late joiners stay in every cohort, and the report says so at the top of the Verdict. `--from`/`--to` pick the
cohort years, `--holds 1` one hold, and `--tickers DECK,ADBE` a quick smoke run. A cohort is
evaluated only when its one-year hold has ended; a three-year result only when that hold has.

How to read it. The Verdict gives one sentence per rule and hold, such as `compounder: beat SPY
in 7 of 8 cohorts with picks (picked in 8 of 10 cohort dates; 1-year hold, equal weight),
median excess +3.1 points; 42 picks in all.` A cohort counts when the rule picked something
there (the cohort dates are those whose hold has a SPY return): "beat" means the equal-weight basket's
return was above SPY's, and the excess is basket minus SPY in percentage points over the hold
(not annualised). Read the cohort count and the picks before the excess: three cohorts or a
dozen picks say little. Compare the references: `value` against `magic_formula` is what the
margin-of-safety bar adds; `compounder` against `compounder_record` what the price bars add;
`compounder_record` against `compounder_hint` what the PM's record bars add. *The bar that
bound* is the compounders the price bars turned away, and how they did. The Sensitivity table
moves one bar at a time over a fixed grid (the others at their defaults, one-year hold): a rule
whose result flips between neighbouring values is noise, not an edge, and a bar is not to be
tuned to this sample. *Coverage* counts the universe, the tickers with statements and prices,
the rows per cohort, the skipped tickers by reason and, per cohort, how many rows' newest
fiscal year lacks revenue, EBIT or free cash flow (a filer whose tags the map does not know).

The website's Backtest page shows the newest report. `scripts/pit_probe.py` checks the
point-in-time statements offline on the EDGAR company facts already cached under
`~/.tradingagents/cache/sec_edgar/`: fiscal years known on four dates and the fields missing.

## Files

```
value_pipeline/data/            (gitignored: your holdings)
  portfolio.json                cash, deposits, positions
  trades.csv                    date, ticker, action, shares, price, fees, note
  backups/<timestamp>/          copy of both files before every write
  cache/<TICKER>.json           Yahoo data, refreshed after 7 days
  cache/prices/<TICKER>.json    closes on past days, for calibration (kept; past closes do not change)
  cache/backtest/facts|prices/  EDGAR company facts and Yahoo price histories for the backtest (kept)
  cache/backtest/added_dates.json  the S&P 500 join dates the backtest last read
  universe.csv                  last index membership fetched
  screens/screen_<date>.csv     every screened ticker with all metrics
  reports/<date>/ACTIONS.md|html, results.json, committee.json, <TICKER>/complete_report.html
  reports/themes_<date>.md      the theme scan (linked from ACTIONS when it added candidates)
  reports/backtest/backtest_<date>.md, backtest_<date>_picks.csv   the lane-rule backtest
  logs/monthly_<date>.log       output of scheduled runs
```

The SPY comparison replays your deposits and trades into SPY on the same dates and with the same dollars. It uses prices without dividends, so it reads slightly in SPY's favour if you don't record your dividends as deposits.

## Daily check

`daily` is the weekday check between reviews. It fetches prices (free) and re-runs the agents only on the few holdings and ideas whose triggers fired: By default (`daily.scope: holdings`) it re-reviews only what you own, to decide keep or sell; an idea that reaches its entry price mid-week is listed under "For Saturday's review" in the daily report and left for the weekly run, which is where buys are found. Set `scope: all` to re-review ideas on weekdays too (the setting since 2026-11-05, so a name that reaches its entry price is reviewed the same morning). Tickers listed under `daily.exclude` are never treated as ideas, for markets you cannot trade.

- **ENTRY PRICE**: the stock is at or below the entry price its last review gave.
- **REACHED VALUE**: a holding is at or above its intrinsic value.
- **EARNINGS**: the company has reported since its last review.
- **FREEFALL**: the stock has just entered FREEFALL.
- **BIG MOVE**: the price is 15% or more away from the price at its last review (`daily.big_move`).

At most 3 names are re-reviewed per day (`daily.max_reviews`); the rest wait until tomorrow. A ticker reviewed in the last 5 days is skipped (`daily.cooldown_days`), except for EARNINGS and REACHED VALUE, which always go through. The report is `value_pipeline/data/reports/daily_<date>.md`, and the website's dashboard shows the newest of it and the Saturday watch under "Latest check". Nothing trades; you decide.

To run it by hand without spending anything: `uv run python -m value_pipeline daily --dry-run` (it fetches prices and lists which triggers fired, but re-runs no agents). Add `--no-notify` to skip the macOS notification, or `--max N` to change the cap for one run.

## Scheduling (macOS: full review every Saturday, daily check on weekdays)

`scripts/monthly.sh` (the name is historical) is started by launchd every Saturday at 9:00 and, since 2026-11-05 and until the cash is invested, every weekday at 10:00 as well (`scripts/local.value-pipeline.plist`); it logs to `value_pipeline/data/logs/`. A daily review moves down the screen: candidates reviewed in the last `review.candidate_cooldown_days` days (5) give their place to the next names, and statements are cached for `screen.cache_days` (7) while `screen.price_days` (1) refreshes each day's price and closes from two cheap calls per ticker. It runs `run-all --yes`: the screen, the theme and dislocation scans, and the agents on holdings and candidates, about 10 to 12 runs. The free `watch` is no longer scheduled; `scripts/monthly.sh --watch` runs it by hand, and the weekday daily check covers the days between. If a review for today already exists (you started one by hand overnight, say), the script does not repeat it: it refreshes the watch and the calibration prices and exits. To run the full review by hand on any day: `scripts/monthly.sh`.

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
3. Test it now: `launchctl kickstart gui/$(id -u)/local.value-pipeline` (it will spend a full review; `scripts/monthly.sh --check` only tests access).

**The Mac must be awake.** launchd runs a missed job when the Mac wakes from sleep, but not if it was shut down. To wake it automatically for the Saturday job alone: `sudo pmset repeat wakeorpoweron S 08:55:00` (the daily-check line below replaces it; `pmset repeat` holds only one schedule). Keep the repo out of `~/Desktop`, `~/Documents` and `~/Downloads`: macOS blocks launchd jobs from those folders ("Operation not permitted", exit 126). `scripts/monthly.sh --check` tests access without spending anything.

**The weekday daily check.** `scripts/daily.sh` runs `daily` Monday to Friday at 7:30 and logs to `value_pipeline/data/logs/daily_<date>.log`; it shows a notification if it fails. Install the template that ships in the repo (it assumes the repo path above; edit it if yours differs):

```bash
cp scripts/local.value-pipeline-daily.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.value-pipeline-daily.plist
sudo pmset repeat wakeorpoweron MTWRFS 07:25:00
```

`scripts/daily.sh --check` tests access without spending anything. The `pmset` line wakes the Mac at 7:25 Monday to Saturday, five minutes before the jobs. So that one wake serves both, move the Saturday plist's hour from 9 to 7 and its minute to 30:

```bash
launchctl bootout gui/$(id -u)/local.value-pipeline
# edit ~/Library/LaunchAgents/local.value-pipeline.plist: Hour 7, Minute 30
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.value-pipeline.plist
```

## Tests

```bash
uv run pytest tests/test_value_pipeline.py
```

The portfolio maths, action rules, F-Score, Z-Score, Magic Formula, trend classification and the HTML page are tested on fixed sample data, with no network.
