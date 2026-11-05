# Nordnet Markets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daily screen ranks every market the user can trade on Nordnet (UK, Germany, France, Benelux, Portugal, Ireland, Italy, Switzerland, Austria, Spain, the Nordics, Canada, the US) in one list, in dollars, with per-share figures in the quote currency, and refreshes prices daily without refetching statements.

**Architecture:** `market_data.MARKETS` describes each market (Wikipedia pages, Yahoo suffix, country); `universe()` finds the constituents table by column name, normalises codes, drops cross-listings and records market and country. `fundamentals()` stores the financial currency and refreshes prices on a short cycle; `market_data.in_usd()` converts a record for `screener.evaluate`, and `screener.to_quote()` puts the per-share columns back in the quote unit after every computation. Pages, the pipeline note, ACTIONS and the README name the market and currency.

**Tech Stack:** Python 3.11+, pandas (`read_html`), yfinance (`fast_info`, `history`), requests, Jinja (website), pytest (`@pytest.mark.unit`, no network), ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-11-04-nordnet-markets-design.md`

## Global Constraints

- Nothing trades. The lane backtest stays US-only and its report says so.
- No network in tests: synthetic Wikipedia HTML, patched `requests.get`, `yf.Ticker`, `usd_rate`, `closes`.
- Markets and suffixes exactly as the spec's table: ftse100/ftse250 `.L`, dax/mdax `.DE`, cac40/sbf120 `.PA`, aex/amx `.AS`, bel20 `.BR`, psi `.LS`, iseq20 `.IR`, ftsemib `.MI`, smi/smim `.SW`, atx `.VI`, ibex35 `.MC`, omxs30 `.ST`, omxc25 `.CO`, omxh25 `.HE`, obx `.OL`, tsxcomp `.TO`, sp500/sp400 no suffix. No Japan, Korea, Poland, Iceland, TSX Venture.
- One sector vocabulary (GICS names). Yahoo → GICS: `Financial Services`→`Financials`, `Technology`→`Information Technology`, `Consumer Cyclical`→`Consumer Discretionary`, `Consumer Defensive`→`Consumer Staples`, `Basic Materials`→`Materials`, `Healthcare`→`Health Care`; `Industrials`, `Energy`, `Real Estate`, `Utilities`, `Communication Services` unchanged.
- `fx_quote` = US dollars per one quoted unit (pence: pound rate / 100, as `quote_fx` already does). Market cap and enterprise value convert at the major-unit `usd_rate(currency)`. Statements convert at `usd_rate(financial_currency)`.
- Dollars for ranking, filters, market cap and EV; quote unit for `price`, `value_base`, `value_low`, `value_high` in the written row. Nothing else is divided back.
- Old caches: a `CACHE_SCHEMA` 6 record is fetched again in full; a record without `price_at` counts as stale for the price refresh. Old screen CSVs without the new columns still render.
- ruff clean; full suite green after each task; conventional commits ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_015r4hMD2DRWvm5AXmWo8KRV`.

## Review Focus

1. A Wikipedia page whose first `read_html` table is an infobox or table of contents without a ticker column: the finder must take the first table that has both a ticker and a name column, not index 0. Test in Task 1 (`test_universe_finds_the_constituents_table_by_column_name`).
2. Codes with spaces or dots (`NOVO B`, `BT.A`, `RR.`, `LSE: AZN`, `BRK.B`): Yahoo's dash form with the market suffix (`NOVO-B.CO`, `BT-A.L`, `RR.L`, `AZN.L`, `BRK-B`). Test in Task 1 (`test_universe_normalises_ticker_codes`).
3. Yahoo gives no `financialCurrency` for a ticker: the statements convert at the quote currency's major-unit rate and nothing raises. Test in Task 2 (`test_in_usd_without_financial_currency_uses_the_quote_major_rate`).
4. A schema-6 cache record (no `financial_currency`, no `price_at`) under the new code: fetched again in full, never price-refreshed into an unconverted record. Test in Task 3 (`test_old_schema_record_is_fetched_in_full_not_price_refreshed`).
5. The screen page over a CSV written before the `market` column existed: renders with an empty Market cell and an empty market filter. Test in Task 4 (`test_screen_page_renders_a_csv_without_market_columns`).

---

### Task 1: Nordnet markets in the universe

**Files:**
- Modify: `value_pipeline/market_data.py` (`WIKI` → `MARKETS`; `universe()`), `value_pipeline/config.py:16-19` (`universe.indexes` default), `value_pipeline/config.yaml` (`universe.indexes`, one line per market with the country as a comment), `value_pipeline/cli.py:86` (`cmd_screen` message: "Screening Nordnet's markets (first run fetches every new ticker; about an hour)...")
- Create: `scripts/check_markets.py` (network script, not a test: `python scripts/check_markets.py` fetches every market's pages through `market_data.fetch_market(market)` and prints `key country count` per market, `WARNING` on a count under 10; exit code 1 when any market is under 10)
- Test: `tests/test_nordnet_markets.py` (new; `@pytest.mark.unit`)

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Market: key: str; country: str; pages: tuple[str, ...]; suffix: str = ""; gics_column: str | None = None` (sp500/sp400: `gics_column="GICS Sector"`; everyone else None).
- Produces: `MARKETS: dict[str, Market]` with the spec's keys, countries and Wikipedia pages (`pages` holds the index page URL; tsxcomp's `pages` lists the index page and `https://en.wikipedia.org/wiki/List_of_companies_listed_on_the_S%26P/TSX_Composite_Index` — the controller corrects any page after running `scripts/check_markets.py`).
- Produces: `TICKER_COLUMNS = ("ticker", "symbol", "code", "ticker symbol")`, `NAME_COLUMNS = ("company", "name", "security", "constituent")` (matched case-insensitively against stripped column names; a column that is a tuple joins its parts with a space first).
- Produces: `normalise_code(code: str, market: Market) -> str`: strip; drop a prefix `^[A-Z]+:\s*`; drop a trailing `market.suffix` if present; drop a trailing `.`; replace remaining `.` and whitespace with `-`; append `market.suffix`.
- Produces: `name_key(name: str) -> str`: lower-case, punctuation removed, whitespace-split, the tokens `plc nv sa ag se ab asa oyj spa group holding holdings` dropped (so `n.v.`, `s.a.`, `s.p.a.`, `a/s` fall out with the punctuation), joined by one space.
- Produces: `fetch_market(market: Market) -> pd.DataFrame` with columns `ticker, name, sector, market, country` (sector from `gics_column` when set, else None); the first page with a usable table wins; an empty frame when no page has one.
- Produces: `universe(paths, indexes)` → columns `ticker, name, sector, index, market, country` (`index` kept equal to `market` for the existing callers); raises `ValueError("unknown universe.indexes entry: X")` for a key not in `MARKETS` before any fetch; a market under 10 tickers logs a warning and contributes nothing that run; duplicates dropped first by `name_key` (first market in `indexes` order wins, each drop logged with both tickers) then by `ticker`; the whole result written to `paths.universe` as today, with the same cache fallback.

- [ ] **Step 1: Write the failing tests** in `tests/test_nordnet_markets.py`

```python
@pytest.mark.unit
def test_universe_finds_the_constituents_table_by_column_name(tmp_path, monkeypatch):
    # page: an infobox table (columns "Foundation", "Operator") then a members table (Ticker, Company)
    html = TOC_TABLE + members_table([("AZN", "AstraZeneca"), ("BT.A", "BT Group")])
    monkeypatch.setattr(market_data.requests, "get", lambda url, **k: FakeResponse(html))
    df = market_data.universe(paths, ["ftse100"])
    assert list(df["ticker"]) == ["AZN.L", "BT-A.L"]
    assert set(df["market"]) == {"ftse100"} and set(df["country"]) == {"UK"}
    assert df["sector"].isna().all()

def test_universe_normalises_ticker_codes():
    uk = market_data.MARKETS["ftse100"]; dk = market_data.MARKETS["omxc25"]; us = market_data.MARKETS["sp500"]
    assert market_data.normalise_code("LSE: AZN", uk) == "AZN.L"
    assert market_data.normalise_code("AZN.L", uk) == "AZN.L"
    assert market_data.normalise_code("RR.", uk) == "RR.L"
    assert market_data.normalise_code("BT.A", uk) == "BT-A.L"
    assert market_data.normalise_code("NOVO B", dk) == "NOVO-B.CO"
    assert market_data.normalise_code("BRK.B", us) == "BRK-B"

def test_universe_keeps_gics_sector_for_the_sp_markets(...)   # Symbol/Security/GICS Sector table → sector filled, no suffix

def test_universe_drops_cross_listings_keeping_the_first_market(...)
    # ftse100 "Unilever PLC" + aex "Unilever N.V." with indexes ["ftse100", "aex"] → one row, market ftse100; caplog names both tickers

def test_universe_skips_a_market_under_ten_tickers_with_a_warning(...)   # 3 tickers → no rows for it, warning in caplog, the other market's rows present

def test_universe_rejects_an_unknown_index_key(tmp_path):
    with pytest.raises(ValueError, match="unknown universe.indexes entry: nikkei"):
        market_data.universe(paths, ["nikkei"])

def test_universe_csv_carries_market_and_country(...)   # read paths.universe back: columns include market, country

def test_markets_table_matches_the_spec():
    assert {k: m.suffix for k, m in market_data.MARKETS.items()} == {... the spec's table ...}
    assert "nikkei225" not in market_data.MARKETS and "kospi" not in market_data.MARKETS

def test_default_indexes_are_every_nordnet_market():
    assert set(config.DEFAULTS["universe"]["indexes"]) == set(market_data.MARKETS)
```

- [ ] **Step 2: Run to verify they fail.** `pytest tests/test_nordnet_markets.py -v` → ImportError/AttributeError on `MARKETS`.
- [ ] **Step 3: Implement** `Market`, `MARKETS`, `normalise_code`, `name_key`, `fetch_market`, the new `universe`; the config defaults and `config.yaml`; the `cmd_screen` message; `scripts/check_markets.py`.
- [ ] **Step 4: Run the file, then the full suite and ruff.** Existing tests that patch `requests.get` for the S&P pages (`grep -n "Symbol" tests/`) must still pass; fix a test only when it asserted the old column set.
- [ ] **Step 5: Commit** — `feat(universe): Nordnet's markets from Wikipedia, one list, one sector vocabulary`

### Task 2: Dollars in, quote unit out

**Files:**
- Modify: `value_pipeline/market_data.py` (`fundamentals`: store `financial_currency`, `CACHE_SCHEMA = 7`; new `gics_sector`, `in_usd`), `value_pipeline/screener.py` (`run`, new `to_quote`), `value_pipeline/review.py:359-380` (`offscreen_row`)
- Test: `tests/test_nordnet_markets.py`

**Interfaces:**
- Consumes: `universe()` rows with `market`, `country`, `sector` (None for non-S&P) from Task 1; `usd_rate`, `MINOR_UNITS` from `tradingagents.dataflows.vendors.yahoo.common`; `YEAR_FIELDS` from `tradingagents.dataflows.value_metrics`.
- Produces: `MONEY_FIELDS: tuple[str, ...]` = every `YEAR_FIELDS` entry except `shares` (quarters share the shape).
- Produces: `gics_sector(sector: str | None) -> str | None`: the Global Constraints mapping; an unmapped name passes through unchanged.
- Produces: `in_usd(data: dict) -> dict`: a deep-enough copy (years and quarters are new lists of new dicts) with `price × fx_quote`, `market_cap` and `enterprise_value` × `usd_rate(currency)`, every `MONEY_FIELDS` value of each year and quarter × `fx_financial`, and keys `fx_quote`, `fx_financial`, `quote_currency` (= `currency`). `fx_financial = usd_rate(financial_currency)` when set, else `usd_rate(currency)`. `currency` None or `USD` with no financial currency → the same record plus `fx_quote=fx_financial=1.0`. Missing rate → `DataUnavailable("no USD rate for GBp")` (the currency whose rate failed). None values stay None.
- Produces: `screener.to_quote(row: dict, data: dict) -> dict`: divides `price`, `value_base`, `value_low`, `value_high` by `data["fx_quote"]` (None stays None) and sets `row["quote_currency"]`, `row["fx_quote"]`; returns `row`.
- Produces: `screener.run` rows carry `market`, `country`, `quote_currency`, `fx_quote`; `res["excluded"]` becomes a `list[dict]` (`ticker, name, sector`) holding the pre-fetch exclusions (S&P GICS sector; a non-S&P ticker whose `gics_sector(cached_sector(...))` is excluded) and the post-fetch ones (a non-S&P ticker whose fetched sector maps to an excluded one — not evaluated, not in `rows`). A row's `sector` is the universe's GICS sector when set, else `gics_sector(data["sector"])`. `inputs[ticker]` is built from the converted data (USD price, USD years). `to_quote` runs in the final per-row loop after `lane_hints`. The summary log line: `log.info("screened by market: %s", ", ".join(f"{m} {n}" ...))`.
- Produces: `offscreen_row` evaluates `in_usd(data)` and calls `to_quote` last; sets `row["market"]`/`row["country"]` from `MARKETS` by matching the ticker's suffix (`.L` → ftse100's country "UK"; no suffix → "US"; `market` = the first `MARKETS` key with that suffix) *(controller's call: the row only needs the country for the note)*.

- [ ] **Step 1: Write the failing tests**

```python
def test_in_usd_converts_a_pence_quote_with_pound_statements(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: {"GBp": 1.30, "GBP": 1.30}.get(c))
    data = record(price=800.0, market_cap=2.0e9, enterprise_value=2.5e9, currency="GBp",
                  financial_currency="GBP", years=[{"revenue": 100.0, "shares": 10.0, ...}], quarters=[{"revenue": 25.0, ...}])
    out = market_data.in_usd(data)
    assert out["price"] == pytest.approx(10.4) and out["fx_quote"] == pytest.approx(0.013)
    assert out["market_cap"] == pytest.approx(2.6e9) and out["enterprise_value"] == pytest.approx(3.25e9)
    assert out["years"][0]["revenue"] == pytest.approx(130.0) and out["years"][0]["shares"] == 10.0
    assert out["quarters"][0]["revenue"] == pytest.approx(32.5)
    assert data["price"] == 800.0 and data["years"][0]["revenue"] == 100.0      # the input is untouched

def test_in_usd_converts_statements_at_the_financial_currency(...)   # USD quote, EUR statements: price unchanged, revenue × 1.1
def test_in_usd_passes_a_dollar_record_through(...)                  # fx_quote == fx_financial == 1.0, values equal
def test_in_usd_without_financial_currency_uses_the_quote_major_rate(...)   # GBp quote, financial_currency None → revenue × 1.30
def test_in_usd_raises_without_a_rate(monkeypatch):
    monkeypatch.setattr(market_data, "usd_rate", lambda c: None)
    with pytest.raises(market_data.DataUnavailable, match="no USD rate for SEK"): ...
def test_to_quote_round_trips_the_per_share_columns():
    row = {"price": 10.4, "value_base": 13.0, "value_low": 9.1, "value_high": 15.6, "market_cap": 2.6e9}
    out = screener.to_quote(row, {"fx_quote": 0.013, "quote_currency": "GBp"})
    assert out["price"] == pytest.approx(800.0) and out["value_base"] == pytest.approx(1000.0)
    assert out["market_cap"] == 2.6e9 and out["quote_currency"] == "GBp" and out["fx_quote"] == 0.013
def test_gics_sector_maps_yahoo_names(): assert market_data.gics_sector("Financial Services") == "Financials"; ... unmapped passes through; None → None
def test_fundamentals_stores_the_financial_currency_under_schema_7(...)   # FakeTicker info financialCurrency "GBP"
def test_screen_run_ranks_in_dollars_and_writes_quote_units(tmp_path, monkeypatch):
    # universe: AAA (sp500, Industrials) and DNLM.L (ftse100, sector None); patched fundamentals (USD and GBp records), usd_rate, superinvestors
    res = screener.run(paths, cfg, owned=set(), today="2026-11-05")
    row = {r["ticker"]: r for r in pd.read_csv(res["path"]).to_dict("records")}["DNLM.L"]
    assert row["market_cap"] == pytest.approx(gbp_mcap * 1.30) and row["price"] == pytest.approx(800.0)
    assert row["market"] == "ftse100" and row["country"] == "UK" and row["quote_currency"] == "GBp"
    assert row["sector"] == "Consumer Discretionary"          # from Yahoo's "Consumer Cyclical"
def test_screen_run_excludes_non_sp_tickers_by_yahoo_sector(...)
    # BARC.L cached sector "Financial Services" → not fetched (fundamentals fake records calls), in res["excluded"];
    # LAND.L no cache, fetched sector "Real Estate" → in res["excluded"], not in rows
def test_offscreen_row_gives_quote_units_for_a_london_ticker(...)   # price 800, market_cap USD, country "UK"
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Full suite and ruff** (tests that read `res["excluded"]` as a DataFrame need the list form; `tests/test_value_pipeline.py::test_sector_falls_back_to_the_cached_fundamentals` must still pass). — [ ] **Step 5: Commit** — `feat(screen): rank every market in dollars, keep per-share figures in the quote unit`

### Task 3: Daily prices without refetching statements

**Files:**
- Modify: `value_pipeline/market_data.py` (`fundamentals(ticker, paths, cache_days=7, price_days=None)`, new `_refresh_price`), `value_pipeline/screener.py` (`run` passes `cfg["screen"].get("price_days")`; throttles after a refresh), `value_pipeline/review.py` (`offscreen_row` passes it), `value_pipeline/config.py` (`"price_days": 1` in the screen defaults, `cache_days` stays 7), `value_pipeline/config.yaml` (`cache_days: 7`, `price_days: 1`, with comments)
- Test: `tests/test_nordnet_markets.py`

**Interfaces:**
- Produces: every full fetch writes `price_at` (ISO date). `fundamentals(..., price_days=None)`: with a fresh, schema-7, quarters-complete record, when `price_days` is an int and (`price_at` missing or older than `price_days` days), call `_refresh_price(ticker, data) -> dict` and write the record back; return it with `from_cache=True, price_refreshed=True`. `price_days=None` never refreshes (today's behaviour).
- Produces: `_refresh_price(ticker, data)`: `fi = yf.Ticker(ticker).fast_info`; `price = fi["last_price"]`, `mcap = fi["market_cap"]`; `closes = closes(ticker)`; `enterprise_value += mcap − old_mcap` only when both market caps are numbers; `price_at` = today; never touches `info`, `years`, `quarters`. Any exception → warning `price refresh failed for %s: %s`, the cached record returned unchanged (and not marked refreshed).
- Produces: `screener.run` throttles when `not data.get("from_cache") or data.get("price_refreshed")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_stale_price_is_refreshed_without_info(tmp_path, monkeypatch):
    # cache: schema 7, fetched_at today, price_at 3 days ago, price 10, market_cap 1e9, enterprise_value 1.2e9
    # FakeTicker: fast_info {"last_price": 12.0, "market_cap": 1.2e9}; property info raises AssertionError("info must not be read")
    monkeypatch.setattr(market_data, "closes", lambda t: [11.0, 12.0])
    data = market_data.fundamentals("ABC", paths, cache_days=7, price_days=1)
    assert data["price"] == 12.0 and data["market_cap"] == 1.2e9 and data["enterprise_value"] == pytest.approx(1.4e9)
    assert data["closes"] == [11.0, 12.0] and data["price_at"] == date.today().isoformat()
    assert data["from_cache"] is True and data["price_refreshed"] is True
    assert json.loads((paths.cache / "ABC.json").read_text())["price"] == 12.0
def test_fresh_price_makes_no_call(...)              # price_at today; yf.Ticker patched to raise → unchanged record, not refreshed
def test_record_without_price_at_is_refreshed(...)   # schema 7 record lacking price_at → refreshed
def test_old_schema_record_is_fetched_in_full_not_price_refreshed(...)   # schema 6 → full fetch path (info read), new record has price_at and financial_currency
def test_failed_refresh_serves_the_cached_record(...)   # fast_info raises → old price, warning in caplog, no price_refreshed
def test_ev_is_left_alone_when_a_market_cap_is_missing(...)   # old market_cap None → enterprise_value unchanged
def test_price_days_none_never_refreshes(...)
def test_screen_config_defaults(): assert config.DEFAULTS["screen"]["price_days"] == 1 and config.DEFAULTS["screen"]["cache_days"] == 7
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Full suite and ruff.** — [ ] **Step 5: Commit** — `feat(screen): refresh prices daily from fast_info, statements weekly`

### Task 4: Review note, pages, ACTIONS, README

**Files:**
- Modify: `value_pipeline/review.py:101-119` (`pipeline_note`), `value_pipeline/review.py` (`run`: pass `foreign=` to `actions_markdown`), `value_pipeline/render.py:26-41` (`actions_markdown(..., foreign: bool = False)`), `value_pipeline/web/views.py:115-135` (`_SCREEN_COLUMNS` + `market, country, quote_currency`; `screen_table` returns `"markets": sorted({r["country"] ...})`), `value_pipeline/web/templates/screen.html` (Market filter beside Sector on `data-market`; `Market` column after Ticker showing `r.country`), `value_pipeline/web/views.py:63` (`stock_history` returns `currency`: the latest review row's `currency`, None when unknown), `value_pipeline/web/templates/stock.html:4` (price line `Price 800.00 GBp` when a currency is known), `value_pipeline/README.md` (lines 5, 56, 64-66, 248-260, 360-362), `value_pipeline/backtest.py` report header sentence (one line: the backtest covers the S&P 500 and 400 only; non-US markets have no point-in-time filings) — check `reports/backtest/backtest_2026-10-24.md` for where the header is rendered.
- Test: `tests/test_nordnet_markets.py`, `tests/test_value_web.py`

**Interfaces:**
- Consumes: row keys `market`, `country`, `quote_currency` (Task 2).
- Produces: `pipeline_note(row)` starts with `UK (ftse100), quoted in GBp; screen figures in dollars` when `row["country"]` is set and `quote_currency` is not USD, with `US` rows unchanged (no prefix), followed by the existing parts.
- Produces: `actions_markdown(..., foreign=True)` adds, after the "These are suggestions" line: `Prices in each stock's quote currency; Nordnet charges FX and, in the UK and Ireland, stamp duty.` `review.run` passes `foreign=any(row["currency"] not in (None, "USD") for row in rows)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_pipeline_note_names_the_market_and_currency():
    note = review.pipeline_note({"country": "UK", "market": "ftse100", "quote_currency": "GBp", "hints": "compounder", "move_3m": -0.12})
    assert note.startswith("UK (ftse100), quoted in GBp; screen figures in dollars; lane hint compounder")
    assert not review.pipeline_note({"country": "US", "quote_currency": "USD", "hints": "compounder"}).startswith("US")
def test_actions_header_mentions_fx_for_foreign_names():   # foreign=True → sentence present; default → absent
def test_screen_page_shows_market_column_and_filter(...)   # CSV with market/country → "<option>UK</option>", a cell "UK"
def test_screen_page_renders_a_csv_without_market_columns(...)   # old CSV → 200, no exception, empty Market cell
def test_stock_page_shows_the_quote_currency(...)   # results.json currency "GBp" → "GBp" next to the price
def test_backtest_report_says_us_only(...)   # the report header contains "S&P 500 and 400 only"
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement** (templates and README included). — [ ] **Step 4: Full suite and ruff.** — [ ] **Step 5: Commit** — `feat(review): market and currency in the note, pages and ACTIONS; README`

### Task 5: Verification (controller's step, after the branch review)

- [ ] Before the merge: `python scripts/check_markets.py` (network); fix any market under 10 by pointing its `pages` at the constituents page, commit.
- [ ] After the merge, outside the 10:00/9:00 job window and with no review running (`pgrep -f "value_pipeline"`): `python -m value_pipeline screen` (first run fetches every new ticker, about an hour); read the per-market counts in the log; then `python -m value_pipeline review --tickers <two best non-US names> --dry-run` and `python -m value_pipeline daily --dry-run`; check the formula value, price and entry price are in the quote unit.
- [ ] Finish with superpowers:finishing-a-development-branch; push.
