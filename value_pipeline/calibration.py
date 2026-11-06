"""Calibration: every past decision set against the prices that followed it.

``rows`` reads each real review's ``results.json`` (dry runs and errored rows left out) and
records, for every horizon already reached, the close then, the return and whether the price
was at or above the agents' value, below the bear case or below the entry price. ``score``
sums them up by rating and by lane. Each row also carries the paper Buy rules it met
(``value_pipeline.paper``) and its dollar return and excess over SPY, which ``paper_scores`` sums
up per rule. Nothing here trades; it only looks back.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from functools import partial

from tradingagents.dataflows.vendors.yahoo.common import MINOR_UNITS
from value_pipeline import market_data, paper
from value_pipeline.config import Paths
from value_pipeline.review import review_dirs

HORIZONS = (30, 90, 180, 365)
BUYS = ("Buy", "Overweight")
SELLS = ("Sell", "Underweight")
MIN_CALIBRATION_ROWS = 10
MIN_BRIER_ROWS = 30
OVER_CONFIDENT = 0.10        # stated mean above the realised frequency by more than this
BENCHMARK = "SPY"
PAPER_GAP_DAYS = 90          # the same ticker counts again only this long after its last count
PAPER_HORIZON = 180
MIN_PAPER = 20
MIN_PAPER_MONTHS = 3
PAPER_EXCESS = 0.03          # mean dollar return ahead of SPY, as a fraction
PAPER_AHEAD = 0.55           # share of paper Buys ahead of SPY
PAPER_OVER_BASELINE = 0.02   # and this far ahead of every reviewed stock
EPS = 1e-9


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


def pair(currency: str | None) -> str | None:
    """The Yahoo symbol for US dollars per unit of ``currency``; None when it is dollars."""
    if currency is None or currency == "USD":
        return None
    return f"{MINOR_UNITS.get(currency, currency)}USD=X"


def _moved(price_fn, symbol: str | None, day: str, later_day: str) -> float | None:
    """The close on ``later_day`` over the close on ``day``, or None if either is missing."""
    if symbol is None:
        return None
    then, later = price_fn(symbol, day), price_fn(symbol, later_day)
    return later / then if then and later is not None else None


def _in_dollars(h: dict, price_fn, row: dict, pair_symbol: str | None, later_day: str) -> None:
    """Add ``ret_usd``, ``spy`` and ``excess`` to a reached horizon."""
    if h["ret"] is None:
        ret_usd = None
    elif pair_symbol is None:
        ret_usd = h["ret"]
    else:
        rate = _moved(price_fn, pair_symbol, row["date"], later_day)
        ret_usd = None if rate is None else (1 + h["ret"]) * rate - 1
    spy = _moved(price_fn, BENCHMARK, row["date"], later_day)
    spy = None if spy is None else spy - 1
    h.update({"ret_usd": ret_usd, "spy": spy,
              "excess": None if ret_usd is None or spy is None else ret_usd - spy})


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
               "entry_price": entry, "expected_return": _num(r.get("expected_return")),
               "paper": paper.flags(r), "currency": r.get("currency"),
               "reached": [n for n in HORIZONS if reviewed + timedelta(days=n) <= today]}
        for n in HORIZONS:
            at = reviewed + timedelta(days=n)
            later = price_fn(ticker, at.isoformat()) if at <= today else None
            h = row[f"h{n}"] = _horizon(price, later, value, bear, entry)
            if h:
                _in_dollars(h, price_fn, row, pair(row["currency"]), at.isoformat())
        out.append(row)
    return out


def _prefetch(paths: Paths, decisions: list, today: date) -> None:
    """Every (ticker, day) the rows will ask for, fetched as one series per ticker."""
    needed: dict[str, set[str]] = {}
    for _, day, r in decisions:
        reviewed = date.fromisoformat(day)
        reached = {(reviewed + timedelta(days=n)).isoformat() for n in HORIZONS
                   if reviewed + timedelta(days=n) <= today}
        days = needed.setdefault(r["ticker"], set())
        if _num(r.get("price")) is None and reviewed <= today:
            days.add(day)
        days.update(reached)
        # SPY and the currency pair are read on the review day and on every reached horizon.
        for symbol in (BENCHMARK, pair(r.get("currency"))):
            if symbol is not None and reviewed <= today:
                needed.setdefault(symbol, set()).update({day, *reached})
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


def _brier_leg(ps: list[float], outcomes: list[float]) -> dict:
    bs = statistics.fmean((p - o) ** 2 for p, o in zip(ps, outcomes, strict=True))
    base = statistics.fmean(outcomes)
    ref = statistics.fmean((base - o) ** 2 for o in outcomes)
    return {"brier": bs, "reference": ref, "skill": None if ref == 0 else 1 - bs / ref,
            "stated": statistics.fmean(ps), "realised": base}


def _brier_text(name: str, g: dict) -> str:
    if g["skill"] is None:
        skill = "no skill score, every outcome was the same"
    else:
        skill = f"skill {g['skill']:+.2f}"
    return (f"{name}: Brier {g['brier']:.2f} vs {g['reference']:.2f} for the base rate ({skill}), "
            f"stated {g['stated']:.0%} vs realised {g['realised']:.0%}")


def brier(rs: list[dict], horizon: int = 365) -> dict | None:
    """Brier scores of the stated bear and bull probabilities at one horizon.

    Over the rows with ``h{horizon}`` and a scenario value and probability for the leg, the bear
    outcome is 1 when the price at the horizon is at or below the bear value (the bull outcome:
    at or above the bull value), else 0. ``brier`` is the mean squared gap between the stated
    probability and the outcome; ``reference`` is the same score for a forecaster who always
    states the realised frequency; ``skill`` is 1 - brier / reference (None when every outcome
    was the same). None below ``MIN_BRIER_ROWS`` rows.

    Reading rule, fixed in advance: with at least 30 rows at 365 days the probabilities are
    informative when both skill scores are above 0, over-confident when the stated mean exceeds
    the realised frequency by more than 10 points for either leg, uninformative otherwise.
    Before 30 rows the reading is "not yet readable (n of 30 at 365 days)". The same rule
    labels the 180-day read, which is an early read.
    """
    have = [r for r in rs if r.get(f"h{horizon}")]
    out: dict = {"horizon": horizon}
    legs = {}
    for leg, hit in (("bear", lambda price, ref: price <= ref), ("bull", lambda price, ref: price >= ref)):
        use = [r for r in have if r.get(leg) is not None and r.get(f"{leg}_probability") is not None]
        if len(use) < MIN_BRIER_ROWS:
            return None
        legs[leg] = (use, [r[f"{leg}_probability"] for r in use],
                     [float(hit(r[f"h{horizon}"]["price"], r[leg])) for r in use])
    out["n"] = min(len(v[0]) for v in legs.values())
    for leg, (_, ps, outcomes) in legs.items():
        out[leg] = _brier_leg(ps, outcomes)
    bear, bull = out["bear"], out["bull"]
    if all(g["skill"] is not None and g["skill"] > EPS for g in (bear, bull)):
        label = "Informative"
    elif any(g["stated"] - g["realised"] > OVER_CONFIDENT + EPS for g in (bear, bull)):
        label = "Over-confident"
    else:
        label = "Uninformative"
    out["reading"] = f"{label}. {_brier_text('Bear', bear)}; {_brier_text('Bull', bull)}"
    return out


def brier_view(rs: list[dict], scores: dict) -> list[dict]:
    """The 365-day reading and the 180-day early read, as label and text."""
    view = []
    for h, label in ((365, "365 days"), (180, "180 days, early read")):
        b = scores["brier"][str(h)]
        if b:
            text = b["reading"]
        else:
            # Both legs need the rows, so the shorter one is how far along the reading is.
            n = min(sum(1 for r in rs if r.get(f"h{h}") and r.get(leg) is not None
                        and r.get(f"{leg}_probability") is not None) for leg in ("bear", "bull"))
            text = f"not yet readable ({n} of {MIN_BRIER_ROWS} at {h} days)"
        view.append({"label": label, "text": text})
    return view


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
    case or above the bull case against the probabilities the agents stated, and their Brier
    scores at 365 and 180 days (see ``brier``)."""
    gaps = [r["value"] / r["formula_value"] - 1 for r in rs
            if r.get("value") is not None and r.get("formula_value")]
    by_lane = _by(rs, "lane")
    lanes: dict[str, list[dict]] = {}
    for r in rs:
        lanes.setdefault(r.get("lane") or "(none)", []).append(r)
    return {"paper": paper_scores(rs),
            "by_rating": _by(rs, "rating"),
            "by_lane": by_lane,
            "median_value_gap": _median(gaps),
            "calibrated_probabilities": _calibrated(rs),
            "brier": {"365": brier(rs, 365), "180": brier(rs, 180)},
            "verdicts": [_verdict(lane, lanes[lane], by_lane[lane]) for lane in by_lane]}


def counted(rs: list[dict], rule: str | None = None, gap_days: int = PAPER_GAP_DAYS) -> list[dict]:
    """The rows that count for ``rule`` (every row when None), oldest first. A ticker counts again
    only ``gap_days`` or more after the last time it counted, so a stock re-reviewed often is not
    counted many times over."""
    wanted = [r for r in rs if rule is None or rule in (r.get("paper") or [])]
    last: dict[str, date] = {}
    out = []
    for r in sorted(wanted, key=lambda r: (r["date"], r["ticker"], r.get("folder") or "")):
        day = date.fromisoformat(r["date"])
        if r["ticker"] in last and (day - last[r["ticker"]]).days < gap_days:
            continue
        last[r["ticker"]] = day
        out.append(r)
    return out


def _scored(h: dict | None) -> bool:
    return bool(h) and h.get("excess") is not None


def _reached(r: dict) -> list[int]:
    """The horizons whose day has come for a row (a hand-made row without ``reached`` gives the
    horizons it has a close for)."""
    got = r.get("reached")
    return got if got is not None else [n for n in HORIZONS if r.get(f"h{n}")]


def _paper_horizon(rows_: list[dict], n: int) -> dict:
    hs = [r[f"h{n}"] for r in rows_ if r.get(f"h{n}") and r[f"h{n}"].get("excess") is not None]
    excess = [h["excess"] for h in hs]
    unscored = sum(n in _reached(r) and not _scored(r.get(f"h{n}")) for r in rows_)
    return {"n": len(hs), "no_dollar_result": unscored,
            "mean_ret_usd": statistics.fmean(h["ret_usd"] for h in hs) if hs else None,
            "mean_excess": statistics.fmean(excess) if hs else None,
            "median_excess": _median(excess),
            "ahead": _share([x > 0 for x in excess])}


def _paper_horizons(rows_: list[dict]) -> dict:
    return {n: _paper_horizon(rows_, n) for n in HORIZONS}


def _points(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _paper_verdict(rule: str, rule_rows: list[dict], baseline_mean: float | None) -> dict:
    done = [r for r in rule_rows
            if (r.get(f"h{PAPER_HORIZON}") or {}).get("excess") is not None]
    n = len(done)
    months = len({r["date"][:7] for r in done})
    excess = [r[f"h{PAPER_HORIZON}"]["excess"] for r in done]
    mean = statistics.fmean(excess) if excess else None
    ahead = sum(x > 0 for x in excess) / n if n else None
    out = {"supports": None, "sentence": "", "n": n, "months": months, "mean_excess": mean,
           "ahead": ahead, "baseline_mean_excess": baseline_mean}
    if n < MIN_PAPER or months < MIN_PAPER_MONTHS:
        out["sentence"] = (f"{rule}: not enough paper Buys yet ({n} of {MIN_PAPER} past "
                           f"{PAPER_HORIZON} days, from {months} of {MIN_PAPER_MONTHS} months).")
        return out
    failed = []
    if mean < PAPER_EXCESS - EPS:
        failed.append(f"the average gain over SPY is under {PAPER_EXCESS:.0%}")
    if ahead < PAPER_AHEAD - EPS:
        failed.append(f"fewer than {PAPER_AHEAD:.0%} are ahead of SPY")
    if baseline_mean is None:
        failed.append("there is no all-stocks baseline to compare with")
    elif mean - baseline_mean < PAPER_OVER_BASELINE - EPS:
        failed.append(f"the average is not at least {PAPER_OVER_BASELINE * 100:.0f} points "
                      "above the all-stocks baseline")
    out["supports"] = not failed
    facts = (f"{n} paper Buys past {PAPER_HORIZON} days gained {_points(mean)} on average over "
             f"SPY, {round(ahead * n)}/{n} ahead of SPY, against {_points(baseline_mean)} for "
             "every reviewed stock." if baseline_mean is not None else
             f"{n} paper Buys past {PAPER_HORIZON} days gained {_points(mean)} on average over "
             f"SPY, {round(ahead * n)}/{n} ahead of SPY.")
    if failed:
        out["sentence"] = f"{rule} does not support a live test of the rule: {facts} " \
                          f"Failed: {'; '.join(failed)}."
    else:
        out["sentence"] = f"{rule} supports a live test of the rule: {facts}"
    return out


def paper_scores(rs: list[dict]) -> dict:
    """Each paper rule's counted rows scored in dollars against SPY at every horizon, the same for
    every reviewed stock as a baseline, and one verdict on the primary rule at 180 days."""
    rules = {}
    for name, description in paper.RULES.items():
        c = counted(rs, name)
        rules[name] = {"description": description, "counted": c, "horizons": _paper_horizons(c)}
    base = counted(rs)
    baseline = {"counted": base, "horizons": _paper_horizons(base)}
    verdict = _paper_verdict(paper.PRIMARY, rules[paper.PRIMARY]["counted"],
                             baseline["horizons"][PAPER_HORIZON]["mean_excess"])
    return {"rules": rules, "baseline": baseline, "verdict": verdict}


def _paper_cell(h: dict) -> str:
    if not h["n"]:
        return "–"
    return f"{_points(h['mean_excess'])} over SPY ({h['ahead']['k']}/{h['ahead']['n']} ahead, n={h['n']})"


DIVIDEND_NOTE = ("Returns are price changes without dividends, for the stock and for SPY alike, "
                 "which understates high-yield names a little.")


def _missing_text(missing: int) -> str:
    """The sentence about counted results with no dollar return ("" for none)."""
    if not missing:
        return ""
    who = "1 counted result has" if missing == 1 else f"{missing} counted results have"
    return (f"{who} no dollar return (a currency rate or SPY close is missing); "
            "run `calibration` to fetch them.")


def paper_view(paper_scores_: dict) -> dict:
    """What the paper Buys section prints, as text: ``table`` (a name and its counted number and
    one cell per horizon, for each rule and then every review), ``listed`` (the counted rows of
    every rule once, oldest first, at the latest horizon each has reached) and ``missing`` (the
    reached horizons of those rows with no dollar result)."""
    table = []
    for name, g in [*paper_scores_["rules"].items(), ("every review", paper_scores_["baseline"])]:
        table.append({"name": name, "counted": len(g["counted"]),
                      "cells": [_paper_cell(g["horizons"][n]) for n in HORIZONS]})
    seen, rows_ = {}, []
    for name, g in paper_scores_["rules"].items():
        for r in g["counted"]:
            if id(r) not in seen:
                seen[id(r)] = []
                rows_.append(r)
            seen[id(r)].append(name)
    rows_.sort(key=lambda r: (r["date"], r["ticker"], r.get("folder") or ""))
    listed, missing = [], 0
    for r in rows_:
        reached = [n for n in HORIZONS if r.get(f"h{n}")]
        h = r[f"h{reached[-1]}"] if reached else None
        missing += sum(not _scored(r.get(f"h{n}")) for n in _reached(r))
        price = r.get("price")
        cur = r.get("currency")
        then = "–" if price is None else (
            f"{price:,.2f}" if not cur or cur == "USD" else f"{price:,.2f} {cur}")
        listed.append({"date": r["date"], "ticker": r["ticker"], "rules": ", ".join(seen[id(r)]),
                       "price": then, "horizon": f"{reached[-1]} days" if reached else "–",
                       "ret_usd": _pct1(h and h.get("ret_usd")),
                       "excess": _pct1(h and h.get("excess"))})
    return {"table": table, "listed": listed, "missing": missing, "note": DIVIDEND_NOTE,
            "missing_text": _missing_text(missing)}


def _pct1(x: float | None) -> str:
    return "–" if x is None else _points(x)


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
              "| Reached value | Below bear | Paper |",
              "|---|---|---|---|--:|--:|--:|---|---|---|"]
    for r in rs:
        h = r.get(f"h{horizon}") or {}
        lines.append(f"| {r['date']} | {r['ticker']} | {r.get('lane') or '–'} "
                     f"| {r.get('rating') or '–'} | "
                     f"{_money(r.get('price'))} | {_money(r.get('value'))} | {_pct(h.get('ret'))} | "
                     f"{_yes(h.get('reached_value'))} | {_yes(h.get('below_bear'))} | "
                     f"{', '.join(r.get('paper') or []) or '–'} |")
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
    lines += ["", "### Brier scores", ""]
    lines += [f"- {v['label']}: {v['text']}" for v in brier_view(rs, scores)]
    lines += ["", "## Paper Buys", "",
              "Tracked only: no money follows these. Each rule is applied in code to what a "
              "review recorded.", ""]
    lines += [f"- **{name}**: {g['description']}" for name, g in scores["paper"]["rules"].items()]
    lines += ["", scores["paper"]["verdict"]["sentence"], ""]
    view = paper_view(scores["paper"])
    if not view["listed"]:
        lines += ["No paper Buys yet.", ""]
    else:
        lines += ["| Rule | Counted | 30 days | 90 days | 180 days | 365 days |",
                  "|---|--:|---|---|---|---|"]
        lines += [f"| {t['name']} | {t['counted']} | {' | '.join(t['cells'])} |"
                  for t in view["table"]]
        lines += ["", "| Date | Ticker | Rules | Price then | Latest horizon | Return in dollars "
                  "| Against SPY |", "|---|---|---|--:|---|--:|--:|"]
        lines += [f"| {x['date']} | {x['ticker']} | {x['rules']} | {x['price']} | {x['horizon']} "
                  f"| {x['ret_usd']} | {x['excess']} |" for x in view["listed"]]
        if view["missing"]:
            lines += ["", view["missing_text"]]
        lines += ["", view["note"], ""]
    lines += ["## Verdicts", "", *[f"- {v}" for v in scores["verdicts"]], ""]
    return "\n".join(lines)
