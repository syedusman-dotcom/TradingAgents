"""The investment committee: one deep-tier call that ranks and allocates across every stock
the review looked at, then is checked against the deterministic rules. Advice only; nothing trades."""

from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import BaseModel, Field

from value_pipeline.actions import Action, Decision

PROMPT = (
    "You are the investment committee of a concentrated long-term value portfolio (5–10 stocks, "
    "1–3 year horizon, never more than 10% in one stock or 30% in one sector). Rank every reviewed "
    "stock by expected annual return adjusted for the probability-weighted downside and any accounting "
    "or distress red flags; propose the allocation under those caps; propose a swap only when a "
    "candidate's expected return exceeds a holding's by at least 5 percentage points; name "
    "concentration and correlation risks (same sector, same end market, same macro factor); give one "
    "paragraph of rationale per action. Never recommend buying a stock whose own review rated it Sell. "
    "Nothing is traded automatically; this is advice for a human."
)

_BUYS = {"BUY", "ADD"}


class RankedIdea(BaseModel):
    ticker: str
    rank: int = Field(description="1 is the best idea")
    action: Literal["BUY", "ADD", "HOLD", "TRIM", "SELL", "SKIP"]
    weight: float = Field(description="Proposed portfolio weight as a fraction (0.08 = 8%)")
    expected_return: float | None = Field(description="Expected annual return as a fraction (0.12 = 12%/yr)")
    downside: str = Field(description="The probability-weighted downside in a sentence")
    rationale: str = Field(description="One paragraph on why this action")


class Swap(BaseModel):
    sell: str
    buy: str
    reason: str


class Committee(BaseModel):
    ranking: list[RankedIdea]
    swaps: list[Swap]
    concentration: list[str] = Field(description="Concentration and correlation risks, one per item")
    summary: str


def _clean(x):
    """JSON-safe: non-finite floats become None, containers are walked."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def build_payload(decisions: list[Decision], results: list[dict], portfolio, summary: dict, sizing: dict) -> dict:
    """What the committee sees: every reviewed stock's verdict and evidence, the cash and the rules."""
    rows = {r["ticker"]: r for r in results}
    total = summary.get("total_value") or 0.0
    reviewed = {d.ticker for d in decisions}
    stocks = []
    for d in decisions:
        row = rows.get(d.ticker, {})
        value = d.held_shares * d.price if d.held and d.price else 0.0
        stocks.append({
            "ticker": d.ticker, "name": row.get("name"), "rating": d.rating, "error": row.get("error"),
            "held": d.held, "held_weight": value / total if total > 0 else 0.0,
            "price": d.price, "intrinsic_value": d.intrinsic_value, "margin_of_safety": d.margin_of_safety,
            "expected_return": d.expected_return, "entry_price": d.entry_price,
            "upside_downside": d.upside_downside, "sector": d.sector, "fscore": d.fscore, "trend": d.trend,
            "checklist": row.get("checklist"), "scenarios": row.get("scenarios"),
            "red_flags": row.get("red_flags") or [], "kill_criteria": list(d.kill_criteria),
            "thesis": row.get("thesis"),
        })
    others = [{"ticker": p.ticker, "shares": p.shares, "avg_price": p.avg_price}
              for p in portfolio.positions if p.ticker not in reviewed]
    return _clean({"stocks": stocks, "other_holdings": others, "cash": portfolio.cash,
                   "total_value": total, "sizing": dict(sizing)})


def run_committee(llm, payload: dict) -> Committee | None:
    """The committee's answer, or None when the model gave no structured one."""
    from tradingagents.agents.structured import (
        NO_EXTERNAL_TOOLS,
        bind_structured,
        invoke_structured,
    )

    prompt = f"{PROMPT}\n\n{json.dumps(payload, indent=2)}\n\n{NO_EXTERNAL_TOOLS}"
    structured = bind_structured(llm, Committee, "Investment Committee")
    return invoke_structured(structured, prompt, "Investment Committee")


def reconcile(committee: Committee, actions: list[Action]) -> tuple[Committee, list[str]]:
    """Drop the committee's buys of anything its own review rated Sell, and name every
    ticker where the committee and the rules disagree."""
    rules = {a.ticker: a for a in actions}
    sold = {t for t, a in rules.items() if a.rating == "Sell"}
    notes: list[str] = []
    ranking = []
    for idea in committee.ranking:
        if idea.action in _BUYS and idea.ticker in sold:
            notes.append(f"committee wanted to buy {idea.ticker} but its review rated Sell: the review stands")
            continue
        ranking.append(idea)
    swaps = []
    for s in committee.swaps:
        if s.buy in sold:
            note = f"committee wanted to buy {s.buy} but its review rated Sell: the review stands"
            if note not in notes:
                notes.append(note)
            continue
        swaps.append(s)
    for idea in ranking:
        rule = rules.get(idea.ticker)
        if rule is not None and _rules_word(idea.action, rule.held) != rule.action:
            notes.append(f"{idea.ticker}: committee says {idea.action}, rules say {rule.action} ({rule.reason})")
    return committee.model_copy(update={"ranking": ranking, "swaps": swaps}), notes


def _rules_word(action: str, held: bool) -> str:
    """The committee's action in the rules' words: HOLD is KEEP; buying more of a holding is
    ADD, and adding to a stock not yet owned is BUY."""
    if action == "HOLD":
        return "KEEP"
    if action in _BUYS:
        return "ADD" if held else "BUY"
    return action


SKIPPED_FEW = "skipped (fewer than 2 successful reviews)"
SKIPPED_DRY_RUN = "skipped (dry run: no agent calls)"


def _weight(weight: float, cap: float) -> str:
    """A weight as a percentage; one above 1.0 is taken as already a percentage, and one above
    the position cap is shown at the cap."""
    w = weight / 100 if weight > 1.0 else weight
    if w > cap + 1e-9:
        return f"{cap:.0%} (capped from {w:.0%})"
    return f"{w:.0%}"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_committee(committee: Committee, notes: list[str], names: dict[str, str] | None,
                     max_weight: float = 0.10) -> list[str]:
    """The ACTIONS section, as Markdown lines."""
    def who(ticker: str) -> str:
        return f"{names[ticker]} ({ticker})" if names and names.get(ticker) else ticker

    lines = ["## Investment committee", "", committee.summary, "",
             "| Rank | Ticker | Action | Weight | Expected return | Rationale |", "|---|---|---|---|---|---|"]
    for r in sorted(committee.ranking, key=lambda r: r.rank):
        er = "–" if r.expected_return is None else f"{r.expected_return:.0%}/yr"
        lines.append(f"| {r.rank} | {_cell(who(r.ticker))} | {r.action} | {_weight(r.weight, max_weight)} | {er} | {_cell(r.rationale)} |")
    lines += ["", "### Swaps", ""]
    lines += [f"- Sell {s.sell}, buy {s.buy}: {s.reason}" for s in committee.swaps] or ["None proposed."]
    lines += ["", "### Concentration", ""]
    lines += [f"- {c}" for c in committee.concentration] or ["None named."]
    lines += ["", "### Where the committee and the rules disagree", ""]
    lines += [f"- {n}" for n in notes] or ["Nowhere: the committee agrees with the rules."]
    return lines + [""]


def render_unavailable(reason: str) -> list[str]:
    """The section when there is no committee answer: skipped on purpose, or a failure's reason."""
    line = reason if reason.startswith("skipped") else f"not available ({reason})"
    return ["## Investment committee", "", f"Investment committee: {line}", ""]
