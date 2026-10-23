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
from value_pipeline.render import numbered

log = logging.getLogger(__name__)


@dataclass
class Alert:
    ticker: str
    kind: str          # REACHED VALUE / FREEFALL / BUY RANGE / ENTRY PRICE
    message: str


def alerts(holdings: list[dict], ideas: list[dict], prices: dict, trends: dict, buy_mos: float,
           today: date | None = None, max_age_days: int = 120) -> list[Alert]:
    """Holdings: ticker, intrinsic_value, avg_price. Ideas (not held): ticker, rating, intrinsic_value,
    date; a Buy rating older than ``max_age_days`` is stale (the facts may have changed) and is not alerted."""
    today = today or date.today()
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
        if price is None or trends.get(t) == "FREEFALL":
            continue
        if idea.get("date") and (today - date.fromisoformat(idea["date"])).days > max_age_days:
            continue
        entry = idea.get("entry_price")
        if entry and idea.get("rating") != "Sell" and price <= entry:
            out.append(Alert(t, "ENTRY PRICE",
                             f"{t} at {price:,.2f} is at or below the agents' entry price {entry:,.2f} "
                             f"(rated {idea.get('rating')} on {idea.get('date')})"))
        if idea.get("rating") not in BUY_RATINGS or not value:
            continue
        mos = (value - price) / value
        if mos >= buy_mos:
            out.append(Alert(t, "BUY RANGE",
                             f"{t} at {price:,.2f} is {mos:.0%} below its value {value:,.2f} "
                             f"(last rated {idea['rating']}): in the buy range"))
    return out


def latest_results(paths: Paths) -> list[dict]:
    """Each ticker's newest result across the review folders."""
    from value_pipeline.review import review_dirs

    latest: dict[str, dict] = {}
    for folder in review_dirs(paths):
        f = folder / "results.json"
        if f.exists():
            for r in json.loads(f.read_text(encoding="utf-8")):
                if not r.get("error"):
                    latest.setdefault(r["ticker"], {**r, "date": folder.name[:10]})
    return list(latest.values())


_latest_results = latest_results


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
    ideas = [r for r in latest_results(paths) if r["ticker"] not in owned]
    tickers = sorted(owned | {i["ticker"] for i in ideas})
    prices = {t: market_data.latest_price(t) for t in tickers}
    trends = {}
    for t in tickers:
        try:
            trends[t] = metrics.trend(market_data.closes(t)).get("trend")
        except Exception as exc:  # noqa: BLE001 - a missing trend just skips that alert
            log.warning("no trend for %s: %s", t, exc)

    found = alerts(holdings, ideas, prices, trends, cfg["watch"]["buy_margin_of_safety"],
                   max_age_days=cfg["watch"].get("max_rating_age_days", 120))
    today = date.today().isoformat()
    lines = [f"# Weekly watch {today}", "",
             "Prices only, no agent runs. Nothing changes until you act and record a trade.", ""]
    lines += [f"- **{a.kind}**: {a.message}" for a in found] or ["Nothing needs attention this week."]
    criteria = [(p.ticker, p.kill_criteria) for p in pf.positions if p.kill_criteria]
    if criteria:
        lines += ["", "## Kill criteria to check", "",
                  "Shown for you to check against the news; nothing here is evaluated."]
        lines += [f"- {t}: {numbered(c)}" for t, c in criteria]
    lines += ["", f"Checked: {', '.join(tickers) if tickers else 'no holdings or rated ideas yet'}"]
    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f"watch_{today}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if notify and found:
        _notify("Value watch", f"{len(found)} alert(s): " + "; ".join(f"{a.ticker} {a.kind}" for a in found))
    return found, out
