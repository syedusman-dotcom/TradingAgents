# Weekend research ledger — program: docs/superpowers/research/2026-11-06-weekend-program.md

Started 2026-11-06 23:55 CET. Statuses: open, running <task id>, done, dropped <why>.

## Index
- R1 open
- S1 open
- A1 open
- S2 open
- S3 open
- R2 open
- F1–F5 open (F6+ added by A1/R2)
- S4 open (verdicts fixed per rule before each run)
- R3 open (only if time remains)
- D1 open
- Z open

## S1 — Hold-out 2013–2014 (running bbrpvyypa)
Verdict fixed (program): robust = mean 1y excess over SPY ≥ 0 in both periods on both universes, ≥ 5 picks in ≥ 6 hold-out cohorts. Started 2026-11-06 23:58 CET: `backtest --from 2013 --to 2014 --no-fetch` default indexes, then `--indexes sp500`. Logs `backtest_holdout*_2026-11-07.log`.

## R1 — Deep research (running, deep-research skill, researchers on sonnet, writer on opus)
Started 2026-11-06 23:58 CET. Notes `~/Desktop/ValueTrading/research_notes/What beats the index at Nordnet/`, report `~/Desktop/ValueTrading/reports/What beats the index at Nordnet.md`.

### S1 result (done 2026-11-07 00:05 CET)
Reports `backtest_2026-11-06.md` / `_sp500.md` (8 cohorts 2013-03-31 → 2014-12-31; cached data only). Mean 1-year excess over SPY on cohorts with ≥ 5 picks (cohorts, mean, beat count; "u" = mean excess over the equal-weight universe):

| rule | hold-out both | hold-out sp500 | 2015–25 both | 2015–25 sp500 | verdict |
|---|---|---|---|---|---|
| magic_formula | 8: +2.7, 8/8, u +0.0 | 8: +5.0, 8/8, u +1.1 | 43: −2.4, 19/43, u −9.3 | 43: −2.0, u −3.0 | not robust |
| compounder_record | 7: +4.3, 4/7, u +2.6 | 7: +6.7, 5/7, u +3.0 | 43: +0.1, 20/43, u −6.8 | 43: +0.9, 19/43, u −0.1 | **robust by the fixed rule, but the main-period edge is +0.1/+0.9: indistinguishable from SPY** |
| compounder_hint | 8: +1.9, 6/8 | 8: +5.2, 6/8 | 43: +0.1 | 43: +0.3 | robust by the rule; edge ≈ 0 |
| compounder (strict live lane) | no cohort with ≥ 5 picks (3 picks in all) | – | 0 countable | – | **cannot be judged: too thin** |
| compounder_overweight | – | 7: +16.8, 6/7 | – | – | too thin |
| compounder_fcf | 5: +1.0 | 3: +10.0 | 43: +0.3 | 32: +1.5 | not robust (hold-out < 6 cohorts) |
| er_hurdle | 8: +5.1, 6/8 | 8: +6.5, 7/8 | 43: −0.8 | 43: −0.6 | not robust |
| er_momentum | 8: +8.1, 7/8 | 8: +9.3, 8/8 | 33: +0.4 | 35: −1.7 | not robust |
| trending_value | 8: +4.2 | 8: +6.3 | 43: −2.3 | 43: −2.5 | not robust |
| value_graded | 7: +5.2 | 6: +10.9 | 32: −1.9 | 17: −2.9 | not robust |
| value (live rule) | <5 picks per cohort | – | 11: −0.7 | 6: −3.1 | not robust |
| inflection, dislocation, value_no_downtrend | too thin | | | | too thin |

Reading: 2013–14 was a period when the equal-weight universe itself beat SPY (every rule's "u" is near 0 while its SPY excess is positive), and survivorship is heavier that far back (about 200 of today's S&P 500 names joined later and are excluded, but leavers are missing), so the hold-out flatters every rule. The only rules non-negative in all four cells are the quality-record filters (`compounder_record`, `compounder_hint`), at +0.1 to +0.9 points in 2015–25: a quality tilt that matches SPY, not an edge. The strict live `compounder` lane picks too few names to be judged at all.
Ruling: S1 verdict applied as fixed; `compounder_record`/`compounder_hint` are recorded as "robust, edge ≈ 0" rather than as a pass worth money — the fixed rule tested sign, not size, and I will not read +0.1 as evidence. Cost if wrong: a real +1 pt tilt left unused.
Table script: session scratchpad `holdout_table.py`.

## A1 — Audit of the live review process (done 2026-11-07 00:20 CET)
Source: 153 decisions in the reports folder (2026-10-11 → 2026-11-06; table in the session scratchpad `reviews_audit.csv`, not committed). 102 US, 51 non-US. Ratings: Hold 84, Underweight 39, Sell 21, Overweight 9, Buy 0. 40 early decisions predate the checklist (before 2026-10-23).

Gates, on the 113 decisions with a checklist:
- **Cheap: FAIL 110, PASS 3.** Margin of safety against the agents' own scenario value: median −5%, quartiles −16% / +7%, max 21%; 50 at or above 0, 8 at or above 15%, 0 at or above 30%. The agents' value is median 0.89× the no-growth formula value (quartiles 0.75× / 1.10×): they are more conservative than the formula, not less. This gate blocks nearly everything and it is doing what it was built to do; the backtest says the value rule has no edge anyway (S1).
- **Good management: FAIL 36 of 112.** 29 of those passed "good business". About 12 of the 36 say "not proven / unproven / not established / not verified / untested": a data gap (no insider data, no capital-allocation history, no filings read), concentrated in the non-US names reviewed since 2026-11-05. These block the paper Buys too (the `fair_price` rule needs management PASS), so the European live test is losing sample to missing data, not to evidence.
- **Still investing: FAIL 44, PASS 40, blank 69.** Does not bar a rating outside the compounder lane.
- **Trend:** FREEFALL mentioned in 71 decisions (candidates arrive after a fall; the dislocation lane).
- **Overweight 9:** 4 pre-checklist (ADBE, HRB twice, a Korean name no longer in scope) and DECK four times (2026-10-23/24/31; compounder lane, margin 12–17%, upside/downside up to 2.5, no red flags). The committee said BUY 5% on 2026-10-24 and 2026-10-31; the rules said SKIP: "Overweight in a Buy-only lane" — the user's standing choice "value plus strict compounder Buys". So the one name the process liked was blocked by configuration, not by evidence. (Goes to D1 as a user decision.)
- **Data routes:** "not provided / returned nothing" appears in 35% of US and 37% of non-US decisions; the news tool is Yahoo only (`news_data: yfinance`), which was down on 2026-11-06 (CLS.TO got no news at all) and is thin for Nordic names; insider transactions are Yahoo (US only). Altman Z and the formula value are computed by the screen but not always passed to the Portfolio Manager.

Defects added:
- F6: news has a single vendor (Yahoo). Add a keyless fallback (Google News RSS search by company name, filtered to the window) so an outage or a non-US name never yields "returned nothing".
- F7: non-US insider and capital-allocation evidence: route Nordic names to the public insider registers (Finansinspektionen insynsregistret, FIN-FSA, Oslo Børs newsweb, Danish FSA) — R2 to confirm the free routes; until then the PM's "not proven" should be written as `UNPROVEN` rather than FAIL so the paper rules can count it separately (D1 decides whether paper rules accept it).
- F8: the committee cannot see the formula value, Altman Z and the F-score for every name (CLS review: "Altman Z not retrieved" although the screen has it) → pass the screen row's accounting scores into the instrument context (same as F5).
Ruling: the "cheap" gate is not a defect; the live process is producing the output a fair-value market should produce. The improvement targets are data completeness (F6–F8), the paper-Buy sample (F7), and the configuration decision on compounder Overweights (D1), not the bars. Cost if wrong: a bar that is too tight stays too tight; the paper Buys will show that by mid-2027.

## S2 — Entry-price (pullback) study (done 2026-11-07 00:05 CET)
Code: `value_pipeline/pullback.py`, `pullback-backtest` (commit fefff21, 20 tests). Run on the 2015–2025 picks (`pullback_2026-11-07.md`). Mean 1-year excess over SPY, points; "filled" = bought at the first close ≥ X% below the start price within 120 days, held one year from the fill:

| dip 10% | picks | fill rate | buy now | filled (from fill) | same names bought at start | never filled (buy-now) |
|---|---|---|---|---|---|---|
| magic_formula | 1290 | 36% | −2.4 | −1.7 | −12.7 | +3.3 |
| compounder_record | 1082 | 48% | −0.2 | +6.0 | −7.6 | +6.8 |
| value_graded | 246 | 41% | −0.9 | +2.2 | −11.9 | +6.9 |
| compounder | 62 | 47% | +7.6 | +15.2 | −1.7 | +15.8 |

At 15%: fill rates 18–31%, filled excess −0.6 / +8.7 / +3.8 / +27.3, never-filled +1.2 / +4.4 / +4.7 / +10.1.

Fixed verdict: **earns its keep** (filled beats the same names bought at the start by ≥ 2 points for all four rules; the "costs money" condition held for only 2 of 4). Portfolio-level reading, which is the one that matters (money not filled sits in the index at 0 excess): fill_rate × filled excess against buy-now — magic_formula −0.6 vs −2.4, compounder_record +2.9 vs −0.2, value_graded +0.9 vs −0.9, compounder +7.1 vs +7.6. Waiting for a 10% dip and otherwise staying in the index was as good as or better than buying at once for every rule, even though the names that never dipped were the best performers (+3 to +16). Caveats: picks are survivorship-biased; close-based fills; the filled window starts later so SPY windows differ; one sample.
Ruling: the live entry-price queue with the index fund as the parking place is consistent with the evidence; keep it. The implementer's choices (both conditions → inconclusive; missing price cache excluded from n; dip at exactly the level fills) accepted. Cost if wrong: none to the live process; the study is informational.

## S3 — Leaver bias (running: implementer a73869910b022b68f on sonnet; brief `weekend/s3-brief.md` in the session scratchpad)
Started 2026-11-07 00:10 CET. Flag `backtest --leavers`; the run follows when the code lands.

## R2 — Deep research: reliable LLM equity analysis (running, 4 researchers on opus)
Started 2026-11-07 00:12 CET. Ruling: R2 researchers run on opus with a 35-call budget because the R1 sonnet researchers returned partial notes (many fetches failed within 15 calls); cost is tokens, not Fable quota.

## F1 — formula value with mixed currencies (running: implementer a9639c1624e379fac on opus; brief `weekend/f1-brief.md`)
Started 2026-11-07 00:20 CET. Cause found: `yahoo/value_metrics.py::get_value_metrics` drops the price whenever `info["currency"] != info["financialCurrency"]` (except pence/pounds), so the worksheet has no formula value for CAD/USD, SEK/EUR, CHF/USD names. Fix: convert at the cross rate, render per-share values in the quote unit as the pence case does.

## R1 — report writer running (a641729c75aae58b2, opus); second round done: `european_momentum.md`, `swedish_investment_companies.md`.

## F6 — Google News RSS fallback (running: implementer a63613793f47589d9 on sonnet; brief `weekend/f6-brief.md`)
Started 2026-11-07 00:30 CET. Ruling: three implementers (S3, F1, F6) work in the same tree on disjoint files, told to `git add` by name; a conflict costs one re-run.
F4 (committee thesis truncation: `parse.thesis_summary` limit 300 feeds results.json) and F5/F8 (screen accounting scores into the pipeline note) are queued after these land to avoid touching `review.py` twice.

### S3 code landed (commit 55b4fe0, 2353 tests); run `backtest --leavers` started 2026-11-07 00:35 CET (task bwvuhvmnv, log `backtest_leavers_2026-11-07.log`; fetches EDGAR facts and Yahoo prices for the leavers, so it takes a while).

### R1 done (2026-11-07 00:40 CET)
Report `~/Desktop/ValueTrading/reports/What beats the index at Nordnet.md` (sent to the user); notes in the folder of the same name (8 researcher notes: quality/QARP, momentum & trend, yield/equal-weight/low-vol, compounder managers, Nordic small caps, base rates, European momentum, Swedish investment companies).
Findings that matter for the process:
- The only strategy with a live, net, buyable record of beating its index: European momentum (iShares Edge MSCI Europe Momentum, CEMR, 0.25%): +1.8–2.0 pts/yr vs MSCI Europe in EUR 2015–Oct 2026, but −13.6 pts over 2021–23, +0.6 over 5 years, −4.6 in 2026 to date; t ≈ 0.9 on the live record alone; supported by the French-library Europe momentum factor 2010–26 (10.5%/yr, t 4.0) and MSCI's back-test. World momentum (IS3R) +1.9 pts over 10 years, worse over 5; US momentum no edge (MSCI USA Momentum 14.75% vs 14.86% over 10 years).
- Quality ETFs tie their parent before fees, trail after. Trend overlays cost 3–5 pts/yr 2010–26. Equal weight, min-vol, dividend aristocrats, buyback funds all trailed. Compounder funds gave back their lead after 2021. Nordic small-cap funds on average tied their index to 2021 and lost 13 pts to large caps in 2025.
- Swedish investment companies: Investor B ≈ 19.8%/yr vs SIXRX 10.6% (2011–25); Spiltan Investmentbolag ≈ 14.6% vs 11.9% (10 years, 0.22% all-in); roughly a tie with the S&P 500 in SEK (≈ 15.9%/yr 2010–25, 1.6 pts of it from the dollar); 2–2.5 pts/yr of Investor's lead was the NAV discount closing (now a 4% premium).
- Base rates: 97% of European and 100% of Nordic active funds trailed over 10 years; the notes' estimate for a disciplined retail quality/value process is ≈ 0%/yr excess (−2 to +2).
- Ranked satellites next to an S&P 500 core: (1) CEMR 10–20%, expected +0.5–1.5 pts on the sleeve; (2) 12-1 momentum as a ranking or veto inside the stock process; (3) Swedish investment companies for SEK diversification, ≈ 0 vs S&P; (4) IS3R; (5) a direct quality/value book only with a scorecard fixed in advance.
Ruling: nothing here changes a live bar. Candidates for S4 (our data can test): a 12-1 momentum veto/rank inside the existing rules (partly covered: `er_momentum`, `trending_value` were not robust in S1). Candidates for D1 as user decisions: a European momentum ETF sleeve (CEMR) — the one satellite with evidence; Swedish investment companies as SEK diversification. The report's conflict with the earlier (2026-11-05) estimate of +1–3 pts for a stock book is resolved in favour of ≈ 0.

### F1 done (commit b823beb) and F6 done (commit fabfc11); suite 2362 green, ruff clean, pushed 2026-11-07 00:50 CET.
Live checks: BETS-B.ST's worksheet now prints per-share values in SEK converted from EUR statements (formula base 232 SEK against a 97.75 price; before the fix the worksheet had no formula value for any SEK/EUR, CAD/USD or CHF/USD name). `google_news.get_news("CLS.TO", ...)` returns dated articles where Yahoo returned nothing. Default `news_data` is now `yfinance,google_news`.
Ruling (F1): the implementer kept money-in-millions in the statement currency and converted only per-share figures (price, value, buyback averages, consensus) at today's cross rate — accepted; historical buyback prices converted at today's rate are approximate, noted in the worksheet. Cost if wrong: a buyback-price comparison off by the currency move.
Ruling (F6): Google News is a sample matched by company name; the tool says so in its output so the analyst verifies. Accepted.

## R2 researcher 2 of 4 done (process designs / evaluation). Key numbers for D1: a 2-point edge at 15% tracking error is undetectable from returns (≈ 225 years for t = 2); pooled decisions need ≈ 1,800–3,100 to detect 2 points; a 0.02 Brier improvement needs ≈ 200–785 probability forecasts, so calibration of stated probabilities is the only fast signal. Multi-agent debate does not beat equal-cost majority voting or self-correction (Smit et al. 2024; "Cost of Consensus" 2026: 58–86% of agents adopt the majority, debate costs 2.2–3.4× tokens); it helps only when assigned opposing debaters hold evidence the judge lacks. LLM confidence clusters at 80–100% regardless of accuracy; anchoring on numbers in the prompt survives chain-of-thought. Fixes with evidence: figures from tool-extracted fields, a valuation pass that cannot see the price, a bear agent with its own document search, randomised argument order, recalibration from the logged record after 50–100 resolved forecasts, rejected candidates scored as paper positions too.

## F3 — share counts (closed 2026-11-07 01:00 CET, not reproducible)
Checked the point-in-time stores and the price cache: NXPI 2024 shares 261M (no split in its history); CXT's 2.879 "split" on 2023-04-04 (Yahoo records the Crane spin-off as a split) gives 58.1M × 2.879 = 167M shares and a 2015 market cap of $3.1–4.2bn in the current picks CSV, which is right. The trillion-dollar CXT row in the 2026-11-05 trait dump came from a price cache that then carried the ratio as 2879; the refetch since fixed it. No code change. The remaining caveat (a spin-off treated as a split deflates pre-spin prices and inflates the count by the same ratio, so market cap is right and per-share history is on Yahoo's basis) is the existing documented behaviour.

## R2 researcher 3 of 4 done (LLM accuracy). Key facts for D1: the Kim–Muhn–Nikolaev "LLM beats analysts" paper was withdrawn (Feb 2025); Li et al. 2025: GPT-4 Turbo less accurate than the analyst consensus on earnings, beats it in ~¼ of firm-quarters, worse after its cut-off while its stated confidence (0.685) does not fall; no out-of-sample test of LLM fair values exists; LLM return forecasts over-extrapolate; the TradingAgents paper's Sharpe 5.6–8.2 was three stocks over three months without costs, and a 2026 re-run gives 0.43 gross / 0.22 net, behind buy-and-hold on 4 of 5 stocks; FinMem/FinAgent lose to buy-and-hold in a survivorship-free replication; nothing found beats the index net of costs after the model's cut-off on a broad universe. Leakage: backtests of LLM picks are contaminated wherever the window overlaps training data; only a logged forward record counts.

### F4 + F5/F8 done (commit 331c49c, 2365 tests, pushed 2026-11-07 01:05 CET): the committee reads whole theses (`thesis_full`); the pipeline note now carries F-Score, Altman Z, Beneish M, accounting flags and the screen's formula value.

## R2 researcher 4 of 4 done (European data routes, endpoints called live on 2026-11-07): MFN.se JSON gives the full text of Nordic regulatory releases and annual-report PDFs by company slug (undocumented backend; ~10 years deep); Nasdaq Nordic `api.news.eu.nasdaq.com/news/query.action` (STO/HEL/CPH, ~4 years, category labels incl. "Managers' Transactions"); Oslo Børs NewsWeb `api3.oslo.oslobors.no/v1/newsreader`; filings.xbrl.org JSON for ESEF annual reports (SE 1,838, DK 2,218, FI 1,169, NO 1,199, UK 2,970, DE 0); FI Insynsregistret CSV export (Swedish insiders; UTF-16LE, semicolons, 1,000-row cap per query); BaFin and AFM CSV; Yahoo insider data is EMPTY for every Nordic name (shows zero purchases for Volvo although FI recorded one on 2026-10-27); Yahoo consensus fields are filled for European tickers; Google News RSS ~100 items cap; ESAP public portal not before July 2027.
R2 report writer running (a658046c831b0adaf, opus).
Ruling: F7 becomes a small build — a Nordic news vendor (MFN + Nasdaq Nordic + NewsWeb) in the news chain ahead of Google News for .ST/.CO/.HE/.OL tickers, and an insider vendor (FI Insynsregistret for .ST; the exchanges' "Managers' Transactions" category for .HE/.CO/.OL) in the insider chain — because A1 showed a third of management fails are data gaps on exactly these names. Spec-light: a brief with the endpoints, tests on canned payloads.

### R2 done (2026-11-07 01:15 CET)
Report `~/Desktop/ValueTrading/reports/Reliable LLM equity analysis.md` (sent to the user); 3 notes. Conclusions for the process: no published evidence of an LLM picking or valuing stocks better than the market or analysts after its training cut-off, net of costs, on a broad universe; the closest design to ours (TradingAgents) re-ran at Sharpe 0.22 net; debate/committee designs converge on the majority (58–86%) and do not beat averaging independent views; stated confidence stays high after the cut-off; a return-based proof of a 2-point edge takes ~225 years, so calibration (Brier) of dated probability statements is the only signal available within 1–3 years. Ranked changes: (1) log every probability with a date and resolution rule, Brier-score against a base-rate forecaster, recalibrate from the record; (2) score Rejects and Buys-not-taken as paper positions, fix rules per period, count variants; (3) worksheet figures only from tools with source pointers (done for the formula value in F1/F5); (4) independent first views, incl. a price-blind fair value; (5) assigned bear with its own evidence, randomised order for the judge, compare with plain averaging; (6) anchor fundamentals on analysts' metrics; (7) scenarios from base rates, treat LLM margins as optimistic; (8) keep the checklist as hard gates; pre-mortem only if the log shows it moves probabilities; (9) only forward records count; (10) wire the data routes (F7).
Ruling: items (1), (2) and (10) are buildable this weekend with pre-fixed scoring rules and no bar change; (4) and (5) are prompt/graph changes to the agents and go to D1 as recommendations with a measurement plan, not applied blind. Cost if wrong: a cheap improvement delayed.

### S3 rerun: the live Wikipedia page lost its changes table (the first run found 0 leavers); `removed_dates` now reads the pinned 2025-12-23 revision (commit on main); rerun started 01:12 CET (task bw36oj1a8).

## F7 — Nordic releases and insiders as vendors (running: implementer af17fcc611e8be8e9 on opus; brief `weekend/f7-brief.md`). Started 2026-11-07 01:20 CET.

## P1 — Brier scores for scenario probabilities (running: implementer ae83881b733b9a154 on sonnet; brief `weekend/p1-brief.md`). Started 2026-11-07 01:25 CET. Reading rule fixed in the brief: n ≥ 30 at 365 days; informative = both skill scores > 0; over-confident = stated − realised > 10 points; else uninformative. No bar change; measurement only (R2 change #1).

## S4 — record_momentum (verdict fixed BEFORE the run, 2026-11-07 01:30 CET)
Rule: quality record (`_record`) rows at or above the cohort's median 12-1 momentum, top 20 by momentum. Verdict: the standard `bar_verdict` (≥ 20 cohorts with ≥ 5 picks, beats the universe in ≥ 60%, mean excess over the universe ≥ +2 and over SPY ≥ 0, 3-year ≥ 0, better than `magic_formula`) on the 2015–2025 run AND on the S&P 500-only run, AND mean 1-year excess over SPY ≥ 0 on the 2013–14 hold-out. Anything less: not supported. Implementer dispatched (sonnet, brief `weekend/s4-brief.md`).

### P1 done (commit 1a94128): `calibration.brier`, the page card and the markdown reading; README. Ruling: the implementer's order (informative checked before over-confident) follows the brief as fixed; a leg both skilled and overstated reads informative, with its stated/realised numbers printed, so the overstatement is visible. Accepted.
### S4 code done (commit ac80802); runs started 01:40 CET (task bogoxvsr1): default, sp500, hold-out 2013–14.

### S3 result (done 2026-11-07 01:50 CET; report `backtest_2026-11-07_leavers.md`)
300 names left the S&P 500 between 2013 and 2025-12 (pinned Wikipedia revision). 83 could be added (still listed, so Yahoo prices and an EDGAR CIK by ticker exist); 217 could not: the SEC ticker→CIK map no longer carries acquired or delisted tickers, so their facts are unreachable by ticker (an improvement would map by company name or LEI; not done this weekend). Effect of the 83 demoted names on mean 1-year excess over SPY, 2015–25, 43 cohorts (today's members → with leavers): magic_formula −2.4 → −2.7; compounder_record +0.1 → −0.5; compounder_hint +0.1 → −0.4; value_graded −1.9 → −2.6; er_momentum +0.4 → −0.2; trending_value −2.3 → −2.7; value −0.7 → −1.2. The equal-weight universe excess moves by +0.1 to +0.4 (less inflated).
Reading: the demoted leavers cost every rule about 0.3 to 0.7 points, which turns the quality-record rules' "≈ 0" into "slightly negative"; the 217 acquired or bankrupt names are still missing and their net sign is unknown (acquisitions are usually paid at a premium, bankruptcies lose everything). Every "robust" reading in S1 should be taken as "no edge, possibly slightly negative".
Ruling: no further survivorship work this weekend; the CIK-by-name mapping is listed as a follow-up. Cost if wrong: the remaining bias is unmeasured in both directions.

### F7 done (commit 3aee834, 2391 tests, pushed 01:55 CET): `tradingagents/dataflows/vendors/nordic.py`; `news_data` chain is now `nordic,yfinance,google_news` (the insider tool lives in that category too). Live: Betsson's Q3 release text and FI insider rows (a chairman's buy on 2026-03-13; the June "Tilldelning" rows are share awards) now reach the agents. Implementer's findings recorded as caveats: Nasdaq `freeText` returns other issuers (filtered on `company`), FI's issuer search returns Volvo Cars for Volvo (filtered on `Emittent`), NewsWeb overflow at 601 messages (window halved), the insider notice is withheld for past-dated runs by the existing guard.

## R3 — Deep research: entry, selling, sizing (running, 3 researchers on opus). Started 01:55 CET. Ruling: run it because kill criteria, the 10% position cap and the 5-stock minimum are live process parts without an evidence note; tokens only.

### S4 result (done 2026-11-07 02:05 CET): record_momentum **not supported**
2015–25 full universe (43 cohorts): beat the universe in 10 of 43, mean 1-year excess −8.4 over the universe and −1.5 over SPY, 3-year −18.1 (report `backtest_2026-11-07.md`). S&P 500 alone (36 cohorts): 16/36, −1.1 / −0.9, 3-year +1.9 (`_sp500.md`). Hold-out 2013–14: 3 countable cohorts, +3.2 over SPY (`_holdout_s4.md`, copied aside because the hold-out run writes the same filename as the main run — a naming gap noted as a follow-up). Fails conditions 2, 3 and 4. Quality + momentum on this universe does not beat SPY; consistent with R1 (US momentum ≈ 0 since 2010).

### R3 researchers done (3 notes). Key points: no study supports a 7–20% entry price below market (limit orders fill on bad news and miss good news; Coutts: waiting for a 10% correction 6.6%/yr vs 12.1% buying at once, 1990–2024); our S2 found the opposite at the portfolio level for formula picks, so the two readings are recorded side by side for D1. Selling: institutional sells underperform a random sell by ~0.8%/yr (Selling Fast, Buying Slow); stops lower returns under a random walk and tight stops always; Mauboussin: 41% of top-ROIC companies still top-quintile after nine years; Akre: costliest mistakes were selling too soon; implication — kill criteria as measurable fundamental thresholds, no price stops on quality/value holdings, trim at the cap with a band, re-verify after each quarterly report, log every sell against a random-sell counterfactual. Sizing: Bessembinder — a 5-stock portfolio beats the market 47% of 1-year periods and 41% of 10-year periods with no skill; best ideas (top-5 conviction positions) beat managers' other holdings by 2.8–4.5%/yr; no optimiser beats equal weight (DeMiguel); edge/tracking-error ratio is the same at any satellite size, so a 10% satellite with zero edge costs ≈ 0.1%/yr; idle cash costs more than a zero-edge satellite; 8–10 names better than 5; cap within the satellite at 20% equal weight.

## D1 — draft written 2026-11-07 02:30 CET: `docs/superpowers/specs/2026-11-08-best-process-design.md` (applied / recommended / rejected classification; sizing and compounder-Overweight decisions left to the user; agent-round changes as the next build with an A/B measurement plan). Final review of the weekend's code running (afd0db55857e9701b, opus, package `.superpowers/sdd/2026-11-06-weekend-program/review-6952d54..5ca12bf.diff`); R3 report writer running (ae36bd68a109a0f49).

### R3 done (2026-11-07 02:40 CET): report `~/Desktop/ValueTrading/reports/Entry selling and sizing evidence.md` (sent). Its reconciliation of S2 with the literature: the dip gate is free only where the picks have no alpha; in the compounder lane it cost 0.5 points at 10% and 2.7 at 15%. D1 amended: entry-price queue kept for value and dislocation candidates; compounder Buys near market (next agent round). Note: the writer read backtest reports under the data folder to quote exact figures; those files hold no portfolio data.

## Final review of the weekend's code (2026-11-07 02:50 CET): 8 Important, 10 Minor (`.superpowers/sdd/2026-11-06-weekend-program/final-findings.md`). ONE fix wave dispatched (opus, rulings in `fix-wave-brief.md`): I1 ADR share basis (convert only when market cap/price agrees with the statement share count), I2 historic averages at each year's rate, I3 "(3% growth base)" label, I4 leaver membership intervals from the Added column, I5 ticker-reuse guard by entity name, I6 fail loudly without leave dates, I7 no confident negatives on a failed name match, I8 truncated window → unavailable; minors M1, M3–M10 fixed, M2 documented. After the wave: ONE scoped re-review, then S3 rerun (I4/I5 change the leaver set).
Ruling: I2's per-year rate adds two FX history calls per mixed-currency review; accepted because the buyback-price comparison is one of the management test's main inputs. Cost if wrong: slower reviews for Nordic names by a few seconds.

### Fix wave done (commits 05a2a05, c2f7a13; 2415 tests). Scoped re-review (`re-review.md`): 17 addressed, 1 partly (M7: Nasdaq gives no id linking translations, so a non-English twin is dropped only when an English release shares the minute and the category or headline), 0 not; 2 new minors (N1: a foreign quote lost its conversion when the share bases could not be compared; N2: the CLI swallowed every RuntimeError) fixed by the controller in 20d4fa7 (2417 tests, pushed 03:20 CET). Remaining gaps recorded by the re-reviewer, left open: the capital-allocation total buyback price still at today's rate; a ticker reused inside the changes table keeps the earlier company's intervals; `--no-fetch` runs on stores cached before the entity name was saved count every leaver as "ticker reused" until one fetching run. S3 rerun with the fixed leaver set running (task b0upx0p5b).

## P2 — `sizing.max_stock_share` (running: implementer a1424bca8f45d0be5 on sonnet; brief `weekend/p2-brief.md`). Default 1.0 = no change; gives the user the one-line lever for D1's satellite cap. Started 03:30 CET.
Correction to A1: the compounder Overweight block (`lane_buy_only`) was set on 2026-10-24 on the first backtest's −2.5 points, not without evidence; D1 §5.2 corrected and the recommendation softened to "weakly held".

### S3 corrected rerun (03:40 CET): with membership intervals and the ticker-reuse guard, 51 leavers included (137 rejected: no EDGAR facts or ticker reused). Mean 1-year excess over SPY 2015–25 moves the same way as before: magic_formula −2.4 → −2.7, compounder_record +0.1 → −0.5, compounder_hint +0.1 → −0.4, value_graded −1.9 → −2.6, er_momentum +0.4 → −0.2, trending_value −2.3 → −2.7, value −0.7 → −1.2; the universe's own excess falls by 0.5–1.0. Conclusion unchanged: no rule has an edge; the quality-record rules are slightly negative once demoted leavers are in.
### P2 done (commit 4f59769, 2425 tests, pushed): `sizing.max_stock_share`, default 1.0, ACTIONS shows "Stocks: x% of the account (cap y%)" when set. The user flips it to 0.2 to apply D1 §5.1.

## Z — final report drafted and sent 2026-11-07 04:10 CET: `~/Desktop/ValueTrading/reports/Weekend research 2026-11-08.md`. Website restarted on the new code (calibration page 200, Brier section renders). Pullback study rerun on the S&P 500 picks ("earns its keep", 4 of 4) and on the leaver-corrected picks (inconclusive: 3 rules fit "costs money", 4 "earns its keep"); the two runs overwrote each other's report (same-day filename; follow-up noted).
Remaining before the loop ends: check the Saturday 09:00 scheduled review (first live run with F1/F5/F6/F7 in the agents' tools) and note the outcome; update the memory file; then the promise. Ruling: the loop stays open until that review has run, because a data-route fix that breaks the agents would be worse than no fix.

## Decisions of 2026-11-07 08:30 (the user delegated them: "Any you decide based on the goal")

The goal is the most money over the coming years, measured against SPY on 2027-10-24. Decided on the weekend's evidence:

1. **Satellite cap applied.** `sizing.max_stock_share: 0.2`, `sizing.max_position_weight: 0.05` (was 1.0 and 0.10). The satellite has no evidenced edge (S1, S3, S4, A1), so the cap costs nothing in expectation and bounds the loss if the process is wrong; it lifts when the paper Buys (mid-2027) and the Brier reading (late 2027) say more.
2. **Compounder Overweights allowed at half size.** `sizing.lane_buy_only: []` (was `[compounder]`, the user's 2026-10-24 choice). The evidence is mixed and thin (1-year −2.0, 3-year +5.6, hold-out +16.8); with the cap, the cost of being wrong is bounded, and the live record then holds the process's own best ideas (A1: Deckers).
3. **European momentum ETF: yes, at 10% of the account.** CEMR, with the pre-registered sell rule (trails MSCI Europe by > 5 points over any rolling three years from purchase, or MSCI's 10-year factsheet edge below +1 point). The user trades manually and records it like any ticker. Ruling: the low end of 10–15% — the evidenced edge is over MSCI Europe, not over SPY, so against the benchmark it is a diversification position with a factor premium, not a return bet.
4. **Next agent round: go.** Built behind a config flag, A/B measured by Brier against the unchanged graph (D1 §6). Spec and plan follow in this ledger.

Ruling: the sizing changes went live before the Saturday 09:00 review; they touch `actions` only, not the agents, so the data-route check on that review is unaffected.

## A2 — The independent agent round (decision 4 of 2026-11-07)

Status: `running` (spec written 09:00; builder check, plan, SDD build in `.worktrees/agent-round`, branch `agent-round`).

Spec: `docs/superpowers/specs/2026-11-07-agent-round-design.md`. Two arms (`current`, `independent`), assignment per ticker and date by hash under `review.agent_round: ab`, arm recorded in every result row; the independent arm adds a price-blind valuer (one structured call, price-free input), independent bull/bear openings with an adverse evidence pack for the bear, a randomised judge order for the Research Manager and the Portfolio Manager, independent risk views, and a base-rate scenario rule. Both arms get the exact base-rate table and a kill-criterion check for holdings.

**Adoption rule (fixed before any data):** with ≥ 30 resolved decisions per arm at 180 days (early read) or 365 days (verdict): adopt `independent` if both its bear and bull Brier skills are ≥ the current arm's and its median missing-data count is not higher; retire it if both skills are lower by ≥ 0.05; otherwise continue. Read by Claude in a Saturday review once the count is reached; recorded here before any config change.

Ruling: the worksheet's base-rate cohorts were approximations "read off the charts" of the Base Rate Book; the book's Exhibit 4 (ten-year real sales CAGR by base-year sales, 1950–2015) is now in hand and replaces them in both arms. Effect on the compounder lane: the earned-growth cap falls from 7% to 5% for most companies above $700M of sales and rises to 10% for $325–700M; direction conservative (fewer false Buys); classified as a data correction from the source, not a bar change. Cost if wrong: a compounder Buy with 5–7% earned growth is missed until the first review of the live record.

Ruling: the independent arm goes live in the scheduled reviews from Monday 2026-11-09 with no agent run this weekend, because the end-to-end scripted-model test covers the new graph path and the quota rule for the weekend stands. Cost if wrong: one broken Monday review, visible in ACTIONS and fixable before Saturday.

Correction (09:05, from the builder check): the cap claim above was wrong. With the exact table and the old "highest tabulated rate reached by 25%" rule, the > $50B cohort would get a 0% cap and the < $325M cohort 15%. The spec now defines the cap as the 75th percentile interpolated within the 5-point bands: the caps move by at most two points from the old table except below $325M of sales (16.8%, rare under the screen's size floors). It is a data correction, neither conservative nor loosening on balance. Also from the check: per-arm Brier counts only rows that carry an arm (no pre-2026-11-09 rows); the blind valuer shows no buyback cash (cash per share retired reveals the price) and is skipped for ADRs or missing cross rates; the agents' `Position` model carries kill criteria; the risk routing is forced to one turn each in the independent arm.
