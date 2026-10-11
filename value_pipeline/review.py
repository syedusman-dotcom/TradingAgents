"""The monthly review: run the agents on every holding and the top screen candidates,
then write the action list. Positions and cash are never changed here."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from value_pipeline import (
    benchmark,
    market_data,
    metrics,
    parse,
    portfolio as pfmod,
    render,
    screener,
)
from value_pipeline.actions import Decision, plan
from value_pipeline.config import Paths

log = logging.getLogger(__name__)


class CostLimitExceeded(SystemExit):
    pass


@dataclass
class RunResult:
    ticker: str
    rating: str
    decision: str = ""
    report: str | None = None      # path relative to the review folder
    error: str | None = None


# --- running the agents ---------------------------------------------------------

STATEMENT_TOOLS = ("get_income_statement", "get_balance_sheet", "get_cashflow")


def graph_config(statement_vendor: str | None) -> dict:
    """DEFAULT_CONFIG (with your .env), reading statements from ``statement_vendor``.

    The default chain starts with SEC EDGAR: about 20 years, but only the top-line
    rows (no debt, interest, PP&E, retained earnings, buybacks or dividends).
    A review always analyses today, so Yahoo's fuller, shorter statements carry
    no look-ahead risk and serve the F-Score, Z-Score and capital-allocation checks.
    """
    import copy

    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if statement_vendor:
        cfg["tool_vendors"] = {**cfg.get("tool_vendors", {}), **dict.fromkeys(STATEMENT_TOOLS, statement_vendor)}
    return cfg


def make_graph(analysts: list[str], statement_vendor: str | None = None):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(selected_analysts=tuple(analysts), config=graph_config(statement_vendor))


def run_agents(ta, ticker: str, day: str, portfolio_ctx, out_dir: Path) -> RunResult:
    """One TradingAgents run, retried once; a second failure is reported, not raised."""
    from tradingagents.agents.rating import is_review

    error = None
    for attempt in (1, 2):
        try:
            final_state, rating = ta.propagate(ticker, day, portfolio=portfolio_ctx)
            ta.save_reports(final_state, ticker, save_path=out_dir / ticker, html=True)
            return RunResult(ticker, "REVIEW" if is_review(rating) else rating,
                             final_state.get("final_trade_decision", ""), f"{ticker}/complete_report.html")
        except Exception as exc:  # noqa: BLE001 - one failed ticker must not stop the review
            error = f"{type(exc).__name__}: {exc}"
            log.warning("%s run %d failed: %s", ticker, attempt, error)
    return RunResult(ticker, "REVIEW", error=error)


_FAKE_HELD = ("Buy", "Hold", "Underweight", "Sell", "Overweight")
_FAKE_NEW = ("Buy", "Overweight", "Hold")


def fake_run(ticker: str, index: int, held: bool, price: float | None) -> RunResult:
    """A made-up rating and decision for --dry-run, cycling through every rule."""
    rating = (_FAKE_HELD if held else _FAKE_NEW)[index % len(_FAKE_HELD if held else _FAKE_NEW)]
    value = round(price * (0.9 if held and index % 2 else 1.45), 2) if price else None
    mos = None if not value or not price else (value - price) / value
    text = (f"**Rating**: {rating}\n\n**Executive Summary**: Dry run.\n\n"
            f"**Investment Thesis**: Fake thesis for {ticker}. Margin of safety: {mos * 100 if mos is not None else 0:.0f}%\n\n"
            f"**Price Target**: {value if value else 'not provided'}")
    return RunResult(ticker, rating, text)


# --- the review -------------------------------------------------------------------

def _screen_rows(paths: Paths) -> dict[str, dict]:
    path = screener.latest_screen(paths)
    if path is None:
        return {}
    return {r["ticker"]: r for r in pd.read_csv(path).to_dict("records")}


def _trend(ticker: str, screen_row: dict | None) -> str | None:
    if screen_row and isinstance(screen_row.get("trend"), str):
        return screen_row["trend"]
    try:
        return metrics.trend(market_data.closes(ticker)).get("trend")
    except Exception as exc:  # noqa: BLE001
        log.warning("no trend for %s: %s", ticker, exc)
        return None


def run(paths: Paths, cfg: dict, *, yes: bool = False, dry_run: bool = False,
        today: str | None = None, confirm=input) -> Path:
    today = today or date.today().isoformat()
    rcfg = cfg["review"]
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    candidates = screener.top_candidates(paths, owned, rcfg["candidates"])
    if not candidates:
        print("No screen found (or no candidates); reviewing holdings only. Run `screen` first for new ideas.")
    tickers = [p.ticker for p in pf.positions] + [c["ticker"] for c in candidates]
    if not tickers:
        raise SystemExit("Nothing to review: no holdings and no screen candidates.")

    estimate = len(tickers) * rcfg["cost_per_run"]
    print(f"Review {today}: {len(tickers)} runs ({len(owned)} holdings + {len(candidates)} candidates): "
          f"{', '.join(tickers)}")
    print(f"Estimated cost: ${estimate:,.2f} (${rcfg['cost_per_run']:,.2f}/run, limit ${rcfg['max_cost_per_review']:,.2f})")
    if dry_run:
        print("DRY RUN: no agent calls, fake ratings, portfolio.json untouched.")
    else:
        if estimate > rcfg["max_cost_per_review"]:
            raise CostLimitExceeded(
                f"Estimated cost ${estimate:,.2f} exceeds max_cost_per_review ${rcfg['max_cost_per_review']:,.2f}. "
                "Lower review.candidates or raise the limit in value_pipeline/config.yaml.")
        if not yes and confirm("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("Cancelled.")

    out_dir = paths.review_dir(today + ("-dry-run" if dry_run else ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    screen = _screen_rows(paths)
    prices = {t: market_data.latest_price(t) for t in tickers}

    results: list[RunResult] = []
    if dry_run:
        results = [fake_run(t, i, t in owned, prices[t]) for i, t in enumerate(tickers)]
    else:
        ta = make_graph(rcfg["analysts"], rcfg.get("statement_vendor"))
        print("Scoring past decisions (memory log)...")
        ta.settle_all_pending()
        ctx = pf.to_context()
        for i, t in enumerate(tickers, 1):
            print(f"[{i}/{len(tickers)}] {t}...", flush=True)
            results.append(run_agents(ta, t, today, ctx, out_dir))

    decisions = []
    for r in results:
        held = pf.position(r.ticker)
        row = screen.get(r.ticker)
        fscore = row.get("fscore") if row else None
        decisions.append(Decision(
            ticker=r.ticker,
            rating=r.rating,
            price=prices.get(r.ticker),
            held_shares=held.shares if held else 0.0,
            consecutive_underweight=held.consecutive_underweight if held else 0,
            trend=_trend(r.ticker, row),
            intrinsic_value=parse.intrinsic_value(r.decision) or (held.intrinsic_value_estimate if held else None),
            margin_of_safety=parse.margin_of_safety(r.decision),
            fscore=int(fscore) if fscore is not None and not pd.isna(fscore) else None,
        ))
    actions, summary = plan(decisions, pf.cash, cfg["sizing"])
    failures = [f"{r.ticker}: {r.error}" for r in results if r.error]
    cost = 0.0 if dry_run else sum(1 for r in results if not r.error) * rcfg["cost_per_run"]
    comparison = benchmark.compare(summary["total_value"], pf.deposits, pfmod.read_trades(paths))
    knives = []
    if screen:
        ranked = sorted((r for r in screen.values() if not pd.isna(r.get("mf_position"))), key=lambda r: r["mf_position"])
        knives = [r["ticker"] for r in ranked[:50] if r.get("trend") == "FREEFALL" and r["ticker"] not in owned][:10]

    md = render.actions_markdown(today, actions, summary, reports={r.ticker: r.report for r in results if r.report},
                                 comparison=comparison, cost=cost, failures=failures, dry_run=dry_run, knives=knives)
    (out_dir / "ACTIONS.md").write_text(md, encoding="utf-8")
    (out_dir / "ACTIONS.html").write_text(render.actions_html(md, f"Value review {today}"), encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps([
        {"ticker": r.ticker, "rating": r.rating, "error": r.error, "report": r.report,
         "intrinsic_value": parse.intrinsic_value(r.decision), "margin_of_safety": parse.margin_of_safety(r.decision),
         "thesis": parse.thesis_summary(r.decision)}
        for r in results
    ], indent=2), encoding="utf-8")

    if not dry_run:
        _record_ratings(paths, pf, actions, today)
    return out_dir / "ACTIONS.md"


def _record_ratings(paths: Paths, pf: pfmod.Portfolio, actions, today: str) -> None:
    """Store each holding's rating, review date, Underweight streak and value estimate."""
    for a in actions:
        held = pf.position(a.ticker)
        if held is None:
            continue
        held.last_rating = a.rating
        held.last_review_date = today
        held.consecutive_underweight = a.consecutive_underweight
        if a.decision.intrinsic_value is not None:
            held.intrinsic_value_estimate = a.decision.intrinsic_value
    pfmod.save(paths, pf)


def latest_result(paths: Paths, ticker: str) -> dict | None:
    """The newest real review's result for a ticker, used to fill a new position's thesis."""
    for folder in sorted(paths.reports.glob("????-??-??"), reverse=True):
        f = folder / "results.json"
        if f.exists():
            for r in json.loads(f.read_text(encoding="utf-8")):
                if r["ticker"] == ticker.upper() and not r.get("error"):
                    return r
    return None
