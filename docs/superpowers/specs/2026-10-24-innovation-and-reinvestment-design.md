# Innovation and reinvestment — design

**Date:** 2026-10-24
**Status:** the user asked "Do we have any considerations for innovation in the business"; Claude answered that the method measures the results of innovation but not innovation itself and proposed four additions; the user said "go ahead". Calls Claude made are marked *(my call)*.

## Goal

Make the analysis ask whether the company is still building its next decade or harvesting the last one, with numbers from the statements and words from the filings, and let that answer bear on the rating:

1. **Numbers** — an `## Innovation and reinvestment` section in the worksheet: R&D intensity and trend, what a dollar of R&D produced, growth capex, growth spend against operating cash flow, return on incremental capital, and a harvesting signal.
2. **Words** — the 10-K's Business section (Item 1) in `get_annual_report`, with the product, launch and pipeline paragraphs kept whole, so the fundamentals analyst can quote the company's own account of what it is building.
3. **Verdict** — a fourth checklist line, `Still investing`, on the Portfolio Manager's decision; required to pass in the compounder lane, advisory elsewhere.
4. **Threat** — the pre-mortem names the disruption exposure: who or what could make the product unnecessary, and the signal that it is happening.

### Non-goals

Patent counts and external product databases (no free, reliable source). A screen-level innovation filter or hint. Changing the value lane's bars. Trading.

## Verified facts (code on 2026-10-24)

- `YEAR_FIELDS` in `tradingagents/dataflows/value_metrics.py` has revenue, gross_profit, ebit, capex, depreciation, sga, acquisitions, goodwill and the rest, but no research and development. The Yahoo vendor maps rows in `ROWS` with `assert set(ROWS) == set(YEAR_FIELDS)`. yfinance serves the income-statement row `Research And Development` for companies that report it (ADBE: 4,294 / 3,944 / 3,473 / 2,987 $M) and nothing for companies that do not break it out (DECK, HRB).
- Capex is stored as Yahoo reports it (negative); existing code takes `abs()` (`value_metrics.py:295`, `:760`).
- `record()` already computes `reinvestment_rate` (retained share of earnings for sustainable growth); the new section uses a different, named quantity (growth spend) to avoid the collision.
- `inflection()` computes `incremental_roic` against the five-year base using `_invested_capital(period)`; the same helper serves a three-year return on incremental capital.
- `sec_filings.SECTIONS` holds (title, start heading, end heading) regexes for Item 1A and Item 7; `section_spans` takes the longest span between headings (table of contents entries lose). `_sections` caches each filing's spans forever at `10k_{cik}_{accession}.json`. `latest_earnings_release` keeps paragraphs matching `OUTLOOK_WORDS` whole under a heading, capped at `OUTLOOK_CHARS` (4,000).
- The checklist is three `Verdict` fields (`good_business`, `good_management`, `cheap`) on `PortfolioDecision`; rendered in `render_pm_decision`, required by `decision_checks` for non-Sell ratings, parsed by `value_pipeline.parse.checklist` (regex over the three labels), shown on `stock.html`, carried to the committee as the `checklist` dict. The compounder and inflection lanes require `good_business` and `good_management` to pass (`_lane_buy_problems`).
- The pre-mortem (`managers/premortem.py`) is one plain quick-tier call: three numbered questions, about 250 words.

## Design

### 1. Worksheet: `## Innovation and reinvestment`

`value_metrics.innovation(years: list[dict]) -> dict` (years newest first; None-tolerant; every value None when not computable). Keys:

| Key | Definition |
|---|---|
| `rnd_to_revenue` | latest fiscal year R&D / revenue |
| `rnd_to_revenue_avg` | mean of R&D / revenue over up to 5 years |
| `rnd_cagr`, `rnd_cagr_years` | R&D CAGR over the longest span with ≥ 3 intervals of reported R&D (same rule as `revenue_cagr`) |
| `revenue_cagr_same_span` | revenue CAGR over exactly that span |
| `gross_profit_per_rnd` | (gross profit latest − gross profit at the span start) / sum of R&D over the span's years excluding the latest; None when the sum is 0 |
| `growth_capex_to_revenue` | mean over up to 3 years of (abs(capex) − depreciation) / revenue; may be negative |
| `growth_spend_to_cfo` | sum over up to 3 years of (R&D + max(abs(capex) − depreciation, 0) + abs(acquisitions)) / sum of operating cash flow; R&D and acquisitions count as 0 when None; None when CFO sum ≤ 0 |
| `roic_incremental_3y` | (EBIT latest − EBIT three years earlier) × (1 − `TAX_RATE`) / (invested capital latest − invested capital three years earlier); needs ≥ 4 years; None when the capital change ≤ 0 |
| `harvesting` | True when, in the latest year, R&D / revenue fell ≥ 1 point from the year before **or** abs(capex) < depreciation, **and** EBIT margin rose from the year before; False when neither cut happened or the margin did not rise; None when neither R&D nor capex and depreciation are available for both years |
| `rnd_reported` | True when at least one year has R&D |

`render_report` adds the section after `## Record and inflection`, one number per fixed label (a check may read them later; `n/a` when None):

```
## Innovation and reinvestment

- R&D / revenue 17.2%
- R&D / revenue 5-year average 16.8%
- R&D growth 12.9%/yr over 4 years
- Revenue growth over the same span 10.4%/yr
- Gross profit added per R&D dollar 1.4
- Growth capex / revenue (3-year average) 0.3%
- Growth spend / operating cash flow (3-year) 52%
- Return on incremental capital (3-year) 24%
- Harvesting signal no
```

Followed by one prose line: when `rnd_reported` is False, "R&D is not reported separately; judge reinvestment from growth capex, acquisitions and the filings' product evidence." Otherwise a sentence naming whether R&D grew faster or slower than revenue and the return on incremental capital against `DISCOUNT_RATE`-class hurdle (the worksheet's discount rate, as a plain comparison: "above" / "below" the cost of capital) *(my call)*.

Yahoo vendor: `ROWS["rnd"] = ("Research And Development",)`; `YEAR_FIELDS` gains `"rnd"`. Old cached `years` rows without the key read as None (no cache schema bump) *(my call)*.

### 2. Filings: the Business section in `get_annual_report`

`SECTIONS` gains, first, `("Business (Item 1)", _ITEM + r"1" + _SEP + r"business\.?\s*$", _ITEM + r"1a\b")`. The heading regex must not match "Item 1A. Risk Factors" (the `\s*$` after "business" and the literal `1` followed by the separator handle it; test it).

`get_annual_report` prints the Business section first, cut to `BUSINESS_CHARS = 6_000` with the "more characters" note, then `## Products and innovation lines`: the Business section's paragraphs containing any of `INNOVATION_WORDS = ("new product", "launch", "innovation", "innovative", "research and development", "pipeline", "patent", "roadmap", "introduced", "next generation", "next-generation")` kept whole, capped at `OUTLOOK_CHARS` (4,000), "None found." when none; then Items 1A and 7 as today. The section cache file name gains a version (`10k_{cik}_{accession}_v2.json`) so filings cached without Item 1 are read again *(my call)*.

### 3. The fourth checklist line and the prompts

- `PortfolioDecision.still_investing: Verdict | None` — "Still investing: the company is building its next decade — R&D and growth capex held or rising, return on incremental capital above the cost of capital, and product or pipeline evidence quoted from the filings — rather than harvesting margins by cutting them." Rendered as the fourth checklist line `Still investing — PASS: note` after Good management; parsed by `parse.checklist` (`still_investing`); listed on `stock.html`; flows to the committee in the `checklist` dict; the dry-run fake decision sets it.
- `decision_checks`: for a non-Sell rating a missing verdict is a problem ("the checklist needs all four tests"); the compounder lane requires it to pass (`_lane_buy_problems(..., ("good_business", "good_management", "still_investing"))`); the inflection lane does not (an inflection is by definition investing; the demand evidence covers it) *(my call)*; elsewhere a FAIL does not bar the rating.
- Portfolio Manager prompt: the rubric names the fourth test; "Still investing must PASS for the compounder lane. Elsewhere a FAIL does not bar the rating, but the Investment Thesis says what is being harvested and for how long the margin can be held, and one kill criterion names the reinvestment level (R&D or capex as a share of revenue) below which the thesis is wrong." Use the worksheet's Innovation and reinvestment numbers and the fundamentals report's product evidence; a PASS without a quoted filing or a number is not a PASS.
- Fundamentals analyst prompt: "Innovation and reinvestment: state the new-product, launch and pipeline evidence from the annual report's Business section (`get_annual_report`) and the quarterly report, with filing dates; read the worksheet's Innovation and reinvestment section and say whether the numbers support the words (R&D and growth capex held or rising, return on incremental capital against the cost of capital), and conclude 'investing' or 'harvesting'."
- Pre-mortem: a fourth question, "4. The disruption exposure: who or what could make the product unnecessary, and the signal visible first that it is happening." About 300 words.

### 4. Website

`stock.html` lists the four checklist lines. No other page changes.

## Testing

- `innovation()` on synthetic years: full data, no R&D (every R&D key None, `rnd_reported` False, growth capex still computed), fewer than four years (incremental return None), capital change ≤ 0, the harvesting rule (cut + margin up → True; cut + margin down → False; no data → None); rendering labels and `n/a`.
- SEC: a synthetic 10-K whose table of contents lists "Item 1. Business" and "Item 1A. Risk Factors" before the body: the Business span is the body, not the TOC line, and does not swallow Item 1A; innovation paragraphs kept beyond the cut; "None found."; the cache file name carries the version.
- Decision: round-trip of `still_investing`; checks for a missing verdict (non-Sell) and a compounder FAIL; an inflection FAIL raises no lane problem; `parse.checklist` reads the fourth label; `stock.html` shows it; dry-run review writes it to results.json.
- Prompt tests: the fundamentals prompt and the pre-mortem prompt carry the new sentences.
- Real run after the merge: ADBE (reports R&D) and DECK (does not) — read the section, the Business lines in the fundamentals report, the fourth checklist line and the pre-mortem's fourth answer.

## Rollout

Worktree, subagent-driven implementation, final review, merge, website restart, the two real runs.
