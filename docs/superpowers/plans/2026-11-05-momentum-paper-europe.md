# Momentum test, paper Buys and European candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A momentum sleeve backtest with a pass rule fixed in advance, paper Buys scored against SPY on the calibration page, and value candidates drawn from European markets.

**Architecture:** Three independent parts. `value_pipeline/paper.py` flags reviews in code from fields already in `results.json`; `calibration` scores the flags in dollars against SPY. `screener.top_candidates` gains a country filter that `review.preview` passes from config. `value_pipeline/momentum.py` reuses `trend.simulate`, `trend.metrics` and `trend.load_closes` unchanged on S&P 500 members from their join dates, over two periods, and writes its own report.

**Tech Stack:** Python 3, pandas, numpy, pytest, FastAPI + Jinja templates, argparse CLI. Run tests with `uv run pytest -q`, lint with `uv run ruff check .` (line length 100).

**Spec:** `docs/superpowers/specs/2026-11-05-momentum-paper-europe-design.md`

## Global Constraints

- Nothing trades and no agent is called by anything in this plan. No prompt, no `decision_checks`, no sizing and no live Buy bar changes.
- `value_pipeline/trend.py` is not modified.
- The pass rules' numbers are fixed by the spec: momentum `+2 points`, `+1 point`, `1.25`, `55%`, top-20 `+1 point`; paper `20` rows, `3` months, `+3 points`, `55%`, baseline `+2 points`, `90` days, horizon `180`. All bounds inclusive (use an epsilon of `1e-9`).
- Tests never touch the network: every price, rate and universe is built in the test or monkeypatched.
- User-facing text (reports, pages, printed lines) is plain English, short sentences, no jargon the README does not already use. A paper Buy is never worded as a suggestion.
- New config keys get a default in `config.DEFAULTS`, a commented entry in `value_pipeline/config.yaml`, and a README line.
- Every commit message ends with the two trailer lines the dispatch gives you.

## Review Focus

1. A `results.json` row whose `checklist` is missing, not a dict, or holds `"passed": "true"` (a string): `paper.flags` returns `[]` and nothing raises. (Task 1)
2. The calibration page reading the cache only, with no SPY close or no currency rate cached: `excess` is None, the page renders, and the Paper Buys section says how many counted rows have no dollar result. (Tasks 1, 2)
3. `review.candidate_countries` leaving fewer Magic Formula names than `review.candidates` (or none): fewer candidates come back, holdings are still reviewed, nothing raises. (Task 3)
4. A hold-out month in which fewer names than `positions` are eligible and above their 200-day average, and a member whose join date is after the period's end: the sleeve holds fewer names and never the late joiner. (Task 4)
5. A fund pair whose two symbols have unequal date ranges, and a pair with no cached prices under `--no-fetch`: common dates only; the missing pair prints as unavailable and the report is still written. (Tasks 4, 5)

---

### Task 1: Paper rules and their scores

**Files:**
- Create: `value_pipeline/paper.py`
- Modify: `value_pipeline/calibration.py`
- Test: `tests/test_paper.py` (new), `tests/test_calibration.py`

**Interfaces:**
- Produces (`paper.py`):
  - `RULES: dict[str, str]`, keys in this order with these meanings:
    - `"fair_price"`: `"a good business with good management, rated Hold or better, at or below the agents' own value"`
    - `"fair_price_investing"`: `"the same, and the company is still investing for growth"`
    - `"discount_15"`: `"a good business with good management, rated Hold or better, at least 15% below the agents' value"`
  - `PRIMARY = "fair_price"`, `NOT_NEGATIVE = ("Buy", "Overweight", "Hold")`, `DISCOUNT = 0.15`
  - `flags(row: dict) -> list[str]`: the rules a `results.json` row meets, in `RULES` order. Pure.
- Produces (`calibration.py`):
  - constants `BENCHMARK = "SPY"`, `PAPER_GAP_DAYS = 90`, `PAPER_HORIZON = 180`, `MIN_PAPER = 20`, `MIN_PAPER_MONTHS = 3`, `PAPER_EXCESS = 0.03`, `PAPER_AHEAD = 0.55`, `PAPER_OVER_BASELINE = 0.02`
  - `pair(currency: str | None) -> str | None`: None for None or `"USD"`, else `f"{MINOR_UNITS.get(currency, currency)}USD=X"` (`MINOR_UNITS` from `tradingagents.dataflows.vendors.yahoo.common`)
  - every row from `rows()` gains `"paper": list[str]` and `"currency": str | None`; every non-None `h{n}` gains `"ret_usd"`, `"spy"`, `"excess"` (each float or None)
  - `counted(rs: list[dict], rule: str | None = None, gap_days: int = PAPER_GAP_DAYS) -> list[dict]`
  - `paper_scores(rs: list[dict]) -> dict` with the shape below; `score(rs)["paper"]` is `paper_scores(rs)`

```python
# paper_scores(rs)
{"rules": {name: {"description": str, "counted": list[dict], "horizons": {30: H, 90: H, 180: H, 365: H}}
           for name in paper.RULES},
 "baseline": {"counted": list[dict], "horizons": {30: H, 90: H, 180: H, 365: H}},
 "verdict": {"supports": True | False | None, "sentence": str, "n": int, "months": int,
             "mean_excess": float | None, "ahead": float | None, "baseline_mean_excess": float | None}}
# H: over the counted rows whose h{n} has an excess
H = {"n": int, "no_dollar_result": int,   # counted rows whose h{n} exists but has no excess
     "mean_ret_usd": float | None, "mean_excess": float | None, "median_excess": float | None,
     "ahead": {"k": int, "n": int, "share": float} | None}   # excess > 0
```

- [ ] **Step 1: Write the failing tests for `paper.flags`** in `tests/test_paper.py` (`pytestmark = pytest.mark.unit`)

```python
def _row(mos=0.05, rating="Hold", business=True, management=True, investing=True, **over):
    checklist = {"good_business": {"passed": business, "note": ""},
                 "good_management": {"passed": management, "note": ""}}
    if investing is not None:
        checklist["still_investing"] = {"passed": investing, "note": ""}
    return {"ticker": "AAA", "rating": rating, "error": None, "margin_of_safety": mos,
            "checklist": checklist, **over}

def test_fair_price_at_exactly_zero():
    assert paper.flags(_row(mos=0.0)) == ["fair_price", "fair_price_investing"]
    assert paper.flags(_row(mos=-0.001)) == []

def test_discount_at_exactly_fifteen_percent():
    assert paper.flags(_row(mos=0.15)) == ["fair_price", "fair_price_investing", "discount_15"]
    assert "discount_15" not in paper.flags(_row(mos=0.1499))

def test_still_investing_failed_or_missing_keeps_fair_price():
    assert paper.flags(_row(investing=False)) == ["fair_price"]
    assert paper.flags(_row(investing=None, mos=0.2)) == ["fair_price", "discount_15"]

@pytest.mark.parametrize("row", [
    _row(business=False), _row(management=False), _row(rating="Underweight"), _row(rating="Sell"),
    _row(error="boom"), _row(mos=None), _row(mos=True), _row(mos=float("nan")),
    {"ticker": "AAA", "rating": "Hold", "margin_of_safety": 0.2},                 # no checklist
    {"ticker": "AAA", "rating": "Hold", "margin_of_safety": 0.2, "checklist": "x"},
    _row(business="true"),                                                        # a string is not True
    {"ticker": "AAA", "rating": "Hold", "margin_of_safety": 0.2,
     "checklist": {"good_business": True, "good_management": {"passed": True}}},  # not a dict
])
def test_rows_that_flag_nothing(row):
    assert paper.flags(row) == []

def test_buy_and_overweight_pass_the_gate():
    assert paper.flags(_row(rating="Buy"))[0] == "fair_price"
    assert paper.flags(_row(rating="Overweight"))[0] == "fair_price"
```

- [ ] **Step 2: Run them; expect import failure.** `uv run pytest tests/test_paper.py -q`

- [ ] **Step 3: Implement `value_pipeline/paper.py`** (module docstring says: paper rules are tracked only, nothing sizes or trades on them). `passed` must be `is True`.

- [ ] **Step 4: Run; expect pass.**

- [ ] **Step 5: Write the failing calibration tests** in `tests/test_calibration.py`, following that file's existing way of building a review folder and a fake `price_fn`. Cases, with exact values:

  - **Dollar return, pence quote:** a row `currency="GBp"`, `price=1000.0`, reviewed `2026-01-05`; `price_fn` gives the stock 1100.0 at +30 days, `GBPUSD=X` 1.25 on the review day and 1.30 at +30 days, `SPY` 500.0 then 510.0. Expect `h30["ret"] == pytest.approx(0.10)`, `h30["ret_usd"] == pytest.approx(1.10 * 1.30 / 1.25 - 1)`, `h30["spy"] == pytest.approx(0.02)`, `h30["excess"] == pytest.approx(1.10 * 1.30 / 1.25 - 1 - 0.02)`.
  - **Dollar and currency-less rows:** `currency="USD"` and `currency=None` both give `ret_usd == ret` and ask `price_fn` for no pair.
  - **Missing rate:** `currency="CHF"` with no `CHFUSD=X` close: `ret_usd is None`, `excess is None`, `ret` still set.
  - **Missing SPY:** `spy is None`, `excess is None`.
  - **`pair`:** `pair(None) is None`, `pair("USD") is None`, `pair("GBp") == "GBPUSD=X"`, `pair("SEK") == "SEKUSD=X"`.
  - **Rows carry the flags:** a row built from `tests/test_paper.py`'s passing shape has `row["paper"] == ["fair_price", "fair_price_investing"]`.
  - **`counted`:** the same ticker flagged on `2026-01-01`, `2026-03-02` (60 days later) and `2026-04-06` (95 days after the first): `counted(rs, "fair_price")` keeps the first and the third; a different ticker on `2026-03-02` is kept; `counted(rs)` (baseline) applies the same gap to every row whatever its flags; the result is oldest first whatever the input order.
  - **`paper_scores` horizons:** three counted rows with `h180` excess `+0.10`, `-0.02`, None (h180 present, `ret_usd` None): `H["n"] == 2`, `H["no_dollar_result"] == 1`, `mean_excess == pytest.approx(0.04)`, `median_excess == pytest.approx(0.04)`, `ahead == {"k": 1, "n": 2, "share": 0.5}`.
  - **Verdict, not enough:** 19 counted `fair_price` rows with a 180-day excess gives `supports is None` and the sentence `"fair_price: not enough paper Buys yet (19 of 20 past 180 days, from 4 of 3 months)."`; 20 rows all dated in two calendar months gives `supports is None` with `"from 2 of 3 months"`.
  - **Verdict, supports:** 20 rows over 3 months, each excess `+0.03`, baseline mean excess `+0.01` gives `supports is True`; the sentence starts `"fair_price supports a live test of the rule:"`.
  - **Verdict, each condition failing alone** (20 rows, 3 months): mean excess `0.0299`; 10 of 20 ahead with a mean of `+0.05`; baseline mean `0.011` against a rule mean of `0.03`. Each gives `supports is False`, and the sentence starts `"fair_price does not support a live test of the rule:"` and names the failed condition after `"Failed:"`.
  - **`score(rs)["paper"]`** equals `paper_scores(rs)`.
  - **`_prefetch`** asks `fill_close_cache` (monkeypatched) for `SPY` and for `GBPUSD=X` with the review day and the reached horizon days.

- [ ] **Step 6: Run; expect failures.** `uv run pytest tests/test_calibration.py -q`

- [ ] **Step 7: Implement in `calibration.py`.** The stock's, SPY's and the pair's closes are read with the same `price_fn` on the same two days (the review day and the horizon day; SPY and the pair use the close on or before the review day even when the row recorded its own `price`). `ret_usd = (1 + ret) * rate_later / rate_then - 1`. The months in the verdict are the distinct `row["date"][:7]` of the rows counted in `n`. The baseline's 180-day mean excess comes from `paper_scores`' own baseline. Existing keys and behaviour of `rows` and `score` stay as they are.

- [ ] **Step 8: Run the whole suite and lint.** `uv run pytest -q && uv run ruff check .` Expected: all pass.

- [ ] **Step 9: Commit.** `git add value_pipeline/paper.py value_pipeline/calibration.py tests/test_paper.py tests/test_calibration.py && git commit -m "feat: paper Buy rules and their scores against SPY"`

---

### Task 2: Paper Buys on the calibration page, the command and ACTIONS

**Files:**
- Modify: `value_pipeline/calibration.py` (`markdown`), `value_pipeline/web/templates/calibration.html`, `value_pipeline/render.py` (`actions_markdown`), `value_pipeline/review.py` (`_run`), `README.md`
- Test: `tests/test_calibration.py`, `tests/test_value_web.py`, `tests/test_value_pipeline.py`

**Interfaces:**
- Consumes: `paper.flags(row)`, `paper.RULES`, `paper.PRIMARY`, `calibration.paper_scores(rs)` and `score(rs)["paper"]` (Task 1 shapes).
- Produces: `render.actions_markdown(..., paper: list[dict] | None = None)`, each dict `{"ticker": str, "rules": list[str], "price": float | None, "value": float | None, "currency": str | None}`.

- [ ] **Step 1: Write the failing tests.**

  - `calibration.markdown(rs, scores)` contains a `## Paper Buys` section after `## Scores` and before `## Verdicts` with: the line `"Tracked only: no money follows these. Each rule is applied in code to what a review recorded."`; one bullet per rule `- **fair_price**: <description>`; the verdict sentence; a table headed `| Rule | Counted | 30 days | 90 days | 180 days | 365 days |` whose cells read `"+4.0% over SPY (1/2 ahead, n=2)"` for a horizon with results and `"–"` without, with a last row `every review`; a table of the counted paper Buys headed `| Date | Ticker | Rules | Price then | Latest horizon | Return in dollars | Against SPY |`; and, when any counted row at any horizon has no dollar result, the line `"N counted results have no dollar return (a currency rate or SPY close is missing); run `calibration` to fetch them."` with the count.
  - With no flagged row the section still prints the rules and the verdict and says `"No paper Buys yet."` instead of the two tables.
  - The Decisions table of `markdown` is unchanged.
  - `/calibration` (use `tests/test_value_web.py`'s client and review-folder helpers): the page shows a "Paper Buys" heading, the verdict sentence, each rule's name, and the Decisions table has a `Paper` column holding the row's rules joined by `", "` (or `–`). The page still renders when no SPY close is cached.
  - `actions_markdown(..., paper=[{"ticker": "AAA", "rules": ["fair_price", "discount_15"], "price": 96.2, "value": 120.0, "currency": "USD"}], names={"AAA": "Alpha"})` contains, between `## Candidates not bought` (when present) and `## Watch: falling knives` (when present):

    ```
    ## Paper Buys

    Tracked on the calibration page only; not a suggestion to buy. Each name is one the agents call a good business with good management, priced at or below their own value: a looser rule than the live one, recorded to see whether it would have paid.

    - Alpha (AAA): fair_price, discount_15 (price 96.20, agents' value 120.00)
    ```

    A `GBp` entry prints its two amounts the way the New buys table prints a quote-currency value (`_quoted`'s format, for example `4,376.10 GBp`). `paper=None` and `paper=[]` print no section.
  - A real (non-dry) `review._run` with one fake agent result whose decision passes the gate (use the existing test that runs `_run` with a stubbed graph) writes the section into `ACTIONS.md`; a dry run writes none even though `fake_run`'s rows pass the gate.

- [ ] **Step 2: Run; expect failures.**

- [ ] **Step 3: Implement.** In `review._run`, build `paper` from the `rows` list written to `results.json` (`paper.flags(row)` non-empty), `None` on a dry run, and pass it to `actions_markdown`. In `calibration.html` add the section above "Decisions" (rules, verdict, the rule-by-horizon table, the counted list) and the `Paper` column; the page reads only `scores.paper` and `rows`. README: under the calibration section, what a paper Buy is, the three rules, the verdict rule with its numbers, and that nothing trades on it.

- [ ] **Step 4: Run the whole suite and lint.** `uv run pytest -q && uv run ruff check .`

- [ ] **Step 5: Commit.** `git commit -m "feat: paper Buys on the calibration page and in ACTIONS"`

---

### Task 3: European value candidates

**Files:**
- Modify: `value_pipeline/screener.py`, `value_pipeline/review.py` (`preview`, `_run`), `value_pipeline/config.py`, `value_pipeline/config.yaml`, `value_pipeline/web/templates/run_preview.html`, `README.md`
- Test: `tests/test_candidate_countries.py` (new; follow `tests/test_run_rate_filter.py`'s helpers)

**Interfaces:**
- Produces:
  - `screener.top_candidates(paths, owned, n, compounders=0, dislocations=0, inflections=0, min_run_rate=0.0, countries: list[str] | None = None) -> list[dict]`
  - `screener.run_rate_skips(paths, owned, n, min_run_rate, countries: list[str] | None = None) -> list[dict]`
  - `screener.missing_countries(paths, countries: list[str] | None) -> list[str]`: the listed names with no row in the latest screen, in the list's order; `[]` without a screen, without a `country` column, or for an empty list
  - `review.preview(...)` result gains `"candidate_countries": list[str]` (the configured list; `[]` when `only` is given or the list is empty) and `"missing_countries": list[str]`
  - `config.DEFAULTS["review"]["candidate_countries"] = []`

- [ ] **Step 1: Write the failing tests.** Screen rows: `US1` (US, rank 1), `SE1` (Sweden, rank 2), `US2` (US, rank 3), `DE1` (Germany, rank 4), `UK1` (UK, rank 5), `FR1` (France, rank 6); hinted rows `USC` (US, compounder), `USD` (US, dislocation), `SED` (Sweden, dislocation), `USB` (US, hints `compounder;dislocation`).

  - `top_candidates(paths, set(), 3, countries=["Sweden", "Germany", "UK"])` returns `SE1, DE1, UK1` in that order (existing rank order, nothing re-ranked).
  - `countries=["sweden", " Germany "]` matches the same rows as `["Sweden", "Germany"]`.
  - `countries=None` and `countries=[]` return `US1, SE1, US2`.
  - With `compounders=2, dislocations=2, countries=["Sweden"]`: the Magic Formula pick is `SE1`; the compounder places go to `USC` and `USB` (US names allowed); the dislocation place goes to `SED` only (`USD` and `USB` cannot take one).
  - `countries=["Norway"]` with `n=3` returns no Magic Formula names and does not raise; with `compounders=1` it returns the compounder.
  - A screen CSV without a `country` column is not filtered.
  - `run_rate_skips(..., countries=["Sweden", "Germany"])` lists only listed-country names that were dropped for a low run rate and rank above the last pick; a US name with a low ratio is not listed.
  - `missing_countries(paths, ["Sweden", "Norge", "UK"]) == ["Norge"]`; `[]` for `None`, `[]`, no screen, no column.
  - `review.preview` with `cfg["review"]["candidate_countries"] = ["Sweden", "Norge"]` has `candidate_countries == ["Sweden", "Norge"]`, `missing_countries == ["Norge"]`, and candidates from Sweden only (plus hinted lanes); with `only=["AAA"]` both keys are `[]`.
  - A dry-run review prints `"Value candidates from: Sweden, Norge (review.candidate_countries); compounder candidates from every market."` and `"No screen rows from: Norge (check the spelling in review.candidate_countries)."`; with an empty list it prints neither.
  - `validate_config`: `candidate_countries: "Sweden"` raises `ValueError` naming `review.candidate_countries`; `["Sweden", 3]` and `["Sweden", " "]` raise `ValueError` with `"review.candidate_countries entries must be country names"`; `[]` and `["Sweden"]` pass.
  - `load_config()` (the shipped `config.yaml`) gives exactly `["Sweden", "Norway", "Denmark", "Finland", "Germany", "France", "Netherlands", "Switzerland", "Italy", "Spain", "Ireland", "Portugal", "Austria", "Belgium", "UK"]` and passes `validate_config`.
  - `/run/preview` shows the "Value candidates from:" line when the list is set.

- [ ] **Step 2: Run; expect failures.**

- [ ] **Step 3: Implement.** The filter drops rows from the Magic Formula frame and from the dislocation-hinted frame before the existing ordering; compounder- and inflection-hinted frames are untouched. `review.preview` reads `rcfg.get("candidate_countries") or []` and passes it to both screener calls. `config.yaml` entry, under `review:`:

  ```yaml
    # Countries the Magic Formula and dislocation candidates may come from (the screen's country
    # column). Since 2026-11-05: Europe only. The buy-bar backtest found no US value rule that
    # beats the index, and value has paid in Europe since 2021. Compounder candidates, holdings,
    # lanes.tickers and --tickers ignore this list. [] means every country.
    candidate_countries: [Sweden, Norway, Denmark, Finland, Germany, France, Netherlands, Switzerland, Italy, Spain, Ireland, Portugal, Austria, Belgium, UK]
  ```

  README: one paragraph where review candidates are described.

- [ ] **Step 4: Run the whole suite and lint.** Existing tests that build a config from `DEFAULTS` keep an empty list and must still pass unchanged.

- [ ] **Step 5: Commit.** `git commit -m "feat: value candidates from the countries in review.candidate_countries"`

---

### Task 4: Momentum sleeve engine, verdict and fund record

**Files:**
- Create: `value_pipeline/momentum.py`
- Modify: `value_pipeline/config.py` (`DEFAULTS["momentum"]`, `_POSITIVE`)
- Test: `tests/test_momentum.py` (new; synthetic closes in the style of `tests/test_trend.py`)

**Interfaces:**
- Consumes (unchanged): `trend.simulate(system, closes, spy, settings, eligible)`, `trend.metrics(result, spy)`, `trend.load_closes(paths, tickers, start, fetch, progress) -> (closes, spy)` (`closes.attrs` has `missing`, `short`, `filled`), `trend.Settings`, `trend._eligibility(added)`, `trend._adj_closes(hist)`, `trend.cagr`, `trend.max_drawdown`, `backtest._join_dates(paths, fetch, None) -> dict[str, str]`, `backtest.price_history(ticker, paths, fetch=..., stale_before=...)`, `backtest.PRICE_SLACK_DAYS`.
- Produces:

```python
SYSTEM = "momentum_12_1"
BEAT_SPY, BEAT_UNIVERSE, DRAWDOWN_RATIO, YEARS_AHEAD, CHECK_BEAT_SPY, EPS = 0.02, 0.01, 1.25, 0.55, 0.01, 1e-9
SENSITIVITY = [("positions", [10, 20, 30]), ("cost_per_side", [0.0, 0.0035, 0.005, 0.0075]),
               ("market_filter", [False])]

def universe_equity(closes: pd.DataFrame, added: dict[str, str], start: str) -> pd.Series
def sleeve(closes: pd.DataFrame, spy: pd.Series, added: dict[str, str], m: dict, start: str,
           end: str | None = None, **overrides) -> dict     # {"metrics": dict, "trades": list[dict]}
def verdict(recent: dict, holdout: dict, recent_check: dict, holdout_check: dict) -> dict
def fund_record(paths: Paths, pairs: list[dict], fetch: bool = True, today: date | None = None) -> list[dict]
def run(paths: Paths, cfg: dict, today: date | None = None, fetch: bool = True, progress=None) -> dict
```

`DEFAULTS["momentum"]`:

```python
"momentum": {"recent_start": "2015-07-01", "holdout_start": "2005-01-03", "holdout_end": "2015-06-30",
             "equity": 10000, "positions": 10, "check_positions": 20, "cost_per_side": 0.005,
             "market_filter": True,
             "funds": [
                 {"fund": "MTUM", "parent": "SPY",
                  "label": "iShares MSCI USA Momentum against the S&P 500 (US-listed; not sold to EU retail investors)"},
                 {"fund": "IS3R.DE", "parent": "EUNL.DE",
                  "label": "iShares Edge MSCI World Momentum against iShares Core MSCI World (Xetra, at Nordnet)"},
                 {"fund": "QDVA.DE", "parent": "SXR8.DE",
                  "label": "iShares Edge MSCI USA Momentum against iShares Core S&P 500 (Xetra, at Nordnet)"}]},
```

Add `"positions"`, `"check_positions"` and `"equity"` to `_POSITIVE`.

- `universe_equity`: a series from the first row on or after `start`, starting at 1.0. Each later day's return is the mean of `close[t] / close[t-1] - 1` over the names that have a join date on or before day `t-1` and a close on both days; a day with no such name returns 0.
- `sleeve`: `m` is the merged `momentum` settings; `overrides` may set `positions`, `cost_per_side`, `market_filter`. With `end`, `closes` and `spy` are cut to rows on or before `end` first. It builds `trend.Settings(start=start, equity=m["equity"], max_positions=positions, max_position_weight=1.0, cost_per_side=..., market_filter=..., risk_per_trade/stop_n/n_days from DEFAULTS["trend"])`, runs `trend.simulate(SYSTEM, ...)` with `trend._eligibility(added)` restricted to names in `added` (a name with no join date is never eligible), and returns `metrics = trend.metrics(...)` plus `"start"`, `"end"` (first and last equity dates, ISO), `"positions"`, `"cost_per_side"`, `"market_filter"`, `"universe_cagr"`, `"universe_max_drawdown"` (from `universe_equity` over the same rows), `"years_ahead"` and `"years_counted"` (calendar years in `metrics["years"]` whose SPY return is not None; ahead is `system > spy`).
- `verdict` takes the four `metrics` dicts (top 10 recent, top 10 hold-out, top 20 recent, top 20 hold-out) and returns `{"supports": bool, "checks": {"1": bool, ..., "6": bool}, "sentence": str, "years_ahead": int, "years_counted": int}`:
  1. `recent.cagr >= recent.spy_cagr + 0.02`
  2. `recent.cagr >= recent.universe_cagr + 0.01`
  3. `holdout.cagr >= holdout.spy_cagr` and `holdout.cagr >= holdout.universe_cagr`
  4. `max_drawdown <= 1.25 * spy_max_drawdown` in both periods
  5. `(recent.years_ahead + holdout.years_ahead) / (recent.years_counted + holdout.years_counted) >= 0.55`
  6. `recent_check.cagr >= recent_check.spy_cagr + 0.01` and `holdout_check.cagr >= holdout_check.spy_cagr`

  The sentence starts `"The test supports a small live sleeve: "` or `"The test does not support a live sleeve: "`, then gives, for the top 10 at the cost used: both periods' CAGR against SPY's and the universe's, both drawdowns against SPY's, the years ahead as `"k of n calendar years"`, and the top 20's two CAGRs; a failing verdict ends `" Failed: (1) ...; (4) ..."` naming each failed condition in words.
- `fund_record`: for each pair, both price histories (`stale_before` = `today` minus `backtest.PRICE_SLACK_DAYS` days, so a stale cache is refreshed when `fetch`), adjusted closes on their common dates. Each result: `{"fund", "parent", "label", "start", "end", "years", "fund_cagr", "parent_cagr", "excess", "fund_max_drawdown", "parent_max_drawdown", "error"}`; with a missing history or fewer than two common dates the numbers are None and `error` is `"no price history for <symbol>"` or `"no common dates"`.
- `run`: merges `DEFAULTS["momentum"]` with `cfg.get("momentum")`; `added = backtest._join_dates(paths, fetch, None)` and raises `ValueError("no S&P 500 join dates in the cache; run without --no-fetch")` when empty; tickers are `sorted(added)`; one `trend.load_closes(paths, tickers, m["holdout_start"], fetch=fetch, progress=progress)`. Returns:

```python
{"generated": iso, "settings": m, "members": len(tickers), "used": len(closes.columns),
 "periods": {"recent": {"start", "end", "eligible_at_start": int},
             "holdout": {"start", "end", "eligible_at_start": int}},
 "results": {("recent", 10): metrics, ("holdout", 10): metrics, ("recent", 20): metrics, ("holdout", 20): metrics},
 "trades": {("recent", 10): [...], ("holdout", 10): [...]},
 "sensitivity": [{"period", "setting", "value", **metrics}, ...],   # both periods, one setting at a time from the primary
 "verdict": verdict(...), "funds": fund_record(...),
 "missing": [...], "short": [...], "filled": {"cells": int, "tickers": int}}
```

  The keys `10` and `20` are `m["positions"]` and `m["check_positions"]`. `eligible_at_start` counts names in `closes` with a join date on or before the period's start.

- [ ] **Step 1: Write the failing tests** (`tests/test_momentum.py`, `pytestmark = pytest.mark.unit`; build closes with `pd.bdate_range`, 300 warm rows before the start, SPY rising steadily so the market filter is on):

  - `universe_equity`: two names with daily returns +1% and +3%, both joined before the start: the series grows 2% a day; a third name with a join date in the middle joins the mean from the day after its join date; a name with no join date is never in the mean; a NaN close on either day leaves that name out for that day; no eligible name gives a 0% day.
  - `sleeve`: with 12 steadily rising names of different slopes and `positions=10`, the trades never include the two flattest; a name whose join date is after the last row never trades; a name absent from `added` never trades; with only 4 eligible names the sleeve holds at most 4 and does not raise; `end="..."` gives `metrics["end"]` on or before it and the same result as passing frames cut by hand; `metrics` carries `universe_cagr`, `universe_max_drawdown`, `years_ahead`, `years_counted`, `positions`, `cost_per_side`; a higher `cost_per_side` override gives a lower CAGR.
  - `verdict`: a helper `_m(cagr, spy, universe, dd, spy_dd, ahead, counted)`. All six passing at the bounds exactly (`recent` 0.12 vs SPY 0.10 and universe 0.11; `holdout` 0.08 vs 0.08 and 0.08; drawdowns 0.25 vs 0.20; years 11 of 20; top 20 recent 0.11 vs 0.10, hold-out 0.08 vs 0.08) gives `supports is True` and all checks true. Then six cases each moving one number just past its bound (recent CAGR 0.1199; universe 0.1101; hold-out SPY 0.0801; hold-out drawdown 0.2501 against 0.20; years 10 of 20; top-20 recent 0.1099): exactly that check is False, `supports is False`, and the sentence contains `"Failed: (N)"` for that N only.
  - `fund_record` (monkeypatch `backtest.price_history`): a fund doubling over exactly two years against a parent up 21% gives `fund_cagr == pytest.approx(2 ** 0.5 - 1, abs=1e-3)`, `parent_cagr == pytest.approx(0.10, abs=1e-3)`, `excess` their difference; a parent whose history starts a year later gives `start` equal to the later first date; a missing fund gives `error == "no price history for XXX"` and None numbers; `fetch=False` is passed through to `price_history`.
  - `run` (monkeypatch `backtest._join_dates`, `trend.load_closes`, `momentum.fund_record`): the result has the four `results` keys, both periods' `eligible_at_start`, sensitivity rows for both periods and every `SENSITIVITY` value, and the hold-out metrics' `end` is on or before `holdout_end`; empty join dates raise the `ValueError` above.
  - `validate_config(copy of DEFAULTS)` passes; `momentum.positions: 0` raises.

- [ ] **Step 2: Run; expect import failure.**

- [ ] **Step 3: Implement `value_pipeline/momentum.py`** and the config defaults. Module docstring: what the test is, that the rule was fixed before the first run, that nothing trades.

- [ ] **Step 4: Run the whole suite and lint.**

- [ ] **Step 5: Commit.** `git commit -m "feat: momentum sleeve backtest engine, its fixed verdict and the fund record"`

---

### Task 5: Momentum report, command and page link

**Files:**
- Modify: `value_pipeline/momentum.py` (`render_report`, `write`, `latest_report`), `value_pipeline/cli.py`, `value_pipeline/web/app.py`, `value_pipeline/web/templates/backtest.html`, `value_pipeline/config.yaml`, `README.md`
- Test: `tests/test_momentum.py`, `tests/test_value_web.py`

**Interfaces:**
- Consumes: `momentum.run`'s result (Task 4 shape), `render.actions_html(text, title)`, `replace_file` (as `trend.write` uses them).
- Produces: `momentum.render_report(result) -> str`; `momentum.write(paths, result) -> Path` (writes `reports/backtest/momentum_<generated>.md`, `.html`, `_trades.csv`, returns the Markdown path); `momentum.latest_report(paths) -> Path | None` (newest `momentum_*.html`); command `momentum-backtest [--no-fetch]`.

- [ ] **Step 1: Write the failing tests.**

  - `render_report(result)` on a hand-built result contains, in this order: `# Momentum sleeve backtest, <generated>`; a first paragraph naming the universe (`"today's S&P 500 members, each from the day it joined the index"`), `members` and `used`, both periods with their dates and `eligible_at_start`, and the settings; `## Verdict` with the rule's six conditions listed in words and the sentence; `## Results` with one row per `(period, positions)` and the columns `| Period | Positions | CAGR | SPY | Equal-weight universe | Max drawdown | SPY drawdown | Years ahead of SPY | Trades a year | Costs a year |`; `## Calendar years` for the top 10 in both periods (`| Year | Sleeve | SPY |`, the hold-out years first); `## Sensitivity` (`| Period | Setting | Value | CAGR | SPY | Max drawdown |`); `## Real momentum funds` with a row per pair (`| Fund | Against | From | To | Fund a year | Parent a year | Difference | Fund drawdown | Parent drawdown |`) and, for a pair with an `error`, the label and `"unavailable: <error>"`; `## Limits` with the spec's five limits, plus the missing and short ticker counts when there are any.
  - The verdict sentence in the report is `result["verdict"]["sentence"]` verbatim.
  - `write` creates the three files; the CSV has a `period` and a `positions` column and one row per trade; `latest_report` returns the newest `.html` and None in an empty folder.
  - The command (call `cli.main(["--data-dir", str(tmp), "momentum-backtest", "--no-fetch"])` or the file's existing way of driving the CLI, with `momentum.run` monkeypatched to a fixed result) prints the report and `Saved`, and passes `fetch=False`.
  - `/backtest` shows `Momentum sleeve backtest:` with a link to the newest `momentum_*.html` when one exists, and no such line when none does; the trend link and the lane report are unchanged.

- [ ] **Step 2: Run; expect failures.**

- [ ] **Step 3: Implement.** Mirror `trend.render_report`, `trend.write`, `trend.latest_report`, `cmd_trend_backtest` and the trend link in `backtest.html`. The command's opening line says what it replays, that the caches make it a few minutes, and that nothing trades. `config.yaml` gets a `momentum:` section with every `DEFAULTS["momentum"]` key, each with a one-line comment (the cost comment: Nordnet Mini outside the Nordics, 0.25% commission plus 0.25% currency fee). README: a "Momentum sleeve backtest" section with the command, the two periods, the six conditions with their numbers, the fund record, the limits, and that a pass turns nothing on.

- [ ] **Step 4: Run the whole suite and lint.**

- [ ] **Step 5: Commit.** `git commit -m "feat: momentum-backtest command, report and page link"`
