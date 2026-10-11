# Value Pipeline Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local website (`uv run python -m value_pipeline web`) that does everything the `value_pipeline` CLI does: record trades, show the portfolio and its history, browse reviews and reports, run screens, reviews and watches, and edit settings.

**Architecture:** FastAPI serves server-rendered Jinja pages that call the existing pipeline functions. A small `operations` module becomes the single place that records trades and computes the portfolio view, shared by the CLI and the website. Long jobs run one at a time in a background thread that logs to a file.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, Jinja2, python-multipart, pandas; pytest with FastAPI's `TestClient` (httpx is already installed).

**Spec:** `docs/superpowers/specs/2026-10-11-value-pipeline-web-design.md`

## Global Constraints

- Bind to `127.0.0.1` only; default port `8765`.
- The website must never place trades; it only records them.
- Every POST carries the per-process token; a POST without it gets 403.
- Pages make no external requests (no CDN, fonts or scripts from the internet). CSS and JS are inline; charts are inline SVG.
- All writes to `portfolio.json` and `trades.csv` go through `value_pipeline.portfolio.save` / `append_trade`, so they are backed up.
- One background job at a time; a review needs an explicit confirmation of its preview, and `max_cost_per_review` still applies.
- Lint with `ruff check .` (line length 100 is not enforced; E501 is ignored). Never run `ruff format`.
- Tests must not touch the network (`tests/conftest.py` blocks it) and must not assume UTC.
- Commit messages: Conventional Commits with a scope, plain English, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A ticker typed in lower case or with spaces (`" hrb "`) must be recorded as `HRB`, not a new position. Tested in Task 1.
2. A number typed with a comma or text (`"1,5"`, `"abc"`) in a trade form must re-show the form with a message, not a 422/500 page. Tested in Task 4.
3. "Undo" after something else changed the files (a review stored new ratings) must refuse instead of rolling back the review. Tested in Task 1.
4. A report URL containing `../` must not serve files outside `data/reports`. Tested in Task 5.
5. With no portfolio yet, the dashboard and trade page must show a "start portfolio" form instead of an error. Tested in Tasks 3 and 4.

## File Structure

| File | Responsibility |
|---|---|
| `value_pipeline/operations.py` (new) | Record buy/sell/deposit/init, undo the last entry, the portfolio view and realised gains: shared by CLI and web |
| `value_pipeline/history.py` (new) | Portfolio value and SPY shadow value over time (pure, plus a fetch wrapper) |
| `value_pipeline/portfolio.py` (modify) | `save` returns the backup folder it made |
| `value_pipeline/review.py` (modify) | `preview()`: tickers, run count, estimate, cap |
| `value_pipeline/cli.py` (modify) | Use `operations` and `review.preview`; new `web` subcommand |
| `value_pipeline/web/__init__.py` (new) | `create_app` re-export |
| `value_pipeline/web/app.py` (new) | FastAPI app factory and routes |
| `value_pipeline/web/views.py` (new) | Data for pages: reviews index, stock history, screen table, next review date |
| `value_pipeline/web/jobs.py` (new) | Background job runner |
| `value_pipeline/web/charts.py` (new) | Inline SVG line chart |
| `value_pipeline/web/templates/*.html` (new) | Jinja templates |
| `tests/test_value_operations.py` (new) | Tasks 1–2 |
| `tests/test_value_web.py` (new) | Tasks 3–8 |
| `pyproject.toml` (modify) | Dependencies and template package data |

---

### Task 1: Shared operations (record trades, undo, portfolio view)

**Files:**
- Create: `value_pipeline/operations.py`
- Modify: `value_pipeline/portfolio.py` (`save` returns the backup path)
- Modify: `value_pipeline/cli.py` (`cmd_init`, `cmd_deposit`, `cmd_buy`, `cmd_sell`, `cmd_status` call `operations`)
- Test: `tests/test_value_operations.py`

**Interfaces:**
- Consumes: `portfolio.load/save/apply_buy/apply_sell/apply_deposit/append_trade/read_trades/backup`, `review.latest_result`, `benchmark.compare`, `market_data.latest_price`.
- Produces:
  - `operations.init_portfolio(paths, cash: float, currency="USD", on: str|None=None, force=False) -> Portfolio`
  - `operations.record_buy(paths, ticker, shares, price, fees=0.0, on=None, note="") -> tuple[Position, Portfolio]`
  - `operations.record_sell(paths, ticker, shares, price, fees=0.0, on=None, note="") -> tuple[float, Portfolio]` (realised gain)
  - `operations.record_deposit(paths, amount, on=None) -> Portfolio`
  - `operations.undo_info(paths) -> dict|None` (`{"description": str}`)
  - `operations.undo_last(paths) -> str` (description); raises `operations.UndoUnavailable`
  - `operations.portfolio_view(paths, price_fn=market_data.latest_price, benchmark_fn=benchmark.compare) -> dict` with keys `currency, cash, total, positions (list of dicts: ticker, shares, avg_price, price, value, gain, weight, last_rating, intrinsic_value, margin_of_safety, days_held, thesis), comparison`
  - `operations.realised_gains(trades: list[dict]) -> list[float|None]` (one per trade, None for buys)
  - `operations.PortfolioExists` (raised by `init_portfolio` without `force`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_value_operations.py
"""Recording trades, undo and the portfolio view, shared by the CLI and the website."""

import pytest

from value_pipeline import operations as ops
from value_pipeline import portfolio as pfmod
from value_pipeline.config import Paths


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path)
    ops.init_portfolio(p, 1000.0, on="2026-10-01")
    return p


@pytest.mark.unit
def test_tickers_are_normalised_and_recorded_with_the_trade(paths):
    ops.record_buy(paths, " hrb ", 2, 44.0, fees=1.0, on="2026-10-02", note="first")
    pf = pfmod.load(paths)
    assert [p.ticker for p in pf.positions] == ["HRB"]
    trade = pfmod.read_trades(paths)[0]
    assert (trade["ticker"], trade["action"], trade["note"]) == ("HRB", "BUY", "first")
    ops.record_buy(paths, "HRB", 1, 40.0, on="2026-10-03")
    assert len(pfmod.load(paths).positions) == 1


@pytest.mark.unit
def test_init_refuses_to_overwrite_without_force(paths):
    with pytest.raises(ops.PortfolioExists):
        ops.init_portfolio(paths, 50.0)
    assert ops.init_portfolio(paths, 50.0, force=True).cash == 50.0


@pytest.mark.unit
def test_undo_restores_the_state_before_the_last_entry(paths):
    ops.record_buy(paths, "HRB", 2, 44.0, on="2026-10-02")
    ops.record_sell(paths, "HRB", 1, 50.0, on="2026-10-03")
    assert ops.undo_info(paths)["description"].startswith("SELL 1 HRB")
    ops.undo_last(paths)
    pf = pfmod.load(paths)
    assert pf.position("HRB").shares == 2 and pf.cash == pytest.approx(912.0)
    assert [t["action"] for t in pfmod.read_trades(paths)] == ["BUY"]
    assert ops.undo_info(paths) is None           # one level only
    with pytest.raises(ops.UndoUnavailable):
        ops.undo_last(paths)


@pytest.mark.unit
def test_undo_of_the_first_trade_removes_the_trade_log(paths):
    ops.record_buy(paths, "HRB", 2, 44.0)
    ops.undo_last(paths)
    assert pfmod.read_trades(paths) == [] and pfmod.load(paths).cash == 1000.0


@pytest.mark.unit
def test_undo_refuses_after_something_else_changed_the_files(paths):
    ops.record_buy(paths, "HRB", 2, 44.0)
    pf = pfmod.load(paths)
    pf.position("HRB").last_rating = "Hold"        # e.g. a review stored a rating
    pfmod.save(paths, pf)
    with pytest.raises(ops.UndoUnavailable, match="changed"):
        ops.undo_last(paths)
    assert pfmod.load(paths).position("HRB").last_rating == "Hold"


@pytest.mark.unit
def test_portfolio_view_values_positions_with_given_prices(paths):
    ops.record_buy(paths, "HRB", 2, 40.0, on="2026-10-02")
    pf = pfmod.load(paths)
    pf.position("HRB").intrinsic_value_estimate = 55.0
    pfmod.save(paths, pf)
    view = ops.portfolio_view(paths, price_fn={"HRB": 44.0}.get, benchmark_fn=lambda *a: None)
    pos = view["positions"][0]
    assert view["total"] == pytest.approx(920 + 88)
    assert pos["value"] == pytest.approx(88) and pos["gain"] == pytest.approx(0.10)
    assert pos["weight"] == pytest.approx(88 / 1008)
    assert pos["margin_of_safety"] == pytest.approx((55 - 44) / 55)


@pytest.mark.unit
def test_realised_gains_follow_average_cost_with_fees():
    trades = [
        {"date": "2026-01-02", "ticker": "A", "action": "BUY", "shares": "2", "price": "10", "fees": "2"},
        {"date": "2026-01-03", "ticker": "A", "action": "BUY", "shares": "2", "price": "20", "fees": "0"},
        {"date": "2026-01-04", "ticker": "A", "action": "SELL", "shares": "1", "price": "30", "fees": "1"},
    ]
    gains = ops.realised_gains(trades)
    assert gains[:2] == [None, None]
    assert gains[2] == pytest.approx(30 - 1 - (62 / 4))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_value_operations.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'value_pipeline.operations'`

- [ ] **Step 3: Make `save` return its backup folder**

In `value_pipeline/portfolio.py` replace `save`:

```python
def save(paths: Paths, pf: Portfolio) -> Path | None:
    """Write portfolio.json; returns the backup of the previous files (None on the first write)."""
    made = backup(paths)
    paths.root.mkdir(parents=True, exist_ok=True)
    tmp = paths.portfolio.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pf.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(paths.portfolio)
    return made
```

- [ ] **Step 4: Write `value_pipeline/operations.py`**

```python
"""Recording trades and reading the portfolio: one implementation for the CLI and the website.

Nothing here trades. It records what you did at your broker, keeps a one-step
undo, and values the holdings.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date

from value_pipeline import benchmark, market_data, review
from value_pipeline import portfolio as pfmod
from value_pipeline.config import Paths

_UNDO = "undo.json"


class UndoUnavailable(Exception):
    """There is no last entry to undo, or undoing it would roll back something else."""


class PortfolioExists(Exception):
    """init_portfolio would replace an existing portfolio."""


def _fingerprint(paths: Paths) -> str:
    digest = hashlib.sha256()
    for f in (paths.portfolio, paths.trades):
        digest.update(f.read_bytes() if f.exists() else b"-")
    return digest.hexdigest()


def _remember(paths: Paths, backup_dir, description: str) -> None:
    """Note how to undo the entry just written: the backup taken before it and the files' state after."""
    (paths.root / _UNDO).write_text(json.dumps({
        "backup": str(backup_dir) if backup_dir else None,
        "description": description,
        "fingerprint": _fingerprint(paths),
    }), encoding="utf-8")


def init_portfolio(paths: Paths, cash: float, currency: str = "USD", on: str | None = None,
                   force: bool = False) -> pfmod.Portfolio:
    if paths.portfolio.exists() and not force:
        raise PortfolioExists(f"{paths.portfolio} already exists; start over with force (a backup is kept)")
    pf = pfmod.Portfolio(cash=0.0, currency=currency)
    pfmod.apply_deposit(pf, cash, on=on)
    pfmod.save(paths, pf)
    (paths.root / _UNDO).unlink(missing_ok=True)
    return pf


def record_buy(paths: Paths, ticker: str, shares: float, price: float, fees: float = 0.0,
               on: str | None = None, note: str = "") -> tuple[pfmod.Position, pfmod.Portfolio]:
    pf = pfmod.load(paths)
    ticker, on = ticker.strip().upper(), on or date.today().isoformat()
    found = review.latest_result(paths, ticker) if pf.position(ticker) is None else None
    held = pfmod.apply_buy(pf, ticker, shares, price, fees, on=on,
                           thesis=(found or {}).get("thesis", ""),
                           intrinsic_value=(found or {}).get("intrinsic_value"))
    before = pfmod.save(paths, pf)
    pfmod.append_trade(paths, {"date": on, "ticker": ticker, "action": "BUY", "shares": shares,
                               "price": price, "fees": fees, "note": note})
    _remember(paths, before, f"BUY {shares:g} {ticker} @ {price:,.2f} on {on}")
    return held, pf


def record_sell(paths: Paths, ticker: str, shares: float, price: float, fees: float = 0.0,
                on: str | None = None, note: str = "") -> tuple[float, pfmod.Portfolio]:
    pf = pfmod.load(paths)
    ticker, on = ticker.strip().upper(), on or date.today().isoformat()
    gain = pfmod.apply_sell(pf, ticker, shares, price, fees)
    before = pfmod.save(paths, pf)
    pfmod.append_trade(paths, {"date": on, "ticker": ticker, "action": "SELL", "shares": shares,
                               "price": price, "fees": fees, "note": note})
    _remember(paths, before, f"SELL {shares:g} {ticker} @ {price:,.2f} on {on}")
    return gain, pf


def record_deposit(paths: Paths, amount: float, on: str | None = None) -> pfmod.Portfolio:
    pf = pfmod.load(paths)
    pfmod.apply_deposit(pf, amount, on=on)
    before = pfmod.save(paths, pf)
    kind = "DEPOSIT" if amount > 0 else "WITHDRAW"
    _remember(paths, before, f"{kind} {abs(amount):,.2f} on {on or date.today().isoformat()}")
    return pf


def undo_info(paths: Paths) -> dict | None:
    f = paths.root / _UNDO
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def undo_last(paths: Paths) -> str:
    """Restore the files as they were before the last recorded entry; one level only."""
    info = undo_info(paths)
    if info is None or not info.get("backup"):
        raise UndoUnavailable("Nothing to undo.")
    if info["fingerprint"] != _fingerprint(paths):
        raise UndoUnavailable("The portfolio changed since that entry (a review or another edit), "
                              "so undoing it would roll that back too. Record a correcting trade instead.")
    pfmod.backup(paths)
    saved = pfmod.Path(info["backup"])
    for f in (paths.portfolio, paths.trades):
        if (saved / f.name).exists():
            shutil.copy2(saved / f.name, f)
        else:
            f.unlink(missing_ok=True)
    (paths.root / _UNDO).unlink()
    return info["description"]


def realised_gains(trades: list[dict]) -> list[float | None]:
    """Each sale's gain against the average cost (fees included) at the time; None for buys."""
    held: dict[str, tuple[float, float]] = {}   # ticker -> (shares, cost)
    gains: list[float | None] = []
    for t in trades:
        shares, price, fees = float(t["shares"]), float(t["price"]), float(t.get("fees") or 0)
        n, cost = held.get(t["ticker"], (0.0, 0.0))
        if t["action"] == "BUY":
            held[t["ticker"]] = (n + shares, cost + shares * price + fees)
            gains.append(None)
        else:
            avg = cost / n if n else 0.0
            gains.append(shares * price - fees - shares * avg)
            held[t["ticker"]] = (n - shares, cost - shares * avg)
    return gains


def portfolio_view(paths: Paths, price_fn=market_data.latest_price, benchmark_fn=benchmark.compare) -> dict:
    """The holdings valued at current prices, with weights, gains and the SPY comparison."""
    pf = pfmod.load(paths)
    today = date.today()
    rows = []
    for p in pf.positions:
        price = price_fn(p.ticker)
        value = p.shares * (price if price is not None else p.avg_price)
        iv = p.intrinsic_value_estimate
        rows.append({
            "ticker": p.ticker, "shares": p.shares, "avg_price": p.avg_price, "price": price,
            "value": value, "gain": price / p.avg_price - 1 if price else None,
            "last_rating": p.last_rating, "intrinsic_value": iv,
            "margin_of_safety": (iv - price) / iv if iv and price else None,
            "days_held": (today - date.fromisoformat(p.first_bought)).days, "thesis": p.thesis,
        })
    total = pf.cash + sum(r["value"] for r in rows)
    for r in rows:
        r["weight"] = r["value"] / total if total else None
    return {
        "currency": pf.currency, "cash": pf.cash, "total": total, "positions": rows,
        "comparison": benchmark_fn(total, pf.deposits, pfmod.read_trades(paths)),
    }
```

Note: `pfmod.Path` is `pathlib.Path` imported in `portfolio.py`; import `Path` from `pathlib` directly instead if preferred: `from pathlib import Path` and use `Path(info["backup"])`.

- [ ] **Step 5: Use `operations` in the CLI**

In `value_pipeline/cli.py` replace `cmd_init`, `cmd_deposit`, `cmd_buy`, `cmd_sell` and the computation part of `cmd_status`:

```python
from value_pipeline import operations as ops


def cmd_init(args, paths: Paths, cfg) -> None:
    try:
        ops.init_portfolio(paths, args.cash, args.currency, on=args.date, force=args.force)
    except ops.PortfolioExists as exc:
        raise SystemExit(str(exc)) from exc
    console.print(f"Started a portfolio with {args.cash:,.2f} {args.currency} cash in {paths.root}")


def cmd_deposit(args, paths: Paths, cfg) -> None:
    pf = ops.record_deposit(paths, args.amount, on=args.date)
    console.print(f"Cash is now {pf.cash:,.2f} {pf.currency}")


def cmd_buy(args, paths: Paths, cfg) -> None:
    held, pf = ops.record_buy(paths, args.ticker, args.shares, args.price, args.fees, on=args.date, note=args.note)
    console.print(f"Bought {args.shares:g} {held.ticker} @ {args.price:,.2f}. Now {held.shares:g} sh, "
                  f"avg {held.avg_price:,.2f}. Cash {pf.cash:,.2f}")


def cmd_sell(args, paths: Paths, cfg) -> None:
    gain, pf = ops.record_sell(paths, args.ticker, args.shares, args.price, args.fees, on=args.date, note=args.note)
    left = pf.position(args.ticker.strip().upper())
    console.print(f"Sold {args.shares:g} {args.ticker.upper()} @ {args.price:,.2f} (realised {gain:+,.2f}). "
                  f"{'Now ' + format(left.shares, 'g') + ' sh' if left else 'Position closed'}. Cash {pf.cash:,.2f}")


def cmd_status(args, paths: Paths, cfg) -> None:
    view = ops.portfolio_view(paths)
    table = Table(title=f"Portfolio {date.today().isoformat()} ({view['currency']})")
    for col in ("Ticker", "Shares", "Avg", "Price", "Value", "Gain", "Weight", "Rating", "Days"):
        table.add_column(col, justify="left" if col in ("Ticker", "Rating") else "right")
    total = view["total"]
    for r in view["positions"]:
        table.add_row(r["ticker"], f"{r['shares']:g}", f"{r['avg_price']:,.2f}",
                      f"{r['price']:,.2f}" if r["price"] else "no price", f"{r['value']:,.2f}",
                      f"{r['gain']:+.1%}" if r["gain"] is not None else "–",
                      f"{r['weight']:.1%}" if r["weight"] is not None else "–", r["last_rating"] or "–",
                      str(r["days_held"]))
    table.add_row("Cash", "", "", "", f"{view['cash']:,.2f}", "", f"{view['cash'] / total:.1%}" if total else "–", "", "")
    console.print(table)
    console.print(f"Total value: [bold]{total:,.2f}[/bold] {view['currency']}")
    cmp = view["comparison"]
    if cmp:
        console.print(f"Since {cmp['since']}: portfolio {cmp['portfolio_return']:+.1%} vs SPY {cmp['spy_return']:+.1%} "
                      f"(same cash flows; {cmp['invested']:,.2f} put in)")
```

Remove imports that become unused (`benchmark`, `market_data`) if ruff reports them.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_value_operations.py tests/test_value_pipeline.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass; `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add value_pipeline/operations.py value_pipeline/portfolio.py value_pipeline/cli.py tests/test_value_operations.py
git commit -m "refactor(value_pipeline): record trades and value the portfolio in one shared module

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Review preview and portfolio value history

**Files:**
- Modify: `value_pipeline/review.py` (add `preview`, use it in `run`)
- Create: `value_pipeline/history.py`
- Test: `tests/test_value_operations.py` (append)

**Interfaces:**
- Consumes: `review.select_tickers`, `screener.top_candidates`, `portfolio.load/read_trades`, `market_data.closes_between`.
- Produces:
  - `review.preview(paths, cfg, only: list[str]|None=None) -> dict` with keys `tickers: list[str], n_held: int, n_new: int, estimate: float, cost_per_run: float, limit: float, blocked: bool`
  - `history.value_series(deposits, trades, closes: dict[str, pd.Series], spy: pd.Series, days: list[date]) -> list[dict]` with keys `date: str, portfolio: float, spy: float`
  - `history.fetch_value_series(paths, step_days=7) -> list[dict]` (network; returns `[]` on any failure)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_value_operations.py`)

```python
from datetime import date

import pandas as pd

from value_pipeline import history, review
from value_pipeline.config import DEFAULTS


@pytest.mark.unit
def test_review_preview_counts_runs_and_applies_the_cap(paths):
    ops.record_buy(paths, "HRB", 1, 44.0)
    cfg = {**DEFAULTS, "review": {**DEFAULTS["review"], "cost_per_run": 2.0, "max_cost_per_review": 5.0}}
    p = review.preview(paths, cfg, only=["hog", "ingr"])
    assert (p["tickers"], p["n_held"], p["n_new"], p["estimate"], p["blocked"]) == (["HOG", "INGR"], 0, 2, 4.0, False)
    p = review.preview(paths, cfg, only=["HRB", "A", "B"])
    assert p["blocked"] and p["estimate"] == 6.0
    p = review.preview(paths, cfg)          # no screen yet: holdings only
    assert p["tickers"] == ["HRB"]


@pytest.mark.unit
def test_value_series_replays_cash_shares_and_the_spy_shadow():
    deposits = [{"date": "2026-01-02", "amount": 1000}]
    trades = [{"date": "2026-01-05", "ticker": "A", "action": "BUY", "shares": "10", "price": "50", "fees": "0"}]
    idx = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 9)]
    closes = {"A": pd.Series([50.0, 50.0, 60.0], index=idx)}
    spy = pd.Series([100.0, 100.0, 110.0], index=idx)
    s = history.value_series(deposits, trades, closes, spy, idx)
    assert [round(x["portfolio"], 2) for x in s] == [1000.0, 1000.0, 1100.0]
    assert [round(x["spy"], 2) for x in s] == [1000.0, 1000.0, 1050.0]   # 500 of SPY at 100 -> 110
    assert s[0]["date"] == "2026-01-02"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_value_operations.py -q -k "preview or value_series"`
Expected: FAIL with `AttributeError: module 'value_pipeline.review' has no attribute 'preview'`

- [ ] **Step 3: Add `review.preview` and use it in `run`**

In `value_pipeline/review.py` add above `run`:

```python
def preview(paths: Paths, cfg: dict, only: list[str] | None = None) -> dict:
    """What a review would run and cost, without running anything."""
    rcfg = cfg["review"]
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    candidates = [] if only else screener.top_candidates(paths, owned, rcfg["candidates"])
    tickers, n_held, n_new = select_tickers([p.ticker for p in pf.positions], [c["ticker"] for c in candidates], only)
    estimate = len(tickers) * rcfg["cost_per_run"]
    return {"tickers": tickers, "n_held": n_held, "n_new": n_new, "estimate": estimate,
            "cost_per_run": rcfg["cost_per_run"], "limit": rcfg["max_cost_per_review"],
            "blocked": estimate > rcfg["max_cost_per_review"]}
```

In `run`, replace the block from `pf = pfmod.load(paths)` through the two `print(...)` lines with:

```python
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    plan_ = preview(paths, cfg, only)
    tickers = plan_["tickers"]
    if not only and plan_["n_new"] == 0:
        print("No screen found (or no candidates); reviewing holdings only. Run `screen` first for new ideas.")
    if not tickers:
        raise SystemExit("Nothing to review: no holdings and no screen candidates.")

    estimate = plan_["estimate"]
    print(f"Review {today}: {len(tickers)} runs ({plan_['n_held']} holdings + {plan_['n_new']} candidates): "
          f"{', '.join(tickers)}")
    print(f"Estimated cost: ${estimate:,.2f} (${rcfg['cost_per_run']:,.2f}/run, limit ${rcfg['max_cost_per_review']:,.2f})")
```

Keep the rest of `run` unchanged (`estimate > rcfg["max_cost_per_review"]` still raises `CostLimitExceeded`).

- [ ] **Step 4: Write `value_pipeline/history.py`**

```python
"""Portfolio value over time against SPY bought with the same cash flows, for the dashboard chart."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from value_pipeline import market_data
from value_pipeline import portfolio as pfmod
from value_pipeline.config import Paths

log = logging.getLogger(__name__)


def _asof(series: pd.Series | None, day: date) -> float | None:
    if series is None or series.empty:
        return None
    before = series[series.index <= day]
    return float(before.iloc[-1]) if not before.empty else None


def value_series(deposits: list[dict], trades: list[dict], closes: dict[str, pd.Series],
                 spy: pd.Series, days: list[date]) -> list[dict]:
    """Value of the portfolio and of the SPY shadow on each day in ``days``."""
    events = sorted(
        [(d["date"], "deposit", d) for d in deposits] + [(t["date"], "trade", t) for t in trades],
        key=lambda e: e[0],
    )
    cash = spy_cash = spy_shares = 0.0
    shares: dict[str, float] = {}
    last_price: dict[str, float] = {}
    out, i = [], 0
    for day in days:
        while i < len(events) and date.fromisoformat(events[i][0]) <= day:
            when, kind, e = events[i]
            if kind == "deposit":
                cash += float(e["amount"])
                spy_cash += float(e["amount"])
            else:
                n, price, fees = float(e["shares"]), float(e["price"]), float(e.get("fees") or 0)
                amount = n * price + fees if e["action"] == "BUY" else n * price - fees
                sign = 1 if e["action"] == "BUY" else -1
                shares[e["ticker"]] = shares.get(e["ticker"], 0.0) + sign * n
                last_price[e["ticker"]] = price
                cash -= sign * amount
                spy_price = _asof(spy, date.fromisoformat(when))
                if spy_price:
                    spy_shares += sign * amount / spy_price
                    spy_cash -= sign * amount
            i += 1
        held = 0.0
        for t, n in shares.items():
            p = _asof(closes.get(t), day) or last_price.get(t, 0.0)
            held += n * p
        spy_now = _asof(spy, day) or 0.0
        out.append({"date": day.isoformat(), "portfolio": cash + held, "spy": spy_cash + spy_shares * spy_now})
    return out


def fetch_value_series(paths: Paths, step_days: int = 7) -> list[dict]:
    """The series from the first deposit to today, every ``step_days``; [] when prices are unavailable."""
    try:
        pf = pfmod.load(paths)
        trades = pfmod.read_trades(paths)
        if not pf.deposits:
            return []
        start = min(d["date"] for d in pf.deposits)
        tickers = sorted({t["ticker"] for t in trades})
        closes = {t: market_data.closes_between(t, start) for t in tickers}
        spy = market_data.closes_between("SPY", start)
        first, today = date.fromisoformat(start), date.today()
        days = [first + timedelta(days=k) for k in range(0, (today - first).days, step_days)] + [today]
        return value_series(pf.deposits, trades, closes, spy, days)
    except Exception as exc:  # noqa: BLE001 - the chart is optional
        log.warning("value history unavailable: %s", exc)
        return []
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_value_operations.py tests/test_value_pipeline.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add value_pipeline/review.py value_pipeline/history.py tests/test_value_operations.py
git commit -m "feat(value_pipeline): preview a review's cost and chart value over time

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Web app skeleton, token, dashboard and the `web` command

**Files:**
- Modify: `pyproject.toml` (dependencies; package data)
- Create: `value_pipeline/web/__init__.py`, `value_pipeline/web/app.py`, `value_pipeline/web/charts.py`, `value_pipeline/web/views.py`
- Create: `value_pipeline/web/templates/base.html`, `value_pipeline/web/templates/dashboard.html`, `value_pipeline/web/templates/start.html`
- Modify: `value_pipeline/cli.py` (`web` subcommand)
- Test: `tests/test_value_web.py`

**Interfaces:**
- Consumes: `operations.portfolio_view/init_portfolio/PortfolioExists`, `history.fetch_value_series`, `watch` report files (`data/reports/watch_*.md`).
- Produces:
  - `web.create_app(paths: Paths, *, config_path: Path = CONFIG_PATH, price_fn=market_data.latest_price, series_fn=history.fetch_value_series, benchmark_fn=benchmark.compare, runner=None) -> FastAPI` (`runner` is created in Task 7; default `None` until then)
  - `app.state.token: str`; templates receive `token`.
  - `views.next_review_date(today: date) -> date` (first Saturday of this month if not past, else next month's)
  - `views.latest_watch(paths) -> tuple[str, str] | None` (date, markdown)
  - `charts.line_chart(labels: list[str], lines: dict[str, list[float]], height=180) -> str` (SVG markup)
  - Helper `form_value(form, name) -> str` and `parse_number(text, field) -> float` raising `FormError` (used by Tasks 4 and 8)

- [ ] **Step 1: Add dependencies**

In `pyproject.toml` `dependencies`, add (alphabetical position):

```toml
    "fastapi>=0.115",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "uvicorn>=0.30",
```

and change the package data entry to:

```toml
value_pipeline = ["config.yaml", "web/templates/*.html"]
```

Run: `.venv/bin/pip install -q -e ".[dev]"` then `uv run python -c "import fastapi, uvicorn, jinja2, multipart"`
Expected: no output (imports succeed)

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_value_web.py
"""The local website: pages, forms, files and jobs (no network, agents faked)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from value_pipeline import operations as ops
from value_pipeline.config import Paths
from value_pipeline.web import create_app, views


def _client(tmp_path, **kw):
    paths = Paths(tmp_path / "data")
    kw.setdefault("price_fn", {"HRB": 44.0, "ADBE": 240.0}.get)
    kw.setdefault("series_fn", lambda p: [])
    kw.setdefault("benchmark_fn", lambda *a: None)
    app = create_app(paths, config_path=tmp_path / "config.yaml", **kw)
    return TestClient(app), app, paths


@pytest.mark.unit
def test_dashboard_without_a_portfolio_offers_to_start_one(tmp_path):
    client, app, paths = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200 and "Start your portfolio" in r.text
    r = client.post("/start", data={"cash": "1000", "currency": "USD", "token": app.state.token})
    assert r.status_code == 303
    assert "1,000.00" in client.get("/").text


@pytest.mark.unit
def test_dashboard_lists_holdings(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    ops.record_buy(paths, "HRB", 1, 40.0)
    r = client.get("/")
    assert r.status_code == 200
    assert "HRB" in r.text and "44.00" in r.text and "+10.0%" in r.text


@pytest.mark.unit
def test_post_without_the_token_is_refused(tmp_path):
    client, app, paths = _client(tmp_path)
    r = client.post("/start", data={"cash": "1000", "currency": "USD"})
    assert r.status_code == 403 and not paths.portfolio.exists()


@pytest.mark.unit
@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 11), date(2026, 11, 7)),    # after this month's first Saturday
    (date(2026, 11, 2), date(2026, 11, 7)),
    (date(2026, 11, 7), date(2026, 11, 7)),     # on the day itself
])
def test_next_review_is_the_first_saturday(today, expected):
    assert views.next_review_date(today) == expected


@pytest.mark.unit
def test_line_chart_is_inline_svg():
    from value_pipeline.web.charts import line_chart

    svg = line_chart(["a", "b", "c"], {"Portfolio": [1.0, 2.0, 3.0], "SPY": [1.0, 1.5, 2.0]})
    assert svg.startswith("<svg") and svg.count("<polyline") == 2 and "http" not in svg.replace(
        'xmlns="http://www.w3.org/2000/svg"', "")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_value_web.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'value_pipeline.web'`

- [ ] **Step 4: Write `value_pipeline/web/charts.py`**

```python
"""Small inline SVG charts: no scripts, no external files, colours from the page's CSS."""

from __future__ import annotations

from html import escape

_COLORS = ("var(--accent)", "var(--muted)", "var(--flag)")


def line_chart(labels: list[str], lines: dict[str, list[float]], height: int = 180) -> str:
    """One polyline per series over a shared x axis, scaled to the data's range."""
    width, pad = 640, 28
    values = [v for series in lines.values() for v in series if v is not None]
    if len(labels) < 2 or not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    def x(i: int) -> float:
        return pad + i * (width - 2 * pad) / (len(labels) - 1)

    def y(v: float) -> float:
        return height - pad - (v - lo) * (height - 2 * pad) / span

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
             f'class="chart" aria-label="{escape(", ".join(lines))}">']
    parts.append(f'<text x="{pad}" y="14" class="axis">{hi:,.0f}</text>')
    parts.append(f'<text x="{pad}" y="{height - 6}" class="axis">{lo:,.0f}  ({escape(labels[0])} → {escape(labels[-1])})</text>')
    for k, (name, series) in enumerate(lines.items()):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series) if v is not None)
        color = _COLORS[k % len(_COLORS)]
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>')
        parts.append(f'<text x="{width - pad - 90}" y="{16 + 14 * k}" class="axis" fill="{color}">{escape(name)}</text>')
    parts.append("</svg>")
    return "".join(parts)
```

- [ ] **Step 5: Write `value_pipeline/web/views.py`** (dashboard helpers; later tasks append)

```python
"""Data assembled for the website's pages, from the pipeline's files."""

from __future__ import annotations

from datetime import date, timedelta

from value_pipeline.config import Paths


def next_review_date(today: date) -> date:
    """The first Saturday of this month if it is still ahead (or today), else next month's."""
    def first_saturday(year: int, month: int) -> date:
        d = date(year, month, 1)
        return d + timedelta(days=(5 - d.weekday()) % 7)

    this = first_saturday(today.year, today.month)
    if today <= this:
        return this
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return first_saturday(year, month)


def latest_watch(paths: Paths) -> tuple[str, str] | None:
    files = sorted(paths.reports.glob("watch_????-??-??.md")) if paths.reports.exists() else []
    if not files:
        return None
    return files[-1].stem.removeprefix("watch_"), files[-1].read_text(encoding="utf-8")
```

- [ ] **Step 6: Write `value_pipeline/web/app.py`**

```python
"""The local website: FastAPI routes over the pipeline's own functions. It never trades."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from value_pipeline import benchmark, history, market_data
from value_pipeline import operations as ops
from value_pipeline.config import CONFIG_PATH, Paths
from value_pipeline.web import charts, views

TEMPLATES = Path(__file__).parent / "templates"


class FormError(ValueError):
    """A form value that cannot be used; the message is shown to the user."""


def parse_number(text: str, field: str) -> float:
    try:
        return float(str(text).strip())
    except ValueError:
        raise FormError(f"{field}: '{text}' is not a number (use a dot for decimals, e.g. 1.5)") from None


def _markdown(text: str) -> str:
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def create_app(paths: Paths, *, config_path: Path = CONFIG_PATH, price_fn=market_data.latest_price,
               series_fn=history.fetch_value_series, benchmark_fn=benchmark.compare, runner=None) -> FastAPI:
    app = FastAPI(title="Value pipeline", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.paths, app.state.config_path = paths, config_path
    app.state.price_fn, app.state.series_fn, app.state.benchmark_fn = price_fn, series_fn, benchmark_fn
    app.state.runner = runner
    app.state.token = secrets.token_urlsafe(24)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["markdown"] = _markdown
    app.state.templates = templates

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.update(request=request, token=app.state.token, msg=request.query_params.get("msg"))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    app.state.render = render

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        """Every form post carries this run's token, so another site open in the browser cannot post here."""
        if request.method == "POST":
            form = await request.form()
            if not secrets.compare_digest(str(form.get("token", "")), app.state.token):
                return HTMLResponse("Missing or wrong form token. Reload the page and try again.", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if not paths.portfolio.exists():
            return render(request, "start.html")
        view = ops.portfolio_view(paths, price_fn=app.state.price_fn, benchmark_fn=app.state.benchmark_fn)
        series = app.state.series_fn(paths)
        chart = charts.line_chart([s["date"] for s in series],
                                  {"Portfolio": [s["portfolio"] for s in series],
                                   "SPY, same cash flows": [s["spy"] for s in series]})
        from datetime import date

        return render(request, "dashboard.html", view=view, chart=chart, watch=views.latest_watch(paths),
                      next_review=views.next_review_date(date.today()))

    @app.post("/start")
    async def start(request: Request):
        form = await request.form()
        try:
            cash = parse_number(form.get("cash", ""), "Cash")
            ops.init_portfolio(paths, cash, str(form.get("currency") or "USD").upper().strip(),
                               on=str(form.get("date") or "") or None)
        except (FormError, ops.PortfolioExists) as exc:
            return render(request, "start.html", status=400, error=str(exc))
        return RedirectResponse("/?msg=Portfolio+started", status_code=303)

    return app
```

Note on the middleware: Starlette caches the parsed form on the request, so routes can call `await request.form()` again after the middleware has read it.

- [ ] **Step 7: Write `value_pipeline/web/__init__.py`**

```python
"""The value pipeline's local website (uv run python -m value_pipeline web)."""

from value_pipeline.web.app import create_app

__all__ = ["create_app"]
```

- [ ] **Step 8: Write the templates**

`value_pipeline/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Value pipeline{% endblock %}</title>
<style>
:root { --bg:#fbfaf7; --panel:#ffffff; --fg:#1d1d1b; --muted:#6b6a65; --line:#e3e1da; --accent:#0b6e4f; --flag:#b4530c; --bad:#b42318; }
@media (prefers-color-scheme: dark) { :root { --bg:#161614; --panel:#1f1f1c; --fg:#ecebe6; --muted:#a3a29b; --line:#33322e; --accent:#5fc29b; --flag:#f0a35e; --bad:#f97066; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { border-bottom:1px solid var(--line); background:var(--panel); }
nav { max-width:1100px; margin:0 auto; padding:10px 16px; display:flex; gap:16px; flex-wrap:wrap; align-items:center; }
nav a { color:var(--fg); text-decoration:none; } nav a.brand { font-weight:700; margin-right:8px; }
main { max-width:1100px; margin:0 auto; padding:20px 16px 48px; }
h1 { font-size:1.4rem; margin:0 0 12px; } h2 { font-size:1.05rem; margin:24px 0 8px; }
a { color:var(--accent); }
.muted { color:var(--muted); } .bad { color:var(--bad); } .flag { color:var(--flag); }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
.card b { display:block; font-size:1.25rem; }
.table { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:0.92rem; }
th, td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; white-space:nowrap; cursor:default; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
form.inline { display:inline; }
fieldset { border:1px solid var(--line); border-radius:8px; padding:12px; margin:0 0 16px; background:var(--panel); }
label { display:inline-block; margin:4px 12px 4px 0; } input, select, textarea { font:inherit; padding:5px 7px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
button { font:inherit; padding:6px 14px; border-radius:6px; border:1px solid var(--accent); background:var(--accent); color:var(--bg); cursor:pointer; }
button.secondary { background:transparent; color:var(--accent); }
.msg { padding:8px 12px; border-radius:6px; border:1px solid var(--accent); margin-bottom:12px; }
.error { padding:8px 12px; border-radius:6px; border:1px solid var(--bad); color:var(--bad); margin-bottom:12px; }
pre.log { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; max-height:420px; overflow:auto; font-size:0.82rem; white-space:pre-wrap; }
svg.chart { width:100%; height:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px; }
svg.chart .axis { font-size:11px; fill:var(--muted); }
</style>
</head>
<body>
<header><nav>
  <a class="brand" href="/">Value pipeline</a>
  <a href="/">Dashboard</a><a href="/trade">Trade</a><a href="/trades">History</a>
  <a href="/reviews">Reviews</a><a href="/screen">Screen</a><a href="/run">Run</a><a href="/settings">Settings</a>
</nav></header>
<main>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% block content %}{% endblock %}
</main>
</body>
</html>
```

`value_pipeline/web/templates/start.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Start your portfolio</h1>
<p class="muted">Enter the cash you start with. Record stocks you already own afterwards on the Trade page, with the date you bought them.</p>
<form method="post" action="/start"><fieldset>
  <input type="hidden" name="token" value="{{ token }}">
  <label>Cash <input name="cash" inputmode="decimal" required></label>
  <label>Currency <input name="currency" value="USD" size="4"></label>
  <label>Start date <input name="date" type="date"></label>
  <button>Start</button>
</fieldset></form>
{% endblock %}
```

`value_pipeline/web/templates/dashboard.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Dashboard</h1>
<div class="cards">
  <div class="card"><span class="muted">Total value</span><b>{{ "{:,.2f}".format(view.total) }} {{ view.currency }}</b></div>
  <div class="card"><span class="muted">Cash</span><b>{{ "{:,.2f}".format(view.cash) }}</b></div>
  {% if view.comparison %}
  <div class="card"><span class="muted">Return since {{ view.comparison.since }}</span><b>{{ "{:+.1%}".format(view.comparison.portfolio_return) }}</b></div>
  <div class="card"><span class="muted">SPY, same cash flows</span><b>{{ "{:+.1%}".format(view.comparison.spy_return) }}</b></div>
  {% endif %}
  <div class="card"><span class="muted">Next review</span><b>{{ next_review.strftime("%a %d %b") }}</b></div>
</div>
{% if chart %}<h2>Value over time</h2>{{ chart | safe }}{% endif %}
<h2>Holdings</h2>
{% if view.positions %}
<div class="table"><table>
<tr><th>Ticker</th><th class="num">Shares</th><th class="num">Avg</th><th class="num">Price</th><th class="num">Value</th><th class="num">Gain</th><th class="num">Weight</th><th>Rating</th><th class="num">Value est.</th><th class="num">MoS</th><th class="num">Days</th></tr>
{% for p in view.positions %}
<tr>
  <td><a href="/stock/{{ p.ticker }}">{{ p.ticker }}</a></td>
  <td class="num">{{ "{:g}".format(p.shares) }}</td>
  <td class="num">{{ "{:,.2f}".format(p.avg_price) }}</td>
  <td class="num">{{ "{:,.2f}".format(p.price) if p.price is not none else "no price" }}</td>
  <td class="num">{{ "{:,.2f}".format(p.value) }}</td>
  <td class="num">{{ "{:+.1%}".format(p.gain) if p.gain is not none else "–" }}</td>
  <td class="num">{{ "{:.1%}".format(p.weight) if p.weight is not none else "–" }}</td>
  <td>{{ p.last_rating or "–" }}</td>
  <td class="num">{{ "{:,.2f}".format(p.intrinsic_value) if p.intrinsic_value else "–" }}</td>
  <td class="num {{ 'flag' if p.margin_of_safety is not none and p.margin_of_safety < 0 }}">{{ "{:.0%}".format(p.margin_of_safety) if p.margin_of_safety is not none else "–" }}</td>
  <td class="num">{{ p.days_held }}</td>
</tr>
{% endfor %}
</table></div>
{% else %}<p class="muted">No holdings yet. <a href="/trade">Record a buy</a>.</p>{% endif %}
<h2>Weekly watch</h2>
{% if watch %}<p class="muted">{{ watch[0] }}</p>{{ watch[1] | markdown | safe }}{% else %}<p class="muted">No watch yet. It runs on the Saturdays between reviews, or from <a href="/run">Run</a>.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 9: Add the `web` CLI command**

In `value_pipeline/cli.py` add:

```python
def cmd_web(args, paths: Paths, cfg) -> None:
    import threading
    import webbrowser

    import uvicorn

    from value_pipeline.web import create_app

    url = f"http://127.0.0.1:{args.port}"
    console.print(f"Value pipeline website at {url} (this Mac only). Stop with Ctrl+C.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(create_app(paths), host="127.0.0.1", port=args.port, log_level="warning")
```

and in `build_parser`, before the `for name, func in (("screen", ...` loop:

```python
    s = sub.add_parser("web", help="open the local website (this Mac only)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="don't open the browser")
    s.set_defaults(func=cmd_web)
```

- [ ] **Step 10: Run the tests**

Run: `.venv/bin/pytest tests/test_value_web.py tests/test_value_operations.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml uv.lock value_pipeline/web value_pipeline/cli.py tests/test_value_web.py
git commit -m "feat(web): a local website with the dashboard, started with \`value_pipeline web\`

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Trade page, history and undo

**Files:**
- Modify: `value_pipeline/web/app.py` (routes `/trade`, `/trade/buy`, `/trade/sell`, `/trade/deposit`, `/trades`, `/trades/undo`)
- Create: `value_pipeline/web/templates/trade.html`, `value_pipeline/web/templates/trades.html`
- Test: `tests/test_value_web.py` (append)

**Interfaces:**
- Consumes: `operations.record_buy/record_sell/record_deposit/undo_info/undo_last/realised_gains`, `portfolio.load/read_trades/TradeError`, `review.latest_result`, `FormError`, `parse_number` (Task 3).
- Produces: routes only.

- [ ] **Step 1: Write the failing tests** (append)

```python
from value_pipeline import portfolio as pfmod


def _post(client, app, url, **data):
    return client.post(url, data={**data, "token": app.state.token}, follow_redirects=False)


@pytest.mark.unit
def test_trade_forms_match_the_cli_and_show_errors(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    r = _post(client, app, "/trade/buy", ticker=" hrb", shares="2", price="44", fees="1", date="2026-10-02", note="x")
    assert r.status_code == 303
    pf = pfmod.load(paths)
    assert pf.position("HRB").shares == 2 and pf.cash == pytest.approx(911.0)
    r = _post(client, app, "/trade/sell", ticker="HRB", shares="5", price="44", fees="0", date="", note="")
    assert r.status_code == 400 and "only 2 held" in r.text and 'value="5"' in r.text
    r = _post(client, app, "/trade/buy", ticker="HRB", shares="1,5", price="44", fees="0", date="", note="")
    assert r.status_code == 400 and "is not a number" in r.text
    r = _post(client, app, "/trade/deposit", amount="250", date="")
    assert r.status_code == 303 and pfmod.load(paths).cash == pytest.approx(1161.0)


@pytest.mark.unit
def test_trade_page_shows_the_latest_rating_and_price(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    folder = paths.reports / "2026-10-11"
    folder.mkdir(parents=True)
    (folder / "results.json").write_text('[{"ticker": "HRB", "rating": "Overweight", "intrinsic_value": 55.0, '
                                         '"margin_of_safety": 0.2, "error": null, "report": "HRB/complete_report.html", "thesis": "t"}]')
    r = client.get("/trade?ticker=hrb")
    assert r.status_code == 200 and "Overweight" in r.text and "55.00" in r.text and "44.00" in r.text


@pytest.mark.unit
def test_history_lists_trades_with_gains_and_undo(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    ops.record_buy(paths, "HRB", 2, 40.0, on="2026-10-02")
    ops.record_sell(paths, "HRB", 1, 50.0, on="2026-10-03")
    r = client.get("/trades")
    assert r.status_code == 200 and "+10.00" in r.text and "Undo: SELL 1 HRB" in r.text
    assert _post(client, app, "/trades/undo").status_code == 303
    assert pfmod.load(paths).position("HRB").shares == 2
    r = _post(client, app, "/trades/undo")
    assert r.status_code == 303 and "Nothing+to+undo" in r.headers["location"]


@pytest.mark.unit
def test_trade_page_without_a_portfolio_offers_to_start_one(tmp_path):
    client, app, paths = _client(tmp_path)
    assert "Start your portfolio" in client.get("/trade").text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_value_web.py -q -k "trade or history"`
Expected: FAIL with 404 on `/trade`

- [ ] **Step 3: Add the routes** inside `create_app` in `app.py`, before `return app`

```python
    from urllib.parse import quote_plus

    from value_pipeline import portfolio as pfmod
    from value_pipeline import review as reviewmod

    def _trade_page(request: Request, status: int = 200, error: str | None = None, values: dict | None = None):
        if not paths.portfolio.exists():
            return render(request, "start.html")
        pf = pfmod.load(paths)
        ticker = (request.query_params.get("ticker") or (values or {}).get("ticker") or "").strip().upper()
        info = None
        if ticker:
            info = {"ticker": ticker, "price": app.state.price_fn(ticker),
                    "result": reviewmod.latest_result(paths, ticker), "position": pf.position(ticker)}
        return render(request, "trade.html", status=status, error=error, pf=pf, info=info, values=values or {})

    @app.get("/trade", response_class=HTMLResponse)
    def trade(request: Request):
        return _trade_page(request)

    @app.post("/trade/{kind}")
    async def record(request: Request, kind: str):
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "token"}
        on = values.get("date") or None
        try:
            if kind in ("buy", "sell"):
                ticker = values.get("ticker", "").strip().upper()
                if not ticker:
                    raise FormError("Ticker is required")
                shares = parse_number(values.get("shares", ""), "Shares")
                price = parse_number(values.get("price", ""), "Price")
                fees = parse_number(values.get("fees") or "0", "Fees")
                if kind == "buy":
                    held, pf = ops.record_buy(paths, ticker, shares, price, fees, on=on, note=values.get("note", ""))
                    msg = f"Recorded: bought {shares:g} {ticker} @ {price:,.2f}. Now {held.shares:g} sh; cash {pf.cash:,.2f}"
                else:
                    gain, pf = ops.record_sell(paths, ticker, shares, price, fees, on=on, note=values.get("note", ""))
                    msg = f"Recorded: sold {shares:g} {ticker} @ {price:,.2f} (realised {gain:+,.2f}); cash {pf.cash:,.2f}"
            elif kind == "deposit":
                pf = ops.record_deposit(paths, parse_number(values.get("amount", ""), "Amount"), on=on)
                msg = f"Recorded. Cash is now {pf.cash:,.2f}"
            else:
                return HTMLResponse("Unknown trade type", status_code=404)
        except (FormError, pfmod.TradeError, ValueError) as exc:
            return _trade_page(request, status=400, error=str(exc), values=values)
        return RedirectResponse(f"/trades?msg={quote_plus(msg)}", status_code=303)

    @app.get("/trades", response_class=HTMLResponse)
    def trades(request: Request):
        if not paths.portfolio.exists():
            return render(request, "start.html")
        rows = pfmod.read_trades(paths)
        gains = ops.realised_gains(rows)
        entries = [{**t, "kind": t["action"], "gain": g} for t, g in zip(rows, gains, strict=True)]
        entries += [{"date": d["date"], "kind": "DEPOSIT" if float(d["amount"]) > 0 else "WITHDRAW",
                     "ticker": "", "shares": "", "price": "", "fees": "", "note": "",
                     "amount": abs(float(d["amount"])), "gain": None} for d in pfmod.load(paths).deposits]
        entries.sort(key=lambda e: e["date"], reverse=True)
        return render(request, "trades.html", entries=entries, undo=ops.undo_info(paths))

    @app.post("/trades/undo")
    def undo(request: Request):
        try:
            msg = f"Undone: {ops.undo_last(paths)}"
        except ops.UndoUnavailable as exc:
            msg = str(exc)
        return RedirectResponse(f"/trades?msg={quote_plus(msg)}", status_code=303)
```

Note: the `ValueError` in the `except` covers `date.fromisoformat` on a malformed date.

- [ ] **Step 4: Write `trade.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Record a trade</h1>
<p class="muted">Place the order at your broker first, then record the fill here. Cash: <b>{{ "{:,.2f}".format(pf.cash) }} {{ pf.currency }}</b>.</p>
<form method="get" action="/trade"><label>Look up a ticker <input name="ticker" value="{{ info.ticker if info else '' }}" size="8"></label><button class="secondary">Look up</button></form>
{% if info %}
<fieldset><b>{{ info.ticker }}</b>
  · price {{ "{:,.2f}".format(info.price) if info.price is not none else "unavailable" }}
  {% if info.position %} · you hold {{ "{:g}".format(info.position.shares) }} sh at {{ "{:,.2f}".format(info.position.avg_price) }}{% endif %}
  {% if info.result %} · last rated <b>{{ info.result.rating }}</b>
    {% if info.result.intrinsic_value %}, value {{ "{:,.2f}".format(info.result.intrinsic_value) }}{% endif %}
    {% if info.result.margin_of_safety is not none %}, margin of safety {{ "{:.0%}".format(info.result.margin_of_safety) }}{% endif %}
    · <a href="/stock/{{ info.ticker }}">history</a>{% endif %}
</fieldset>
{% endif %}
{% for kind, label in [("buy", "Buy"), ("sell", "Sell")] %}
<form method="post" action="/trade/{{ kind }}"><fieldset><legend>{{ label }}</legend>
  <input type="hidden" name="token" value="{{ token }}">
  <label>Ticker <input name="ticker" size="8" required value="{{ values.ticker or (info.ticker if info else '') }}"></label>
  <label>Shares <input name="shares" inputmode="decimal" size="8" required value="{{ values.shares if values.ticker is defined and request.url.path.endswith(kind) else '' }}"></label>
  <label>Price <input name="price" inputmode="decimal" size="8" required value="{{ values.price or '' }}"></label>
  <label>Fees <input name="fees" inputmode="decimal" size="6" value="{{ values.fees or '0' }}"></label>
  <label>Date <input name="date" type="date" value="{{ values.date or '' }}"></label>
  <label>Note <input name="note" size="20" value="{{ values.note or '' }}"></label>
  <button>Record {{ label | lower }}</button>
</fieldset></form>
{% endfor %}
<form method="post" action="/trade/deposit"><fieldset><legend>Deposit or withdraw cash</legend>
  <input type="hidden" name="token" value="{{ token }}">
  <label>Amount (negative to withdraw) <input name="amount" inputmode="decimal" size="10" required value="{{ values.amount or '' }}"></label>
  <label>Date <input name="date" type="date"></label>
  <button>Record</button>
</fieldset></form>
{% endblock %}
```

The `shares` field shows the entered value only on the form that was posted (`request.url.path` is `/trade/sell` after a failed sell), so the test's `value="5"` check matches the sell form.

- [ ] **Step 5: Write `trades.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>History</h1>
{% if undo %}
<form method="post" action="/trades/undo" class="inline"
      onsubmit="return confirm('Undo: {{ undo.description }}?')">
  <input type="hidden" name="token" value="{{ token }}">
  <button class="secondary">Undo: {{ undo.description }}</button>
</form>
{% endif %}
<div class="table"><table>
<tr><th>Date</th><th>Type</th><th>Ticker</th><th class="num">Shares</th><th class="num">Price</th><th class="num">Fees</th><th class="num">Amount</th><th class="num">Realised</th><th>Note</th></tr>
{% for e in entries %}
<tr><td>{{ e.date }}</td><td>{{ e.kind }}</td>
<td>{% if e.ticker %}<a href="/stock/{{ e.ticker }}">{{ e.ticker }}</a>{% endif %}</td>
<td class="num">{{ e.shares }}</td><td class="num">{{ e.price }}</td><td class="num">{{ e.fees }}</td>
<td class="num">{{ "{:,.2f}".format(e.amount) if e.amount is defined else "" }}</td>
<td class="num">{{ "{:+,.2f}".format(e.gain) if e.gain is not none else "" }}</td><td>{{ e.note }}</td></tr>
{% else %}<tr><td colspan="9" class="muted">Nothing recorded yet.</td></tr>{% endfor %}
</table></div>
{% endblock %}
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_value_web.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add value_pipeline/web tests/test_value_web.py
git commit -m "feat(web): record buys, sells and deposits, with history and undo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Reviews, report files and the stock page

**Files:**
- Modify: `value_pipeline/web/views.py` (add `reviews_index`, `stock_history`, `safe_report_path`)
- Modify: `value_pipeline/web/app.py` (routes `/reviews`, `/reports/{path:path}`, `/stock/{ticker}`)
- Create: `value_pipeline/web/templates/reviews.html`, `value_pipeline/web/templates/stock.html`
- Test: `tests/test_value_web.py` (append)

**Interfaces:**
- Consumes: `review.review_dirs`, `screener.latest_screen`, `portfolio.read_trades/load`.
- Produces:
  - `views.reviews_index(paths) -> list[dict]` with keys `folder, results (list), cost (str|None)`
  - `views.stock_history(paths, ticker) -> dict` with keys `ticker, reviews (list of {folder, rating, intrinsic_value, margin_of_safety, report}), screen (dict|None), trades (list)`
  - `views.safe_report_path(paths, rel: str) -> Path|None`

- [ ] **Step 1: Write the failing tests** (append)

```python
def _review(paths, folder, rows, actions="# Value review\n\n- Cost of this review: ~$4.00\n"):
    d = paths.reports / folder
    (d / "HRB").mkdir(parents=True)
    (d / "HRB" / "complete_report.html").write_text("<p>HRB report</p>")
    (d / "ACTIONS.html").write_text("<p>actions</p>")
    (d / "ACTIONS.md").write_text(actions)
    import json
    (d / "results.json").write_text(json.dumps(rows))


ROW = {"ticker": "HRB", "rating": "Overweight", "intrinsic_value": 55.0, "margin_of_safety": 0.2,
       "error": None, "report": "HRB/complete_report.html", "thesis": "t"}


@pytest.mark.unit
def test_reviews_list_and_report_files(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    _review(paths, "2026-10-11", [ROW])
    _review(paths, "2026-10-11-2", [dict(ROW, rating="Hold")])
    r = client.get("/reviews")
    assert r.status_code == 200 and r.text.index("2026-10-11-2") < r.text.index('"/reports/2026-10-11/ACTIONS.html"')
    assert "$4.00" in r.text
    assert client.get("/reports/2026-10-11/HRB/complete_report.html").text == "<p>HRB report</p>"


@pytest.mark.unit
@pytest.mark.parametrize("path", ["../portfolio.json", "2026-10-11/../../portfolio.json", "%2e%2e/portfolio.json"])
def test_report_files_stay_inside_the_reports_folder(tmp_path, path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    _review(paths, "2026-10-11", [ROW])
    assert client.get(f"/reports/{path}").status_code == 404


@pytest.mark.unit
def test_stock_page_shows_rating_history_and_trades(tmp_path):
    client, app, paths = _client(tmp_path)
    ops.init_portfolio(paths, 1000.0)
    ops.record_buy(paths, "HRB", 1, 44.0, on="2026-10-12")
    _review(paths, "2026-10-11", [ROW])
    _review(paths, "2026-11-07", [dict(ROW, rating="Hold", intrinsic_value=58.0)])
    r = client.get("/stock/hrb")
    assert r.status_code == 200
    assert r.text.index("2026-11-07") < r.text.index("2026-10-11")
    assert "Overweight" in r.text and "58.00" in r.text and "2026-10-12" in r.text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_value_web.py -q -k "reviews or report or stock"`
Expected: FAIL with 404

- [ ] **Step 3: Add to `views.py`**

```python
import json
import re
from pathlib import Path

import pandas as pd

from value_pipeline import portfolio as pfmod
from value_pipeline import review as reviewmod
from value_pipeline import screener

_COST = re.compile(r"Cost of this review: ~?(\$[\d,.]+)")


def _results(folder: Path) -> list[dict]:
    f = folder / "results.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def reviews_index(paths: Paths) -> list[dict]:
    out = []
    for folder in reviewmod.review_dirs(paths):
        actions = folder / "ACTIONS.md"
        cost = _COST.search(actions.read_text(encoding="utf-8")) if actions.exists() else None
        out.append({"folder": folder.name, "results": _results(folder), "cost": cost.group(1) if cost else None})
    return out


def stock_history(paths: Paths, ticker: str) -> dict:
    ticker = ticker.strip().upper()
    reviews = [{"folder": folder.name, **r} for folder in reviewmod.review_dirs(paths)
               for r in _results(folder) if r["ticker"] == ticker]
    screen = None
    latest = screener.latest_screen(paths)
    if latest is not None:
        df = pd.read_csv(latest)
        match = df[df["ticker"] == ticker]
        if not match.empty:
            screen = {k: (None if pd.isna(v) else v) for k, v in match.iloc[0].to_dict().items()}
    trades = [t for t in pfmod.read_trades(paths) if t["ticker"] == ticker] if paths.trades.exists() else []
    return {"ticker": ticker, "reviews": reviews, "screen": screen, "trades": trades}


def safe_report_path(paths: Paths, rel: str) -> Path | None:
    """The file under data/reports that ``rel`` names, or None if it is missing or outside it."""
    root = paths.reports.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target
```

- [ ] **Step 4: Add the routes** in `create_app` (before `return app`)

```python
    from fastapi.responses import FileResponse

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request):
        return render(request, "reviews.html", reviews=views.reviews_index(paths))

    @app.get("/reports/{rel:path}")
    def report_file(rel: str):
        target = views.safe_report_path(paths, rel)
        if target is None:
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(target)

    @app.get("/stock/{ticker}", response_class=HTMLResponse)
    def stock(request: Request, ticker: str):
        data = views.stock_history(paths, ticker)
        values = [r for r in reversed(data["reviews"]) if r.get("intrinsic_value")]
        chart = charts.line_chart([r["folder"] for r in values], {"Intrinsic value": [r["intrinsic_value"] for r in values]})
        return render(request, "stock.html", data=data, chart=chart, price=app.state.price_fn(data["ticker"]))
```

- [ ] **Step 5: Write `reviews.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Reviews</h1>
<div class="table"><table>
<tr><th>Review</th><th>Cost</th><th>Ratings</th><th></th></tr>
{% for r in reviews %}
<tr><td>{{ r.folder }}</td><td>{{ r.cost or "–" }}</td>
<td>{% for x in r.results %}<a href="/stock/{{ x.ticker }}">{{ x.ticker }}</a> {{ x.rating }}{% if x.report %} (<a href="/reports/{{ r.folder }}/{{ x.report }}">report</a>){% endif %}{% if not loop.last %} · {% endif %}{% endfor %}</td>
<td><a href="/reports/{{ r.folder }}/ACTIONS.html">actions</a></td></tr>
{% else %}<tr><td colspan="4" class="muted">No reviews yet. Start one from <a href="/run">Run</a>.</td></tr>{% endfor %}
</table></div>
{% endblock %}
```

- [ ] **Step 6: Write `stock.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>{{ data.ticker }}</h1>
<p class="muted">Price {{ "{:,.2f}".format(price) if price is not none else "unavailable" }} · <a href="/trade?ticker={{ data.ticker }}">record a trade</a></p>
{% if chart %}<h2>Intrinsic value over time</h2>{{ chart | safe }}{% endif %}
<h2>Ratings</h2>
<div class="table"><table>
<tr><th>Review</th><th>Rating</th><th class="num">Value</th><th class="num">MoS</th><th>Report</th></tr>
{% for r in data.reviews %}
<tr><td>{{ r.folder }}</td><td>{{ r.rating }}</td>
<td class="num">{{ "{:,.2f}".format(r.intrinsic_value) if r.intrinsic_value else "–" }}</td>
<td class="num">{{ "{:.0%}".format(r.margin_of_safety) if r.margin_of_safety is not none else "–" }}</td>
<td>{% if r.report %}<a href="/reports/{{ r.folder }}/{{ r.report }}">report</a>{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">Not analysed by the agents yet.</td></tr>{% endfor %}
</table></div>
{% if data.screen %}
<h2>Latest screen</h2>
<p>Rank {{ data.screen.mf_position | int if data.screen.mf_position is not none else "–" }} ·
EBIT/EV (avg) {{ "{:.1%}".format(data.screen.earnings_yield_avg) if data.screen.earnings_yield_avg is not none else "–" }} ·
ROIC (avg) {{ "{:.0%}".format(data.screen.roic_avg) if data.screen.roic_avg is not none else "–" }} ·
F-Score {{ data.screen.fscore }} · Z {{ "{:.1f}".format(data.screen.altman_z) if data.screen.altman_z is not none else "–" }} ·
formula value {{ "{:,.2f}".format(data.screen.value_base) if data.screen.value_base is not none else "–" }} ·
trend {{ data.screen.trend or "–" }}{% if data.screen.fail_reasons %} · <span class="flag">fails: {{ data.screen.fail_reasons }}</span>{% endif %}</p>
{% endif %}
<h2>Trades</h2>
{% if data.trades %}<ul>{% for t in data.trades %}<li>{{ t.date }} {{ t.action }} {{ t.shares }} @ {{ t.price }}{% if t.note %}: {{ t.note }}{% endif %}</li>{% endfor %}</ul>
{% else %}<p class="muted">No trades.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/test_value_web.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add value_pipeline/web tests/test_value_web.py
git commit -m "feat(web): browse reviews, agent reports and each stock's history

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Screen page

**Files:**
- Modify: `value_pipeline/web/views.py` (add `screen_table`)
- Modify: `value_pipeline/web/app.py` (route `/screen`)
- Create: `value_pipeline/web/templates/screen.html`
- Test: `tests/test_value_web.py` (append)

**Interfaces:**
- Consumes: `screener.latest_screen`; files `screen_<date>_excluded.csv`, `screen_<date>_skipped.csv`.
- Produces: `views.screen_table(paths) -> dict|None` with keys `date, rows (list of dict), sectors (sorted list), excluded (list), skipped (list)`

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.unit
def test_screen_page_shows_the_latest_screen(tmp_path):
    client, app, paths = _client(tmp_path)
    assert "No screen yet" in client.get("/screen").text
    paths.screens.mkdir(parents=True)
    (paths.screens / "screen_2026-10-11.csv").write_text(
        "ticker,name,sector,mf_position,earnings_yield_avg,roic_avg,fcf_yield,fscore,altman_z,mos_formula,trend,passes_filters,fail_reasons\n"
        "HRB,H&R Block,Consumer Discretionary,1,0.129,1.34,0.14,7,3.3,0.50,UPTREND,True,\n"
        "XYZ,Xyz,Industrials,,0.01,0.05,0.01,3,1.0,,DOWNTREND,False,F-Score\n")
    (paths.screens / "screen_2026-10-11_excluded.csv").write_text("ticker,name,sector\nJPM,JPMorgan,Financials\n")
    (paths.screens / "screen_2026-10-11_skipped.csv").write_text("ticker,reason\nBAD,no annual statements\n")
    r = client.get("/screen")
    assert r.status_code == 200
    for text in ("2026-10-11", "HRB", "50%", "XYZ", "F-Score", "JPM", "no annual statements", "Industrials"):
        assert text in r.text, text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_value_web.py -q -k screen_page`
Expected: FAIL with 404

- [ ] **Step 3: Add `screen_table` to `views.py`**

```python
def _csv_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    return [{k: (None if pd.isna(v) else v) for k, v in r.items()} for r in df.to_dict("records")]


def screen_table(paths: Paths) -> dict | None:
    latest = screener.latest_screen(paths)
    if latest is None:
        return None
    day = latest.stem.removeprefix("screen_")
    rows = _csv_records(latest)
    rows.sort(key=lambda r: (r.get("mf_position") is None, r.get("mf_position") or 0))
    return {
        "date": day,
        "rows": rows,
        "sectors": sorted({r["sector"] for r in rows if r.get("sector")}),
        "excluded": _csv_records(paths.screens / f"screen_{day}_excluded.csv"),
        "skipped": _csv_records(paths.screens / f"screen_{day}_skipped.csv"),
    }
```

- [ ] **Step 4: Add the route** in `create_app`

```python
    @app.get("/screen", response_class=HTMLResponse)
    def screen(request: Request):
        return render(request, "screen.html", screen=views.screen_table(paths))
```

- [ ] **Step 5: Write `screen.html`** (client-side sort and filter with a small inline script)

```html
{% extends "base.html" %}
{% block content %}
<h1>Screen</h1>
{% if not screen %}
<p class="muted">No screen yet. Run one from <a href="/run">Run</a>.</p>
{% else %}
<p class="muted">Latest screen {{ screen.date }} · {{ screen.rows | length }} companies screened · click a column to sort.</p>
<fieldset>
  <label>Sector <select id="f-sector"><option value="">All</option>{% for s in screen.sectors %}<option>{{ s }}</option>{% endfor %}</select></label>
  <label>Trend <select id="f-trend"><option value="">All</option><option>UPTREND</option><option>BASING</option><option>DOWNTREND</option><option>FREEFALL</option></select></label>
  <label><input type="checkbox" id="f-pass" checked> Passes filters only</label>
  <label>Min margin of safety <input id="f-mos" type="number" step="5" value="" size="4">%</label>
</fieldset>
<div class="table"><table id="screen">
<thead><tr><th data-k="n">#</th><th>Ticker</th><th>Sector</th><th class="num" data-k="n">EBIT/EV avg</th><th class="num" data-k="n">ROIC avg</th><th class="num" data-k="n">FCF yield</th><th class="num" data-k="n">F</th><th class="num" data-k="n">Z</th><th class="num" data-k="n">Formula MoS</th><th>Trend</th><th>Fails</th></tr></thead>
<tbody>
{% for r in screen.rows %}
<tr data-sector="{{ r.sector or '' }}" data-trend="{{ r.trend or '' }}" data-pass="{{ 'y' if r.passes_filters in (true, 'True') else 'n' }}" data-mos="{{ r.mos_formula if r.mos_formula is not none else '' }}">
<td class="num">{{ r.mf_position | int if r.mf_position is not none else "" }}</td>
<td><a href="/stock/{{ r.ticker }}">{{ r.ticker }}</a></td><td>{{ r.sector or "" }}</td>
<td class="num">{{ "{:.1%}".format(r.earnings_yield_avg) if r.earnings_yield_avg is not none else "–" }}</td>
<td class="num">{{ "{:.0%}".format(r.roic_avg) if r.roic_avg is not none else "–" }}</td>
<td class="num">{{ "{:.1%}".format(r.fcf_yield) if r.fcf_yield is not none else "–" }}</td>
<td class="num">{{ r.fscore if r.fscore is not none else "–" }}</td>
<td class="num">{{ "{:.1f}".format(r.altman_z) if r.altman_z is not none else "–" }}</td>
<td class="num">{{ "{:.0%}".format(r.mos_formula) if r.mos_formula is not none else "–" }}</td>
<td>{{ r.trend or "–" }}</td><td class="muted">{{ r.fail_reasons or "" }}</td></tr>
{% endfor %}
</tbody></table></div>
<h2>Excluded sectors ({{ screen.excluded | length }})</h2>
<p class="muted">{% for e in screen.excluded %}{{ e.ticker }} ({{ e.sector }}){% if not loop.last %}, {% endif %}{% endfor %}</p>
<h2>Skipped for missing data ({{ screen.skipped | length }})</h2>
<p class="muted">{% for s in screen.skipped %}{{ s.ticker }}: {{ s.reason }}{% if not loop.last %}; {% endif %}{% else %}None.{% endfor %}</p>
<script>
(() => {
  const body = document.querySelector("#screen tbody");
  const rows = [...body.rows];
  const f = id => document.getElementById(id);
  function apply() {
    const sector = f("f-sector").value, trend = f("f-trend").value, pass = f("f-pass").checked;
    const mos = f("f-mos").value === "" ? null : Number(f("f-mos").value) / 100;
    for (const r of rows) {
      const ok = (!sector || r.dataset.sector === sector) && (!trend || r.dataset.trend === trend)
        && (!pass || r.dataset.pass === "y") && (mos === null || (r.dataset.mos !== "" && Number(r.dataset.mos) >= mos));
      r.hidden = !ok;
    }
  }
  ["f-sector", "f-trend", "f-pass", "f-mos"].forEach(id => f(id).addEventListener("input", apply));
  document.querySelectorAll("#screen th").forEach((th, i) => th.addEventListener("click", () => {
    const numeric = th.dataset.k === "n", dir = th.dataset.dir === "asc" ? -1 : 1;
    th.dataset.dir = dir === 1 ? "asc" : "desc";
    const key = r => { const t = r.cells[i].textContent.replace(/[%,]/g, "").trim();
      return numeric ? (t === "" || t === "–" ? Infinity : parseFloat(t)) : t; };
    rows.sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * dir).forEach(r => body.appendChild(r));
  }));
  apply();
})();
</script>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_value_web.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add value_pipeline/web tests/test_value_web.py
git commit -m "feat(web): the latest screen as a sortable, filterable table

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Background jobs and the Run page

**Files:**
- Create: `value_pipeline/web/jobs.py`
- Modify: `value_pipeline/web/app.py` (routes `/run`, `/run/preview`, `/run/start`, `/run/status`, `/run/log/{name}`; default runner)
- Create: `value_pipeline/web/templates/run.html`, `value_pipeline/web/templates/run_preview.html`
- Test: `tests/test_value_web.py` (append)

**Interfaces:**
- Consumes: `review.preview/run`, `screener.run`, `watch.run`, `config.load_config`, `portfolio.load`.
- Produces:
  - `jobs.JobRunner(log_dir: Path)` with `start(kind: str, func: Callable[[], object]) -> dict`, `current() -> dict|None`, `history(limit=20) -> list[dict]`, `log_text(name: str) -> str|None`; raises `jobs.JobBusy`
  - Job dict keys: `name, kind, started, ended, status ('running'|'done'|'failed'), message, log`
  - `create_app(..., runner=None)` builds `JobRunner(paths.root / "logs")` when `runner` is None, and `job_funcs: dict|None` parameter that maps `kind -> callable(paths, cfg, **kw)` (tests pass fakes)

- [ ] **Step 1: Write the failing tests** (append)

```python
import threading
import time

from value_pipeline.web.jobs import JobBusy, JobRunner


@pytest.mark.unit
def test_job_runner_runs_one_job_at_a_time_and_logs_output(tmp_path):
    runner = JobRunner(tmp_path / "logs")
    release = threading.Event()

    def slow():
        print("working on it")
        release.wait(5)

    job = runner.start("screen", slow)
    with pytest.raises(JobBusy):
        runner.start("watch", lambda: None)
    release.set()
    for _ in range(100):
        if runner.current() is None:
            break
        time.sleep(0.02)
    done = runner.history()[0]
    assert done["name"] == job["name"] and done["status"] == "done"
    assert "working on it" in runner.log_text(done["name"])


@pytest.mark.unit
def test_failed_job_keeps_its_message(tmp_path):
    runner = JobRunner(tmp_path / "logs")

    def boom():
        raise SystemExit("Estimated cost $50.00 exceeds max_cost_per_review $40.00")

    runner.start("review", boom)
    for _ in range(100):
        if runner.current() is None:
            break
        time.sleep(0.02)
    assert runner.history()[0]["status"] == "failed"
    assert "exceeds" in runner.history()[0]["message"]


@pytest.mark.unit
def test_review_needs_a_confirmed_preview(tmp_path):
    calls = []
    funcs = {"review": lambda paths, cfg, **kw: calls.append(kw)}
    client, app, paths = _client(tmp_path, job_funcs=funcs)
    ops.init_portfolio(paths, 1000.0)
    r = client.post("/run/preview", data={"kind": "review", "tickers": "hog, ingr", "token": app.state.token})
    assert r.status_code == 200 and "2 runs" in r.text and "$4.00" in r.text and "HOG, INGR" in r.text
    assert calls == []
    r = client.post("/run/start", data={"kind": "review", "tickers": "HOG,INGR", "confirm": "yes",
                                        "token": app.state.token}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(100):
        if app.state.runner.current() is None:
            break
        time.sleep(0.02)
    assert calls == [{"only": ["HOG", "INGR"], "dry_run": False}]


@pytest.mark.unit
def test_review_over_the_cost_cap_cannot_be_started(tmp_path):
    calls = []
    client, app, paths = _client(tmp_path, job_funcs={"review": lambda paths, cfg, **kw: calls.append(kw)})
    ops.init_portfolio(paths, 1000.0)
    many = ",".join(f"T{i}" for i in range(30))       # 30 x $2 > $40
    r = client.post("/run/preview", data={"kind": "review", "tickers": many, "token": app.state.token})
    assert "exceeds" in r.text and 'name="confirm"' not in r.text
    r = client.post("/run/start", data={"kind": "review", "tickers": many, "confirm": "yes", "token": app.state.token},
                    follow_redirects=False)
    assert r.status_code == 303 and "exceeds" in r.headers["location"].replace("+", " ") and calls == []
```

`_client` must forward `job_funcs`: change its `create_app(...)` call to pass `**kw` (it already does).

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_value_web.py -q -k "job or review_needs or cost_cap"`
Expected: FAIL with `ModuleNotFoundError: No module named 'value_pipeline.web.jobs'`

- [ ] **Step 3: Write `value_pipeline/web/jobs.py`**

```python
"""One background job at a time (a screen, review or watch), logged to a file.

Jobs cannot be cancelled: a half-finished review is worth nothing. Closing the
browser does not stop them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class JobBusy(RuntimeError):
    """Another job is still running."""


class JobRunner:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._current: dict | None = None

    @property
    def _index(self) -> Path:
        return self.log_dir / "jobs.json"

    def _read(self) -> list[dict]:
        return json.loads(self._index.read_text(encoding="utf-8")) if self._index.exists() else []

    def _write(self, job: dict) -> None:
        jobs = [j for j in self._read() if j["name"] != job["name"]]
        jobs.insert(0, job)
        self._index.write_text(json.dumps(jobs[:200], indent=1), encoding="utf-8")

    def current(self) -> dict | None:
        return dict(self._current) if self._current else None

    def history(self, limit: int = 20) -> list[dict]:
        return self._read()[:limit]

    def log_text(self, name: str) -> str | None:
        f = self.log_dir / f"{name}.log"
        if "/" in name or ".." in name or not f.exists():
            return None
        return f.read_text(encoding="utf-8", errors="replace")

    def start(self, kind: str, func: Callable[[], object]) -> dict:
        with self._lock:
            if self._current is not None:
                raise JobBusy(f"A {self._current['kind']} job is already running; wait for it to finish.")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            name = f"job_{datetime.now():%Y%m%d_%H%M%S}_{kind}"
            job = {"name": name, "kind": kind, "started": datetime.now().isoformat(timespec="seconds"),
                   "ended": None, "status": "running", "message": "", "log": f"{name}.log"}
            self._current = job
            self._write(job)
        threading.Thread(target=self._run, args=(job, func), daemon=True).start()
        return dict(job)

    def _run(self, job: dict, func: Callable[[], object]) -> None:
        log_path = self.log_dir / job["log"]
        handler = None
        try:
            with log_path.open("w", encoding="utf-8", buffering=1) as out:
                handler = logging.StreamHandler(out)
                handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
                logging.getLogger().addHandler(handler)
                try:
                    with contextlib.redirect_stdout(out):
                        func()
                    job["status"] = "done"
                except SystemExit as exc:      # the pipeline's refusals: cost cap, cancelled, nothing to do
                    job["status"], job["message"] = "failed", str(exc)
                    out.write(f"\nStopped: {exc}\n")
                except Exception as exc:  # noqa: BLE001 - any failure is reported on the page
                    job["status"], job["message"] = "failed", f"{type(exc).__name__}: {exc}"
                    out.write(traceback.format_exc())
        finally:
            if handler is not None:
                logging.getLogger().removeHandler(handler)
            job["ended"] = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                self._write(job)
                self._current = None
```

- [ ] **Step 4: Wire jobs into `create_app`**

Change the signature to add `job_funcs: dict | None = None`, and after `app.state.runner = runner` set:

```python
    from value_pipeline.web.jobs import JobBusy, JobRunner

    app.state.runner = runner or JobRunner(paths.root / "logs")
    app.state.job_funcs = job_funcs or _default_job_funcs()
```

Add at module level in `app.py`:

```python
def _default_job_funcs() -> dict:
    from value_pipeline import portfolio as pfmod
    from value_pipeline import review, screener, watch

    def owned(paths):
        return {p.ticker for p in pfmod.load(paths).positions} if paths.portfolio.exists() else set()

    return {
        "screen": lambda paths, cfg, **kw: screener.run(paths, cfg, owned(paths)),
        "review": lambda paths, cfg, only=None, dry_run=False: review.run(paths, cfg, yes=True, dry_run=dry_run, only=only),
        "watch": lambda paths, cfg, **kw: watch.run(paths, cfg, notify=False),
    }
```

Add the routes (before `return app`):

```python
    from value_pipeline import review as reviewmod
    from value_pipeline.config import load_config

    def _tickers(form) -> list[str] | None:
        raw = str(form.get("tickers") or "")
        names = [t.strip().upper() for t in raw.replace(" ", ",").split(",") if t.strip()]
        return names or None

    @app.get("/run", response_class=HTMLResponse)
    def run_page(request: Request):
        return render(request, "run.html", current=app.state.runner.current(), jobs=app.state.runner.history())

    @app.get("/run/status")
    def run_status():
        from fastapi.responses import JSONResponse

        cur = app.state.runner.current()
        log = app.state.runner.log_text(cur["name"]) if cur else None
        return JSONResponse({"current": cur, "log_tail": (log or "")[-6000:]})

    @app.get("/run/log/{name}", response_class=HTMLResponse)
    def run_log(request: Request, name: str):
        text = app.state.runner.log_text(name)
        if text is None:
            return HTMLResponse("Not found", status_code=404)
        return render(request, "run.html", current=None, jobs=app.state.runner.history(), log=text, log_name=name)

    @app.post("/run/preview", response_class=HTMLResponse)
    async def run_preview(request: Request):
        form = await request.form()
        kind = str(form.get("kind"))
        if kind != "review":
            return RedirectResponse("/run", status_code=303)
        if not paths.portfolio.exists():
            return render(request, "start.html")
        only = _tickers(form)
        plan = reviewmod.preview(paths, load_config(app.state.config_path), only)
        return render(request, "run_preview.html", plan=plan, tickers=",".join(only or []),
                      dry_run=form.get("dry_run") == "yes")

    @app.post("/run/start")
    async def run_start(request: Request):
        form = await request.form()
        kind = str(form.get("kind"))
        func = app.state.job_funcs.get(kind)
        if func is None:
            return HTMLResponse("Unknown job", status_code=404)
        cfg = load_config(app.state.config_path)
        kw = {}
        if kind == "review":
            only, dry = _tickers(form), form.get("dry_run") == "yes"
            plan = reviewmod.preview(paths, cfg, only)
            if form.get("confirm") != "yes":
                return RedirectResponse("/run?msg=Confirm+the+review+first", status_code=303)
            if plan["blocked"] and not dry:
                msg = (f"Estimated cost ${plan['estimate']:,.2f} exceeds max_cost_per_review "
                       f"${plan['limit']:,.2f}; nothing was started")
                return RedirectResponse(f"/run?msg={quote_plus(msg)}", status_code=303)
            kw = {"only": only, "dry_run": dry}
        try:
            app.state.runner.start(kind, lambda: func(paths, cfg, **kw))
        except JobBusy as exc:
            return RedirectResponse(f"/run?msg={quote_plus(str(exc))}", status_code=303)
        return RedirectResponse(f"/run?msg={quote_plus(kind.capitalize() + ' started')}", status_code=303)
```

(`quote_plus` is imported in Task 4; keep one import at the top of `create_app`.)

- [ ] **Step 5: Write `run.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Run</h1>
<fieldset><legend>Start a job</legend>
<form method="post" action="/run/start" class="inline"><input type="hidden" name="token" value="{{ token }}"><input type="hidden" name="kind" value="screen"><button>Run screen</button></form>
<span class="muted">about 25 min the first time each week, then minutes</span><br><br>
<form method="post" action="/run/preview"><input type="hidden" name="token" value="{{ token }}"><input type="hidden" name="kind" value="review">
  <label>Tickers (optional, e.g. HOG, INGR) <input name="tickers" size="24"></label>
  <label><input type="checkbox" name="dry_run" value="yes"> Dry run (fake ratings, free)</label>
  <button>Preview review…</button></form>
<span class="muted">Empty tickers: every holding plus the top screen candidates. You confirm the cost before anything runs.</span><br><br>
<form method="post" action="/run/start" class="inline"><input type="hidden" name="token" value="{{ token }}"><input type="hidden" name="kind" value="watch"><button class="secondary">Run weekly watch</button></form>
<span class="muted">prices only, free</span>
</fieldset>
<h2>Current job</h2>
<div id="current">{% if current %}<p><b>{{ current.kind }}</b> running since {{ current.started }}</p>{% else %}<p class="muted">Nothing running.</p>{% endif %}</div>
<pre class="log" id="log" {% if not current and not log %}hidden{% endif %}>{{ log or "" }}</pre>
<h2>Recent jobs</h2>
<div class="table"><table><tr><th>Started</th><th>Job</th><th>Status</th><th>Message</th><th>Log</th></tr>
{% for j in jobs %}<tr><td>{{ j.started }}</td><td>{{ j.kind }}</td><td class="{{ 'bad' if j.status == 'failed' }}">{{ j.status }}</td><td>{{ j.message }}</td><td><a href="/run/log/{{ j.name }}">log</a></td></tr>
{% else %}<tr><td colspan="5" class="muted">No jobs yet.</td></tr>{% endfor %}</table></div>
{% if current %}
<script>
(async function poll() {
  const r = await fetch("/run/status"); const s = await r.json();
  const log = document.getElementById("log"); log.hidden = false; log.textContent = s.log_tail; log.scrollTop = log.scrollHeight;
  if (s.current) { setTimeout(poll, 3000); } else { location.href = "/run?msg=Job+finished"; }
})();
</script>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Write `run_preview.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Review preview{% if dry_run %} (dry run){% endif %}</h1>
<p><b>{{ plan.tickers | length }} runs</b> ({{ plan.n_held }} holdings + {{ plan.n_new }} candidates): {{ plan.tickers | join(", ") or "none" }}</p>
<p>Estimated cost: <b>${{ "{:,.2f}".format(0 if dry_run else plan.estimate) }}</b>
  <span class="muted">(${{ "{:,.2f}".format(plan.cost_per_run) }}/run, limit ${{ "{:,.2f}".format(plan.limit) }})</span></p>
{% if not plan.tickers %}
<p class="error">Nothing to review: no holdings and no screen candidates. Run a screen first.</p>
{% elif plan.blocked and not dry_run %}
<p class="error">This exceeds max_cost_per_review (${{ "{:,.2f}".format(plan.limit) }}). Name fewer tickers or raise the limit in <a href="/settings">Settings</a>.</p>
{% else %}
<form method="post" action="/run/start"><input type="hidden" name="token" value="{{ token }}">
  <input type="hidden" name="kind" value="review"><input type="hidden" name="tickers" value="{{ tickers }}">
  {% if dry_run %}<input type="hidden" name="dry_run" value="yes">{% endif %}
  <input type="hidden" name="confirm" value="yes">
  <button>Start the review</button> <a href="/run">Cancel</a></form>
{% endif %}
{% endblock %}
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/test_value_web.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add value_pipeline/web tests/test_value_web.py
git commit -m "feat(web): run screens, reviews and watches in the background with a cost preview

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Settings page

**Files:**
- Modify: `value_pipeline/web/app.py` (routes `/settings` GET and POST)
- Create: `value_pipeline/web/templates/settings.html`
- Test: `tests/test_value_web.py` (append)

**Interfaces:**
- Consumes: `config.load_config(path)`, `config.DEFAULTS`.
- Produces: routes only.

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.mark.unit
def test_settings_validate_and_back_up(tmp_path):
    client, app, paths = _client(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("review:\n  cost_per_run: 2.0\n")
    assert "cost_per_run: 2.0" in client.get("/settings").text
    r = client.post("/settings", data={"text": "review: [unclosed", "token": app.state.token})
    assert r.status_code == 400 and "not valid" in r.text and cfg.read_text() == "review:\n  cost_per_run: 2.0\n"
    r = client.post("/settings", data={"text": "- just a list\n", "token": app.state.token})
    assert r.status_code == 400
    r = client.post("/settings", data={"text": "review:\n  cost_per_run: 3.0\n", "token": app.state.token},
                    follow_redirects=False)
    assert r.status_code == 303 and "3.0" in cfg.read_text()
    assert list(paths.backups.glob("config_*.yaml"))[0].read_text() == "review:\n  cost_per_run: 2.0\n"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_value_web.py -q -k settings`
Expected: FAIL with 404 or 405

- [ ] **Step 3: Add the routes** in `create_app`

```python
    import yaml

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        cfg_path = app.state.config_path
        text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
        return render(request, "settings.html", text=text, path=str(cfg_path))

    @app.post("/settings")
    async def save_settings(request: Request):
        from datetime import datetime

        form = await request.form()
        text = str(form.get("text", "")).replace("\r\n", "\n")
        cfg_path = app.state.config_path
        try:
            parsed = yaml.safe_load(text) if text.strip() else {}
            if not isinstance(parsed, dict):
                raise ValueError("the file must be a set of 'key: value' sections")
            tmp = cfg_path.with_suffix(".yaml.check")
            tmp.write_text(text, encoding="utf-8")
            try:
                load_config(tmp)
            finally:
                tmp.unlink(missing_ok=True)
        except (yaml.YAMLError, ValueError, TypeError) as exc:
            return render(request, "settings.html", status=400, text=text, path=str(cfg_path),
                          error=f"Settings not saved: not valid ({exc})")
        if cfg_path.exists():
            paths.backups.mkdir(parents=True, exist_ok=True)
            (paths.backups / f"config_{datetime.now():%Y%m%d_%H%M%S_%f}.yaml").write_text(
                cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        cfg_path.write_text(text, encoding="utf-8")
        return RedirectResponse("/settings?msg=Settings+saved", status_code=303)
```

- [ ] **Step 4: Write `settings.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Settings</h1>
<p class="muted">{{ path }}: filters, sizing, sector cap, review cost and limits. The previous version is backed up on every save. Your API keys live in <code>.env</code> and are not editable here.</p>
<form method="post" action="/settings">
  <input type="hidden" name="token" value="{{ token }}">
  <textarea name="text" rows="34" style="width:100%; font-family:ui-monospace, Menlo, monospace; font-size:0.85rem">{{ text }}</textarea>
  <p><button>Save settings</button></p>
</form>
{% endblock %}
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_value_web.py -q && .venv/bin/ruff check value_pipeline tests`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add value_pipeline/web tests/test_value_web.py
git commit -m "feat(web): edit the pipeline settings with validation and backups

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Docs, full suite and the real check

**Files:**
- Modify: `value_pipeline/README.md` (a "Website" section and the `web` command row)
- Modify: `CLAUDE.md` (one line about the website)

- [ ] **Step 1: Document**

Add to the Commands table in `value_pipeline/README.md`:

```markdown
| `web [--port 8765] [--no-browser]` | Open the local website (this Mac only): dashboard, trades, history with undo, reviews and reports, stock pages, the screen, running jobs and settings. Stop with Ctrl+C. |
```

Add a section after "Monthly routine":

```markdown
## Website

`uv run python -m value_pipeline web` opens http://127.0.0.1:8765. It does everything the commands do, on the same files, so the two always agree. It never trades, and it can only be reached from this Mac.

- **Dashboard**: value, cash, return vs SPY with a chart, holdings, the latest weekly watch, the next review date.
- **Trade**: record buys, sells and deposits; looking up a ticker shows its price and last rating.
- **History**: every trade and deposit with realised gains, and undo for the last entry. Undo is refused once something else, such as a review, has changed the files since.
- **Reviews** and **Stock**: past reviews, agent reports, each ticker's ratings and values over time.
- **Screen**: the latest screen, sortable and filterable.
- **Run**: start a screen, a review or a watch. A review shows its run count and cost first and starts only when you confirm. One job runs at a time; it keeps running if you close the page.
- **Settings**: edit `config.yaml`, validated and backed up on every save.
```

Add under the bullets near the top of `CLAUDE.md`:

```markdown
- `value_pipeline/web/` is the pipeline's local website (FastAPI + Jinja). Its routes call `value_pipeline/operations.py` and the pipeline modules; keep trade logic there, not in routes, so the CLI and the website stay identical.
```

- [ ] **Step 2: Run everything**

Run: `.venv/bin/ruff check . && .venv/bin/pytest -q && TZ=America/New_York .venv/bin/pytest -q`
Expected: `All checks passed!`, all tests pass in both runs

- [ ] **Step 3: Clean-install smoke test**

Run:
```bash
tmp=$(mktemp -d) && python3 -m venv "$tmp/venv" && "$tmp/venv/bin/pip" install -q . && (cd "$tmp" && "$tmp/venv/bin/python" -c "import value_pipeline.web, pathlib, value_pipeline.web.app as a; assert (pathlib.Path(a.TEMPLATES) / 'base.html').exists(); print('smoke OK')"); rm -rf "$tmp"
```
Expected: `smoke OK` (templates are packaged)

- [ ] **Step 4: Real check against the real data folder**

Run `uv run python -m value_pipeline web --no-browser` in the background, then request each page and confirm HTTP 200 and expected content:

```bash
for p in / /trade /trade?ticker=HRB /trades /reviews /stock/HRB /screen /run /settings "/reports/2026-10-11/ACTIONS.html"; do
  printf "%s " "$p"; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8765$p"; done
```
Expected: `200` for every page. Then stop the server. Do not record trades or start reviews against the real data during this check.

- [ ] **Step 5: Commit**

```bash
git add value_pipeline/README.md CLAUDE.md
git commit -m "docs(web): how to use the local website

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
