# Small companies in the screen — design

**Date:** 2026-11-06
**Status:** the user asked why the pipeline cannot look at small companies (the documented value premium lives mostly in small, neglected stocks) and then said "ok go ahead with small companies then" to Claude's proposal: Nordic small caps tested live, US small caps (S&P 600) tested on history first. Every choice below is Claude's call.

## Why

The screen reads index member lists only (S&P 500/400, FTSE 100/250, OMX Stockholm 30 and similar), and drops anything under $2 billion. A Nordic small cap is never seen. The research report (`~/Desktop/ValueTrading/reports/Successful investor strategies and gaps.md`) found the value premium mostly in small stocks; a private investor's edge over funds is also largest there.

## Goals

1. The screen covers every main-market share on Nasdaq Stockholm, Copenhagen and Helsinki, every company on the Oslo exchange, and the S&P 600.
2. Those small-company markets use a lower size floor and a trading-value floor; every other market keeps today's filters unchanged.
3. Nordic small caps can become review candidates at once (their countries are already in `review.candidate_countries`).
4. US small caps become review candidates only if a backtest, with its pass rule fixed here, supports it.

### Non-goals

First North, Iceland (not a Nordnet market for this user), Euronext Growth or other European small-cap lists. A European backtest (no free point-in-time filings). Changing any buying rule, prompt, sizing or the review's candidate count.

### Anti-goals

- Weakening the filters for the large-cap markets.
- More reviews or higher cost per day: the number of candidates per review stays 5.
- Illiquid names a $1,000 order would move.

## Verified facts (main at cb2f9dc)

- `market_data.MARKETS` holds `Market(key, country, pages, suffix, gics_column, tickers)`; `fetch_market` reads the first usable Wikipedia table (ticker and name columns, at least `MIN_TICKERS` rows) or a static ticker list; `normalise_code` strips `OSE:`-style prefixes, turns spaces and dots into dashes and adds the suffix; `universe(paths, indexes)` fetches each market in order, drops a later listing of a company already seen (by `name_key`) or a repeated ticker, keeps the cached rows of a market that fails, and writes `universe.csv`.
- `https://api.nasdaq.com/api/nordic/screener/shares?category=MAIN_MARKET&tableonly=false&market=STO` (also `CPH`, `HEL`) returns JSON `data.instrumentListing.rows`, each with `symbol` ("ERIC B"), `fullName`, `currency`, `sector` (names such as "Financials", "Real Estate", "Utilities"), `isin`, `turnover`. Counts on 2026-11-06: Stockholm 408, Copenhagen 117, Helsinki 149. It needs a browser-like `User-Agent` and `Accept: application/json`. Oslo (`OSL`) returns 400.
- Wikipedia `List_of_companies_listed_on_the_Oslo_Stock_Exchange`: table 2 has `Company`, `Ticker` ("OSE: ABG"), `First day of listing`, a GICS industry column; 187 rows.
- Wikipedia `List_of_S%26P_600_companies`: `Symbol`, `Security`, `GICS Sector`; 603 rows.
- Yahoo resolves `ERIC-B.ST`, `ATCO-A.ST`, `APETIT.HE` and similar; `info` carries `marketCap` (quote currency), `averageVolume` (shares a day).
- `screener.failed_filters(row, filters)` checks `min_market_cap` on the row's dollar market cap. The screen sets `row["market"]` before calling it. The backtest's `snapshot` calls it with no market on the row; `_universe` returns `(ticker, sector)`.
- `backtest.bar_verdict(one_year, three_year, baseline_matched, rule)` holds the buy-bar pass rule's five conditions; `backtest.run` takes `cfg["universe"]["indexes"]` and the command takes `--indexes`. S&P 500 join dates are the only ones known.
- `review.candidate_countries` limits Magic Formula and dislocation candidates to listed countries (Europe and the UK in `config.yaml`).

## Design

### 1. New markets

| Key | Country | Source | Yahoo suffix |
|---|---|---|---|
| `sto` | Sweden | Nasdaq Nordic API, `market=STO` | `.ST` |
| `cph` | Denmark | Nasdaq Nordic API, `market=CPH` | `.CO` |
| `hel` | Finland | Nasdaq Nordic API, `market=HEL` | `.HE` |
| `ose` | Norway | Wikipedia Oslo list | `.OL` |
| `sp600` | US | Wikipedia S&P 600 list, GICS sector kept | none |

- `Market` gains `api: str | None` (the Nasdaq Nordic market code). `fetch_market` reads the API for such a market: `ticker` from `symbol` through `normalise_code`, `name` from `fullName`, `sector` from `sector`. A request that fails raises, as a Wikipedia failure does, so `universe` keeps the cached rows.
- **Share classes:** a company listed in two classes (`ACRI A`, `ACRI B`) keeps the class with the higher `turnover` that day. Two rows are one company when their symbols agree after removing a final space-separated token of one to four capital letters (`A`, `B`, `C`, `SDB`, `PREF`).
- The five keys go at the end of `DEFAULTS["universe"]["indexes"]` and of `config.yaml`, so the existing index lists keep their names (a company in OMX Stockholm 30 stays under `omxs30` and the large-cap filters).
- `scripts/check_markets.py` covers the new markets as it covers the others.

### 2. Filters for the small-company markets

- `filters.small_markets`: `{sto: 2.5e8, cph: 2.5e8, hel: 2.5e8, ose: 2.5e8, sp600: 2.5e8}`, the dollar size floor for each listed market. A row whose market is listed uses that floor instead of `min_market_cap`; every other row is unchanged.
- `filters.min_daily_value`: 250000 dollars, the average daily traded value a listed-market row needs: Yahoo's `averageVolume` times the dollar price. A missing value fails the check (reason "liquidity"). Rows outside the listed markets are not checked.
- `market_data.fundamentals` stores `average_volume` from `info`; `in_usd` leaves it alone; the screen row gains `daily_value` (dollars). The cache schema does not change: a record without the field gets it at its next weekly fetch, and until then a large-cap row is unaffected and a small-market row (always new, so freshly fetched) has it.
- The backtest has no volume history: its rows skip the liquidity check (a limit printed in the report). Its rows gain `market`, so the size floor applies.

### 3. US small caps as candidates

- `review.candidate_markets`: market keys whose Magic Formula and dislocation candidates are allowed whatever their country. Default `[]`; `config.yaml` ships `[]`. A row qualifies when its country is in `candidate_countries` or its market is in `candidate_markets` (an empty `candidate_countries` still means every country). `validate_config` checks it is a list of known market keys. The review's "Value candidates from" line names the markets too.
- **The pass rule (fixed before any run).** `backtest --indexes sp600` prints a section "## Small-company verdict" for the `magic_formula` rule (the screen's top 30, which is what becomes candidates). It **supports adding `sp600` to `review.candidate_markets`** when conditions 1 to 4 of `bar_verdict` hold: at least 20 one-year cohorts with five or more picks in at least 7 calendar years; beat the equal-weight universe in at least 60% of them; mean one-year excess at least +2 points over the universe and at least 0 over SPY; mean three-year excess over the universe at least 0. Condition 5 (beating the plain screen) does not apply, since the rule is the plain screen. The `value` rule's numbers print beside it for information.
- If it supports, Claude adds `sp600` to `review.candidate_markets` in `config.yaml` and tells the user; if not, nothing changes.
- **Limits printed:** today's S&P 600 members only, and no join dates: companies that failed or were taken over are missing (flatters the screen and the universe alike), and those that grew into the S&P 400 or 500 are missing too (the reverse). The liquidity check is skipped.

## Testing

- `fetch_market` on a recorded API payload (built in the test): tickers `ERIC-B.ST`-style, names, sectors; share-class dedupe by turnover; a failed request raises; the Oslo table's `OSE:` prefix stripped.
- `failed_filters`: a `sto` row at $300M passes size and fails at $200M; a `sp500` row at $1B still fails; missing `daily_value` fails liquidity for `sto`, not for `sp500`; exactly at a floor passes as today's checks do.
- The screen row carries `daily_value`; `fundamentals` stores `average_volume`.
- Backtest rows carry `market`; a `sp600` row uses the small floor; liquidity is not checked there.
- `candidate_markets`: a US `sp600` row is a candidate with `candidate_markets: [sp600]` and not without; validation rejects an unknown key.
- `small_cap_verdict` on hand-built summaries: each of the four conditions failing alone, all passing, the sentence; the report prints the section only for an `sp600`-only run.

## After the build

Run the screen once (the first run fetches about 1,400 new tickers; an hour or two). Run `backtest --indexes sp600` (fetches 600 companies' SEC filings). Report the verdict as it falls, the number of small companies passing the filters, and which of them the next review would see.
