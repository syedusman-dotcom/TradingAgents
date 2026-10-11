"""Recording trades and reading the portfolio: one implementation for the CLI and the website.

Nothing here trades. It records what you did at your broker, keeps a one-step
undo, and values the holdings.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

from value_pipeline import benchmark, market_data, portfolio as pfmod, review
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


def _remember(paths: Paths, backup_dir: Path | None, description: str) -> None:
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
    on = on or date.today().isoformat()
    pfmod.apply_deposit(pf, amount, on=on)
    before = pfmod.save(paths, pf)
    kind = "DEPOSIT" if amount > 0 else "WITHDRAW"
    _remember(paths, before, f"{kind} {abs(amount):,.2f} on {on}")
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
    saved = Path(info["backup"])
    if not (saved / paths.portfolio.name).exists():
        raise UndoUnavailable(f"The backup this undo needs ({saved}) is gone, so nothing was changed.")
    pfmod.backup(paths)
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
