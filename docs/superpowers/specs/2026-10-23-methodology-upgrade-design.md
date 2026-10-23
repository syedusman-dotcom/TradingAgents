# Methodology upgrade — design

**Date:** 2026-10-23
**Status:** the user opted out of the brainstorming questions and asked for Claude's own list of improvements, built tonight; calls Claude made on their behalf are marked *(my call)*.

## Goal

Make each stock review more consistent and more decisive without adding debate rounds: anchor every valuation to a worksheet computed in code, move the accounting and capital-allocation judgments from prose into numbers, give the agents the facts they currently guess at (earnings date, peers, the company's name), add one targeted pre-mortem, and make the Portfolio Manager's decision a parsed checklist with an entry price the weekly watch can act on.

### What the evidence says (reviews of 2026-10-15, 10-22, 10-23)

- Valuations drift between runs with no trace of why: MO's intrinsic value was $76 on 10-15 and $58 on 10-22. HRB keeps tripping the "agents' value is 30% below the formula" flag with no bridge between the two numbers.
- 13 of 15 candidates on 10-22 were deferred with "wait for the Q3 print". Some of that is earnings season; some is the agents dodging a call because nothing tells them when the print is or asks them for a price.
- The agents infer management quality from news and eyeball accounting from raw statements; both are the weakest parts of every report.
- A Korean ticker (000270.KS) went through the graph on 10-23 and worked, but the agents never learned the company's name, Yahoo returned no news for the listing, and the two SEC-only tools returned nothing.

### What the user said

- "Some other methodology you want to include that will improve the analysis? Do you want to increase the messages between agents?" and then "go ahead" with Claude's list.
- Earlier, recorded in memory: a checklist in the portfolio manager's decision, accounting red-flag metrics in code, a peer-comparison tool and capital-allocation history were wanted as small follow-ups.

### Non-goals

- More bull/bear or risk rounds. The reports are already ~11k words; the problems above are input problems. One extra targeted call (the pre-mortem) is the whole increase.
- The discovery work (wider universe in the pipeline proper, weekly new-ideas section, website integration beyond tonight's exploratory section). Separate spec.
- Per-market risk-free rates. The 9% floor in `discount_rate` dominates for every market the user trades in today; revisit if the floor ever drops.
- Changing the user's hard filters or rating thresholds. New metrics are shown and flagged; nothing new blocks a stock unless the user turns it on in config.
- Any trading. Unchanged.

## Verified facts (from the code on 2026-10-23)

- Graph order: analysts in parallel with the memory node → Bull ↔ Bear (1 round) → Research Manager → Trader → Aggressive → Conservative → Neutral (1 round) → Portfolio Manager → END (`tradingagents/graph/setup.py:109-187`). Quick tier runs everything except the two managers.
- The PM's structured output is `PortfolioDecision` (`tradingagents/agents/schemas.py:221-265`; fields rating, executive_summary, investment_thesis, price_target, time_horizon) rendered by `render_pm_decision`; `value_pipeline/parse.py` reads intrinsic value, margin of safety and thesis from the rendered text; `results.json` carries ticker, rating, error, report, tokens, nominal cost, intrinsic_value, margin_of_safety, thesis (`value_pipeline/review.py:301-307`). The price at review time is not recorded.
- `tradingagents/dataflows/value_metrics.py` computes ROIC, F-Score, Z, `formula_value` (owner-earnings and EBIT perpetuities at 0/3/5% growth, reverse DCF `implied_growth`), `company_metrics`, `render_report`. Fiscal years are dicts of `YEAR_FIELDS` (22 fields; no receivables, depreciation, SG&A or acquisitions). Yahoo rows map to fields in `tradingagents/dataflows/vendors/yahoo/value_metrics.py:ROWS`.
- Tools are LangChain tools in `tradingagents/agents/tools.py`; the MCP server (`tradingagents/mcp_server.py`) wraps whichever names are listed, so a new tool registered there is available to the Claude Code provider with no further wiring. `get_financial_history` and `get_annual_report` are SEC-only; `get_news` is Yahoo/Alpha Vantage by ticker.
- Nothing in the repo records an earnings date. Nothing passes the company's name; `company_of_interest` is the ticker.
- Memory: the PM's decision text and rating are stored per run and scored on the 126-day price return; only the PM reads the lessons.
- Actions: Buy/Overweight on a non-held name becomes BUY unless FREEFALL or margin of safety < 0; the 30%/15% thresholds live only in the prompts. The weekly watch alerts "BUY RANGE" when price is ≥ 30% below the agents' value (`watch.buy_margin_of_safety`).
- The `⚑` flag: `value_gap_flag` compares the PM's value with the screen's `value_base` and fires above ±30%.

## Design

Eight parts. Parts 1–4 are code in `tradingagents/dataflows/value_metrics.py` and its Yahoo vendor; 5–6 are tools and run context; 7 is one graph node; 8 is the decision format and what the pipeline does with it.

### 1. Valuation worksheet (anchor)

`formula_value` gains:
- **Earnings-power value** (Greenwald): normalized EBIT × (1 − tax) / rate − net debt, per share. Zero growth by construction; sits beside the 0% perpetuity.
- **Sensitivity grid**: base method values at rate −1pt / rate / rate +1pt × growth 0/3/5%. Nine numbers.
- Everything already there stays (`base`, `low`, `high`, `implied_growth`, `methods`).

`render_report` gets a **Valuation worksheet** section: normalized owner earnings and EBIT with the years used, EPV, the three growth cases per method, the grid, implied growth, price, margin of safety. The fundamentals analyst prompt already says "quote, do not recompute"; it now also says the worksheet's `base` is the anchor every later agent must refer to as "the formula value".

The Portfolio Manager must supply a **value bridge**: the formula value, then each adjustment with a sign, size and one-line reason, ending at their price target (part 8). `value_gap_flag` keeps firing at ±30%, and now prints the bridge under the flag instead of "check their reasons".

### 2. Accounting checks in code

New `YEAR_FIELDS`: `receivables`, `depreciation`, `sga`, `acquisitions` (Yahoo rows, best first: "Accounts Receivable"/"Receivables"; "Reconciled Depreciation"/"Depreciation And Amortization"/"Depreciation Amortization Depletion"; "Selling General And Administration"; "Purchase Of Business"). None when missing, as today.

New functions in `value_metrics.py`, each tolerant of None:
- `accruals_ratio(cur, prev)`: (net income − operating cash flow) / average total assets.
- `cash_conversion(years, n=3)`: mean of CFO / net income over up to n years.
- `receivables_vs_revenue(cur, prev)`: growth in receivables minus growth in revenue.
- `beneish_m(cur, prev)`: the 8-variable M-score (DSRI, GMI, AQI, SGI, DEPI, SGAI, TATA, LVGI); None if any input is missing. Threshold −1.78.
- `accounting_flags(years)`: the list of plain-English flags that fire: accruals ratio above 10%, cash conversion below 0.8, receivables outrunning revenue by more than 30 points, M-score above −1.78, share count up more than 3%/yr (already computed as dilution), capex below half of depreciation for two years (under-investment).

`render_report` gets an **Accounting checks** section: the numbers, then "Flags: …" or "Flags: none". `company_metrics` returns them so the Bear's accounting bullet and the PM's red-flag field quote them.

The screen (`value_pipeline/screener.evaluate`) adds columns `accruals_ratio`, `beneish_m`, `accounting_flags` (joined text). Optional hard filters `filters.max_accruals_ratio` and `filters.max_beneish_m` are honoured when present in config.yaml; the shipped config leaves them commented out *(my call: show first, block later if the user wants)*. The website's screen table shows the flags column.

### 3. Capital allocation history in code

`capital_allocation(years, closes_by_year=None)` returns per year: free cash flow, dividends, buybacks, acquisitions, net debt change, share count change, and the average price paid per repurchased share when both buybacks and shares retired are known; plus five-year totals: cash generated, returned (dividends + buybacks), spent on acquisitions, change in net debt, and "unexplained" (the remainder). If yearly average prices are supplied, each year's buyback price is shown against that year's average close and the current price.

`render_report` gets a **Capital allocation** section with that table and the totals line ("Over 5 years: generated $X, returned $Y (buybacks at an average of $Z vs $P today), acquisitions $A, net debt +$D"). The Yahoo vendor supplies yearly average closes from the price history it already fetches.

The fundamentals analyst's GOOD MANAGEMENT question is reworded to start from this table (what was done with the cash, at what prices) before insider transactions and news.

### 4. Price recorded at review time

`results.json` gains `price` (the review's price for the ticker) and `formula_value` (the worksheet base). That makes a later calibration page possible without re-fetching. No page is built now; two weeks of history is too little *(my call)*.

### 5. Facts the agents currently guess

- **Company name.** The run state gets `company_name` from Yahoo `longName` (fallback: the ticker), rendered in the instrument context and in every analyst's system prompt as "Company: Kia Corporation (000270.KS)". Reports and the ACTIONS table show the name.
- **Next earnings date.** `get_verified_market_snapshot` adds "Next earnings: YYYY-MM-DD (N days)" from Yahoo's calendar when available, else "unknown". The same line goes in the instrument context so the PM has it without a tool call.
- **News for non-US listings.** `get_news` falls back, when the ticker returns nothing, to a Yahoo search by company name; the report says which source was used. Best effort; empty stays empty.
- **SEC-only tools on non-US tickers** return one clear sentence ("Not a US SEC filer: use get_fundamentals, get_income_statement, get_balance_sheet and get_cashflow (Yahoo) and the annual report on the company's investor site") instead of empty output, and the fundamentals prompt tells the analyst not to retry them.

### 6. Peer comparison tool

New tool `get_peer_comparison(ticker)` on the fundamentals analyst:
- Peers: Yahoo's industry listing (`yfinance.Industry(industry_key).top_companies`), top 6 by weight excluding the ticker itself; fallback: an empty table with the reason.
- For the ticker and each peer: market cap (USD), EV/EBIT, gross margin, EBIT margin, ROIC, FCF yield, 5-year revenue CAGR, share count change — from the same Yahoo statements path as `get_value_metrics`, so the figures agree.
- Rendered as one Markdown table, ticker first, with a line for the median. Money converted to USD using the quote currency; London quotes in pence divided by 100 (reuse the exploratory screen's handling).
- Cached per ticker per day under the dataflows cache so a retry is free.

The fundamentals prompt's CHEAP question gains "cheap relative to peers, and if so, why (quote the peer table)".

### 7. Pre-mortem node

One new quick-tier node, **Pre-mortem**, between the end of the risk debate and the Portfolio Manager. Input: the fundamentals report, the research manager's plan and the risk debate. Output (~250 words, structured): "It is three years on and this position has lost 40%. The single most likely cause. The evidence visible today that supports it. The one metric that would show it first, with the level that confirms it." Stored in `state["premortem"]`, written to the report as its own section, and included in the PM's prompt with the instruction that the thesis must answer it explicitly (part 8).

Graph: `Neutral Analyst` routes to `Pre-mortem` when the risk round is complete; `Pre-mortem` → `Portfolio Manager`. The run signature gains `premortem=1`; `_log_state`, the reporters and the CLI display learn the new section.

### 8. The decision as a checklist

`PortfolioDecision` gains fields, all required unless noted:
- `good_business: Verdict` and `good_management: Verdict` and `cheap: Verdict`, where `Verdict = {pass: bool, note: str}`; `cheap.note` must contain the margin of safety as a percentage.
- `red_flags: list[str]` (may be empty) — accounting and distress items, quoting the code's flags where they fired.
- `kill_criteria: list[str]` — 3 to 6 specific, checkable facts that would make the PM sell.
- `entry_price: float | None` — the price at or below which the margin of safety is sufficient to rate Buy today, for the stock's own currency. Required when the rating is Hold, Underweight or Overweight on a name not held; may be None for Sell.
- `value_bridge: list[Adjustment]` with `Adjustment = {reason: str, amount: float}` from the formula value to `price_target`; the sum must land within 2% of `price_target`, else the PM is asked once to fix it (the structured-output retry that already exists for a bad rating).
- `premortem_answer: str` — how the thesis survives the pre-mortem's failure path, or why the PM accepts it.
- Existing fields stay. `price_target` remains the intrinsic value.

**Prompt changes (PM):** express entry conditions as prices and verifiable facts; use the earnings date only when the print is genuinely binary and say why; if the stock would be a Buy at today's price after a clean print, say so and give the entry price; the bridge must start from the worksheet's formula value.

**Rendering:** `render_pm_decision` adds `**Checklist**:` (three lines: Good business — PASS/FAIL: note…), `**Red Flags**:`, `**Kill Criteria**:` (numbered), `**Entry Price**:`, `**Value Bridge**:` (one line per adjustment, then "= Price Target"), `**Pre-mortem Answer**:`. `parse.py` reads `entry_price`, `kill_criteria`, the three verdicts and the bridge; `results.json` carries them; a position's `thesis` in `portfolio.json` gains `kill_criteria` and `entry_price` when a buy is recorded or a holding is re-reviewed.

**Pipeline behaviour:**
- ACTIONS.md: candidates not bought show "buy below X" from `entry_price`; holdings show their kill criteria under the table *(my call: the user reads ACTIONS, this is where a checklist earns its keep)*.
- The weekly watch alerts "ENTRY PRICE" when a reviewed name's price is at or below its `entry_price` and the rating is ≤ 120 days old, in addition to today's 30% rule. Holdings list their kill criteria so the user can tick them off; the watch does not evaluate them *(my call: most are judgment calls)*.
- The website's stock page shows the checklist, kill criteria and entry price from the latest review.

## Testing

- Unit tests for every new metric with hand-built years dicts, including None-tolerance and the M-score against a published worked example.
- Yahoo row mapping tests with synthetic frames for the four new fields.
- Schema tests: the bridge sum check, required fields by rating, rendering round-trip through `parse.py`.
- Graph tests: the pre-mortem node runs once, feeds the PM, appears in the report; run signature changes.
- Tool tests with mocked yfinance for the peer table, the earnings date and the news fallback; one real-CLI integration test on a foreign ticker guarded like the existing Claude Code integration tests.
- Pipeline tests: ACTIONS rendering of entry price and kill criteria; watch's ENTRY PRICE alert; results.json fields.
- Existing 1,600 tests keep passing; ruff clean.

## Rollout

1. Build in a worktree off `main`, merge when green.
2. Re-run one US holding (HRB) and one foreign name (000270.KS) through `review --tickers` and read the two reports end to end before trusting the new sections.
3. Restart the website after the merge.
4. The first full run with the new method is the November first-Saturday review; compare its values with 10-22's and read every bridge.
