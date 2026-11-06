"""The pullback study: what an entry-price gate would have done to the backtest's past picks.

The live process buys only below an entry price under today's price. The mechanical proxy
measured here, on the picks CSV that ``backtest`` writes: buy at the pick's start price, or buy
only if a close falls at least ``dip`` below that start price within ``WINDOW_DAYS`` calendar
days, at that first close, and hold one year from the fill. Fills use ``close`` (the screen's
price basis); returns use ``adj_close``, as the backtest does.

The verdict in ``verdict`` (its numbers below) was fixed before the first run and is not tuned
afterwards. Nothing trades and no agent is called.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.files import replace_file
from value_pipeline import backtest
from value_pipeline.config import Paths

DIPS = (0.10, 0.15)
WINDOW_DAYS = 120
RULES = ("magic_formula", "compounder_record", "value_graded", "compounder")

VERDICT_DIP = 0.10       # the verdict counts rules at this dip
MIN_RULES = 3            # of the four rules
GIVEN_UP = 0.03          # costs money: unfilled minus filled excess at least this
LOW_FILL = 0.5           # ... and a fill rate below this
FILL_BENEFIT = 0.02      # earns its keep: filled minus same-names buy-now excess at least this
EPS = 1e-9               # every bound is inclusive to this tolerance

COSTS = "costs money"
EARNS = "earns its keep"
INCONCLUSIVE = "inconclusive"


def fill(hist, start: str, dip: float, window_days: int = WINDOW_DAYS) -> tuple[str, float] | None:
    """``(date, close)`` of the first close after the start day, within ``window_days`` calendar
    days of ``start``, at or below ``(1 - dip)`` times the close on the first trading day on or
    after ``start``; None when it never gets there. The start day itself is not a fill."""
    first = backtest.close_on_or_after(hist, start)
    if first is None:
        return None
    day, base = first
    limit = pd.Timestamp(start) + timedelta(days=window_days)
    rows = hist[(hist["date"] > pd.Timestamp(day)) & (hist["date"] <= limit)].dropna(subset=["close"])
    hit = rows[rows["close"] <= (1 - dip) * base + EPS]
    if hit.empty:
        return None
    row = hit.iloc[0]
    return row["date"].date().isoformat(), float(row["close"])


def pick_outcome(hist, spy_hist, pick: dict, dip: float) -> dict:
    """``{"filled", "fill_date", "ret_fill", "spy_fill", "excess_fill"}``: the one-year total
    return from the fill date, and SPY's over the same window. Unfilled picks carry None."""
    hit = fill(hist, pick["start_date"], dip)
    if hit is None:
        return {"filled": False, "fill_date": None, "ret_fill": None, "spy_fill": None,
                "excess_fill": None}
    day = hit[0]
    end = backtest._years_after(day, 1)
    own = backtest.total_return(hist, day, end)
    spy = backtest.total_return(spy_hist, day, end)
    ret = own["ret"] if own else None
    bench = spy["ret"] if spy else None
    excess = None if ret is None or bench is None else ret - bench
    return {"filled": True, "fill_date": day, "ret_fill": ret, "spy_fill": bench,
            "excess_fill": excess}


def _usable(pick: dict) -> bool:
    return pick.get("hold") == 1 and pick.get("ret") is not None and pick.get("spy") is not None


def _excess(pick: dict) -> float:
    value = pick.get("excess_spy")
    return pick["ret"] - pick["spy"] if value is None else value


def _cell(rows: list[tuple[dict, dict]]) -> dict:
    filled = [(p, o) for p, o in rows if o["filled"]]
    unfilled = [p for p, o in rows if not o["filled"]]
    measured = [(p, o) for p, o in filled if o["excess_fill"] is not None]
    return {
        "n": len(rows),
        "fill_rate": len(filled) / len(rows) if rows else None,
        "buy_now_excess": backtest._mean(_excess(p) for p, _ in rows),
        "filled_excess": backtest._mean(o["excess_fill"] for _, o in measured),
        "unfilled_excess": backtest._mean(_excess(p) for p in unfilled),
        "filled_buy_now_excess": backtest._mean(_excess(p) for p, _ in measured),
    }


def study(picks: list[dict], hist_for: Callable[[str], pd.DataFrame | None], spy_hist,
          dips=DIPS, rules=RULES) -> dict:
    """``{rule: {dip: cell}}`` over the one-year-hold picks with a return and a SPY return (a
    pick whose ticker has no price history is left out). A cell holds ``n``, ``fill_rate``,
    ``buy_now_excess`` (all picks), ``filled_excess`` (the fills' excess from the fill),
    ``unfilled_excess`` (what the never-filled picks returned if bought at once: what the gate
    gave up) and ``filled_buy_now_excess`` (the filled picks' buy-now excess, the like-for-like
    comparison). Means are None when empty."""
    result: dict = {}
    for rule in rules:
        mine = [p for p in picks if p.get("rule") == rule and _usable(p)]
        hists = [(p, hist_for(p["ticker"])) for p in mine]
        mine = [(p, h) for p, h in hists if h is not None]
        result[rule] = {
            dip: _cell([(p, pick_outcome(h, spy_hist, p, dip)) for p, h in mine]) for dip in dips
        }
    return result


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f} points"


def _share(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def verdict(result: dict) -> dict:
    """The fixed rule, counted over the rules at a 10% dip. The gate *costs money* when for at
    least 3 of the 4 rules the unfilled picks' buy-now excess beats the filled picks' excess by
    3 points or more and fewer than half the picks fill. It *earns its keep* when for at least
    3 rules the filled picks' excess from the fill beats their buy-now excess by 2 points or
    more. Both at once, or neither, is *inconclusive*."""
    costs, earns = [], []
    for rule, cells in result.items():
        cell = cells.get(VERDICT_DIP)
        if not cell or not cell["n"]:
            continue
        filled, same = cell["filled_excess"], cell["filled_buy_now_excess"]
        unfilled, rate = cell["unfilled_excess"], cell["fill_rate"]
        if (None not in (unfilled, filled, rate)
                and unfilled - filled >= GIVEN_UP - EPS and rate < LOW_FILL):
            costs.append(rule)
        if None not in (filled, same) and filled - same >= FILL_BENEFIT - EPS:
            earns.append(rule)
    is_costs, is_earns = len(costs) >= MIN_RULES, len(earns) >= MIN_RULES
    name = COSTS if is_costs and not is_earns else EARNS if is_earns and not is_costs else INCONCLUSIVE
    return {"verdict": name, "costs_rules": costs, "earns_rules": earns,
            "sentence": _sentence(name, result, costs, earns)}


def _gaps(result: dict, rules: list[str]) -> tuple[float | None, float | None, float | None]:
    cells = [result[r][VERDICT_DIP] for r in rules]
    rate = backtest._mean(c["fill_rate"] for c in cells if c["fill_rate"] is not None)
    gave_up = backtest._mean(c["unfilled_excess"] - c["filled_excess"] for c in cells
                             if None not in (c["unfilled_excess"], c["filled_excess"]))
    gain = backtest._mean(c["filled_excess"] - c["filled_buy_now_excess"] for c in cells
                          if None not in (c["filled_excess"], c["filled_buy_now_excess"]))
    return rate, gave_up, gain


def _sentence(name: str, result: dict, costs: list[str], earns: list[str]) -> str:
    counted = costs if name == COSTS else earns if name == EARNS else []
    rules = counted or [r for r, cells in result.items() if cells.get(VERDICT_DIP, {}).get("n")]
    rate, gave_up, gain = _gaps(result, rules) if rules else (None, None, None)
    if name == COSTS:
        return (f"Waiting for a {VERDICT_DIP:.0%} dip costs money: in {len(costs)} of "
                f"{len(RULES)} rules fewer than half the picks ever filled (on average "
                f"{_share(rate)} of their picks), and the picks that never filled beat the ones that did by "
                f"{_pct(gave_up)} on average, so the gate gave up the better names.")
    if name == EARNS:
        return (f"Waiting for a {VERDICT_DIP:.0%} dip earns its keep: in {len(earns)} of "
                f"{len(RULES)} rules the picks that filled did at least 2 points better from "
                f"the fill than a buy at the start, {_pct(gain)} on average.")
    if rate is None:
        return (f"No rule had enough one-year picks at a {VERDICT_DIP:.0%} dip to judge "
                f"whether waiting for it helps.")
    return (f"The {VERDICT_DIP:.0%} dip gate is inconclusive: {len(costs)} rules fit "
            f"\"costs money\" and {len(earns)} fit \"earns its keep\" (3 of {len(RULES)} needed "
            f"for either); {_share(rate)} of picks filled on average, the fills beat a buy at "
            f"the start by {_pct(gain)} and the unfilled picks beat the filled by {_pct(gave_up)}.")


def _cell_row(rule: str, cell: dict) -> str:
    def show(x):
        return "n/a" if x is None else f"{x * 100:+.1f}"
    rate = "n/a" if cell["fill_rate"] is None else f"{cell['fill_rate']:.0%}"
    return (f"| {rule} | {cell['n']} | {rate} | {show(cell['buy_now_excess'])} | "
            f"{show(cell['filled_excess'])} | {show(cell['filled_buy_now_excess'])} | "
            f"{show(cell['unfilled_excess'])} |")


def report(result: dict, verdict: dict, picks_path) -> str:
    """The markdown report: what was tested, a table per dip, the verdict and the limits."""
    dips = sorted({d for cells in result.values() for d in cells})
    lines = [
        "# Pullback study: does waiting for a dip help?",
        "",
        f"Picks: `{Path(picks_path).name}`, the one-year-hold picks of the lane-rule backtest.",
        "",
        "The live process buys only below an entry price under today's price. This study "
        "measures a mechanical stand-in on the backtest's past picks: buy at the pick's start "
        "price (\"buy now\"), or buy only if a close falls at least a set share below that start "
        f"price within {WINDOW_DAYS} days, at that first close, and hold one year from the fill.",
        "",
        "Excess is the one-year total return minus SPY's over the same window, in percentage "
        "points. \"Same names buy-now\" is the buy-now excess of just the picks that filled, so "
        "the fill's benefit is read on the same companies; \"unfilled\" is the buy-now excess of "
        "the picks that never filled, what the gate gave up.",
    ]
    for dip in dips:
        lines += ["", f"## Dip of {dip:.0%}", "",
                  "| Rule | Picks | Fill rate | Buy-now excess | Filled excess | "
                  "Same names buy-now | Unfilled excess |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        lines += [_cell_row(rule, cells[dip]) for rule, cells in result.items() if dip in cells]
    lines += [
        "", "## Verdict", "",
        f"**{verdict['verdict']}.** {verdict['sentence']}",
        "",
        f"The rule was fixed before the first run: at a {VERDICT_DIP:.0%} dip the gate costs "
        f"money when, for at least {MIN_RULES} of the {len(RULES)} rules, the unfilled picks beat "
        f"the filled by {GIVEN_UP * 100:.0f} points or more and under {LOW_FILL:.0%} fill; it earns "
        f"its keep when, for at least {MIN_RULES} rules, the fills beat a buy at the start by "
        f"{FILL_BENEFIT * 100:.0f} points or more on the same names; otherwise it is inconclusive.",
        "", "## Limits", "",
        "- Cohorts overlap in time and share market moves, so the picks are not independent.",
        "- Survivorship: the universe is today's index members; names that were dropped are missing.",
        "- Fills use daily closes and ignore intraday lows, so a real limit order would fill "
        "somewhat more often.",
        "- No costs, spreads or taxes.",
        "",
    ]
    return "\n".join(lines)


def _newest_picks(paths: Paths) -> Path:
    folder = paths.reports / "backtest"
    found = sorted(p for p in folder.glob("backtest_*_picks.csv")
                   if "_sp500" not in p.name and "_sp600" not in p.name)
    if not found:
        raise FileNotFoundError(f"no backtest_*_picks.csv under {folder}; run `backtest` first")
    return found[-1]


def _read_picks(path: Path) -> list[dict]:
    frame = pd.read_csv(path)
    return frame.astype(object).where(frame.notna(), None).to_dict("records")


def run(paths: Paths, picks_path: Path | None = None) -> tuple[Path, dict]:
    """Study the newest default-universe picks CSV (or ``picks_path``) from the cached price
    histories, write ``pullback_<date>.md`` next to it and return its path and the result."""
    picks_path = Path(picks_path) if picks_path else _newest_picks(paths)
    spy = backtest.price_history("SPY", paths, fetch=False)
    if spy is None:
        raise ValueError("no cached SPY price history; run `backtest` first")
    cache: dict[str, pd.DataFrame | None] = {}

    def hist_for(ticker: str):
        if ticker not in cache:
            cache[ticker] = backtest.price_history(ticker, paths, fetch=False)
        return cache[ticker]

    result = study(_read_picks(picks_path), hist_for, spy)
    text = report(result, verdict(result), picks_path)
    out = picks_path.parent / f"pullback_{date.today().isoformat()}.md"
    replace_file(out, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    return out, result
