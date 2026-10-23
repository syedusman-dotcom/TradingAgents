# Evidence and calibration — design

**Date:** 2026-10-24
**Status:** the user asked how the analysis could get better and said "Do you want to implement it" to Claude's ranked list; this round takes the four items that are code and data. Calls Claude made are marked *(my call)*.

## Goal

Make the analysis better evidenced and measurable:

1. **Filings as evidence** — the company's own words from EDGAR (the latest 10-Q's management discussion and the earnings press release) available to the analysts, so demand evidence, the cause of a fall and management's guidance can be verified rather than labelled "unverified".
2. **Calibration record and page** — every review's value, scenarios, entry price and price stored and scored against later prices and the next reported numbers, so the agents' haircuts and the lane bars can be judged on evidence.
3. **Consensus sanity check** — Yahoo's analyst estimates beside the worksheet, with the Portfolio Manager stating where and why the thesis differs.
4. **Eight-quarter trend table** in the worksheet, so inflections and deteriorations are visible before the annual numbers move.

### What the user said

- "how does the analysis look now, do you see any opportunity to grow" → "get better I mean" → "Do you want to implement it".
- Context: 36 reviews so far, one non-Hold; the agents' haircuts to the formula value run 10–40%; inflection names fail only on "verified demand evidence" because the news vendor serves two months of headlines.

### Non-goals

- Earnings-call transcripts (no reliable free source). The blind second valuation, insider scoring and the lane-rule backtest (later rounds). Trading. Non-US filings (the discovery spec).

## Verified facts (code on 2026-10-24)

- `tradingagents/dataflows/vendors/sec_filings.py` serves `get_financial_history` (XBRL top-line rows) and `get_annual_report` (10-K Items 1A and 7) for US filers via `cik_for`, with caching and a look-ahead guard; non-filers get `NOT_A_US_FILER`.
- EDGAR's submissions API lists a filer's recent filings with form type, date and accession; 10-Q documents carry "Item 2. Management's Discussion and Analysis" and 8-K earnings releases are Exhibit 99.1 of the 8-K filed with Item 2.02.
- `results.json` rows carry `price, intrinsic_value, entry_price, scenarios (bear/base/bull values + probabilities), value_bridge, lane, earnings_date`; the memory module scores decisions on the 126-day price return only.
- yfinance 1.7 `Ticker` exposes `earnings_estimate`, `revenue_estimate`, `growth_estimates`, `analyst_price_targets` (DataFrames/dicts; may be empty).
- The worksheet (`render_report`) has annual per-year and Record/inflection sections; `quarters` (up to ~6 from Yahoo) are already fetched on both paths.

## Design

### 1. Filings as evidence

New vendor functions in `sec_filings.py` (US filers only, same cache and look-ahead guard as the annual report; `as_of` cut on the filing date):
- `latest_10q_mdna(symbol, as_of) -> str`: Item 2 (MD&A) of the most recent 10-Q filed on or before `as_of`, trimmed to the results-of-operations and liquidity discussion, capped at ~12k characters with a note of the filing date and period.
- `latest_earnings_release(symbol, as_of) -> str`: Exhibit 99.1 text of the most recent 8-K with Item 2.02 filed on or before `as_of`, capped at ~10k characters, with the filing date; the outlook/guidance paragraphs kept whole when found (lines containing "outlook", "guidance", "expect", "backlog", "bookings", "book-to-bill", "RPO", "demand").
- Tools `get_quarterly_report` and `get_earnings_release` in `agents/tools.py`, routed like `get_annual_report`; added to the fundamentals analyst's TOOLS and to the news analyst's TOOLS. Prompts: the fundamentals analyst reads the quarterly report for the latest quarter's drivers and guidance; the news analyst uses the earnings release and quarterly report as **verified** sources for demand evidence (a quoted sentence with the filing date counts as verified) and for classifying the cause of a fall.
- Non-filers: the `NOT_A_US_FILER` sentence, as today.

### 2. Calibration record and page

- `value_pipeline/calibration.py`: `rows(paths) -> list[dict]` reads every review folder's `results.json` (newest first) and emits one row per non-errored decision: date, ticker, name, lane, rating, price then, agents' value, formula value, bear/base/bull, entry price, expected return, and for each horizon reached (30, 90, 180, 365 days) the price at that horizon (from `market_data.closes_between`), the return, and whether price went above the value / below the bear / below the entry. `score(rows) -> dict`: by rating and by lane, the median return, the hit rate of "price reached value within 365 days" for Buy/Overweight, the share of Sells that fell, the median gap between agents' value and formula value, and the realised versus assumed probabilities (bear/bull outcomes vs stated probabilities) once ≥ 10 decisions have a 180-day horizon.
- **Next reported numbers**: when a later `results.json` or the fundamentals cache has a newer fiscal year or quarter for the ticker, record whether EBIT margin and revenue growth landed inside the scenario range the decision implied (base assumptions parsed from the scenario basis text where the PM stated them, else skipped) *(my call: best-effort, labelled)*.
- CLI `calibration` prints the table and the scores; website page `/calibration` with the table (sortable), the scores, and a one-line verdict per lane when there are enough rows ("compounder: 3 decisions, none past 90 days yet").
- Prices are fetched once per run and cached by (ticker, date) in `data/cache/prices/` so the page is cheap to reload.

### 3. Consensus sanity check

- Yahoo vendor: `consensus(symbol) -> dict` with next-year and following-year revenue and EPS estimates, the implied revenue growth, the number of analysts, the mean/median/high/low price target, fail-open (empty dict). Rendered in the worksheet as a `## Consensus (Yahoo estimates)` section: `- Consensus revenue growth next year {x%}`, `- Consensus revenue growth following year {x%}`, `- Consensus EPS growth next year {x%}`, `- Consensus price target median {v}` (one number per label; `n/a` when missing), plus one prose line comparing consensus growth with the worksheet's implied growth and earned growth.
- `PortfolioDecision` gains `consensus_view: str = ""`: where the thesis agrees or differs from consensus and why (rendered `**Consensus**:`, parsed, in results.json). The PM prompt: "State where your value differs from consensus and why; a value far below consensus with no stated reason is a problem." `decision_checks` adds a soft check *(my call: a problem only when the field is empty and the consensus section exists)*.
- The committee payload carries the consensus growth and median target per stock.

### 4. Eight-quarter trend table

- `render_report` gains `## Last eight quarters` after the annual table: per quarter (newest first, up to eight when Yahoo has them, else as many as exist): revenue, revenue y/y, gross margin, EBIT margin, operating cash flow, free cash flow, cash conversion (CFO/net income), receivables/revenue, with a prose line naming the direction of margins and cash conversion over the span. Computed in `value_metrics.quarter_trend(quarters) -> list[dict]`, None-tolerant; y/y uses the date-matched year-before quarter.
- The fundamentals analyst prompt: "Use the eight-quarter table for the trend; annual figures for the level."

## Testing

- Vendor tests with synthetic EDGAR submissions and documents (no network): the latest 10-Q on or before as-of, the MD&A extraction and cap, the earnings release exhibit selection, guidance lines kept, non-filer sentence, historical as-of respected.
- Calibration: rows from two synthetic review folders; horizons computed from a patched `closes_between`; scores with and without enough rows; the page renders empty and populated.
- Consensus: vendor parsing from synthetic yfinance frames (empty, partial, full); worksheet labels; PM field round-trip; soft check.
- Quarter trend: y/y matching, partial data, rendering.
- Real run after merge: HRB (US filer) end to end; read the quarterly report and earnings release in the tool outputs, the Consensus line and the trend table.

## Rollout

Build in a worktree; merge after the Saturday 09:00 weekly run has finished (it must use the code tested yesterday); restart the website; run HRB once to read the new sections.
