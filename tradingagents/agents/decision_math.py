"""The Portfolio Manager's decision arithmetic: scenario statistics and the consistency checks
its structured decision must pass. Pure functions, no I/O."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradingagents.agents.schemas import PortfolioDecision, Scenarios

TOLERANCE = 0.02          # of the price target, for the bridge and the expected value
PROBABILITY_TOLERANCE = 0.02
MIN_KILL_CRITERIA = 3


def scenario_stats(scenarios: Scenarios, price: float | None) -> dict:
    """Probability-weighted value, upside/downside and margin of safety at ``price``.

    The weights are normalised by their total, so probabilities that miss 1 still give the
    scenarios' weighted value (the check flags the miss separately). ``upside_downside`` is
    None unless the price lies strictly between the bear and bull values.
    """
    legs = (scenarios.bear, scenarios.base, scenarios.bull)
    total = sum(s.probability for s in legs)
    ev = sum(s.value * s.probability for s in legs) / total if total > 0 else None
    bear, bull = scenarios.bear.value, scenarios.bull.value
    upside_downside = (bull - price) / (price - bear) if price is not None and bear < price < bull else None
    weighted_mos = (ev - price) / ev if price is not None and ev else None
    return {"expected_value": ev, "upside_downside": upside_downside, "weighted_mos": weighted_mos}


def decision_checks(decision: PortfolioDecision, formula_value: float | None,
                    price: float | None) -> list[str]:
    """Plain-English problems with the decision; empty when it is consistent.

    ``formula_value`` is the pipeline's when it has one, else the decision's own; with
    neither, the bridge is not checked. ``price`` (today's, from the worksheet) is passed
    through to the scenario statistics; none of the checks needs it.
    """
    problems = []
    target = decision.price_target
    start = formula_value if formula_value is not None else decision.formula_value
    if start is not None and target is not None:
        end = start + sum(a.amount for a in decision.value_bridge)
        if abs(end - target) > TOLERANCE * abs(target):
            problems.append(f"The value bridge from the formula value {start:.2f} sums to {end:.2f}, "
                            f"not the price target {target:.2f}: make the adjustments add up.")
    if decision.scenarios is not None:
        s = decision.scenarios
        total = s.bear.probability + s.base.probability + s.bull.probability
        if abs(total - 1) > PROBABILITY_TOLERANCE:
            problems.append(f"The scenario probabilities sum to {total:.2f}, not 1.")
        ev = scenario_stats(s, price)["expected_value"]
        if ev is not None and target is not None and abs(ev - target) > TOLERANCE * abs(target):
            problems.append(f"The scenarios' expected value {ev:.2f} is not the price target "
                            f"{target:.2f}: reconcile them.")
    if decision.rating.value != "Sell":
        if decision.entry_price is None:
            rating = decision.rating.value
            article = "an" if rating[0] in "AEIOU" else "a"
            problems.append(f"No entry price: {article} {rating} rating needs the price at which to buy.")
        if decision.scenarios is None:
            problems.append("No scenarios: give bear, base and bull values with their probabilities.")
        for label, verdict in (("Good business", decision.good_business),
                               ("Good management", decision.good_management),
                               ("Cheap", decision.cheap)):
            if verdict is None:
                problems.append(f"No {label} verdict: the checklist needs all three tests.")
    if decision.rating.value != "Sell" and len(decision.kill_criteria) < MIN_KILL_CRITERIA:
        problems.append(f"Only {len(decision.kill_criteria)} kill criteria: give 3 to 6 verifiable "
                        "facts that would prove the thesis wrong.")
    return problems


def worksheet_excerpt(report: str) -> str:
    """The fundamentals report's valuation worksheet: from its heading to the next ``## `` heading
    or the end; empty when the report has none."""
    lines = report.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## Valuation worksheet")), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


_BASE = re.compile(r"base \(3% growth\)\s*([\d,]+(?:\.\d+)?)")


def worksheet_base(excerpt: str) -> float | None:
    """The formula's base value from the worksheet's range line; None when it is absent."""
    match = _BASE.search(excerpt)
    return float(match.group(1).replace(",", "")) if match else None


_PRICE = re.compile(r"Price today\s*([\d,]+(?:\.\d+)?)")


def worksheet_price(excerpt: str) -> float | None:
    """Today's price from the worksheet's ``Price today`` line (the quote unit); None when absent."""
    match = _PRICE.search(excerpt)
    return float(match.group(1).replace(",", "")) if match else None
