# Small companies in the screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The screen covers Nordic main-market shares, the Oslo exchange and the S&P 600, with a lower size floor and a trading-value floor for those markets, and US small caps become candidates only through a fixed backtest verdict.

**Architecture:** `market_data` gains a Nasdaq Nordic API source and five markets; `screener.failed_filters` applies per-market floors from `filters.small_markets` and a liquidity check; the backtest rows carry their market; `review.candidate_markets` admits whole markets as candidates; `backtest` prints a small-company verdict for an `sp600`-only run.

**Tech Stack:** Python 3, pandas, requests, yfinance, pytest. Tests `uv run pytest -q`, lint `uv run ruff check .` (line length 100).

**Spec:** `docs/superpowers/specs/2026-11-06-small-companies-design.md`

## Global Constraints

- Nothing trades and no agent is called. No buying rule, prompt, sizing or `review.candidates` change.
- Rows of markets not in `filters.small_markets` are filtered exactly as today.
- Fixed values: floors `2.5e8` for `sto`, `cph`, `hel`, `ose`, `sp600`; `min_daily_value` `250000`; the verdict's thresholds are `bar_verdict`'s conditions 1 to 4 (20 cohorts, 7 calendar years, 60%, +2 points over the universe, 0 over SPY, three-year 0 over the universe).
- Tests never touch the network (build API payloads and HTML tables in the test, monkeypatch `requests.get` and `yfinance`).
- New config keys: a default in `config.DEFAULTS`, a commented entry in `value_pipeline/config.yaml`, a line in `value_pipeline/README.md`.
- Every commit message ends with the two trailer lines the dispatch gives.

## Review Focus

1. The Nasdaq API answering with a non-JSON page, an empty `rows` list or a 400: `fetch_market` raises (so `universe` keeps the cached rows); nothing writes an empty market into `universe.csv`. (Task 1)
2. A company in OMX Stockholm 30 that also appears in the `sto` list: it stays under `omxs30` with the $2 billion floor, not under `sto`. (Task 1)
3. A cached fundamentals record from before this change (no `average_volume`) for a large-cap market: it still passes or fails exactly as before. (Task 2)
4. A screen CSV from before this change (no `daily_value` column) read by `top_candidates`: nothing raises. (Tasks 2, 3)
5. `review.candidate_markets: [sp600]` with `candidate_countries` empty: every country is still allowed (the empty list keeps meaning "every country"). (Task 3)

---

### Task 1: The five new markets

**Files:** Modify `value_pipeline/market_data.py`, `value_pipeline/config.py` (`DEFAULTS["universe"]["indexes"]`), `value_pipeline/config.yaml`, `scripts/check_markets.py` (only if it needs a change to cover an API market), `value_pipeline/README.md`. Test: `tests/test_nordnet_markets.py` (extend; it holds the market tests).

**Interfaces:**
- `Market` gains `api: str | None = None` (Nasdaq Nordic market code: `"STO"`, `"CPH"`, `"HEL"`).
- `NASDAQ_NORDIC = "https://api.nasdaq.com/api/nordic/screener/shares?category=MAIN_MARKET&tableonly=false&market={code}"` and `NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}`.
- `share_class_key(symbol: str) -> str`: the symbol without a final space-separated token of one to four capital letters (`"ACRI B"` → `"ACRI"`, `"ALVO SDB"` → `"ALVO"`, `"NOKIA"` → `"NOKIA"`).
- `MARKETS` gains, after `tsxcomp`, in this order: `sto` (Sweden, api `STO`, `.ST`), `cph` (Denmark, api `CPH`, `.CO`), `hel` (Finland, api `HEL`, `.HE`), `ose` (Norway, Wikipedia `List_of_companies_listed_on_the_Oslo_Stock_Exchange`, `.OL`), `sp600` (US, Wikipedia `List_of_S%26P_600_companies`, GICS `"GICS Sector"`).
- `DEFAULTS["universe"]["indexes"]` and `config.yaml` list the five keys last, same order, with a comment in the yaml: small companies, own size floor (see `filters.small_markets`).

Steps:
- [ ] Tests first: an API payload with rows `{"symbol": "ERIC B", "fullName": "Ericsson B", "sector": "Telecommunications", "turnover": "5,000,000"}`, `{"symbol": "ERIC A", "fullName": "Ericsson A", "sector": "Telecommunications", "turnover": "40,000"}`, `{"symbol": "AAK", "fullName": "AAK", "sector": "Consumer Staples", "turnover": "17,633,939"}` plus enough others to pass `MIN_TICKERS` gives tickers including `ERIC-B.ST` and `AAK.ST` and not `ERIC-A.ST`; market `sto`, country Sweden. A response that is not JSON, has no `rows`, or has status 400 raises. A turnover of `""` counts as 0. `share_class_key` cases above. The Oslo table with `Ticker` "OSE: ABG" gives `ABG.OL`. An `sp600` table keeps its GICS sector. `universe` with `omxs30` listing "Ericsson B" as `ERIC-B.ST` and `sto` listing the same company keeps only the `omxs30` row. The default index list ends with the five keys.
- [ ] Implement; `README.md`: the five markets, where each list comes from, and that First North and Iceland are not covered.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: Nordic main markets, Oslo and the S&P 600 in the screen`.

### Task 2: Size and trading-value floors for the small-company markets

**Files:** Modify `value_pipeline/market_data.py` (`fundamentals`), `value_pipeline/screener.py` (`evaluate` or the screen loop, `failed_filters`), `value_pipeline/backtest.py` (`_universe`, `_ticker_rows`/`snapshot`, report limits), `value_pipeline/config.py`, `value_pipeline/config.yaml`, `value_pipeline/README.md`. Test: `tests/test_small_markets.py` (new), `tests/test_lane_backtest.py` or `tests/test_backtest.py` for the backtest part.

**Interfaces:**
- `fundamentals` stores `"average_volume": info.get("averageVolume")`. No `CACHE_SCHEMA` change.
- The screen row gains `"daily_value"`: `average_volume * price` with the dollar price from `in_usd` (None when either is missing).
- `DEFAULTS["filters"]["small_markets"] = {"sto": 2.5e8, "cph": 2.5e8, "hel": 2.5e8, "ose": 2.5e8, "sp600": 2.5e8}` and `DEFAULTS["filters"]["min_daily_value"] = 250000`. `validate_config`: `small_markets` is a dict whose keys are known market keys and whose values are positive numbers.
- `failed_filters(row, filters, check_liquidity: bool = True)`: a row whose `market` is in `small_markets` uses that floor for "market cap"; such a row, when `check_liquidity`, fails "liquidity" if `daily_value` is missing or below `min_daily_value` (at the floor passes). Other rows: unchanged.
- Backtest: `_universe` returns `(ticker, sector, market)`; `snapshot(..., market=None)` sets `row["market"]` before `failed_filters(row, cfg["filters"], check_liquidity=False)`. The report's limits gain, when any listed small market is in the run: "The trading-value floor cannot be applied to past dates (no volume history), so small companies are filtered on size only."

Steps:
- [ ] Tests first: the `failed_filters` cases in the spec's Testing section (`sto` $300M passes size, $200M fails; `sp500` $1B fails; liquidity missing fails for `sto` not for `sp500`; exactly $250,000 passes; `check_liquidity=False` skips it); `fundamentals` stores `average_volume` (monkeypatched `yf.Ticker`); a cached record without `average_volume` gives `daily_value` None and a large-cap row's reasons unchanged; a backtest snapshot row carries `market` and an `sp600` row at $300M passes size; `validate_config` rejects an unknown key and a zero floor; the shipped yaml loads and validates.
- [ ] Implement; README lines for both settings and the backtest limit.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: size and trading-value floors for small-company markets`.

### Task 3: US small caps as candidates, and their verdict

**Files:** Modify `value_pipeline/screener.py` (`_in_countries` / candidate filter), `value_pipeline/review.py` (`preview`, printed lines), `value_pipeline/web/templates/run_preview.html`, `value_pipeline/backtest.py` (`small_cap_verdict`, report section), `value_pipeline/cli.py` (print the section), `value_pipeline/config.py`, `value_pipeline/config.yaml`, `value_pipeline/README.md`. Test: `tests/test_candidate_countries.py`, `tests/test_lane_backtest.py`.

**Interfaces:**
- `DEFAULTS["review"]["candidate_markets"] = []`; yaml ships `[]` with a comment that `sp600` is added only if the small-company verdict supports it. `validate_config`: a list of known market keys.
- `screener.top_candidates(..., countries=None, markets=None)` and `run_rate_skips(..., countries=None, markets=None)`: a row qualifies when the country filter is off (empty `countries`), or its country is listed, or its `market` is in `markets`. Compounder and inflection candidates still ignore both lists.
- `review.preview` passes `rcfg.get("candidate_markets") or []`; its result gains `"candidate_markets"`. The printed line becomes `"Value candidates from: <countries>; markets: <markets> (review.candidate_countries, review.candidate_markets); compounder candidates from every market."`, with the `; markets: ...` part left out when the list is empty (so today's line is unchanged).
- `backtest.small_cap_verdict(one_year: dict, three_year: dict | None, rule: str) -> dict`: conditions 1 to 4 of `bar_verdict`, sharing its code (refactor `bar_verdict` so both read one implementation; `bar_verdict`'s outputs must not change). The sentence ends "supports adding sp600 to review.candidate_markets." or "does not support adding sp600 to review.candidate_markets. Failed: (n) ...".
- The report, when `indexes == ["sp600"]`, gains `## Small-company verdict` after the summary: the four conditions in words, the `magic_formula` sentence, then the `value` rule's sentence "for information", then the spec's survivorship limit. The CLI prints that section after the verdict.

Steps:
- [ ] Tests first: a US `sp600` row is a candidate with `markets=["sp600"]` and not without; a US `sp500` row stays out with `markets=["sp600"]`; empty countries with `markets=["sp600"]` still allows every country; `run_rate_skips` agrees; validation rejects `["sp601"]`; the printed line with and without markets; `small_cap_verdict` each condition failing alone and all passing; `bar_verdict` results unchanged on the existing tests; the report prints the section for an `sp600`-only result and not for the default lists.
- [ ] Implement; README: `candidate_markets`, the fixed rule, and that Claude adds `sp600` only if it passes.
- [ ] `uv run pytest -q && uv run ruff check .`; commit `feat: candidate markets and the small-company verdict`.
