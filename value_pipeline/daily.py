"""The weekday check: which holdings and ideas changed enough to deserve a re-review.

Pure functions; the run itself (prices, reviews, report) is added on top of these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

KINDS = ("ENTRY PRICE", "REACHED VALUE", "EARNINGS", "FREEFALL", "BIG MOVE")
_NO_COOLDOWN = {"REACHED VALUE", "EARNINGS"}


@dataclass
class Trigger:
    ticker: str
    kind: str          # one of KINDS
    message: str


def _entry(t: str, price: float, entry) -> Trigger | None:
    if entry and price <= entry:
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
        if (hit := _entry(t, price, h.get("entry_price"))):
            found.append(hit)
        value = h.get("intrinsic_value")
        if value and price >= value:
            found.append(Trigger(t, "REACHED VALUE", f"{t} at {price:,.2f} is above its value {value:,.2f}"))
        found += _common(h, price, trends.get(t), today, cfg)
        out += sorted(found, key=lambda x: KINDS.index(x.kind))
    for row in ideas:
        t, price = row["ticker"], prices.get(row["ticker"])
        if price is None:
            continue
        h = {"ticker": t, "last_price": row.get("price"), "last_trend": row.get("trend"),
             "next_earnings_date": row.get("earnings_date"), "last_review_date": row.get("date")}
        found = [hit] if (hit := _entry(t, price, row.get("entry_price"))) else []
        found += _common(h, price, trends.get(t), today, cfg)
        out += sorted(found, key=lambda x: KINDS.index(x.kind))
    return out


def select(triggers: list[Trigger], last_review: dict[str, str], today: date,
           cfg: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """Tickers to re-review today, in priority order, and those deferred with the reason."""
    cooldown, cap = cfg["daily"]["cooldown_days"], cfg["daily"]["max_reviews"]
    by_ticker: dict[str, list[str]] = {}
    for tr in triggers:
        by_ticker.setdefault(tr.ticker, []).append(tr.kind)
    ranked, deferred = [], []
    for t, found in by_ticker.items():
        reviewed = last_review.get(t)
        if (reviewed and not _NO_COOLDOWN & set(found)
                and (today - date.fromisoformat(reviewed)).days < cooldown):
            deferred.append((t, f"cooldown: reviewed on {reviewed}"))
        else:
            ranked.append((min(KINDS.index(k) for k in found), t))
    ranked.sort()
    chosen = [t for _, t in ranked[:cap]]
    deferred += [(t, f"over the daily cap of {cap}") for _, t in ranked[cap:]]
    return chosen, deferred
