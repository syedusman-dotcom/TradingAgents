"""The monthly review: run the agents on every holding and the top screen candidates,
then write the action list. Positions and cash are never changed here."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.vendors.yahoo.consensus import consensus
from tradingagents.dataflows.vendors.yahoo.fundamentals import next_earnings_date
from value_pipeline import (
    benchmark,
    committee as committee_mod,
    lock,
    market_data,
    metrics,
    paper,
    parse,
    portfolio as pfmod,
    render,
    screener,
    themes as themes_mod,
)
from value_pipeline.actions import Decision, plan
from value_pipeline.config import Paths, parking_ticker

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
    earnings_date: str | None = None  # the next earnings date (ISO) when the review ran
    nominal_cost_usd: float = 0.0   # what the calls would have cost at API prices (measured, not the estimate)
    agent_round: str = "current"    # the review arm that produced it
    blind_value: float | None = None  # the price-blind valuation, independent arm
    judge_order: str | None = None  # which case the Research Manager read first, independent arm


def arm_for(ticker: str, date: str, setting: str) -> str:
    """The review arm for ``review.agent_round``: fixed, or for ``ab`` drawn from a hash of the
    ticker and date, so a resumed run gets the same arm."""
    if setting != "ab":
        return setting
    even = int(hashlib.sha256(f"{ticker}:{date}".encode()).hexdigest(), 16) % 2 == 0
    return "independent" if even else "current"


_MISSING = re.compile(r"not provided|not retrieved|returned nothing|could not retrieve|unavailable", re.I)


_ENTRY_PRICE = "**Entry Price**"  # the label render_pm_decision writes ("not provided" on a Sell)


def missing_data_lines(text: str) -> int:
    """How many places the decision text says some data was missing, not counting the rendered
    **Entry Price** line (every Sell renders it as "not provided")."""
    return sum(len(_MISSING.findall(line)) for line in (text or "").splitlines()
               if not line.lstrip().startswith(_ENTRY_PRICE))


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


def _present(row: dict, key: str) -> float | None:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def pipeline_note(row: dict | None) -> str:
    """The screen's figures for a ticker, for every agent's instrument context: its lane hints
    (and theme), the 3-month move, the drawdown from the 52-week high, TTM EBIT on the 5-year
    average, the revenue acceleration, and the accounting scores and formula value; the missing ones left out, "" when none is known.
    Analysts run in parallel, so the news analyst learns of a hint or a fall only here."""
    if not row:
        return ""
    hints = row.get("hints") if isinstance(row.get("hints"), str) and row.get("hints") else _hint(row)
    parts = []
    country, quote = row.get("country"), row.get("quote_currency")
    if isinstance(country, str) and country and country != "US" and quote != "USD":
        parts.append(f"{country} ({row.get('market')}), quoted in {quote}; screen figures in dollars")
    if hints:
        theme = row.get("theme")
        parts.append(f"lane hint {', '.join(hints.split(';'))}"
                     + (f" (theme: {theme})" if isinstance(theme, str) and theme else ""))
    figures = (("move_3m", lambda x: f"3-month move {x:+.0%}"),
               ("drawdown_from_high", lambda x: f"drawdown from the 52-week high {x:+.0%}"),
               ("ttm_vs_5y_ebit", lambda x: f"TTM EBIT {x:.2f}× the 5-year average"),
               ("revenue_acceleration", lambda x: f"revenue acceleration {x * 100:+.0f} points"))
    parts += [fmt(v) for key, fmt in figures if (v := _present(row, key)) is not None]
    fscore = _present(row, "fscore")
    if fscore is not None:
        missing = _present(row, "fscore_missing")
        parts.append(f"F-Score {int(fscore)}/9" + (f" ({int(missing)} inputs missing)" if missing else ""))
    if (z := _present(row, "altman_z")) is not None:
        parts.append(f"Altman Z {z:.2f}")
    if (m := _present(row, "beneish_m")) is not None:
        parts.append(f"Beneish M {m:.2f}")
    flags = row.get("accounting_flags")
    if isinstance(flags, str) and flags.strip():
        parts.append(f"accounting flags: {flags.strip()}")
    if (base := _present(row, "value_base")) is not None:
        parts.append(f"screen formula value {base:.2f} per share (3% growth base)")
    return "; ".join(parts) + "." if parts else ""


def run_agents(ta, ticker: str, day: str, portfolio_ctx, out_dir: Path, tally=None,
               note: str = "", arm: str = "current") -> RunResult:
    """One TradingAgents run, retried once; a second failure is reported, not raised. ``note``
    (``pipeline_note``) is appended to the instrument context every agent reads."""
    from tradingagents.agents.rating import is_review

    error = None
    if tally:
        tally.reset()  # once: a failed attempt's tokens were still spent
    name = _company_name(ticker)
    extra = {"context_note": note} if note else {}
    extra["agent_round"] = arm
    for attempt in (1, 2):
        try:
            final_state, rating = ta.propagate(ticker, day, portfolio=portfolio_ctx, **extra)
            ta.save_reports(final_state, ticker, save_path=out_dir / ticker, html=True)
            used = tally.snapshot() if tally else {}
            return RunResult(ticker, "REVIEW" if is_review(rating) else rating,
                             final_state.get("final_trade_decision", ""), f"{ticker}/complete_report.html",
                             tokens_in=used.get("tokens_in", 0), tokens_out=used.get("tokens_out", 0),
                             nominal_cost_usd=used.get("nominal_cost_usd", 0.0), name=name,
                             agent_round=final_state.get("agent_round") or arm,
                             blind_value=parse.blind_value(final_state.get("blind_valuation", "")),
                             judge_order=final_state.get("judge_order") or None)
        except Exception as exc:  # noqa: BLE001 - one failed ticker must not stop the review
            error = f"{type(exc).__name__}: {exc}"
            log.warning("%s run %d failed: %s", ticker, attempt, error)
    used = tally.snapshot() if tally else {}
    return RunResult(ticker, "REVIEW", error=error, tokens_in=used.get("tokens_in", 0),
                     tokens_out=used.get("tokens_out", 0), nominal_cost_usd=used.get("nominal_cost_usd", 0.0), name=name,
                     agent_round=arm)


_FAKE_HELD = ("Buy", "Hold", "Underweight", "Sell", "Overweight")
_FAKE_NEW = ("Buy", "Overweight", "Hold")


def fake_run(ticker: str, index: int, held: bool, price: float | None) -> RunResult:
    """A made-up rating and decision for --dry-run, cycling through every rule. The decision is
    rendered by the Portfolio Manager's own renderer, so the dry run exercises the checklist,
    kill criteria, entry price, scenarios, value bridge and pre-mortem sections; every fourth run
    from the second is a compounder and every fourth from the fourth an inflection decision, so
    the lane sizing and caps run too."""
    from tradingagents.agents.schemas import (
        Adjustment,
        PortfolioDecision,
        Scenario,
        Scenarios,
        Verdict,
        render_pm_decision,
    )

    rating = (_FAKE_HELD if held else _FAKE_NEW)[index % len(_FAKE_HELD if held else _FAKE_NEW)]
    value = round(price * (0.9 if held and index % 2 else 1.45), 2) if price else None
    mos = None if not value or not price else (value - price) / value
    mos_text = f"Margin of safety: {mos * 100 if mos is not None else 0:.0f}%"
    formula = round(value * 0.9, 2) if value else None
    lane = {1: "compounder", 3: "inflection"}.get(index % 4, "value")
    kills = ["Fake: ROIC below 12% for two years", "Fake: net debt above 3x EBITDA",
             "Fake: revenue falls two years running"]
    evidence = {"compounder": ["Fake: ROIC incl. goodwill ≥ 15% 5/5 years", "Fake: FCF 9.0%/yr 4y"],
                "inflection": ["Fake: TTM EBIT 1.80× the 5-year average",
                               "verified: Fake: backlog up 30%, 10-Q filed 2026-08-01"]}.get(lane, [])
    if lane == "inflection":
        kills = ["Fake: backlog falls two quarters running", *kills]
    decision = PortfolioDecision(
        rating=rating,
        executive_summary="Dry run.",
        investment_thesis=f"Fake thesis for {ticker}. {mos_text}",
        price_target=value,
        time_horizon="1-3 years",
        good_business=Verdict(passed=True, note="Fake: returns on capital above 15% for a decade."),
        good_management=Verdict(passed=index % 2 == 0, note="Fake: buybacks below the formula value."),
        still_investing=Verdict(passed=True, note="Fake: R&D 8% of revenue and rising; launches in the 10-K."),
        cheap=Verdict(passed=bool(mos and mos >= 0.3), note=f"Fake: {mos_text}"),
        red_flags=["Fake: receivables growing faster than revenue"] if index % 3 == 2 else [],
        kill_criteria=kills,
        lane=lane,
        lane_evidence=evidence,
        entry_price=round(value * 0.7, 2) if value else None,
        formula_value=formula,
        value_bridge=[Adjustment(reason="Fake: normalised margins", amount=round(value - formula, 2))]
        if value else [],
        scenarios=Scenarios(bear=Scenario(value=round(value * 0.6, 2), probability=0.25, basis="Fake: 0.8x, no growth"),
                            base=Scenario(value=value, probability=0.5, basis="Fake: target multiple, 3% growth"),
                            bull=Scenario(value=round(value * 1.4, 2), probability=0.25, basis="Fake: 1.2x, 6% growth"))
        if value else None,
        premortem_answer="Fake: the failure path needs a price war, and the moat survived the last one.",
    )
    return RunResult(ticker, rating, render_pm_decision(decision))


# --- the review -------------------------------------------------------------------

def _float(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def _scenarios_at(decision: str, price: float | None) -> dict | None:
    """The decision's scenarios with the statistics measured at the review price.

    The Portfolio Manager measures upside/downside at the worksheet's price (its entry price
    without one); the review recomputes at its own price. Without one, the text's figures stay.
    """
    from tradingagents.agents.decision_math import scenario_stats
    from tradingagents.agents.schemas import Scenario, Scenarios

    parsed = parse.scenarios(decision)
    if parsed is None:
        return None
    if price is None or not all(leg in parsed for leg in ("bear", "base", "bull")):
        return {"weighted_mos": None, **parsed, "price_basis": "decision text"}
    legs = Scenarios(**{leg: Scenario(**parsed[leg]) for leg in ("bear", "base", "bull")})
    return {**parsed, **scenario_stats(legs, price), "price_basis": "review price"}


def _consensus_figures(ticker: str) -> tuple[float | None, float | None]:
    """Yahoo's consensus revenue growth next year and median price target (quote currency) for the
    committee; (None, None) when Yahoo has none or fails."""
    try:
        c = consensus(ticker) or {}
    except Exception:  # noqa: BLE001 - the review stands without it
        return None, None
    return _float(c.get("revenue_growth_next")), _float(c.get("target_median"))


def _screen_rows(paths: Paths) -> dict[str, dict]:
    path = screener.latest_screen(paths)
    if path is None:
        return {}
    return {r["ticker"]: r for r in pd.read_csv(path).to_dict("records")}


def _days_between(earlier: str | None, later: str) -> int | None:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days if earlier else None


def _trend(ticker: str, screen_row: dict | None) -> str | None:
    """Today's trend; the screen's (possibly a month old) only when there is no live one."""
    try:
        live = metrics.trend(market_data.closes(ticker)).get("trend")
    except Exception as exc:  # noqa: BLE001 - a missing trend falls back to the screen's
        log.warning("no trend for %s: %s", ticker, exc)
        live = None
    if live:
        return live
    return screen_row["trend"] if screen_row and isinstance(screen_row.get("trend"), str) else None


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


def recently_reviewed(paths: Paths, days: int, today: date | None = None) -> set[str]:
    """Tickers with a result (not errored) from a review dated within the last ``days`` days;
    empty when ``days`` is 0."""
    if days <= 0:
        return set()
    from value_pipeline.watch import latest_results

    today = today or date.today()
    out = set()
    for row in latest_results(paths):
        try:
            reviewed = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if (today - reviewed).days < days:
            out.add(row["ticker"])
    return out


class IndexFundNamed(SystemExit):
    """A ticker list names the index fund, which is never reviewed."""


def refuse_fund(only: list[str] | None, cfg: dict) -> None:
    fund = parking_ticker(cfg)
    if fund and fund in {t.strip().upper() for t in only or []}:
        raise IndexFundNamed(f"{fund} is the index fund for idle cash and is not reviewed. "
                             "Leave it out of the ticker list (or set parking.ticker to \"\" to treat it as a stock).")


def preview(paths: Paths, cfg: dict, only: list[str] | None = None,
            discovered: list[str] | None = None, today: date | None = None) -> dict:
    """What a review would run and cost, without running anything.

    Candidates: the names in ``lanes.tickers``, the screen's best Magic Formula names, up to
    ``lanes.compounder_candidates`` compounder-hinted ones, up to ``lanes.dislocation_candidates``
    dislocation-hinted ones, up to ``lanes.inflection_candidates`` minus the ``discovered`` count
    inflection-hinted ones, then ``discovered`` (the theme scan's inflection candidates, which keep
    priority); the cost cap drops from the end."""
    rcfg, lanes = cfg["review"], cfg.get("lanes", {})
    pf = pfmod.load(paths)
    fund = parking_ticker(cfg)
    owned = {p.ticker for p in pf.positions}
    refuse_fund(only, cfg)
    candidates: list[str] = []
    skips: list[dict] = []
    countries: list[str] = []
    markets: list[str] = []
    missing: list[str] = []
    if not only:
        countries = screener.normalise_countries(rcfg.get("candidate_countries"))
        markets = screener.normalise_countries(rcfg.get("candidate_markets") or [])
        missing = screener.missing_countries(paths, countries)
        n_themes = len({t for t in discovered or [] if t not in owned})
        inflections = max(0, int(lanes.get("inflection_candidates", 0)) - n_themes)
        named = [t.strip().upper() for t in lanes.get("tickers") or [] if t.strip()]
        # A daily review moves down the screen: names reviewed in the last
        # ``candidate_cooldown_days`` give their place to the next ones (holdings and named
        # tickers always run).
        skip = owned | recently_reviewed(paths, int(rcfg.get("candidate_cooldown_days", 0)), today)
        min_run_rate = float(rcfg.get("min_run_rate", 0.0))
        screened = screener.top_candidates(paths, skip, rcfg["candidates"],
                                           compounders=int(lanes.get("compounder_candidates", 0)),
                                           dislocations=int(lanes.get("dislocation_candidates", 0)),
                                           inflections=inflections, min_run_rate=min_run_rate,
                                           countries=countries, markets=markets)
        skips = screener.run_rate_skips(paths, skip, rcfg["candidates"], min_run_rate, countries,
                                        markets)
        candidates = [t for t in dict.fromkeys(named + [c["ticker"] for c in screened] + (discovered or []))
                      if t not in owned]
    tickers, n_held, n_new = select_tickers([p.ticker for p in pf.positions if p.ticker != fund],
                                            candidates, only)
    dropped = 0
    if not only:
        tickers, n_new, dropped = fit_to_cap(tickers, n_held, rcfg["cost_per_run"], rcfg["max_cost_per_review"])
    estimate = len(tickers) * rcfg["cost_per_run"]
    return {"tickers": tickers, "n_held": n_held, "n_new": n_new, "estimate": estimate, "dropped": dropped,
            "run_rate_skips": skips, "candidate_countries": countries, "candidate_markets": markets,
            "missing_countries": missing,
            "cost_per_run": rcfg["cost_per_run"], "limit": rcfg["max_cost_per_review"],
            "blocked": estimate > rcfg["max_cost_per_review"]}


def _sector(paths: Paths, ticker: str, screen_row: dict | None) -> str | None:
    """The screen's sector, else the cached fundamentals' (holdings outside the screen still count
    toward the sector cap)."""
    if screen_row and isinstance(screen_row.get("sector"), str):
        return screen_row["sector"]
    return market_data.gics_sector(market_data.cached_sector(ticker, paths))


def _quote_fx(ticker: str) -> tuple[str | None, float | None]:
    try:
        return market_data.quote_fx(ticker)
    except Exception as exc:  # noqa: BLE001 - fail open: priced as dollars, as before
        log.warning("no quote currency for %s: %s", ticker, exc)
        return None, 1.0


def offscreen_row(ticker: str, paths: Paths, cfg: dict, screen: dict[str, dict]) -> dict | None:
    """The screen's row for a ticker the screen did not cover (a holding or a --tickers name),
    built the same way, with its sector's median EV/EBIT from the screen as the reversion
    target, its hard filters (``fail_reasons``, ``passes_filters``) and lane hints, so the
    pipeline note names a hint for every reviewed ticker; None when its fundamentals cannot be
    fetched."""
    scfg = cfg["screen"]
    try:
        data = market_data.fundamentals(ticker, paths, scfg["cache_days"], price_days=scfg.get("price_days"))
        data = market_data.in_usd(data)
        row = screener.evaluate(data, scfg["normalize_years"], scfg["roic_cap"],
                                metrics.current_discount_rate()["rate"])
    except Exception as exc:  # noqa: BLE001 - the review stands without the formula's figures
        log.warning("no screen figures for %s: %s", ticker, exc)
        return None
    row["sector"] = market_data.gics_sector(market_data.cached_sector(ticker, paths) or data.get("sector"))
    suffix = ticker[ticker.rfind("."):] if "." in ticker else ""
    market = next((m for m in market_data.MARKETS.values() if m.suffix == suffix), None)
    row["market"], row["country"] = (market.key, market.country) if market else (None, None)
    row["daily_value"] = screener.daily_value(data)
    target = screener.sector_target(list(screen.values()), row["sector"])
    screener.with_target(row, data["years"], data.get("price"), row["market_cap"], row["shares"], target)
    reasons = screener.failed_filters(row, cfg["filters"])
    row["passes_filters"], row["fail_reasons"] = not reasons, "; ".join(reasons)
    hints = screener.lane_hints(row)
    row["hints"], row["lane_hint"] = ";".join(hints), hints[0] if hints else ""
    return screener.to_quote(row, data)


def screen_row_for(ticker: str, paths: Paths, cfg: dict, screen: dict[str, dict]) -> dict | None:
    """The screen's row for ``ticker``; built when the screen lacks it, and completed when the
    screen predates the expected-return column (its own values win where it has them)."""
    row = screen.get(ticker)
    if row is not None and "expected_return" in row:
        return row
    built = offscreen_row(ticker, paths, cfg, screen)
    if row is None or built is None:
        return row or built
    return {**built, **{k: v for k, v in row.items() if v is not None and not pd.isna(v)}}


def run(paths: Paths, cfg: dict, *, yes: bool = False, dry_run: bool = False,
        today: str | None = None, confirm=input, only: list[str] | None = None,
        discover: bool = False) -> Path:
    """``discover`` (the ``run-all`` path) runs the theme scan first and adds its inflection
    candidates to the review."""
    try:
        with lock.exclusive(paths, "review"):
            return _run(paths, cfg, yes=yes, dry_run=dry_run, today=today, confirm=confirm, only=only,
                        discover=discover)
    except lock.Locked:
        raise ReviewRunning("Not started: another review is running (the scheduled one, or the website's); "
                            "wait for it to finish.") from None


def discover_themes(paths: Paths, cfg: dict, llm, today: str, owned: set[str],
                    label: str | None = None,
                    scares: dict[str, list[dict]] | None = None) -> tuple[list[dict], Path | None]:
    """The theme scan's inflection candidates (screen rows hinted ``inflection``) and the report
    written to ``reports/themes_<label or today>.md``, with the latest screen's sector scares
    (``scares``, read from the screen when None). No themes but a scare: the report has only the
    scares (no model call). Fails open: a failed scan gives no candidates and a report with only
    the scares (none when there are no scares)."""
    names = cfg.get("lanes", {}).get("themes") or []
    if scares is None:
        scares = screener.latest_scares(paths)
    if not names and not scares:
        return [], None
    extra = themes_mod.scare_lines(scares)
    try:
        if not names:
            out = themes_mod.write_report(paths, label or today, [], [], [], extra=extra)
            print(f"Sector scares: {', '.join(scares)}; see {out}")
            return [], out
        ideas = themes_mod.scan(llm, names, today)
        kept, skipped = themes_mod.candidates(paths, cfg, ideas, owned)
        out = themes_mod.write_report(paths, label or today, ideas, kept, skipped, extra=extra)
    except Exception as exc:  # noqa: BLE001 - discovery is extra; the review stands without it
        log.warning("theme scan failed: %s: %s", type(exc).__name__, exc)
        if not scares:
            return [], None
        try:                    # the scares come from the screen: keep them without the scan
            return [], themes_mod.write_report(paths, label or today, [], [], [], extra=extra)
        except Exception as exc2:  # noqa: BLE001
            log.warning("sector scares report failed: %s: %s", type(exc2).__name__, exc2)
            return [], None
    print(f"Theme scan: {len(ideas)} idea(s), {len(kept)} inflection candidate(s); see {out}")
    return kept, out


def _run(paths: Paths, cfg: dict, *, yes: bool, dry_run: bool, today: str | None, confirm, only,
         discover: bool = False) -> Path:
    today = today or date.today().isoformat()
    rcfg = cfg["review"]
    refuse_fund(only, cfg)
    fund = parking_ticker(cfg)
    pf = pfmod.load(paths)
    owned = {p.ticker for p in pf.positions}
    ta = tally = None
    discovered: dict[str, dict] = {}
    themes_report = None
    names = cfg.get("lanes", {}).get("themes")
    scares = screener.latest_scares(paths) if discover and not only else {}
    if discover and not only and (names or scares):
        if not dry_run and names:
            ta, tally = _graph(rcfg)
        exclude = owned | set(preview(paths, cfg)["tickers"])
        kept, themes_report = discover_themes(paths, cfg, ta.quick_thinking_llm if ta else None, today,
                                              exclude, label=today + ("-dry-run" if dry_run else ""),
                                              scares=scares)
        discovered = {r["ticker"]: r for r in kept}
    scan_usage = tally.snapshot() if tally else {}
    plan_ = preview(paths, cfg, only, list(discovered))
    tickers = plan_["tickers"]
    if not only and plan_["n_new"] == 0 and not plan_["dropped"]:
        print("No screen found (or no candidates); reviewing holdings only. Run `screen` first for new ideas.")
    if plan_["dropped"]:
        print(f"{plan_['dropped']} candidate(s) left out to stay under the ${rcfg['max_cost_per_review']:,.2f} cap "
              "(raise review.max_cost_per_review or lower review.candidates to change this).")
    if plan_["candidate_countries"]:
        if plan_["candidate_markets"]:
            print(f"Value candidates from: {', '.join(plan_['candidate_countries'])}; markets: "
                  f"{', '.join(plan_['candidate_markets'])} (review.candidate_countries, "
                  "review.candidate_markets); compounder candidates from every market.")
        else:
            print(f"Value candidates from: {', '.join(plan_['candidate_countries'])} "
                  "(review.candidate_countries); compounder candidates from every market.")
        if plan_["missing_countries"]:
            print(f"No screen rows from: {', '.join(plan_['missing_countries'])} "
                  "(check the spelling in review.candidate_countries).")
    if plan_["run_rate_skips"]:
        left = ", ".join(f"{s['ticker']} {s['ttm_vs_5y_ebit']:.2f}" for s in plan_["run_rate_skips"])
        print(f"Left out, profit running below the five-year average: {left}")
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
    priced_names = set(tickers) | owned | ({fund} if fund else set())
    prices = {t: market_data.latest_price(t) for t in sorted(priced_names)}
    fx = {t: _quote_fx(t) for t in sorted(priced_names)}
    screen_row = {t: discovered.get(t) or screen_row_for(t, paths, cfg, screen) for t in tickers}

    results: list[RunResult] = []
    usage = None
    if dry_run:
        results = [fake_run(t, i, t in owned, prices[t]) for i, t in enumerate(tickers)]
        for r in results:
            r.agent_round = arm_for(r.ticker, today, rcfg["agent_round"])
    else:
        if ta is None:
            ta, tally = _graph(rcfg)
        print("Scoring past decisions (memory log)...")
        ta.settle_all_pending()
        ctx = pf.to_context()
        for i, t in enumerate(tickers, 1):
            print(f"[{i}/{len(tickers)}] {t}...", flush=True)
            results.append(run_agents(ta, t, today, ctx, out_dir, tally, note=pipeline_note(screen_row.get(t)),
                                     arm=arm_for(t, today, rcfg["agent_round"])))
        usage = {key: sum(getattr(r, key) for r in results) + scan_usage.get(key, 0)
                 for key in ("tokens_in", "tokens_out", "nominal_cost_usd")}
        usage = {**usage,
                 "on_plan": str(ta.config.get("llm_provider", "")).lower() == "claude_code"}

    for r in results:
        try:
            r.earnings_date = next_earnings_date(r.ticker)
        except Exception:
            r.earnings_date = None

    scenarios = {r.ticker: _scenarios_at(r.decision, prices.get(r.ticker)) for r in results}
    # A dry run makes no Yahoo calls for the estimates.
    consensus_of = {r.ticker: (None, None) if dry_run else _consensus_figures(r.ticker) for r in results}
    decisions = []
    for r in results:
        held = pf.position(r.ticker)
        row = screen_row.get(r.ticker)
        if held is not None:     # an unreadable currency is no rate for a foreign holding: size it by hand
            fx[r.ticker] = (fx[r.ticker][0] or held.currency, pfmod.position_rate(held, fx[r.ticker]))
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
            currency=fx[r.ticker][0],
            fx_to_usd=fx[r.ticker][1],
            lane=parse.lane(r.decision),
            lane_hint=_hint(row),
            lane_unresolved=bool(parse.section(r.decision, "Unresolved Checks")),
        ))
    reviewed = {d.ticker for d in decisions}
    others = [(p.ticker, _holding_value(p, prices.get(p.ticker), fx[p.ticker]),
               _sector(paths, p.ticker, screen.get(p.ticker)), p.lane)
              for p in pf.positions if p.ticker not in reviewed and p.ticker != fund]
    parked, parking, fund_info = 0.0, None, None
    if fund:
        parked, parking, fund_info = _parking(pf.position(fund), fund, prices.get(fund), fx[fund], cfg["parking"])
    actions, summary = plan(decisions, pf.cash, cfg["sizing"], others, parked, parking)
    failures = [f"{r.ticker}: {r.error}" for r in results if r.error]
    cost = 0.0 if dry_run else sum(1 for r in results if not r.error) * rcfg["cost_per_run"]
    comparison = benchmark.compare(summary["total_value"], pf.deposits, pfmod.read_trades(paths))
    knives = []
    if screen:
        ranked = sorted((r for r in screen.values() if not pd.isna(r.get("mf_position"))), key=lambda r: r["mf_position"])
        knives = [r["ticker"] for r in ranked[:50] if r.get("trend") == "FREEFALL" and r["ticker"] not in owned][:10]

    trends = {d.ticker: d.trend for d in decisions}
    rows = [
        {"ticker": r.ticker, "name": r.name, "rating": r.rating, "error": r.error, "report": r.report,
         "agent_round": r.agent_round, "missing_data_lines": missing_data_lines(r.decision),
         "blind_value": r.blind_value, "judge_order": r.judge_order,
         "earnings_date": r.earnings_date, "trend": trends.get(r.ticker),
         "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "nominal_cost_usd": round(r.nominal_cost_usd, 4),
         "intrinsic_value": parse.intrinsic_value(r.decision), "margin_of_safety": parse.margin_of_safety(r.decision),
         "thesis": parse.thesis_summary(r.decision),
         "thesis_full": " ".join(parse.section(r.decision, "Investment Thesis").split()), "price": prices.get(r.ticker),
         "formula_value": _float(screen_row[r.ticker].get("value_base")) if screen_row.get(r.ticker) else None,
         "expected_return": _float(screen_row[r.ticker].get("expected_return"))
         if screen_row.get(r.ticker) else None,
         "currency": fx[r.ticker][0], "fx_to_usd": fx[r.ticker][1],
         "entry_price": parse.entry_price(r.decision), "kill_criteria": parse.kill_criteria(r.decision),
         "kill_criteria_check": parse.kill_criteria_check(r.decision),
         "checklist": parse.checklist(r.decision), "scenarios": scenarios[r.ticker],
         "value_bridge": parse.value_bridge(r.decision), "red_flags": parse.red_flags(r.decision),
         "lane": parse.lane(r.decision), "lane_evidence": parse.lane_evidence(r.decision),
         "lane_hint": _hint(screen_row.get(r.ticker)),
         "consensus_view": parse.consensus_view(r.decision),
         "consensus_growth": consensus_of[r.ticker][0], "consensus_target": consensus_of[r.ticker][1]}
        for r in results
    ]

    committee, unavailable = None, None
    if rcfg["committee"] and dry_run:
        unavailable = committee_mod.SKIPPED_DRY_RUN
    elif rcfg["committee"] and sum(1 for r in results if not r.error) < 2:
        unavailable = committee_mod.SKIPPED_FEW
    elif rcfg["committee"]:
        committee, unavailable = _committee(ta, tally, decisions, rows, pf, summary, cfg["sizing"], actions,
                                            fund)
        used = tally.snapshot()
        for key in ("tokens_in", "tokens_out", "nominal_cost_usd"):
            usage[key] += used.get(key, 0)

    md = render.actions_markdown(today, actions, summary, reports={r.ticker: r.report for r in results if r.report},
                                 comparison=comparison, cost=cost, failures=failures, dry_run=dry_run, knives=knives,
                                 usage=usage, names={r.ticker: r.name for r in results if r.name},
                                 committee=committee, committee_unavailable=unavailable,
                                 max_weight=cfg["sizing"]["max_position_weight"],
                                 themes_report=themes_report.name
                                 if themes_report and (set(discovered) & set(tickers) or scares)
                                 else None,
                                 foreign=any(r.get("currency") not in (None, "USD") for r in rows),
                                 fund=fund_info, paper=paper_buys(rows, dry_run))
    (out_dir / "ACTIONS.md").write_text(md, encoding="utf-8")
    (out_dir / "ACTIONS.html").write_text(render.actions_html(md, f"Value review {today}"), encoding="utf-8")
    # Written whole: the website reads these files while a review may be finishing.
    _write_whole(out_dir / "results.json", rows)
    _write_whole(out_dir / "actions.json", actions_todo(today, actions, summary.get("parking"), fund))
    if committee:
        _write_whole(out_dir / "committee.json", {**committee[0].model_dump(), "notes": committee[1]})

    if not dry_run:
        _record_ratings(paths, pf, actions, today, {r.ticker: r.earnings_date for r in results})
    return out_dir / "ACTIONS.md"


def _graph(rcfg: dict):
    """The agents' graph and its usage tally, for a real (not dry) run."""
    from tradingagents.llm_clients.usage import UsageTally

    logging.getLogger("tradingagents.llm_clients.claude_code_client").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(format="%(levelname)s %(message)s")
    tally = UsageTally()
    return make_graph(rcfg["analysts"], rcfg.get("statement_vendor"), tally), tally


def _hint(row: dict | None) -> str:
    """The row's lane hint ("compounder", "inflection", "dislocation" or ""); "" for a screen from
    before it."""
    hint = (row or {}).get("lane_hint")
    return hint if isinstance(hint, str) else ""


def _holding_value(p, price: float | None, fx: tuple[str | None, float | None]) -> float:
    """An unreviewed holding's value in US dollars: today's price at the quote's rate, else its
    cost basis in dollars."""
    rate = pfmod.position_rate(p, fx)
    return p.shares * price * rate if price is not None and rate is not None else p.cost_usd


def _parking(p, fund: str, price: float | None, fx: tuple[str | None, float | None],
             parking: dict) -> tuple[float, dict, dict]:
    """The fund's dollar value (cost basis without a price or rate), the ``plan`` parking settings
    (with ``price_usd`` only when the price and rate are known) and the ACTIONS section's facts
    (its quote currency and, when priced, the dollars per unit of it)."""
    # An unreadable currency leaves the fund unpriced: its 1.0 is a placeholder, and a fund not
    # held yet has no currency of its own to say whether it is one.
    rate = pfmod.position_rate(p, fx) if p is not None else (None if fx[0] is None else fx[1])
    price_usd = price * rate if price is not None and rate is not None else None
    held_value = 0.0
    if p is not None:
        held_value = p.shares * price_usd if price_usd is not None else p.cost_usd
    settings = dict(parking)
    if price_usd is not None:
        settings["price_usd"] = price_usd
    currency = fx[0] or (p.currency if p is not None else None)
    return held_value, settings, {"ticker": fund, "value": held_value, "priced": price_usd is not None,
                                  "currency": currency, "fx": rate if price_usd is not None else None}


def actions_todo(today: str, actions: list, move: dict | None, fund: str | None) -> dict:
    """The trades a review asks the user to place, for the dashboard's "Actions needed": every
    ADD/TRIM/SELL/BUY action and the index-fund move, in ACTIONS order. KEEP and SKIP are not
    actions."""
    items = [{"ticker": a.ticker, "action": a.action, "shares": a.shares, "amount": a.amount,
              "reason": a.reason} for a in actions if a.action in ("ADD", "TRIM", "SELL", "BUY")]
    if move and fund:
        park = move["action"] == "PARK"
        items.append({"ticker": fund, "action": "BUY" if park else "SELL", "shares": move.get("shares"),
                      "amount": move["amount"], "reason": "park the idle money" if park else "fund the buys"})
    return {"date": today, "items": items}


def _write_whole(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def paper_buys(rows: list[dict], dry_run: bool) -> list[dict] | None:
    """The rows of ``results.json`` that meet a paper Buy rule, for ACTIONS.md. None on a dry run,
    whose ratings are fake. Display only: nothing sizes or trades on it."""
    if dry_run:
        return None
    return [{"ticker": r["ticker"], "rules": rules, "price": r.get("price"),
             "value": r.get("intrinsic_value"),
             "margin_of_safety": r.get("margin_of_safety"), "currency": r.get("currency")}
            for r in rows if (rules := paper.flags(r))]


def _committee(ta, tally, decisions, rows, pf, summary, sizing, actions, fund=None):
    """The committee's reconciled answer and notes, or the reason it is not available.
    Its tokens are left in ``tally`` for the review's cost line."""
    tally.reset()
    try:
        payload = committee_mod.build_payload(decisions, rows, pf, summary, sizing, skip=fund)
        result = committee_mod.run_committee(ta.deep_thinking_llm, payload)
    except Exception as exc:  # noqa: BLE001 - the committee is advice on top; the review stands without it
        log.warning("investment committee failed: %s: %s", type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    if result is None:
        log.warning("investment committee gave no structured answer")
        return None, "the model gave no structured answer"
    return committee_mod.reconcile(result, actions), None


def _record_ratings(paths: Paths, pf: pfmod.Portfolio, actions, today: str,
                    earnings: dict[str, str | None] | None = None) -> None:
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
        held.lane = a.lane
        if a.decision.intrinsic_value is not None:
            held.intrinsic_value_estimate = a.decision.intrinsic_value
        if a.decision.kill_criteria:
            held.kill_criteria = list(a.decision.kill_criteria)
        if a.decision.entry_price is not None:
            held.entry_price = a.decision.entry_price
        if (earnings or {}).get(a.ticker):
            held.next_earnings_date = earnings[a.ticker]
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
