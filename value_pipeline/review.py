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
    committee as committee_mod,
    lock,
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


class ReviewRunning(SystemExit):
    """The scheduled run and the website share one data folder: one review at a time."""


@dataclass
class RunResult:
    ticker: str
    rating: str
    decision: str = ""
    report: str | None = None      # path relative to the review folder
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    name: str | None = None         # the company's name, for ACTIONS
    nominal_cost_usd: float = 0.0   # what the calls would have cost at API prices (measured, not the estimate)


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


def make_graph(analysts: list[str], statement_vendor: str | None = None, tally=None):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(selected_analysts=tuple(analysts), config=graph_config(statement_vendor),
                              callbacks=[tally] if tally else None)


def _company_name(ticker: str) -> str | None:
    """The company's name (cached by the graph's own lookup); None when Yahoo has none."""
    from tradingagents.agents.context import resolve_instrument_identity

    try:
        return resolve_instrument_identity(ticker).get("company_name")
    except Exception:  # noqa: BLE001 - a name is a nicety, never a reason to fail a run
        return None


def run_agents(ta, ticker: str, day: str, portfolio_ctx, out_dir: Path, tally=None) -> RunResult:
    """One TradingAgents run, retried once; a second failure is reported, not raised."""
    from tradingagents.agents.rating import is_review

    error = None
    if tally:
        tally.reset()  # once: a failed attempt's tokens were still spent
    name = _company_name(ticker)
    for attempt in (1, 2):
        try:
            final_state, rating = ta.propagate(ticker, day, portfolio=portfolio_ctx)
            ta.save_reports(final_state, ticker, save_path=out_dir / ticker, html=True)
            used = tally.snapshot() if tally else {}
            return RunResult(ticker, "REVIEW" if is_review(rating) else rating,
                             final_state.get("final_trade_decision", ""), f"{ticker}/complete_report.html",
                             tokens_in=used.get("tokens_in", 0), tokens_out=used.get("tokens_out", 0),
                             nominal_cost_usd=used.get("nominal_cost_usd", 0.0), name=name)
        except Exception as exc:  # noqa: BLE001 - one failed ticker must not stop the review
            error = f"{type(exc).__name__}: {exc}"
            log.warning("%s run %d failed: %s", ticker, attempt, error)
    used = tally.snapshot() if tally else {}
    return RunResult(ticker, "REVIEW", error=error, tokens_in=used.get("tokens_in", 0),
                     tokens_out=used.get("tokens_out", 0), nominal_cost_usd=used.get("nominal_cost_usd", 0.0), name=name)


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

def _float(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def _scenarios_at(decision: str, price: float | None) -> dict | None:
    """The decision's scenarios with the statistics measured at the review price.

    The Portfolio Manager sees no market price and measures upside/downside at its entry
    price; the review knows the price, so it recomputes. Without one, the text's figures stay.
    """
    from tradingagents.agents.decision_math import scenario_stats
    from tradingagents.agents.schemas import Scenario, Scenarios

    parsed = parse.scenarios(decision)
    if parsed is None:
        return None
    if price is None or not all(leg in parsed for leg in ("bear", "base", "bull")):
        return {"weighted_mos": None, **parsed, "price_basis": "entry price"}
    legs = Scenarios(**{leg: Scenario(**parsed[leg]) for leg in ("bear", "base", "bull")})
    return {**parsed, **scenario_stats(legs, price), "price_basis": "review price"}


def _screen_rows(paths: Paths) -> dict[str, dict]:
    path = screener.latest_screen(paths)
    if path is None:
        return {}
    return {r["ticker"]: r for r in pd.read_csv(path).to_dict("records")}


def _days_between(earlier: str | None, later: str) -> int | None:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days if earlier else None


def _trend(ticker: str, screen_row: dict | None) -> str | None:
    if screen_row and isinstance(screen_row.get("trend"), str):
        return screen_row["trend"]
    try:
        return metrics.trend(market_data.closes(ticker)).get("trend")
    except Exception as exc:  # noqa: BLE001
        log.warning("no trend for %s: %s", ticker, exc)
        return None


def select_tickers(owned: list[str], candidates: list[str], only: list[str] | None) -> tuple[list[str], int, int]:
    """The tickers to run, and how many are holdings and candidates.

    By default every holding, then the screen's candidates. ``only`` runs just
    those tickers, held or not: to look at ideas outside the top of the screen.
    """
    if only:
        chosen = list(dict.fromkeys(t.strip().upper() for t in only if t.strip()))
        held = sum(1 for t in chosen if t in owned)
        return chosen, held, len(chosen) - held
    return owned + candidates, len(owned), len(candidates)


def fit_to_cap(tickers: list[str], n_held: int, cost_per_run: float, limit: float) -> tuple[list[str], int, int]:
    """Drop candidates from the end until the estimate fits the cap: (tickers, candidates kept, dropped).
    Holdings are never dropped; if they alone exceed the cap, the review is blocked."""
    affordable = int(limit // cost_per_run) if cost_per_run > 0 else len(tickers)
    keep = max(n_held, min(len(tickers), affordable))
    return tickers[:keep], keep - n_held, len(tickers) - keep


def preview(paths: Paths, cfg: dict, only: list[str] | None = None) -> dict:
    """What a review would run and cost, without running anything."""
    rcfg = cfg["review"]
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    candidates = [] if only else screener.top_candidates(paths, owned, rcfg["candidates"])
    tickers, n_held, n_new = select_tickers([p.ticker for p in pf.positions], [c["ticker"] for c in candidates], only)
    dropped = 0
    if not only:
        tickers, n_new, dropped = fit_to_cap(tickers, n_held, rcfg["cost_per_run"], rcfg["max_cost_per_review"])
    estimate = len(tickers) * rcfg["cost_per_run"]
    return {"tickers": tickers, "n_held": n_held, "n_new": n_new, "estimate": estimate, "dropped": dropped,
            "cost_per_run": rcfg["cost_per_run"], "limit": rcfg["max_cost_per_review"],
            "blocked": estimate > rcfg["max_cost_per_review"]}


def _sector(paths: Paths, ticker: str, screen_row: dict | None) -> str | None:
    """The screen's sector, else the cached fundamentals' (holdings outside the screen still count
    toward the sector cap)."""
    if screen_row and isinstance(screen_row.get("sector"), str):
        return screen_row["sector"]
    return market_data.cached_sector(ticker, paths)


def run(paths: Paths, cfg: dict, *, yes: bool = False, dry_run: bool = False,
        today: str | None = None, confirm=input, only: list[str] | None = None) -> Path:
    try:
        with lock.exclusive(paths, "review"):
            return _run(paths, cfg, yes=yes, dry_run=dry_run, today=today, confirm=confirm, only=only)
    except lock.Locked:
        raise ReviewRunning("Not started: another review is running (the scheduled one, or the website's); "
                            "wait for it to finish.") from None


def _run(paths: Paths, cfg: dict, *, yes: bool, dry_run: bool, today: str | None, confirm, only) -> Path:
    today = today or date.today().isoformat()
    rcfg = cfg["review"]
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    plan_ = preview(paths, cfg, only)
    tickers = plan_["tickers"]
    if not only and plan_["n_new"] == 0 and not plan_["dropped"]:
        print("No screen found (or no candidates); reviewing holdings only. Run `screen` first for new ideas.")
    if plan_["dropped"]:
        print(f"{plan_['dropped']} candidate(s) left out to stay under the ${rcfg['max_cost_per_review']:,.2f} cap "
              "(raise review.max_cost_per_review or lower review.candidates to change this).")
    if not tickers:
        raise SystemExit("Nothing to review: no holdings and no screen candidates.")

    estimate = plan_["estimate"]
    print(f"Review {today}: {len(tickers)} runs ({plan_['n_held']} holdings + {plan_['n_new']} candidates): "
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
    if not dry_run:
        # A second review the same day gets its own folder rather than overwriting
        # the first one's results, which `buy` and `watch` read back.
        n = 2
        while (out_dir / "results.json").exists():
            out_dir, n = paths.review_dir(f"{today}-{n}"), n + 1
    out_dir.mkdir(parents=True, exist_ok=True)
    screen = _screen_rows(paths)
    prices = {t: market_data.latest_price(t) for t in sorted(set(tickers) | owned)}

    results: list[RunResult] = []
    usage = None
    if dry_run:
        results = [fake_run(t, i, t in owned, prices[t]) for i, t in enumerate(tickers)]
    else:
        from tradingagents.llm_clients.usage import UsageTally

        logging.getLogger("tradingagents.llm_clients.claude_code_client").setLevel(logging.INFO)
        if not logging.getLogger().handlers:
            logging.basicConfig(format="%(levelname)s %(message)s")
        tally = UsageTally()
        ta = make_graph(rcfg["analysts"], rcfg.get("statement_vendor"), tally)
        print("Scoring past decisions (memory log)...")
        ta.settle_all_pending()
        ctx = pf.to_context()
        for i, t in enumerate(tickers, 1):
            print(f"[{i}/{len(tickers)}] {t}...", flush=True)
            results.append(run_agents(ta, t, today, ctx, out_dir, tally))
        usage = {"tokens_in": sum(r.tokens_in for r in results), "tokens_out": sum(r.tokens_out for r in results),
                 "nominal_cost_usd": sum(r.nominal_cost_usd for r in results),
                 "on_plan": str(ta.config.get("llm_provider", "")).lower() == "claude_code"}

    scenarios = {r.ticker: _scenarios_at(r.decision, prices.get(r.ticker)) for r in results}
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
            formula_value=_float(row.get("value_base")) if row else None,
            sector=_sector(paths, r.ticker, row),
            expected_return=_float(row.get("expected_return")) if row else None,
            days_since_last_review=_days_between(held.last_review_date, today) if held else None,
            entry_price=parse.entry_price(r.decision),
            kill_criteria=parse.kill_criteria(r.decision),
            value_bridge=parse.value_bridge(r.decision),
            upside_downside=(scenarios[r.ticker] or {}).get("upside_downside"),
            weighted_mos=(scenarios[r.ticker] or {}).get("weighted_mos"),
        ))
    reviewed = {d.ticker for d in decisions}
    others = [(p.ticker, p.shares * (prices.get(p.ticker) or p.avg_price), _sector(paths, p.ticker, screen.get(p.ticker)))
              for p in pf.positions if p.ticker not in reviewed]
    actions, summary = plan(decisions, pf.cash, cfg["sizing"], others)
    failures = [f"{r.ticker}: {r.error}" for r in results if r.error]
    cost = 0.0 if dry_run else sum(1 for r in results if not r.error) * rcfg["cost_per_run"]
    comparison = benchmark.compare(summary["total_value"], pf.deposits, pfmod.read_trades(paths))
    knives = []
    if screen:
        ranked = sorted((r for r in screen.values() if not pd.isna(r.get("mf_position"))), key=lambda r: r["mf_position"])
        knives = [r["ticker"] for r in ranked[:50] if r.get("trend") == "FREEFALL" and r["ticker"] not in owned][:10]

    rows = [
        {"ticker": r.ticker, "name": r.name, "rating": r.rating, "error": r.error, "report": r.report,
         "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "nominal_cost_usd": round(r.nominal_cost_usd, 4),
         "intrinsic_value": parse.intrinsic_value(r.decision), "margin_of_safety": parse.margin_of_safety(r.decision),
         "thesis": parse.thesis_summary(r.decision), "price": prices.get(r.ticker),
         "formula_value": _float(screen[r.ticker].get("value_base")) if r.ticker in screen else None,
         "entry_price": parse.entry_price(r.decision), "kill_criteria": parse.kill_criteria(r.decision),
         "checklist": parse.checklist(r.decision), "scenarios": scenarios[r.ticker],
         "value_bridge": parse.value_bridge(r.decision), "red_flags": parse.red_flags(r.decision)}
        for r in results
    ]

    committee, unavailable = None, None
    if rcfg["committee"] and dry_run:
        unavailable = committee_mod.SKIPPED_DRY_RUN
    elif rcfg["committee"] and sum(1 for r in results if not r.error) < 2:
        unavailable = committee_mod.SKIPPED_FEW
    elif rcfg["committee"]:
        committee, unavailable = _committee(ta, tally, decisions, rows, pf, summary, cfg["sizing"], actions)
        used = tally.snapshot()
        for key in ("tokens_in", "tokens_out", "nominal_cost_usd"):
            usage[key] += used.get(key, 0)

    md = render.actions_markdown(today, actions, summary, reports={r.ticker: r.report for r in results if r.report},
                                 comparison=comparison, cost=cost, failures=failures, dry_run=dry_run, knives=knives,
                                 usage=usage, names={r.ticker: r.name for r in results if r.name},
                                 committee=committee, committee_unavailable=unavailable,
                                 max_weight=cfg["sizing"]["max_position_weight"])
    (out_dir / "ACTIONS.md").write_text(md, encoding="utf-8")
    (out_dir / "ACTIONS.html").write_text(render.actions_html(md, f"Value review {today}"), encoding="utf-8")
    # Written whole: the website reads these files while a review may be finishing.
    _write_whole(out_dir / "results.json", rows)
    if committee:
        _write_whole(out_dir / "committee.json", {**committee[0].model_dump(), "notes": committee[1]})

    if not dry_run:
        _record_ratings(paths, pf, actions, today)
    return out_dir / "ACTIONS.md"


def _write_whole(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _committee(ta, tally, decisions, rows, pf, summary, sizing, actions):
    """The committee's reconciled answer and notes, or the reason it is not available.
    Its tokens are left in ``tally`` for the review's cost line."""
    tally.reset()
    try:
        payload = committee_mod.build_payload(decisions, rows, pf, summary, sizing)
        result = committee_mod.run_committee(ta.deep_thinking_llm, payload)
    except Exception as exc:  # noqa: BLE001 - the committee is advice on top; the review stands without it
        log.warning("investment committee failed: %s: %s", type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    if result is None:
        log.warning("investment committee gave no structured answer")
        return None, "the model gave no structured answer"
    return committee_mod.reconcile(result, actions), None


def _record_ratings(paths: Paths, pf: pfmod.Portfolio, actions, today: str) -> None:
    """Store each holding's rating, review date, Underweight streak and value estimate.

    The portfolio is read again here, not reused from the start of the review: a
    review can run for an hour, and a trade recorded meanwhile must survive.
    """
    pf = pfmod.load(paths)
    for a in actions:
        held = pf.position(a.ticker)
        if held is None:
            continue
        held.last_rating = a.rating
        held.last_review_date = today
        held.consecutive_underweight = a.consecutive_underweight
        if a.decision.intrinsic_value is not None:
            held.intrinsic_value_estimate = a.decision.intrinsic_value
        if a.decision.kill_criteria:
            held.kill_criteria = list(a.decision.kill_criteria)
        if a.decision.entry_price is not None:
            held.entry_price = a.decision.entry_price
    pfmod.save(paths, pf)


def review_dirs(paths: Paths) -> list[Path]:
    """Real review folders (YYYY-MM-DD, or YYYY-MM-DD-N for a same-day re-run), newest first."""
    folders = [f for f in paths.reports.glob("????-??-??*")
               if f.is_dir() and (len(f.name) == 10 or f.name[11:].isdigit())]
    return sorted(folders, key=lambda f: (f.name[:10], int(f.name[11:] or 1)), reverse=True)


def latest_result(paths: Paths, ticker: str) -> dict | None:
    """The newest real review's result for a ticker, used to fill a new position's thesis."""
    for folder in review_dirs(paths):
        f = folder / "results.json"
        if f.exists():
            for r in json.loads(f.read_text(encoding="utf-8")):
                if r["ticker"] == ticker.upper() and not r.get("error"):
                    return r
    return None
