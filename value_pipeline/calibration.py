"""Calibration: every past decision set against the prices that followed it.

``rows`` reads each real review's ``results.json`` (dry runs and errored rows left out) and
records, for every horizon already reached, the close then, the return and whether the price
was at or above the agents' value, below the bear case or below the entry price. ``score``
sums them up by rating and by lane. Nothing here trades; it only looks back.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from functools import partial

from value_pipeline import market_data
from value_pipeline.config import Paths
from value_pipeline.review import review_dirs

HORIZONS = (30, 90, 180, 365)
BUYS = ("Buy", "Overweight")
SELLS = ("Sell", "Underweight")
MIN_CALIBRATION_ROWS = 10


def _num(x) -> float | None:
    try:
        return None if x is None or isinstance(x, bool) else float(x)
    except (TypeError, ValueError):
        return None


def _leg(scenarios, leg: str, key: str) -> float | None:
    if not isinstance(scenarios, dict) or not isinstance(scenarios.get(leg), dict):
        return None
    return _num(scenarios[leg].get(key))


def _below(price: float, ref: float | None) -> bool | None:
    return None if ref is None else price < ref


def _horizon(price_then: float | None, later: float | None, value, bear, entry) -> dict | None:
    if later is None:
        return None
    return {"price": later,
            "ret": later / price_then - 1 if price_then else None,
            "reached_value": None if value is None else later >= value,
            "below_bear": _below(later, bear),
            "below_entry": _below(later, entry)}


def price_fn_for(paths: Paths, fetch: bool = True):
    """Closes from the JSON cache in ``data/cache/prices/``; ``fetch=False`` never calls Yahoo."""
    return partial(market_data.close_on_or_before, cache_dir=paths.cache / "prices", fetch=fetch)


def rows(paths: Paths, today: date | None = None, price_fn=None) -> list[dict]:
    """One row per decision in every real review, newest review first.

    ``price_fn(ticker, day) -> float | None`` gives the close on or before a day. By default it
    is the cached Yahoo close, and every close still missing is fetched first with one request
    per ticker. Each ``h30``..``h365`` is None while that day is still in the future or no close
    is known. A row without a recorded price takes the close on the review day. A ticker
    reviewed twice on one day counts once: the newest re-run stands.
    """
    today = today or date.today()
    decisions, seen = [], set()
    for folder in review_dirs(paths):   # newest first, so a same-day re-run comes first
        f = folder / "results.json"
        try:
            results = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        except (OSError, ValueError):
            continue
        day = folder.name[:10]
        for r in results:
            if not isinstance(r, dict) or r.get("error") or not r.get("ticker"):
                continue
            if (day, r["ticker"]) in seen:
                continue
            seen.add((day, r["ticker"]))
            decisions.append((folder.name, day, r))
    if price_fn is None:
        _prefetch(paths, decisions, today)
        price_fn = price_fn_for(paths)
    out = []
    for folder, day, r in decisions:
        reviewed = date.fromisoformat(day)
        ticker, sc = r["ticker"], r.get("scenarios")
        price = _num(r.get("price"))
        if price is None and reviewed <= today:
            price = price_fn(ticker, day)
        value, bear = _num(r.get("intrinsic_value")), _leg(sc, "bear", "value")
        entry = _num(r.get("entry_price"))
        row = {"date": day, "folder": folder, "ticker": ticker, "name": r.get("name"),
               "lane": r.get("lane"), "rating": r.get("rating"), "price": price, "value": value,
               "formula_value": _num(r.get("formula_value")), "bear": bear,
               "base": _leg(sc, "base", "value"), "bull": _leg(sc, "bull", "value"),
               "bear_probability": _leg(sc, "bear", "probability"),
               "bull_probability": _leg(sc, "bull", "probability"),
               "entry_price": entry, "expected_return": _num(r.get("expected_return"))}
        for n in HORIZONS:
            at = reviewed + timedelta(days=n)
            later = price_fn(ticker, at.isoformat()) if at <= today else None
            row[f"h{n}"] = _horizon(price, later, value, bear, entry)
        out.append(row)
    return out


def _prefetch(paths: Paths, decisions: list, today: date) -> None:
    """Every (ticker, day) the rows will ask for, fetched as one series per ticker."""
    needed: dict[str, set[str]] = {}
    for _, day, r in decisions:
        reviewed = date.fromisoformat(day)
        days = needed.setdefault(r["ticker"], set())
        if _num(r.get("price")) is None and reviewed <= today:
            days.add(day)
        days.update((reviewed + timedelta(days=n)).isoformat() for n in HORIZONS
                    if reviewed + timedelta(days=n) <= today)
    for ticker, days in sorted(needed.items()):
        market_data.fill_close_cache(ticker, days, cache_dir=paths.cache / "prices", today=today)


# --- scores ----------------------------------------------------------------------------------

def _share(flags: list[bool]) -> dict | None:
    return {"k": sum(flags), "n": len(flags), "share": sum(flags) / len(flags)} if flags else None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _reached_within_a_year(row: dict) -> bool:
    return any((row.get(f"h{n}") or {}).get("reached_value") for n in HORIZONS)


def _group(rs: list[dict]) -> dict:
    at180 = [r for r in rs if r.get("h180") and r["h180"].get("ret") is not None]
    buys = [r for r in rs
            if r.get("rating") in BUYS and r.get("h365") and r.get("value") is not None]
    sells = [r for r in at180 if r.get("rating") in SELLS]
    return {"n": len(rs),
            "median_ret_180": _median([r["h180"]["ret"] for r in at180]),
            "hit_value_365": _share([_reached_within_a_year(r) for r in buys]),
            "sells_fell_180": _share([r["h180"]["ret"] < 0 for r in sells])}


def _by(rs: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rs:
        groups.setdefault(r.get(key) or "(none)", []).append(r)
    return {k: _group(v) for k, v in sorted(groups.items())}


def _calibrated(rs: list[dict]) -> dict | None:
    at180 = [r for r in rs if r.get("h180")]
    if len(at180) < MIN_CALIBRATION_ROWS:
        return None
    def stated(leg: str) -> list[dict]:
        return [r for r in at180
                if r.get(leg) is not None and r.get(f"{leg}_probability") is not None]

    def mean(xs) -> float | None:
        xs = list(xs)
        return statistics.fmean(xs) if xs else None

    bears, bulls = stated("bear"), stated("bull")
    return {"n": len(at180),
            "below_bear": mean(float(bool(r["h180"]["below_bear"])) for r in bears),
            "bear_probability": mean(r["bear_probability"] for r in bears),
            "above_bull": mean(float(r["h180"]["price"] >= r["bull"]) for r in bulls),
            "bull_probability": mean(r["bull_probability"] for r in bulls)}


def _decisions(n: int) -> str:
    return f"{n} decision{'' if n == 1 else 's'}"


def _verdict(lane: str, rs: list[dict], g: dict) -> str:
    reached = [n for n in HORIZONS if any(r.get(f"h{n}") for r in rs)]
    if 180 not in reached:
        first_unreached = next(n for n in HORIZONS if n not in reached)
        return f"{lane}: {_decisions(g['n'])}, none past {first_unreached} days yet"
    parts = []
    if g["hit_value_365"]:
        hit = g["hit_value_365"]
        parts.append(f"Buy/Overweight reached value within a year {hit['k']}/{hit['n']}")
    if g["sells_fell_180"]:
        parts.append("Sells fell at 180 days {k}/{n}".format(**g["sells_fell_180"]))
    if not parts and g["median_ret_180"] is not None:
        parts.append(f"median return at 180 days {g['median_ret_180']:+.0%}")
    return "; ".join([f"{lane}: {_decisions(g['n'])}", *parts])


def score(rs: list[dict]) -> dict:
    """By rating and by lane: count, median 180-day return, the share of Buy/Overweight calls
    whose price reached the value at a checkpoint within a year, the share of Sell/Underweight
    calls that fell by 180 days; overall the median gap between the agents' value and the
    formula value, and (with at least ten 180-day rows) how often the price ended below the bear
    case or above the bull case against the probabilities the agents stated."""
    gaps = [r["value"] / r["formula_value"] - 1 for r in rs
            if r.get("value") is not None and r.get("formula_value")]
    by_lane = _by(rs, "lane")
    lanes: dict[str, list[dict]] = {}
    for r in rs:
        lanes.setdefault(r.get("lane") or "(none)", []).append(r)
    return {"by_rating": _by(rs, "rating"),
            "by_lane": by_lane,
            "median_value_gap": _median(gaps),
            "calibrated_probabilities": _calibrated(rs),
            "verdicts": [_verdict(lane, lanes[lane], by_lane[lane]) for lane in by_lane]}


def has_horizons(rs: list[dict]) -> bool:
    return any(r.get(f"h{n}") for r in rs for n in HORIZONS)


# --- text ------------------------------------------------------------------------------------

def _pct(x: float | None, signed: bool = True) -> str:
    return "–" if x is None else (f"{x:+.0%}" if signed else f"{x:.0%}")


def _money(x: float | None) -> str:
    return "–" if x is None else f"{x:,.2f}"


def _yes(x: bool | None) -> str:
    return "–" if x is None else ("yes" if x else "no")


def _frac(s: dict | None) -> str:
    return "–" if s is None else f"{s['k']}/{s['n']} ({s['share']:.0%})"


def markdown(rs: list[dict], scores: dict, horizon: int = 180) -> str:
    """The decisions at one horizon, then the scores and one verdict per lane, as Markdown."""
    lines = [f"# Calibration at {horizon} days", ""]
    if not has_horizons(rs):
        lines += ["No decisions old enough yet.", ""]
    lines += ["| Date | Ticker | Lane | Rating | Price then | Value | Return "
              "| Reached value | Below bear |",
              "|---|---|---|---|--:|--:|--:|---|---|"]
    for r in rs:
        h = r.get(f"h{horizon}") or {}
        lines.append(f"| {r['date']} | {r['ticker']} | {r.get('lane') or '–'} "
                     f"| {r.get('rating') or '–'} | "
                     f"{_money(r.get('price'))} | {_money(r.get('value'))} | {_pct(h.get('ret'))} | "
                     f"{_yes(h.get('reached_value'))} | {_yes(h.get('below_bear'))} |")
    lines += ["", "## Scores", ""]
    for title, key in (("By rating", "by_rating"), ("By lane", "by_lane")):
        lines += [f"### {title}", "",
                  "| | Decisions | Median return at 180 days | Reached value within a year "
                  "| Sells fell at 180 days |",
                  "|---|--:|--:|--:|--:|"]
        for name, g in scores[key].items():
            lines.append(f"| {name} | {g['n']} | {_pct(g['median_ret_180'])} "
                         f"| {_frac(g['hit_value_365'])} | "
                         f"{_frac(g['sells_fell_180'])} |")
        lines.append("")
    lines.append("Median gap between the agents' value and the formula value: "
                 f"{_pct(scores['median_value_gap'])}")
    cp = scores["calibrated_probabilities"]
    if cp:
        lines.append(f"\nProbabilities over {cp['n']} decisions at 180 days: below the bear case "
                     f"{_pct(cp['below_bear'], False)} (stated {_pct(cp['bear_probability'], False)}), "
                     f"above the bull case {_pct(cp['above_bull'], False)} "
                     f"(stated {_pct(cp['bull_probability'], False)})")
    else:
        lines.append("\nProbabilities: not enough decisions past 180 days yet "
                     f"(needs {MIN_CALIBRATION_ROWS}).")
    lines += ["", "## Verdicts", "", *[f"- {v}" for v in scores["verdicts"]], ""]
    return "\n".join(lines)
