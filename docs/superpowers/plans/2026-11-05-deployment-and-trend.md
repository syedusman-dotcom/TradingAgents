# Deployment and Trend Round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record and value trades in any quote currency, park idle cash in an index fund, stop spending reviews on peak-cycle names, and backtest a Turtle-style trend sleeve with a verdict fixed in advance.

**Architecture:** Dollars stay the portfolio's base; a position stores its quote-unit price and the dollar rate paid. The index fund is an ordinary holding recognised by one helper and counted as money by `actions.plan`. The run-rate filter lives in `screener.top_candidates`. The trend backtest is a new module, `value_pipeline/trend.py`, that reuses the lane backtest's price cache and cohort rows.

**Tech Stack:** Python 3.14, pandas, pytest, ruff (line length 100), FastAPI + Jinja for the website. Run everything through `uv run` (`uv sync --extra dev` once in a fresh worktree); `python` is not on PATH.

**Spec:** `docs/superpowers/specs/2026-11-05-deployment-and-trend-design.md`

## Global Constraints

- The tool never trades. Every new line of output is a suggestion or a record of a fill the user made.
- Dollars are the portfolio's base. A price is entered, stored and shown in the stock's quote unit (pence for London); a dollar amount is `shares × price × fx`, with `fx` = dollars per quoted unit.
- No rating bar and no stock-sizing rule changes. `parking.ticker: ""` and `review.min_run_rate: 0` must reproduce today's behaviour exactly.
- Old files load unchanged: a `portfolio.json` without `currency` / `avg_fx`, a `trades.csv` with the seven-column header.
- No network in tests: every price, rate and universe is injected or monkeypatched. Mark tests `@pytest.mark.unit`.
- Trend backtest: nothing decided at day t may read a price after t; fills are at the next close.
- The verdict rule in the spec is fixed; code implements it as written, with no tuning after seeing results.
- `uv run ruff check .` and `uv run pytest -q` pass at every commit. Commit messages end with:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_015r4hMD2DRWvm5AXmWo8KRV`.

## Review Focus

1. A sale recorded with a different rate than the purchase: the realised gain must be dollars received minus dollars paid, not a quote-unit difference.
2. An existing `trades.csv` with the old header when the first foreign trade is appended: old rows must keep their values and read as fx 1.
3. The fund's price or rate missing on a review day: no parking suggestion, the fund valued at cost, and no crash in `status`, the dashboard or ACTIONS.
4. A screen CSV from before this change (no `ttm_vs_5y_ebit` column) or with NaN ratios: the filter passes every row.
5. A ticker whose closes start after the backtest's start, or have gaps: no signal from a window that is not full, no fill on a missing close.

---

### Task 1: Positions and trades in the quote currency

**Files:**
- Modify: `value_pipeline/portfolio.py` (`Position`, `apply_buy`, `apply_sell`, `TRADE_FIELDS`, `append_trade`, new `row_fx`)
- Modify: `value_pipeline/operations.py` (`record_buy`, `record_sell`, `realised_gains`, `portfolio_view`, new `trade_fx`)
- Modify: `value_pipeline/benchmark.py` (`cash_flows`)
- Modify: `value_pipeline/review.py` (`_holding_value` fallback)
- Test: `tests/test_foreign_trades.py` (new)

**Interfaces:**
- Produces:
  - `Position.currency: str | None = None`, `Position.avg_fx: float = 1.0`, property `Position.cost_usd -> float` (`shares × avg_price × avg_fx`).
  - `apply_buy(pf, ticker, shares, price, fees=0.0, on=None, thesis="", intrinsic_value=None, kill_criteria=None, entry_price=None, lane="value", currency=None, fx=1.0) -> Position`
  - `apply_sell(pf, ticker, shares, price, fees=0.0, fx=1.0) -> float` (dollar gain)
  - `portfolio.TRADE_FIELDS = ["date", "ticker", "action", "shares", "price", "fees", "note", "currency", "fx"]`; `portfolio.row_fx(row: dict) -> float` (missing or blank → 1.0).
  - `operations.trade_fx(ticker: str, fx: float | None, position: Position | None = None, fx_fn=market_data.quote_fx) -> tuple[str | None, float]`
  - `record_buy(paths, ticker, shares, price, fees=0.0, on=None, note="", fx=None)` and `record_sell(...)` with the same new last parameter; return values unchanged.
  - `portfolio_view(paths, price_fn=market_data.latest_price, benchmark_fn=benchmark.compare, fx_fn=market_data.quote_fx) -> dict`; each position row gains `currency`, `fx` (today's rate or None) and `cost` (dollars).

- [ ] **Step 1: Write the failing tests** in `tests/test_foreign_trades.py`

```python
def test_pence_buy_debits_dollars_and_keeps_pence():
    pf = Portfolio(cash=10_000.0)
    held = apply_buy(pf, "DNLM.L", 100, 900.0, fees=2.0, currency="GBp", fx=0.0135)
    assert pf.cash == pytest.approx(10_000 - 1_217.0)          # 100 × 900 × 0.0135 + 2
    assert held.currency == "GBp" and held.avg_fx == pytest.approx(0.0135)
    assert held.avg_price == pytest.approx(900 + 2.0 / 0.0135 / 100)
    assert held.cost_usd == pytest.approx(1_217.0)

def test_add_reweights_price_and_rate_so_cost_is_dollars_paid():
    pf = Portfolio(cash=10_000.0)
    apply_buy(pf, "DNLM.L", 100, 900.0, fees=2.0, currency="GBp", fx=0.0135)
    held = apply_buy(pf, "DNLM.L", 100, 1_000.0, currency="GBp", fx=0.0130)
    assert held.shares == 200
    assert held.cost_usd == pytest.approx(1_217.0 + 1_300.0)

def test_sale_returns_the_dollar_gain():
    pf = Portfolio(cash=10_000.0)
    apply_buy(pf, "DNLM.L", 100, 900.0, fees=2.0, currency="GBp", fx=0.0135)
    apply_buy(pf, "DNLM.L", 100, 1_000.0, currency="GBp", fx=0.0130)
    gain = apply_sell(pf, "DNLM.L", 200, 1_100.0, fees=1.0, fx=0.014)
    assert gain == pytest.approx(3_079.0 - 2_517.0)
    assert pf.cash == pytest.approx(10_000 - 2_517.0 + 3_079.0)
    assert pf.position("DNLM.L") is None

def test_dollar_trades_are_unchanged():
    pf = Portfolio(cash=1_000.0)
    held = apply_buy(pf, "HRB", 10, 45.0, fees=1.0)
    assert held.currency is None and held.avg_fx == 1.0 and held.avg_price == pytest.approx(45.1)
    assert apply_sell(pf, "HRB", 10, 50.0) == pytest.approx(49.0)

def test_old_portfolio_json_loads(tmp_path):      # a position dict without currency / avg_fx
    ...  # Portfolio.from_dict gives currency None, avg_fx 1.0

def test_trade_fx_reads_the_market_and_refuses_an_unknown_rate():
    assert trade_fx("DNLM.L", None, fx_fn=lambda t: ("GBp", 0.0135)) == ("GBp", 0.0135)
    assert trade_fx("DNLM.L", 0.014, fx_fn=lambda t: ("GBp", 0.0135)) == ("GBp", 0.014)
    assert trade_fx("HRB", None, fx_fn=lambda t: ("USD", 1.0)) == ("USD", 1.0)
    with pytest.raises(TradeError, match="--fx"):
        trade_fx("XYZ.WA", None, fx_fn=lambda t: ("PLN", None))
    # a sale uses the held position's currency when the market's is unreadable
    assert trade_fx("DNLM.L", 0.014, position=Position("DNLM.L", 1, 900.0, "2026-11-05", currency="GBp"),
                    fx_fn=lambda t: (None, 1.0)) == ("GBp", 0.014)

def test_record_buy_writes_currency_and_fx_and_upgrades_an_old_log(tmp_path, monkeypatch):
    # paths with portfolio cash 10,000 and a trades.csv holding the old seven-column header
    # and one HRB row; monkeypatch market_data.quote_fx -> ("GBp", 0.0135) and
    # review.latest_result -> None; record_buy(paths, "DNLM.L", 100, 900.0)
    rows = read_trades(paths)
    assert rows[0]["ticker"] == "HRB" and row_fx(rows[0]) == 1.0 and rows[0]["price"] == "45.59"
    assert rows[1]["currency"] == "GBp" and row_fx(rows[1]) == pytest.approx(0.0135)

def test_portfolio_view_values_pence_in_dollars_and_falls_back_to_cost(tmp_path):
    # one position: 100 DNLM.L, avg_price 900, currency GBp, avg_fx 0.0135; cash 1,000
    view = portfolio_view(paths, price_fn=lambda t: 1_000.0, benchmark_fn=lambda *a: None,
                          fx_fn=lambda t: ("GBp", 0.014))
    row = view["positions"][0]
    assert row["value"] == pytest.approx(1_400.0) and row["cost"] == pytest.approx(1_215.0)
    assert row["gain"] == pytest.approx(1_400 / 1_215 - 1)
    assert row["price"] == 1_000.0 and row["currency"] == "GBp"
    assert view["total"] == pytest.approx(2_400.0)
    view = portfolio_view(paths, price_fn=lambda t: 1_000.0, benchmark_fn=lambda *a: None,
                          fx_fn=lambda t: ("GBp", None))
    assert view["positions"][0]["value"] == pytest.approx(1_215.0)      # no rate: cost basis

def test_benchmark_and_realised_gains_use_the_trade_rate():
    trades = [{"date": "2026-11-05", "ticker": "DNLM.L", "action": "BUY", "shares": "100", "price": "900",
               "fees": "2", "currency": "GBp", "fx": "0.0135"},
              {"date": "2026-11-20", "ticker": "DNLM.L", "action": "SELL", "shares": "100", "price": "1000",
               "fees": "0", "currency": "GBp", "fx": "0.014"}]
    events = benchmark.cash_flows([], trades)
    assert events[0] == ("2026-11-05", "buy", pytest.approx(1_217.0))
    assert events[1] == ("2026-11-20", "sell", pytest.approx(1_400.0))
    assert realised_gains(trades) == [None, pytest.approx(183.0)]
```

- [ ] **Step 2: Run them and see them fail**: `uv run pytest -q tests/test_foreign_trades.py`
- [ ] **Step 3: Implement.** `apply_buy`: `cost_usd = shares × price × fx + fees`; cash check and debit in dollars; quote cost `shares × price + fees / fx`; on an add, `avg_price` is total quote cost over total shares and `avg_fx` is total dollars paid over total quote cost. `append_trade` rewrites an old-header file whole (temporary file, then `replace`) before appending. `trade_fx`: the given `fx` wins over the market's rate; the currency is the market's, else the position's; `"USD"` and None both mean rate 1 when no `fx` is given; an unknown rate raises `TradeError(f"no dollar rate for {currency}: pass --fx (dollars per {currency})")`. `review._holding_value` falls back to `p.cost_usd`.
- [ ] **Step 4: Run the tests and the full suite**: `uv run pytest -q` and `uv run ruff check .`
- [ ] **Step 5: Commit** (`feat: positions and trades in the quote currency`)

---

### Task 2: Buying and holding outside dollars on the CLI and the website

**Files:**
- Modify: `value_pipeline/cli.py` (`cmd_buy`, `cmd_sell`, `cmd_status`, the `buy`/`sell` parsers)
- Modify: `value_pipeline/web/app.py` (trade page and trade post), `value_pipeline/web/templates/trade.html`, `value_pipeline/web/templates/dashboard.html`
- Modify: `value_pipeline/README.md`
- Test: `tests/test_foreign_trades.py`, `tests/test_value_web.py`

**Interfaces:**
- Consumes: Task 1's `record_buy(..., fx=None)`, `record_sell(..., fx=None)`, `Position.currency`, `Position.avg_fx`, `portfolio_view` rows with `currency`, `fx`, `cost`.
- Produces: `buy` / `sell` option `--fx FLOAT`; `cli.trade_line(action: str, shares: float, ticker: str, price: float, currency: str | None, fx: float) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def test_trade_line_states_both_units():
    assert cli.trade_line("BUY", 84, "DNLM.L", 900.0, "GBp", 0.01348) \
        == "BUY 84 DNLM.L @ 900.00 GBp (fx 0.01348) = $1,019.09"
    assert cli.trade_line("SELL", 10, "HRB", 50.0, None, 1.0) == "SELL 10 HRB @ $50.00 = $500.00"

def test_cli_buy_passes_fx(monkeypatch, tmp_path):
    # main(["--data-dir", str(tmp_path), "buy", "DNLM.L", "84", "900", "--fx", "0.0135"]) after init with cash;
    # monkeypatch market_data.quote_fx -> ("GBp", 0.013); the saved position has avg_fx 0.0135

def test_status_shows_the_quote_unit(...):       # a GBp holding prints "900.00 GBp" in the Price column

# tests/test_value_web.py
def test_trade_form_prefills_the_rate_and_shows_dollars(...):
    # GET /trade?ticker=DNLM.L with quote_fx -> ("GBp", 0.0135) and latest_price -> 900.0:
    # the page contains 'name="fx"', 'value="0.0135"' and "GBp"
def test_trade_post_records_the_rate(...):
    # POST /trade/buy with fx=0.014 stores avg_fx 0.014; a blank fx uses quote_fx
def test_dashboard_shows_the_unit_next_to_a_foreign_price(...):   # "900.00 GBp"
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement.** `cmd_buy` / `cmd_sell` print `trade_line(...)` first, then the existing holding and cash sentence with the average price in the quote unit. The trade form gets `<label>Rate ($ per unit) <input name="fx" ...>` in both the Buy and Sell forms, prefilled from `quote_fx` for a looked-up non-dollar ticker and blank for dollars; the lookup line shows the unit after the price. `status` and the dashboard print `{price:,.2f} {currency}` when the currency is not dollars; Avg stays in the quote unit, Value and Gain are dollars. README: a short "Buying outside dollars" paragraph under Commands (price in the quote unit, `--fx` to record the rate you actually got, fees in dollars).
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: record and show trades outside dollars`)

---

### Task 3: Index-fund parking

**Files:**
- Modify: `value_pipeline/config.py` (`DEFAULTS["parking"]`, new `parking_ticker`), `value_pipeline/config.yaml`
- Modify: `value_pipeline/actions.py` (`plan`, new `parking_move`)
- Modify: `value_pipeline/review.py` (holdings to run, `others`, the `plan` call, the `--tickers` refusal, `actions_markdown` call)
- Modify: `value_pipeline/render.py` (`actions_markdown`)
- Modify: `value_pipeline/daily.py`, `value_pipeline/watch.py`, `value_pipeline/committee.py` (skip the fund)
- Modify: `value_pipeline/operations.py` (`portfolio_view` rating text), `value_pipeline/cli.py`, `value_pipeline/web/app.py` (pass the ticker)
- Modify: `value_pipeline/README.md`
- Test: `tests/test_parking.py` (new)

**Interfaces:**
- Consumes: Task 1's `Position.cost_usd`, `portfolio_view(..., fx_fn=...)`.
- Produces:
  - `config.DEFAULTS["parking"] = {"ticker": "SXR8.DE", "share": 0.7, "min_trade_amount": 250}`; `config.parking_ticker(cfg: dict) -> str | None`.
  - `actions.parking_move(liquid: float, buys: float, parked: float, parking: dict) -> dict | None` → `{"action": "PARK" | "UNPARK", "amount": float}`.
  - `plan(decisions, cash, sizing, others=(), parked=0.0, parking=None)`; `parking` is the config section plus an optional `price_usd`. `summary` gains `parked` and `parking` (`{"action", "amount", "shares"}` or None); `cash_after` is the cash left after the stock actions and the parking move.
  - `render.actions_markdown(..., fund: dict | None = None)` with `fund = {"ticker": str, "value": float, "priced": bool}`.
  - `portfolio_view(..., parking_ticker: str | None = None)`: the fund's row has `last_rating == "index fund"`, `intrinsic_value` and `margin_of_safety` None.

- [ ] **Step 1: Write the failing tests** in `tests/test_parking.py`

```python
PARK = {"ticker": "SXR8.DE", "share": 0.7, "min_trade_amount": 250}

def test_parking_move_rules():
    assert parking_move(9_564.07, 0.0, 0.0, PARK) == {"action": "PARK", "amount": pytest.approx(6_694.85, abs=0.01)}
    assert parking_move(3_000.0, 0.0, 6_900.0, PARK) is None                 # target 6,930: 30 short, under 250
    assert parking_move(500.0, 550.0, 5_000.0, PARK) == {"action": "UNPARK", "amount": pytest.approx(50.0)}
    assert parking_move(1_000.0, 1_000.0, 0.0, PARK) is None                 # nothing idle
    assert parking_move(2_000.0, 0.0, 9_000.0, PARK) is None                 # above its share: never sold for that
    assert parking_move(400.0, 300.0, 0.0, {**PARK, "share": 1.0, "min_trade_amount": 50}) \
        == {"action": "PARK", "amount": pytest.approx(100.0)}                # capped at liquid − buys

def test_plan_without_parking_is_unchanged():
    held = Decision("HRB", "Hold", 54.81, held_shares=11.0)
    buy = Decision("AAA", "Buy", 100.0, margin_of_safety=0.4)
    a1, s1 = plan([held, buy], 9_564.07, DEFAULTS["sizing"])
    a2, s2 = plan([held, buy], 9_564.07, DEFAULTS["sizing"], parked=0.0, parking=None)
    assert [(a.ticker, a.action, a.amount, a.shares) for a in a1] == [(a.ticker, a.action, a.amount, a.shares) for a in a2]
    assert s2["parking"] is None and s2["parked"] == 0.0
    assert {k: s1[k] for k in ("total_value", "cash", "target_weight", "cash_after")} \
        == {k: s2[k] for k in ("total_value", "cash", "target_weight", "cash_after")}

def test_plan_parks_idle_money_after_the_buys():
    held = Decision("HRB", "Hold", 54.81, held_shares=11.0)
    _, s = plan([held], 9_564.07, DEFAULTS["sizing"], parked=0.0, parking={**PARK, "price_usd": 809.0})
    assert s["parking"]["action"] == "PARK" and s["parking"]["amount"] == pytest.approx(6_694.85, abs=0.01)
    assert s["parking"]["shares"] == pytest.approx(6_694.85 / 809.0, abs=0.001)
    assert s["cash_after"] == pytest.approx(9_564.07 - 6_694.85, abs=0.01)

def test_plan_sizes_buys_against_the_fund_and_unparks_the_shortfall():
    buy = Decision("AAA", "Buy", 100.0, margin_of_safety=0.4)
    actions, s = plan([buy], 500.0, DEFAULTS["sizing"], parked=5_000.0, parking={**PARK, "price_usd": 809.0})
    assert s["total_value"] == pytest.approx(5_500.0)
    assert actions[0].action == "BUY" and actions[0].amount == pytest.approx(550.0)   # 10% of 5,500
    assert s["parking"] == {"action": "UNPARK", "amount": pytest.approx(50.0), "shares": pytest.approx(50 / 809.0, abs=1e-4)}
    assert s["cash_after"] == pytest.approx(0.0)

def test_plan_without_a_fund_price_gives_no_share_count():
    _, s = plan([], 5_000.0, DEFAULTS["sizing"], parked=0.0, parking=PARK)
    assert s["parking"]["action"] == "PARK" and s["parking"]["shares"] is None

def test_parking_ticker_helper():
    assert parking_ticker({"parking": {"ticker": " sxr8.de "}}) == "SXR8.DE"
    assert parking_ticker({"parking": {"ticker": ""}}) is None and parking_ticker({}) is None

def test_actions_shows_the_index_fund_section():
    # summary from the PARK case, fund={"ticker": "SXR8.DE", "value": 0.0, "priced": True}
    assert "## Index fund" in md
    assert "Buy $6,694.85 of SXR8.DE (about 8.28 shares)" in md
    assert "Idle money sits in the index fund so it moves with the market and can fall; sell it only to fund a buy." in md
    # UNPARK: "Sell $50.00 of SXR8.DE to fund the buys; place it first"
    # no move: "No change."
    # fund with priced False: "No suggestion: no dollar price for SXR8.DE today."
    # fund=None: "## Index fund" not in md
    # Portfolio block with a held fund: "- Index fund (SXR8.DE): $5,000.00 (91% of the portfolio)"

def test_review_never_runs_agents_on_the_fund(tmp_path, monkeypatch):
    # a dry-run review of a portfolio holding HRB and SXR8.DE (8 sh, avg 700, EUR, avg_fx 1.12) with
    # latest_price / quote_fx monkeypatched: results.json has no SXR8.DE row, ACTIONS.md has the
    # Index fund section, and summary total includes the fund's dollar value

def test_review_refuses_the_fund_by_name(...):          # review --tickers SXR8.DE -> SystemExit naming the index fund
def test_daily_and_watch_skip_the_fund(...):             # no trigger and no alert for SXR8.DE in FREEFALL
def test_portfolio_view_labels_the_fund(...):            # last_rating "index fund", margin_of_safety None
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement.** `parking_move`: `idle = liquid + parked − buys`; `buys > liquid` → UNPARK `round(buys − liquid, 2)`; else `gap = share × idle − parked`, PARK `round(min(gap, liquid − buys), 2)` when `gap ≥ min_trade_amount`; else None. In `plan`: `total_value` adds `parked`; `available` starts at `cash + proceeds + parked`; after the sizing loop compute `buys` (BUY and ADD amounts), call `parking_move` when `parking` is given, set `shares = round(amount / price_usd, 4)` when `price_usd` is given, and `cash_after = liquid − buys − (PARK amount) + (UNPARK amount)`. In the review: the fund's ticker is removed from the tickers to run and from `others`; its dollar value (`shares × price × rate`, else `cost_usd`) is `parked`; `price_usd` is passed only when both price and rate are known. `daily`, `watch` and the committee's holdings list filter with `config.parking_ticker(cfg)`. README: a section "Index fund for idle cash" (what it does, the three settings, how to turn it off, that it can fall).
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: park idle cash in an index fund`)

---

### Task 4: Run-rate filter on Magic Formula candidates

**Files:**
- Modify: `value_pipeline/screener.py` (`top_candidates`, new `run_rate_skips`)
- Modify: `value_pipeline/review.py` (`preview`, the print before the cost estimate)
- Modify: `value_pipeline/config.py`, `value_pipeline/config.yaml` (`review.min_run_rate: 0.8`), `value_pipeline/README.md`
- Test: `tests/test_run_rate_filter.py` (new)

**Interfaces:**
- Produces: `top_candidates(paths, owned, n, compounders=0, dislocations=0, inflections=0, min_run_rate=0.0) -> list[dict]`; `run_rate_skips(paths, owned, n, min_run_rate) -> list[dict]` (keys `ticker`, `ttm_vs_5y_ebit`, in rank order); `preview(...)` result gains `run_rate_skips`.

- [ ] **Step 1: Write the failing tests**

```python
def _screen(paths, rows): ...   # writes data/screens/screen_2026-11-05.csv from dicts with defaults
                                # ticker, mf_position, trend "BASING", ttm_vs_5y_ebit, lane_hint "", expected_return

def test_low_run_rate_gives_its_place_to_the_next_name(tmp_path):
    _screen(paths, [("THO", 1, 0.50), ("AAA", 2, 1.10), ("PPC", 3, 0.52), ("BBB", 4, None), ("CCC", 5, 0.80)])
    assert [r["ticker"] for r in top_candidates(paths, set(), 3, min_run_rate=0.8)] == ["AAA", "BBB", "CCC"]
    assert run_rate_skips(paths, set(), 3, 0.8) == [{"ticker": "THO", "ttm_vs_5y_ebit": 0.50},
                                                    {"ticker": "PPC", "ttm_vs_5y_ebit": 0.52}]

def test_zero_turns_the_filter_off(tmp_path):
    assert [r["ticker"] for r in top_candidates(paths, set(), 3)] == ["THO", "AAA", "PPC"]
    assert run_rate_skips(paths, set(), 3, 0.0) == []

def test_a_screen_without_the_column_passes_everything(tmp_path): ...
def test_hinted_lanes_ignore_the_filter(tmp_path):
    # a dislocation-hinted row with ratio 0.4 is still returned by top_candidates(..., dislocations=1, min_run_rate=0.8)
def test_skips_stop_at_the_last_pick(tmp_path):
    # a low-ratio row ranked below the n-th pick is not listed
def test_review_prints_the_left_out_names(tmp_path, capsys, monkeypatch):
    # dry-run review: stdout contains "Left out, profit running below the five-year average: THO 0.50, PPC 0.52"
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement.** The filter applies to the `mf_position` picks only, before `.head(n)`; a NaN or missing ratio passes. `run_rate_skips` lists the dropped rows whose `mf_position` is below the last pick's. `preview` passes `rcfg.get("min_run_rate", 0.0)`; `review --tickers` skips both. README: one paragraph under "The review".
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: skip peak-cycle names when picking review candidates`)

---

### Task 5: Trend simulation engine

**Files:**
- Create: `value_pipeline/trend.py`
- Modify: `value_pipeline/config.py`, `value_pipeline/config.yaml` (the `trend` section from the spec)
- Test: `tests/test_trend.py` (new)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Settings` with `start: str, equity: float, risk_per_trade: float, stop_n: float, n_days: int, max_positions: int, max_position_weight: float, cost_per_side: float, market_filter: bool`; `settings(cfg: dict, **overrides) -> Settings`.
  - `SYSTEMS = {"breakout_55_20": (55, 20), "breakout_20_10": (20, 10), "momentum_12_1": None}`.
  - `simulate(system: str, closes: pd.DataFrame, spy: pd.Series, s: Settings, eligible: Callable[[str, date], bool] | None = None) -> dict`. `closes`: index of trading dates (including the warm-up before `s.start`), one column per ticker, NaN where there is no close. Returns `{"equity": pd.Series, "exposure": pd.Series, "trades": list[dict], "costs": float}`; both series run from `s.start`. A trade dict has `ticker, signal_date, entry_date, entry_price, shares, n, exit_date, exit_price, reason` (`"exit_low" | "stop" | "rebalance" | "open"`), `pnl, ret, days`.
  - `max_drawdown(series: pd.Series) -> float` (a positive fraction), `cagr(series: pd.Series) -> float`.
  - `metrics(result: dict, spy: pd.Series) -> dict` with keys `cagr, total_return, max_drawdown, volatility, return_over_vol, exposure, trades, trades_per_year, win_rate, avg_win, avg_loss, payoff, avg_days, costs_per_year, top5_share, years` (`{year: {"system": float, "spy": float}}`), `spy_cagr, spy_max_drawdown, spy_at_exposure_cagr, years_gained` (share of calendar years with a positive return).

- [ ] **Step 1: Write the failing tests** on synthetic closes (a helper builds a business-day frame; SPY rising 0.05% a day unless the test says otherwise)

```python
def test_breakout_enters_the_day_after_the_signal_and_exits_after_the_low():
    # one ticker flat at 100 for 80 days, 101 on day 80 (a 55-day high), climbing 1 a day to 130,
    # then falling 2 a day. start = day 60.
    # trade.signal_date == day 80, entry_date == day 81, entry_price == close[81]
    # exit: signal on the first close below the prior 20 closes' low, fill the next day, reason "exit_low"

def test_stop_exits_before_the_low():
    # after entry the close drops 3 × N in one day: reason "stop", exit at the following close

def test_sizing_risks_one_percent_and_respects_the_weight_cap():
    # N = 1.0 (closes alternate ±1 before the breakout), equity 10,000, stop_n 2:
    # shares == pytest.approx(10_000 × 0.01 / 2.0) when shares × price ≤ 2,000
    # with N = 0.1 the position value is capped at 0.20 × equity

def test_costs_are_charged_on_both_sides():
    # one round trip with cost_per_side 0.01: result["costs"] == pytest.approx(0.01 × (buy value + sell value))
    # and final equity equals the zero-cost run's minus those costs (cash earns nothing)

def test_slots_go_to_the_strongest_126_day_return():
    # three tickers break out on the same day with max_positions = 2: the two with the higher
    # 126-day return are bought

def test_market_filter_blocks_entries_not_exits():
    # SPY below its 200-day average: no new trade; an open position still exits on its low

def test_no_signal_from_a_window_that_is_not_full_and_no_fill_on_a_missing_close():
    # a ticker with 30 closes before a new high gives no 55-day signal;
    # a signal whose next five closes are NaN is cancelled

def test_eligibility_gates_entries():
    # eligible=lambda t, d: d >= date(2020, 6, 30): a breakout before that date is ignored

def test_momentum_holds_the_top_names_monthly():
    # four tickers with 12-1 returns 40%, 30%, 20%, −10%, max_positions 2: on the first trading day of
    # the month the top two are bought (fill next close) at equity / 2 each; next month a new leader
    # replaces the weaker one (reason "rebalance"); a name below its 200-day average is not bought

def test_no_lookahead():
    # random-walk closes for 20 tickers (seeded), d = a date in the middle: scaling every close after d
    # by seeded factors leaves all trades with entry_date <= d identical in signal_date, entry_date,
    # entry_price and shares

def test_max_drawdown_and_cagr():
    s = pd.Series([100, 120, 90, 110], index=pd.to_datetime(["2020-01-01", "2020-07-01", "2021-01-01", "2022-01-01"]))
    assert max_drawdown(s) == pytest.approx(0.25)
    assert cagr(s) == pytest.approx((110 / 100) ** (1 / 2.0027) - 1, abs=1e-3)

def test_metrics_counts_wins_and_years():
    # a hand-built result with three closed trades (+10%, −5%, +20%): win_rate 2/3, payoff 15/5,
    # years_gained from the equity series, spy_at_exposure_cagr between 0 and spy_cagr
```

- [ ] **Step 2: Run them and see them fail**: `uv run pytest -q tests/test_trend.py`
- [ ] **Step 3: Implement** per the spec's Part 3 and "Settled details". Signals come from rolling windows shifted by one day (the prior `k` closes, all present); the day loop keeps cash, open positions and pending orders; sizes use the equity at the signal's close; an order fills at the ticker's next non-NaN close within five rows. Positions still open on the last day are marked to the last close with reason `"open"` and no sale cost. N is the mean absolute close-to-close change over the prior `n_days` closes.
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: trend-sleeve simulation engine`)

---

### Task 6: Trend backtest run, verdict, report and command

**Files:**
- Modify: `value_pipeline/trend.py` (data loading, the run, sensitivity, verdict, report)
- Modify: `value_pipeline/backtest.py` (`run(..., keep_rows=False)`)
- Modify: `value_pipeline/cli.py` (`trend-backtest`), `value_pipeline/web/app.py` and `value_pipeline/web/templates/backtest.html` (link to the newest trend report), `value_pipeline/README.md`
- Test: `tests/test_trend.py`, `tests/test_lane_backtest.py` (keep_rows), `tests/test_value_web.py` (the link)

**Interfaces:**
- Consumes: Task 5's `simulate`, `metrics`, `settings`, `Settings`, `SYSTEMS`.
- Produces:
  - `backtest.run(..., keep_rows: bool = False)`: with True the result also has `"rows"` (`{cohort: [row, ...]}`) and `"added"` (`{ticker: join date}`); the default result is unchanged.
  - `trend.quality_windows(rows_by_cohort: dict[str, list[dict]], end: str) -> dict[str, list[tuple[date, date]]]` (per ticker, the half-open date spans it passed the filters).
  - `trend.load_closes(paths, tickers: list[str], start: str, fetch: bool = True) -> tuple[pd.DataFrame, pd.Series]` (adj_close frame on SPY's calendar from 300 trading days before `start`, and SPY's adj_close).
  - `trend.verdict(primary: dict, high_cost: dict) -> dict` → `{"supports": bool, "sentence": str, "cagr": float, "spy_cagr": float, "max_drawdown": float, "spy_max_drawdown": float, "years_gained": float}`.
  - `trend.run(paths, cfg, today=None, fetch=True, progress=None) -> dict` → `{"generated", "start", "end", "settings", "universe_sizes": {"all": int, "quality": int}, "results": {(system, universe): metrics dict}, "trades": {(system, universe): [...]}, "sensitivity": [{"setting", "value", **metrics}], "verdicts": {"breakout_55_20": {...}, "momentum_12_1": {...}}}`.
  - `trend.render_report(result: dict) -> str`; `trend.write(paths, result) -> Path` (the `.md`; also writes `.html` and `_trades.csv` beside it); `trend.latest_report(paths) -> Path | None` (newest `.html`).

- [ ] **Step 1: Write the failing tests**

```python
def test_verdict_supports_on_return_or_on_drawdown():
    base = {"spy_cagr": 0.12, "spy_max_drawdown": 0.33, "years_gained": 0.7}
    ok_return = {**base, "cagr": 0.135, "max_drawdown": 0.40}
    ok_risk = {**base, "cagr": 0.105, "max_drawdown": 0.20}
    assert verdict(ok_return, ok_return)["supports"] is True
    assert verdict(ok_risk, ok_risk)["supports"] is True
    assert verdict({**base, "cagr": 0.11, "max_drawdown": 0.30}, ok_return)["supports"] is False   # neither leg
    assert verdict(ok_return, {**base, "cagr": 0.09, "max_drawdown": 0.40})["supports"] is False   # fails at the high cost
    assert verdict({**ok_return, "years_gained": 0.5}, ok_return)["supports"] is False             # too few good years
    assert "supports a live sleeve" in verdict(ok_return, ok_return)["sentence"]
    assert "does not support a live sleeve" in verdict({**ok_return, "years_gained": 0.5}, ok_return)["sentence"]

def test_quality_windows_run_from_cohort_to_cohort_and_the_last_to_the_end():
    rows = {"2015-06-30": [{"ticker": "AAA", "passes_filters": True}, {"ticker": "BBB", "passes_filters": False}],
            "2016-06-30": [{"ticker": "BBB", "passes_filters": True}]}
    w = quality_windows(rows, "2017-03-01")
    assert w["AAA"] == [(date(2015, 6, 30), date(2016, 6, 30))]
    assert w["BBB"] == [(date(2016, 6, 30), date(2017, 3, 1))]

def test_run_reports_every_system_and_universe(tmp_path, monkeypatch):
    # monkeypatch trend.load_closes -> a small synthetic frame, backtest.run -> {"rows": {...}, "added": {}},
    # market_data.universe -> three tickers: result["results"] has the six (system, universe) keys,
    # "sensitivity" has 4 settings × 3 or 2 values, "verdicts" has both systems

def test_report_states_the_verdict_and_the_limits(...):
    # render_report(result) contains the verdict sentence, "survivorship", "next close", a calendar-year
    # table header "| Year | System | SPY |", and one row per (system, universe)

def test_write_saves_markdown_html_and_trades(tmp_path): ...
def test_backtest_run_keep_rows(...):           # default result has no "rows"; keep_rows=True has rows and added
def test_backtest_page_links_the_trend_report(...):   # /backtest contains href="/reports/backtest/trend_2026-11-05.html"
def test_cli_trend_backtest_writes_the_report(tmp_path, monkeypatch): ...   # trend.run monkeypatched
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement.** The universe is `market_data.universe(paths, ["sp500", "sp400"])`; S&P 500 join dates and the cohort rows come from one `backtest.run(paths, cfg_us, fetch=fetch, keep_rows=True)` call with `cfg_us` a copy of the config whose `universe.indexes` is `["sp500", "sp400"]`. Eligibility for `all`: on or after the join date when one is known. For `quality`: inside one of the ticker's windows and on or after its join date. The verdict implements the spec's (a), (b), (c) with `high_cost` = the same system and universe at `cost_per_side` 0.006. The report: a header with the span, the settings and the limits (close-only N, next-close fills, fractional shares, today's members, no shorting); the verdict sentences; one summary table; the calendar-year table for the primary; the sensitivity table. The command prints the report and the saved path; `--from` overrides `trend.start`; `--no-fetch` reads the caches only. README: a "Trend backtest" section under Backtest.
- [ ] **Step 4: Run the tests and the full suite**
- [ ] **Step 5: Commit** (`feat: trend backtest command, verdict and report`)

---

### Task 7: Verification on real data (controller, after the merge)

- [ ] `uv run python -m value_pipeline trend-backtest` on the cached prices; read the verdict and the tables.
- [ ] `uv run python -m value_pipeline review --dry-run --yes`: the left-out line, the Index fund section, quote-unit prices.
- [ ] `uv run python -m value_pipeline buy --help` shows `--fx`; `status` runs.
- [ ] Restart the website; `/`, `/trade?ticker=DNLM.L` and `/backtest` return 200.
