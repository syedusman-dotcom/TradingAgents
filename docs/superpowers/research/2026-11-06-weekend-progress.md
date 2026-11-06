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
