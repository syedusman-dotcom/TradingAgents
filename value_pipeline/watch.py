"""The free weekly check between monthly reviews: prices only, no agents.

It never changes ratings or positions; it says when something deserves a look
before the next review.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from value_pipeline import market_data, metrics, portfolio as pfmod
from value_pipeline.actions import BUY_RATINGS
from value_pipeline.config import Paths

log = logging.getLogger(__name__)


@dataclass
class Alert:
    ticker: str
    kind: str          # REACHED VALUE / FREEFALL / BUY RANGE
    message: str


def alerts(holdings: list[dict], ideas: list[dict], prices: dict, trends: dict, buy_mos: float) -> list[Alert]:
    """Holdings: ticker, intrinsic_value, avg_price. Ideas (not held): ticker, rating, intrinsic_value."""
    out = []
    for h in holdings:
        t, price, value = h["ticker"], prices.get(h["ticker"]), h.get("intrinsic_value")
        if price is not None and value and price > value:
            out.append(Alert(t, "REACHED VALUE",
                             f"{t} at {price:,.2f} is above its intrinsic value {value:,.2f}: consider selling"))
        if trends.get(t) == "FREEFALL":
            out.append(Alert(t, "FREEFALL",
                             f"{t} is in FREEFALL: check the thesis at the next review (no automatic sale)"))
    for idea in ideas:
        t, price, value = idea["ticker"], prices.get(idea["ticker"]), idea.get("intrinsic_value")
        if idea.get("rating") not in BUY_RATINGS or price is None or not value or trends.get(t) == "FREEFALL":
            continue
        mos = (value - price) / value
        if mos >= buy_mos:
            out.append(Alert(t, "BUY RANGE",
                             f"{t} at {price:,.2f} is {mos:.0%} below its value {value:,.2f} "
                             f"(last rated {idea['rating']}): in the buy range"))
    return out


def _latest_results(paths: Paths) -> list[dict]:
    """Each ticker's newest result across the review folders."""
    from value_pipeline.review import review_dirs

    latest: dict[str, dict] = {}
    for folder in review_dirs(paths):
        f = folder / "results.json"
        if f.exists():
            for r in json.loads(f.read_text(encoding="utf-8")):
                if not r.get("error"):
                    latest.setdefault(r["ticker"], r)
    return list(latest.values())


def _notify(title: str, text: str) -> None:
    """A macOS notification, so a scheduled run is seen without opening a file."""
    if platform.system() != "Darwin":
        return
    script = f'display notification {json.dumps(text)} with title {json.dumps(title)}'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("notification failed: %s", exc)


def run(paths: Paths, cfg: dict, notify: bool = True) -> tuple[list[Alert], Path]:
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    holdings = [{"ticker": p.ticker, "intrinsic_value": p.intrinsic_value_estimate, "avg_price": p.avg_price}
                for p in pf.positions]
    ideas = [r for r in _latest_results(paths) if r["ticker"] not in owned]
    tickers = sorted(owned | {i["ticker"] for i in ideas})
    prices = {t: market_data.latest_price(t) for t in tickers}
    trends = {}
    for t in tickers:
        try:
            trends[t] = metrics.trend(market_data.closes(t)).get("trend")
        except Exception as exc:  # noqa: BLE001 - a missing trend just skips that alert
            log.warning("no trend for %s: %s", t, exc)

    found = alerts(holdings, ideas, prices, trends, cfg["watch"]["buy_margin_of_safety"])
    today = date.today().isoformat()
    lines = [f"# Weekly watch {today}", "",
             "Prices only, no agent runs. Nothing changes until you act and record a trade.", ""]
    lines += [f"- **{a.kind}**: {a.message}" for a in found] or ["Nothing needs attention this week."]
    lines += ["", f"Checked: {', '.join(tickers) if tickers else 'no holdings or rated ideas yet'}"]
    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f"watch_{today}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if notify and found:
        _notify("Value watch", f"{len(found)} alert(s): " + "; ".join(f"{a.ticker} {a.kind}" for a in found))
    return found, out
