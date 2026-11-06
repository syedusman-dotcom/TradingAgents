# Weekend research program, 2026-11-06 to 2026-11-08

**Mandate (user, 2026-11-06 23:45 CET):** "you are free to do as much research as you want to modify and improve the process, for the whole weekend you can run research studies and see how we can improve the algo", "use deep research skill along with the ralph loop to make a best process", "create as many agents as you want and come up with a strategy".

**Goal:** a process that gives a small private Nordnet portfolio the best evidenced chance of beating the S&P 500 (SPY) over the coming years, with every change either backed by a verdict fixed before its run or clearly marked as a recommendation for the user to decide.

**Deadline:** Sunday 2026-11-08 21:00 CET. At the deadline, whatever is open is written up as open and the final report is delivered.

## Where things stand (evidence so far, do not redo)

- About 150 agent reviews since 2026-10-11, no Buy. Every review fails "cheap" against the agents' own scenario value; Overweight needs 15%, Buy 30%.
- Buy-bar backtest (43 quarterly cohorts 2015–2025, US S&P 500+400, EDGAR point-in-time; reports `backtest_2026-11-05.md` and `_sp500.md` under the data reports folder): no formula rule beats SPY reliably. `compounder` (strict lane) is +4.7 median on 1 year (20/34 cohorts, only 62 picks) but −7.7 median on 3 years; `er_momentum` +9.8 on 3 years but −1.8 on the S&P 500 alone at 1 year. Everything else at or below SPY.
- Trend/breakout sleeve and the momentum sleeve (pre-fixed rules) both failed. Momentum funds track their parents. S&P 600 Magic Formula failed.
- Trait attribution: no screen trait predicts beating SPY; the ten largest companies beat SPY by +6.8 pts/yr; only 46% of S&P 500 names beat SPY over one year.
- Universe caveat: today's members only (join dates applied for the S&P 500 and 600; leavers missing), so the equal-weight universe is survivorship-inflated. Compare against SPY first.
- Europe/Nordics cannot be backtested with EDGAR; the paper Buys on the calibration page are the live test (first verdict about June 2027).
- Decided and live: the Nordnet USA Index fund as the core, stock picks as a satellite only on a real Buy.

## Constraints (binding)

- Nothing trades. The user trades manually and records fills.
- No rating-bar, sizing or candidate-rule change goes live without a verdict fixed in the ledger BEFORE the run that tests it, and the final report says exactly what changed. A change with evidence may be applied; a change that needs the user's money decision is a recommendation, default off.
- Nordnet markets only (no Japan, Korea, Poland, Iceland, TSX Venture).
- The GitHub fork is PUBLIC: never commit or push anything under `value_pipeline/data`, nor trades, holdings, amounts or portfolio figures, in any file. Subagents never read or write `value_pipeline/data`; the controller runs the backtests and reads the reports.
- Do not start agent reviews (`review`, `run-all`) this weekend: the Fable quota is shared with the scheduled Saturday 09:00 review and Monday's jobs. Backtests, screens and scripts are free.
- Subagent models: researchers and implementers on `sonnet`, report writers and reviewers on `opus`. Do not spawn subagents on Fable.
- Code changes: TDD, `uv run pytest -q` and `uv run ruff check .` green, one commit per item with the session trailers, push to origin main is routine. Work on main directly for research code and small fixes; use a worktree and the SDD workflow for a multi-task build.
- Scratch files go in the session scratchpad; reports for the user go to `~/Desktop/ValueTrading/reports/`; research notes to `~/Desktop/ValueTrading/research_notes/<title>/`.

## Ledger

`docs/superpowers/research/2026-11-06-weekend-progress.md`. One section per item: status (`open`, `running <task id>`, `done`, `dropped <why>`), the verdict fixed before any run, the result, and `Ruling:` lines for every judgment call. Append, never rewrite history. Commit the ledger with each item.

## Items, in priority order

Each Ralph iteration: read this file and the ledger, finish anything `running` whose output is ready, then take the first `open` item. Long runs go to the background (record the task id) and the iteration continues with the next item. Independent items (a deep research and a backtest run) may run at the same time.

### R1 — Deep research: what beats the S&P 500 out of sample, implementable at Nordnet
Question: which investment strategies have robust, out-of-sample, survivorship-aware evidence of beating the S&P 500 (or MSCI USA/World) since 2010 net of costs, and which of them can a private investor implement at Nordnet with UCITS ETFs, Nordnet funds or direct stocks (US, Europe, Nordics)? Cover: quality/QARP, profitability, shareholder yield/buybacks, equal weight, low volatility, momentum (fund and stock level), trend overlays on indices, concentrated "compounder" portfolios, small-cap quality in the Nordics, and the base rate of active stock-picking beating the index. For each: the evidence (source, period, universe, net of costs, out of sample?), the implementable version at Nordnet (instrument, cost), and what would falsify it. Use the deep-research skill (5–6 researchers on sonnet, writer on opus). Report: `~/Desktop/ValueTrading/reports/What beats the index at Nordnet.md`.

### S1 — Hold-out run of every formula rule, 2013–2014 cohorts
Run `backtest --from 2013 --to 2014` (default indexes) and `--indexes sp500`, `--no-fetch` first; fetch only if facts or prices are missing. Verdict fixed now: a rule is **robust** only if its mean 1-year excess over SPY is ≥ 0 in BOTH periods (2013–14 and 2015–25) on BOTH universes, with ≥ 5 picks in ≥ 6 hold-out cohorts. Anything else is **not robust**. Record each rule's numbers. EDGAR XBRL starts with FY2009, so 2013-03-31 is the first cohort with four fiscal years; if the harness yields fewer than 6 countable cohorts, record that the hold-out is too thin and say so in the report.

### A1 — Audit of the live review process
From the per-review `5_portfolio/decision.md` and `results.json` files (controller reads them; subagents get an anonymised CSV without portfolio data): for every review since 2026-10-11 tabulate ticker, date, lane, rating, checklist pass/fail per test, margin of safety, upside/downside, entry price vs price, trend, red flags, and which data the review said it could not retrieve (news, filings, Altman Z, formula value). Questions: which gate blocks most names; how often management is "not proven" for lack of data rather than evidence; how the agents' value compares with the formula value and the backtest's compounder price bar; which data routes fail for non-US tickers. Output: a table in the ledger plus a list of concrete defects (each becomes an F item).

### S2 — The entry-price mechanism (pullback fills)
The live process gates buys on an entry price below today's price. Test the mechanical proxy on the backtest's picks (rules `magic_formula`, `compounder_record`, `value_graded`, `compounder`): "buy now" vs "buy only if the close falls at least X% below the cohort price within 120 calendar days (X = 10, 15), at that first close, held one year from the fill". Measures per rule: fill rate, mean 1-year excess over SPY of filled picks vs the buy-now basket, and of the never-filled picks. Verdict fixed now: the entry-price gate **costs money** if, for at least three of the four rules, never-filled picks beat filled picks by ≥ 3 points and the fill rate is < 50%; it **earns its keep** if filled picks beat buy-now by ≥ 2 points for at least three rules; otherwise **inconclusive**. Implement as `value_pipeline/pullback.py` + a `pullback-backtest` command with tests, reusing `backtest.run(keep_rows=True)` rows and the price cache.

### S3 — Leaver bias
From Wikipedia's S&P 500 "Selected changes" table, list every removal 2013–2025 with its date and reason text. For removed tickers that still have Yahoo price history and EDGAR facts, add them to the universe as members until their removal date and rerun `magic_formula`, `compounder_record` and `compounder` over 2015–2025. Report the change in mean 1-year excess over SPY per rule, and the count of removals that could not be included (acquired or delisted). No verdict: this measures how much to trust the other numbers.

### R2 — Deep research: making LLM equity analysis reliable and measurable
Question: what is known (papers, practitioner write-ups, 2023–2026) about LLM-based equity analysis: accuracy and calibration of LLM forecasts and valuations, which prompting and process designs help (checklists, base rates, pre-mortems, debate, retrieval of filings), how to evaluate such a system with few decisions, and which free or cheap data routes give filings and news for European and Nordic listed companies (company IR RSS, Nasdaq Nordic and Oslo Børs news, Companies House, ESEF filings, GlobeNewswire, Cision). Deep-research skill. Report: `~/Desktop/ValueTrading/reports/Reliable LLM equity analysis.md`.

### F — Fixes found by A1, R2 and the day's reviews (each its own commit, TDD)
Known already:
- F1: the worksheet prints no formula value when the quote currency differs from the statement currency (CLS.TO: CAD quote, USD statements). Convert the statements' per-share values at the day's rate so the bridge has a start.
- F2: the agents' news and filings tools returned nothing for CLS.TO and give thin results for Nordic names; find why (ticker suffix handling, source coverage) and route non-US tickers to their US listing where one exists and to the sources R2 finds.
- F3: `pit_statements` share counts for NXPI (2024) and CXT (2015) give trillion-dollar market caps; find the tag or unit problem and fix it.
- F4: the committee reports "thesis texts are truncated"; raise or remove the truncation so it reads whole theses.
- F5: Altman Z "not retrieved" in the CLS review although the screen has it; pass the screen's accounting scores into the instrument context.
Add F6+ from A1 and R2.

### S4 — Tests of what R1 found, where our data can test it
For each R1 strategy that the US point-in-time harness can replay (shareholder-yield top N, quality plus momentum, equal-weight universe net of costs, profitability sorts, and so on), add a rule to `backtest.rules` behind a config flag, FIX ITS VERDICT IN THE LEDGER FIRST (same shape as `bar_verdict`: ≥ 20 cohorts with ≥ 5 picks, beats the universe in ≥ 60%, mean excess over the universe ≥ +2 and over SPY ≥ 0, 3-year ≥ 0, better than `magic_formula`, and non-negative on the 2013–14 hold-out), then run it. Record every verdict, pass or fail.

### R3 — Deep research: entry timing, selling and sizing for a concentrated quality portfolio
Question: evidence on limit or entry-price waiting against buying immediately, on holding periods and rebalancing frequency for quality and value portfolios, on sell rules (thesis break, price based, time based), and on position counts and sizing for a 5–15 stock satellite next to an index core. Deep-research skill. Report: `~/Desktop/ValueTrading/reports/Entry selling and sizing evidence.md`. Lower priority: run only if time remains after S2.

### D1 — The process proposal and the changes applied
Write `docs/superpowers/specs/2026-11-08-best-process-design.md`: the process as it should be after this weekend: core and satellite split, which sleeves exist and why, which gates stay, what the agents must retrieve before rating, how the process is measured (calibration, paper Buys, the one-year SPY review on 2027-10-24). Every change is classified: (a) applied this weekend, with the evidence and commit; (b) recommended, user decides (money, bars); (c) considered and rejected, with the number that rejected it. Implement (a) items through the SDD workflow if they are more than a small fix.

### Z — Final report and handover
`~/Desktop/ValueTrading/reports/Weekend research 2026-11-08.md`: what was asked, what was tested with each fixed verdict and result, what changed in the code (commits), what the user should decide, open items. Send it with SendUserFile, update the memory file, and end the loop with the promise `<promise>WEEKEND RESEARCH COMPLETE</promise>`.
