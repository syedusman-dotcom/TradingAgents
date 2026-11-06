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
