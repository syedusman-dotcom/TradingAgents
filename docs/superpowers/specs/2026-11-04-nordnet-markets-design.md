# Nordnet markets in the screen — design

**Date:** 2026-11-04
**Status:** the user asked for the daily review to cover "even international market", then "check Nordnet which markets it covers, track only those". Nordnet's own FAQ (nordnet.se, "Vilka marknader kan jag handla på hos Nordnet?") lists: the Nordics (not Iceland), Germany (Xetra), the US (NYSE, Nasdaq), Canada (TSX, Venture), the UK (London, FTSE 350 only), Belgium, France, Ireland, Italy, the Netherlands, Portugal, Switzerland, Austria, Spain (all main exchanges) and Poland (phone orders only). No Japan, no Korea. Calls Claude made are marked *(my call)*.

## Goal

The daily screen covers every market the user can trade on Nordnet, in one ranking, in dollars, so the daily review's candidates come from wherever the cheapest quality sits. Nothing trades; the user buys on Nordnet by hand and records fills with `buy`.

### Non-goals

Nordnet's fees, stamp duty and FX spread (noted on the ACTIONS page only as a sentence; the user knows them). Poland (phone only), TSX Venture, Iceland. A point-in-time backtest for non-US names (EDGAR has no statements for them; the lane backtest stays US-only and says so). Changing any rating bar or lane rule. Japan and Korea.

## Verified facts (code on 2026-11-04)

- `market_data.universe(paths, indexes)` reads two Wikipedia lists (`WIKI`: sp500, sp400), table index 0, columns Symbol/Security/GICS Sector, and caches `data/universe.csv` (ticker, name, sector, index). `screener.run` drops `exclude_sectors` by that sector column before fetching anything.
- `market_data.fundamentals(ticker, paths, cache_days)` caches Yahoo's `info`, annual statements, quarters and ~420 closes per ticker (`CACHE_SCHEMA = 6`) with `currency` (the quote currency) but not `financialCurrency`; no conversion. `screener.evaluate` treats every figure as one currency: `market_cap` (the $2B filter), `enterprise_value`, per-share `price`, `value_base/low/high`; ratios (`earnings_yield`, `roic`, `mos_formula`, `expected_return`, `compounder_return`) are unit-free. `with_target`/`apply_sector_targets` recompute only ratios.
- `review.offscreen_row` builds the same row for a holding or `--tickers` name through `fundamentals` + `evaluate`; `screen_row_for` merges it with the screen's row. The review reads `value_base` as the formula value the PM's bridge starts from and `expected_return`; the PM's price target and entry price are in the worksheet's unit, which is the quote currency (London in pence: `quote_fx` divides the pound rate by 100; `MINOR_UNITS`).
- The review already records `currency` and `fx_to_usd` per ticker, sizes positions through the rate, and reviewed DNLM.L, BNZL.L, GFTU.L, RAND.AS on 2026-10-23. `watch`, `daily` and `calibration` price through `latest_price`/`closes_between`, which return the quote unit, so entry-price triggers compare like with like. EDGAR tools return `NOT_A_US_FILER` for non-US names (the fundamentals analyst is told these are optional for foreign tickers).
- The exploratory script `data/screens/intl/intl_screen.py` (2026-10-23) proved the approach: Wikipedia index lists per market, `fx()` through `{CUR}USD=X`, `to_usd()` converting price/market cap/EV by the quote currency and the statement rows by the financial currency, then `screener.evaluate`; 1,147 names evaluated. Its Wikipedia table indexes are hard-coded per page and drift.
- The screen page (`screen.html`) has a sector filter and no market column; `views.screen_table` fills missing columns with None.
- From 2026-11-05 (installed 2026-11-04) the full review runs every weekday at 10:00 and Saturday at 9:00 (`scripts/local.value-pipeline.plist`); `screen.cache_days` is 1 so each day's screen carries the day's prices (a full refetch, ~2.3 s per ticker).

## Design

### 1. Universe: Nordnet's markets

`market_data.MARKETS: dict[str, Market]` with `Market(key, country, wikipedia_url, suffix, table_columns)`. The S&P entries keep their behaviour (`sp500`, `sp400`: suffix none, class shares `BRK.B` → `BRK-B`, sector from the GICS column). The new entries *(my call: the main and, where Wikipedia keeps a list, the mid-cap index of each Nordnet market)*:

| key | country | Wikipedia list | Yahoo suffix |
|---|---|---|---|
| ftse100, ftse250 | UK (FTSE 350 is what Nordnet allows) | FTSE 100 Index, FTSE 250 Index | `.L` |
| dax, mdax | Germany | DAX, MDAX | `.DE` |
| cac40, sbf120 | France | CAC 40, SBF 120 | `.PA` |
| aex, amx | Netherlands | AEX index, AMX index | `.AS` |
| bel20 | Belgium | BEL 20 | `.BR` |
| psi | Portugal | PSI-20 | `.LS` |
| iseq20 | Ireland | ISEQ 20 | `.IR` |
| ftsemib | Italy | FTSE MIB | `.MI` |
| smi, smim | Switzerland | Swiss Market Index, SMI MID | `.SW` |
| atx | Austria | ATX | `.VI` |
| ibex35 | Spain | IBEX 35 | `.MC` |
| omxs30 | Sweden | OMX Stockholm 30 | `.ST` |
| omxc25 | Denmark | OMX Copenhagen 25 | `.CO` |
| omxh25 | Finland | OMX Helsinki 25 | `.HE` |
| obx | Norway | OBX Index | `.OL` |
| tsxcomp | Canada | S&P/TSX Composite Index | `.TO` |

The table finder is by column name, not index: the first table on the page with a ticker column (`Ticker`, `Symbol`, `Code`, `Ticker symbol`, case-insensitive, first match) and a name column (`Company`, `Name`, `Security`, `Constituent`). The ticker is stripped of an existing exchange prefix or suffix (`LSE: AZN`, `AZN.L`, `AZN`) and written as `{code}{suffix}`; a code containing a dot or space keeps Yahoo's dash form (`RR.` → `RR-`? no: London's trailing dot is dropped, `BT.A` → `BT-A`). Each `Market` may name a second page (`pages`: the index page and, where the constituents live elsewhere, that list page); the finder takes the first page with a usable table. A market whose pages yield fewer than 10 tickers is logged as a warning and skipped for that run (its cached rows, if any, are kept) — and in the verification step that is a failure to fix by pointing at the right page, not a result to accept *(my call)*. `universe.csv` gains columns `market` (the key) and `country`.

**Sectors — one vocabulary** *(my call)*: the S&P markets keep Wikipedia's GICS sector and are excluded before the fetch, as today. Every other market uses Yahoo's sector, mapped to the GICS names (`Financial Services` → `Financials`, `Technology` → `Information Technology`, `Consumer Cyclical` → `Consumer Discretionary`, `Consumer Defensive` → `Consumer Staples`, `Basic Materials` → `Materials`, `Healthcare` → `Health Care`; `Industrials`, `Energy`, `Real Estate`, `Utilities`, `Communication Services` unchanged), so `exclude_sectors` and the sector-target groups see one vocabulary. For those markets the exclusion runs before the fetch when `cached_sector` knows the ticker and after the fetch otherwise (the first run fetches their financials once, ~25% of the names; later runs skip them from the cache). A Wikipedia sector column on a non-S&P page is ignored.

**Cross-listings** *(my call)*: a company listed in two markets (Unilever, Shell: FTSE 100 and AEX) keeps the first market in `universe.indexes` order. The key is the Wikipedia name lower-cased, with punctuation and the legal suffixes `plc, nv, n.v., sa, s.a., ag, se, ab, asa, a/s, oyj, spa, s.p.a., group, holding(s)` removed; the drop is logged by name.

Config `universe.indexes` lists the keys; the default becomes the whole table above plus sp500 and sp400 *(my call)*. `config.yaml` carries one line per market so the user can comment one out. A key not in `MARKETS` raises a config error naming it.

### 2. Dollars in, quote unit out

`fundamentals` stores `financial_currency` (Yahoo `financialCurrency`), bumps `CACHE_SCHEMA` to 7 and keeps native figures. New `market_data.in_usd(data) -> dict`: a copy with `price` multiplied by `fx_quote` (dollars per one quoted unit, exactly what `quote_fx` returns: for a pence quote the pound rate / 100), `market_cap` and `enterprise_value` multiplied by the major-unit rate (`usd_rate(currency)`, which already returns the pound rate for `GBp`; Yahoo reports both in the major unit), every money field of `years` and `quarters` by the financial-currency rate (the fields of `YEAR_FIELDS` other than `fiscal_year_end` and `shares`; a quarter's money fields the same way), and `fx_quote`, `fx_financial` recorded. The rates come from `usd_rate` (already in `vendors/yahoo/common`), cached per process for the run. No rate → `DataUnavailable("no USD rate for XXX")`, the ticker is skipped and listed. A USD/USD record passes through unchanged.

`screener.run` and `review.offscreen_row` call `evaluate(in_usd(data), …)`. After every computation on the row (filters, sector targets, lane hints), `screener.to_quote(row, data)` divides the per-share columns `price`, `value_base`, `value_low`, `value_high` by `fx_quote` (nothing else: the pence are already in that rate) so they read in the unit Nordnet shows and the worksheet uses; `market_cap` and `enterprise_value` stay in dollars (the filters and the page are in dollars). The row gains `market`, `country`, `quote_currency`, `fx_quote`. The review keeps its own `quote_fx` call for sizing (the live rate at review time); the row's `fx_quote` serves the screen's conversions only *(my call)*. `formula_value` in results.json is therefore in the quote unit, matching the PM's bridge and the entry price; the daily and watch triggers keep comparing quote-unit prices.

### 3. Fresh prices without refetching statements

`screen.cache_days` returns to 7 for the statements. New `screen.price_days: 1`: `fundamentals` serves a cached record whose `fetched_at` is fresh but whose `price_at` is older than `price_days` after a price refresh: `price` and `market_cap` from Yahoo `fast_info` (no `info` call), `closes` replaced by a fresh `closes(ticker)` call (the full 420-day series, so trend and drawdown stay exact), and `enterprise_value` moved by the market-cap change (`ev + new_mcap − old_mcap`; left as is when either market cap is missing) so EV, EBIT/EV and the lane hints stay consistent with the price; `price_at` is written back to the cache. The daily screen then costs two cheap calls per ticker and the full fetch once a week. A record without `price_at` (older cache) is treated as stale. A refresh that fails leaves the cached record in place with a warning.

### 4. Review, pages and reports

- The pipeline note in each review names the market and currency (`DNLM.L — UK, quoted in GBp, figures in dollars on the screen`), and `screen_row_for` keeps working for off-screen names.
- `screen.html`: a Market filter beside Sector and a `Market` column (country) after Ticker; the ranked table sorts as today. `stock.html` shows the quote currency next to the price.
- ACTIONS.md's header line gains, when a candidate is non-US: "Prices in the quote currency; Nordnet charges FX and, in the UK and Ireland, stamp duty." *(my call)*.
- The screen's summary log line counts rows per market.
- README: the markets table, the currency rule (dollars for ranking and filters, quote unit for prices and values), `price_days`, and that the backtest stays US-only.

### 5. What does not change

Lane rules and bars, candidate counts, the committee, sizing (already FX-aware), the daily job, the watch, calibration, the backtest (US only; its report says so), the Korea exclusion (`daily.exclude`).

## Testing (unit, no network)

- Universe: synthetic Wikipedia HTML with a table of contents table first and a members table second (`Ticker`/`Company`), ticker forms `AZN`, `BT.A`, `LSE: RR.`; a page with no usable table skips with a warning; `BRK.B` on sp500 still becomes `BRK-B`; `universe.csv` carries market and country; an unknown key raises.
- `in_usd`: a GBp quote with GBP statements (price 800 → 8 × rate; revenue × rate), an ADR-like USD quote with EUR statements, USD passthrough, missing rate → `DataUnavailable`, quarters converted.
- `to_quote`: round trip of the per-share columns for GBp and EUR; market cap unchanged.
- `fundamentals` price refresh: a cached record with a stale `price_at` gets a new price, market cap, closes and an EV moved by the market-cap change, without calling `info` (patched `yf.Ticker`); fresh `price_at` makes no call; an old cache without `price_at` refreshes; a failed refresh serves the cached record.
- Universe: cross-listing dedup keeps the first market (`Unilever PLC` on ftse100 and `Unilever N.V.` on aex → one row, ftse100); Yahoo sector names map to GICS; a non-S&P ticker with a cached excluded sector is not fetched; one without a cache is fetched and then excluded.
- `screener.run` end to end with patched `fundamentals` for one US and one `.L` ticker: the `.L` row's market cap in dollars, price and value in pence, `market` and `country` set, the sector excluded by Yahoo's name when Wikipedia had none.
- `offscreen_row` for a `.L` ticker gives the same units.
- Web: the screen page shows the Market column and filter; the stock page shows the currency.

## Verification after the merge (controller's step)

Before the merge, a network script (not a test) fetches every market's pages and prints the ticker count per market; any market under 10 is fixed by pointing its `pages` at the constituents page (Wikipedia's TSX Composite page, for one, keeps the list in a separate table or page). After the merge, run `screen` on the new universe (first run fetches ~1,500 new tickers, about an hour); check the per-market counts in the log; `review --tickers` on the two best-ranked non-US names and read the formula value, price target and entry price in the quote unit; `daily --dry-run` lists them with quote-unit prices.

## Rollout

Worktree, subagent-driven implementation, final review, merge, push; the next 10:00 run picks up the universe. If the first full fetch would collide with the 10:00 job, run the screen by hand after the merge so the cache is warm.
