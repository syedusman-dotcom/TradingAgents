"""The weekday check: which holdings and ideas changed enough to deserve a re-review.

``triggers`` and ``select`` are pure; ``run`` fetches prices, re-runs the agents on the
names that fired and writes the report. Nothing here trades.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from value_pipeline import lock, market_data, metrics, portfolio as pfmod, review, watch
from value_pipeline.config import Paths
from value_pipeline.render import demand_first, numbered

log = logging.getLogger(__name__)

KINDS = ("ENTRY PRICE", "REACHED VALUE", "EARNINGS", "FREEFALL", "BIG MOVE")
_NO_COOLDOWN = {"REACHED VALUE", "EARNINGS"}


@dataclass
class Trigger:
    ticker: str
    kind: str          # one of KINDS
    message: str


def _entry(t: str, price: float, entry, last) -> Trigger | None:
    """Fires when the price is at or below the entry and the last review did not already see it so."""
    if entry and price <= entry and not (last and last <= entry):
        return Trigger(t, "ENTRY PRICE", f"{t} at {price:,.2f} is at or below its entry price {entry:,.2f}")
    return None


def _common(h: dict, price: float, trend, today: date, cfg: dict) -> list[Trigger]:
    """EARNINGS, FREEFALL and BIG MOVE, which apply to holdings and ideas alike."""
    t, out = h["ticker"], []
    if trend == "FREEFALL" and h.get("last_trend") != "FREEFALL":
        out.append(Trigger(t, "FREEFALL", f"{t} has entered FREEFALL (price {price:,.2f})"))
    reported, reviewed = h.get("next_earnings_date"), h.get("last_review_date")
    if reported and reviewed and reported <= (today - timedelta(days=1)).isoformat() and reviewed < reported:
        out.append(Trigger(t, "EARNINGS", f"{t} reported on {reported} (last review {reviewed})"))
    last = h.get("last_price")
    if last:
        move = price / last - 1
        if round(abs(move), 6) >= cfg["daily"]["big_move"]:
            out.append(Trigger(t, "BIG MOVE", f"{t} moved {move:+.0%} since its review at {last:,.2f}"))
    return out


def triggers(holdings: list[dict], ideas: list[dict], prices: dict, trends: dict, today: date,
             cfg: dict) -> list[Trigger]:
    """Holdings: ticker, intrinsic_value, entry_price, next_earnings_date, last_review_date, last_price,
    last_trend. Ideas: a results.json row plus ``date``. Missing data means that trigger stays quiet."""
    out: list[Trigger] = []
    for h in holdings:
        t, price = h["ticker"], prices.get(h["ticker"])
        if price is None:
            continue
        found = []
        if (hit := _entry(t, price, h.get("entry_price"), h.get("last_price"))):
            found.append(hit)
        value, last = h.get("intrinsic_value"), h.get("last_price")
        if value and price >= value and not (last and last >= value):
            found.append(Trigger(t, "REACHED VALUE",
                                 f"{t} at {price:,.2f} is at or above its value {value:,.2f}"))
        found += _common(h, price, trends.get(t), today, cfg)
        out += sorted(found, key=lambda x: KINDS.index(x.kind))
    for row in ideas:
        t, price = row["ticker"], prices.get(row["ticker"])
        if price is None:
            continue
        h = {"ticker": t, "last_price": row.get("price"), "last_trend": row.get("trend"),
             "next_earnings_date": row.get("earnings_date"), "last_review_date": row.get("date")}
        found = [hit] if (hit := _entry(t, price, row.get("entry_price"), row.get("price"))) else []
        found += _common(h, price, trends.get(t), today, cfg)
        out += sorted(found, key=lambda x: KINDS.index(x.kind))
    return out


def select(found: list[Trigger], last_review: dict[str, str], today: date,
           cfg: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """Tickers to re-review today, in priority order, and those deferred with the reason."""
    cooldown, cap = cfg["daily"]["cooldown_days"], cfg["daily"]["max_reviews"]
    by_ticker: dict[str, list[str]] = {}
    for tr in found:
        by_ticker.setdefault(tr.ticker, []).append(tr.kind)
    ranked, deferred = [], []
    for t, kinds in by_ticker.items():
        reviewed = last_review.get(t)
        if (reviewed and not _NO_COOLDOWN & set(kinds)
                and (today - date.fromisoformat(reviewed)).days < cooldown):
            deferred.append((t, f"cooldown: reviewed on {reviewed}"))
        else:
            ranked.append((min(KINDS.index(k) for k in kinds), t))
    ranked.sort()
    chosen = [t for _, t in ranked[:cap]]
    deferred += [(t, f"over the daily cap of {cap}") for _, t in ranked[cap:]]
    return chosen, deferred


def _iso(text) -> date | None:
    """A stored date, or None when it is missing or malformed (treated as unknown)."""
    try:
        return date.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _inputs(paths: Paths, cfg: dict, today: date):
    """Holdings, ideas, the kill criteria of each holding and the last review date per ticker."""
    pf = pfmod.load(paths)
    latest = {r["ticker"]: r for r in watch.latest_results(paths)}
    holdings, criteria, last_review = [], {}, {}
    for p in pf.positions:
        row = latest.get(p.ticker, {})
        holdings.append({"ticker": p.ticker, "intrinsic_value": p.intrinsic_value_estimate,
                         "entry_price": p.entry_price, "next_earnings_date": p.next_earnings_date,
                         "last_review_date": p.last_review_date, "last_price": row.get("price"),
                         "last_trend": row.get("trend")})
        criteria[p.ticker] = demand_first(p.kill_criteria, p.lane)
        if _iso(p.last_review_date):
            last_review[p.ticker] = p.last_review_date
    max_age = cfg["watch"].get("max_rating_age_days", 120)
    ideas = []
    for t, row in latest.items():
        reviewed = _iso(row.get("date"))
        if (pf.position(t) is None and row.get("rating") != "Sell" and reviewed
                and (today - reviewed).days <= max_age):
            ideas.append(row)
            last_review[t] = row["date"]
    return holdings, ideas, criteria, last_review


def _prices_and_trends(tickers: list[str]) -> tuple[dict, dict]:
    """As the watch does: one price and one history call per ticker; a failure skips that ticker."""
    prices, trends = {}, {}
    for t in tickers:
        try:
            prices[t] = market_data.latest_price(t)
        except Exception as exc:  # noqa: BLE001 - no price just means no trigger
            log.warning("no price for %s: %s", t, exc)
        try:
            trends[t] = metrics.trend(market_data.closes(t)).get("trend")
        except Exception as exc:  # noqa: BLE001 - a missing trend just skips that trigger
            log.warning("no trend for %s: %s", t, exc)
    return prices, trends


def _errored(folder: Path) -> dict[str, str]:
    """Tickers whose run failed in that review (no report to link), with the error."""
    try:
        rows = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s/results.json: %s", folder, exc)
        return {}
    return {r["ticker"]: f"no report ({r['error']})" for r in rows if r.get("error")}


def _reviews(paths: Paths, cfg: dict, selected: list[str],
             dry_run: bool) -> tuple[list[str], int, int]:
    """Run the agents once on every selected ticker; the report lines, reviews run, exit code."""
    if not cfg["daily"].get("enabled", True):
        return ["Disabled in config."], 0, 0
    if not selected:
        return ["None."], 0, 0
    if dry_run:
        return ["Dry run: nothing re-run."], 0, 0
    try:
        actions = review.run(paths, cfg, yes=True, dry_run=False, only=selected)
    except (lock.Locked, review.ReviewRunning):
        return ["a review is already running; triggers are listed, nothing was re-run."], 0, 0
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - reported, and the exit code says so
        log.warning("daily review failed: %s", exc)
        return [f"review failed: {exc}"], 0, 1
    folder = actions.parent.relative_to(paths.reports).as_posix()
    # Site paths, as the Reviews page links them: the dashboard renders this report.
    lines = [f"- [ACTIONS](/reports/{folder}/ACTIONS.html)"]
    failed = _errored(actions.parent)
    lines += [f"- {t}: {failed[t]}" if t in failed
              else f"- [{t}](/reports/{folder}/{t}/complete_report.html)" for t in selected]
    return lines, len(selected), 0


def run(paths: Paths, cfg: dict, *, dry_run: bool = False, notify: bool = True,
        max_reviews: int | None = None,
        today: date | None = None) -> tuple[list[Trigger], Path, int]:
    """Check prices, re-review what fired, write ``reports/daily_<date>.md``; returns
    (triggers, report path, exit code), the code 1 only when the review failed."""
    today = today or date.today()
    if max_reviews is not None:
        cfg = {**cfg, "daily": {**cfg["daily"], "max_reviews": max(0, max_reviews)}}
    holdings, ideas, criteria, last_review = _inputs(paths, cfg, today)
    rows = holdings + [{"ticker": i["ticker"], "last_price": i.get("price")} for i in ideas]
    prices, trends = _prices_and_trends([r["ticker"] for r in rows])
    fired = triggers(holdings, ideas, prices, trends, today, cfg)
    selected, deferred = select(fired, last_review, today, cfg)
    review_lines, n_run, code = _reviews(paths, cfg, selected, dry_run)

    lines = [f"# Daily check {today.isoformat()}", "",
             "Prices, then the agents on what changed. Nothing is traded: you act at your broker "
             "and record the trade.", "", "## Checked", ""]
    if rows:
        lines += ["| Ticker | Price | Since review | Trend |", "| --- | ---: | ---: | --- |"]
        for r in rows:
            t, price, last = r["ticker"], prices.get(r["ticker"]), r.get("last_price")
            since = f"{price / last - 1:+.0%}" if price is not None and last else "–"
            shown = f"{price:,.2f}" if price is not None else "no price"
            lines.append(f"| {t} | {shown} | {since} | {trends.get(t) or '–'} |")
    else:
        lines.append("No holdings or rated ideas yet.")
    lines += ["", "## Triggers", ""]
    lines += [f"- **{tr.kind}**: {tr.message}" for tr in fired] or ["Nothing fired."]
    lines += ["", "## Reviews run", "", *review_lines]
    if deferred:
        lines += ["", "## Deferred", "", *[f"- {t}: {why}" for t, why in deferred]]
    hit = list(dict.fromkeys(tr.ticker for tr in fired if criteria.get(tr.ticker)))
    if hit:
        lines += ["", "## Kill criteria to check", "",
                  "Shown for you to check against the news; nothing here is evaluated.", ""]
        lines += [f"- {t}: {numbered(criteria[t])}" for t in hit]
    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f"daily_{today.isoformat()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if notify:
        text = (f"{len(fired)} trigger(s): " + "; ".join(f"{tr.ticker} {tr.kind}" for tr in fired)
                + f" — {n_run} review(s) run") if fired else "Nothing fired"
        watch._notify("Daily check", text)
    return fired, out, code
