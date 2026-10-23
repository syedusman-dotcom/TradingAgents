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
                    price: float | None, worksheet: str = "") -> list[str]:
    """Plain-English problems with the decision; empty when it is consistent.

    ``formula_value`` is the pipeline's when it has one, else the decision's own; with
    neither, the bridge is not checked. ``price`` is today's, from the worksheet. A compounder
    or inflection lane is checked against ``worksheet`` (the valuation worksheet and its Record
    and inflection lines): a lane claim the numbers there do not support is a problem.
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
    if decision.rating.value != "Sell":
        if decision.lane == "compounder":
            problems += _compounder_problems(decision, formula_value, price, worksheet)
        elif decision.lane == "inflection":
            problems += _inflection_problems(decision, price, worksheet)
    return problems


# --- the lanes' bars ------------------------------------------------------------

COMPOUNDER_ROIC_YEARS = 5         # ROIC above 20% in every one of these years (four when only four exist)
COMPOUNDER_MIN_ROIC_YEARS = 4
COMPOUNDER_REVENUE_CAGR = 0.08
COMPOUNDER_MIN_CAGR_YEARS = 5
COMPOUNDER_SUSTAINABLE_GROWTH = 0.10
COMPOUNDER_BUY_PREMIUM = 1.15     # price over the worksheet base
COMPOUNDER_OVERWEIGHT_PREMIUM = 1.25
INFLECTION_EBIT_MULTIPLE = 1.5    # TTM EBIT over its five-year average
INFLECTION_ACCELERATION = 0.10
INFLECTION_INCREMENTAL_ROIC = 0.20
INFLECTION_BEAR_PROBABILITY = 0.30
COMPOUNDER_UPSIDE_DOWNSIDE = 1.5  # for a Buy, at today's price
COMPOUNDER_BUY_RETURN = 0.12      # yield + growth + reversion (expansion not counted), per year
COMPOUNDER_OVERWEIGHT_RETURN = 0.10
INFLECTION_WEIGHTED_MOS = 0.20    # for a Buy, at today's price
INFLECTION_UPSIDE_DOWNSIDE = 2.0
_EXPECTED_RETURN = re.compile(
    r"Expected return \(\d+-year\):\s*[+\-−]?[\d.]+%/yr = yield ([+\-−]?[\d.]+)% \+ growth "
    r"([+\-−]?[\d.]+)% \+ multiple reversion ([+\-−]?[\d.]+)%")
_DEMAND = re.compile(r"\b(orders?|backlog|capex|capital spending|demand|bookings?)\b", re.I)


def _pct(x: float) -> str:
    return f"{x:.1%}"


def worksheet_expected_return(excerpt: str) -> dict | None:
    """``{"yield", "growth", "reversion"}`` as fractions from the worksheet's expected-return line;
    None when it is absent."""
    match = _EXPECTED_RETURN.search(excerpt)
    if not match:
        return None
    y, g, r = (float(x.replace("−", "-")) / 100 for x in match.groups())
    return {"yield": y, "growth": g, "reversion": r}


def _upside_downside_problem(decision, today, bar, lane) -> str | None:
    """A Buy's upside/downside at today's price below ``bar``; a price at or under the bear case
    passes, one at or over the bull case fails."""
    if decision.scenarios is None or today is None:
        return None
    ratio = scenario_stats(decision.scenarios, today)["upside_downside"]
    if ratio is None and today <= decision.scenarios.bear.value:
        return None
    if ratio is not None and ratio >= bar:
        return None
    shown = f"{ratio:.1f}×" if ratio is not None else "none (the price is above the bull case)"
    return (f"The {lane} lane's Buy needs an upside/downside of at least {bar:.1f}× at today's price "
            f"{today:.2f}; the scenarios give {shown}.")


def _compounder_problems(decision, formula_value, price, worksheet) -> list[str]:
    rec = worksheet_record(worksheet)
    problems = []
    above, counted = rec["roic_years_above_20"], rec["roic_years_counted"]
    if (above is None or counted is None or counted < COMPOUNDER_MIN_ROIC_YEARS
            or above < min(counted, COMPOUNDER_ROIC_YEARS)):
        shown = f"{above}/{counted}" if above is not None and counted else "no ROIC record"
        problems.append("The compounder lane needs ROIC above 20% in every one of the last five years "
                        f"(four when only four years are on record); the worksheet shows {shown}.")
    cagr, years = rec["revenue_cagr"], rec["revenue_cagr_years"]
    if cagr is None or years is None or years < COMPOUNDER_MIN_CAGR_YEARS:
        shown = f"{_pct(cagr)} over {years} years" if cagr is not None and years else "none"
        problems.append(f"The compounder lane needs a revenue CAGR of at least {COMPOUNDER_REVENUE_CAGR:.0%} "
                        f"over ten years (five when ten are unavailable); the worksheet gives {shown}.")
    elif cagr < COMPOUNDER_REVENUE_CAGR:
        problems.append(f"The compounder lane needs a revenue CAGR of at least {COMPOUNDER_REVENUE_CAGR:.0%}; "
                        f"the worksheet shows {_pct(cagr)} over {years} years.")
    sustainable, implied = rec["sustainable_growth"], rec["implied_growth"]
    if sustainable is None or sustainable < COMPOUNDER_SUSTAINABLE_GROWTH:
        shown = _pct(sustainable) if sustainable is not None else "none"
        problems.append("The compounder lane needs sustainable growth (ROIC × reinvestment rate) of at least "
                        f"{COMPOUNDER_SUSTAINABLE_GROWTH:.0%}; the worksheet shows {shown}.")
    elif implied is None:
        problems.append("The compounder lane needs the reverse DCF's implied growth to compare with "
                        "sustainable growth; the worksheet has none.")
    elif implied > sustainable:
        problems.append(f"The compounder lane needs the price's implied growth ({_pct(implied)}, reverse DCF) "
                        f"at or below sustainable growth ({_pct(sustainable)}).")
    caps = {"Buy": COMPOUNDER_BUY_PREMIUM, "Overweight": COMPOUNDER_OVERWEIGHT_PREMIUM}
    cap = caps.get(decision.rating.value)
    base = formula_value if formula_value is not None else worksheet_base(worksheet)
    base = base if base is not None else decision.formula_value
    today = price if price is not None else worksheet_price(worksheet)
    if cap is not None:
        if base is None or today is None or base <= 0:
            problems.append(f"The compounder lane's {decision.rating.value} needs today's price and the "
                            "worksheet base to measure the premium; one is missing.")
        elif today > cap * base:
            verb = "buys" if decision.rating.value == "Buy" else "adds (Overweight)"
            problems.append(f"The compounder lane {verb} at no more than {cap:.2f}× the worksheet base "
                            f"({base:.2f}); today's price {today:.2f} is {today / base:.2f}×.")
    if decision.rating.value == "Buy":
        problem = _upside_downside_problem(decision, today, COMPOUNDER_UPSIDE_DOWNSIDE, "compounder")
        if problem:
            problems.append(problem)
    er = worksheet_expected_return(worksheet)
    floor = {"Buy": COMPOUNDER_BUY_RETURN, "Overweight": COMPOUNDER_OVERWEIGHT_RETURN}.get(decision.rating.value)
    if er is not None and floor is not None:
        earned = er["yield"] + er["growth"] + min(er["reversion"], 0.0)
        if earned < floor - 1e-9:
            problems.append(f"The compounder lane's {decision.rating.value} needs an expected return of at "
                            f"least {floor:.0%}/yr with no multiple expansion; the worksheet gives "
                            f"{_pct(earned)} (yield {_pct(er['yield'])} + growth {_pct(er['growth'])} + "
                            f"reversion {_pct(min(er['reversion'], 0.0))}).")
    return problems


def _inflection_problems(decision, price, worksheet) -> list[str]:
    inf = worksheet_inflection(worksheet)
    problems = []
    for key, bar, words, fmt in (
        ("ttm_vs_5y_ebit", INFLECTION_EBIT_MULTIPLE, "TTM EBIT at least 1.5× its five-year average",
         lambda x: f"{x:.2f}×"),
        ("revenue_acceleration", INFLECTION_ACCELERATION,
         "revenue growth accelerated by at least 10 points", lambda x: f"{x:+.0%}"),
        ("incremental_roic", INFLECTION_INCREMENTAL_ROIC, "incremental ROIC of at least 20%",
         lambda x: f"{x:.0%}"),
    ):
        value = inf[key]
        if value is None or value < bar:
            shown = fmt(value) if value is not None else "n/a"
            problems.append(f"The inflection lane needs {words}; the worksheet shows {shown}.")
    if decision.scenarios is not None and decision.scenarios.bear.probability < INFLECTION_BEAR_PROBABILITY - 1e-9:
        problems.append("The inflection lane needs the bear case (growth stops now) at a probability of at "
                        f"least {INFLECTION_BEAR_PROBABILITY:.0%}; it is "
                        f"{decision.scenarios.bear.probability:.0%}.")
    if not any(_DEMAND.search(k) for k in decision.kill_criteria):
        problems.append("The inflection lane needs a kill criterion on a demand indicator: orders, backlog, "
                        "bookings, customer capex or demand.")
    if not any(item.strip().lower().startswith("verified:") for item in decision.lane_evidence):
        problems.append("The inflection lane needs verified demand evidence from the news report: add a lane "
                        "evidence item starting 'verified:' naming its source.")
    today = price if price is not None else worksheet_price(worksheet)
    if decision.rating.value == "Buy" and decision.scenarios is not None and today is not None:
        mos = scenario_stats(decision.scenarios, today)["weighted_mos"]
        if mos is not None and mos < INFLECTION_WEIGHTED_MOS - 1e-9:
            problems.append("The inflection lane's Buy needs a probability-weighted margin of safety of at "
                            f"least {INFLECTION_WEIGHTED_MOS:.0%} at today's price {today:.2f}; the "
                            f"scenarios give {mos:.0%}.")
        problem = _upside_downside_problem(decision, today, INFLECTION_UPSIDE_DOWNSIDE, "inflection")
        if problem:
            problems.append(problem)
    return problems


def _section_excerpt(report: str, heading: str, last: bool = False) -> str:
    """From a ``heading`` line (the first, or the last) to the next ``## `` heading or the end."""
    lines = report.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading)]
    if not starts:
        return ""
    start = starts[-1] if last else starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def worksheet_excerpt(report: str) -> str:
    """The fundamentals report's valuation worksheet: from its heading to the next ``## `` heading
    or the end; empty when the report has none."""
    return _section_excerpt(report, "## Valuation worksheet")


def record_excerpt(report: str) -> str:
    """The report's ``## Record and inflection`` section; the last one, since the tool's is
    appended after the analyst's prose. Empty when the report has none."""
    return _section_excerpt(report, "## Record and inflection", last=True)


# Each ``- Label value`` line of the Record and inflection section holds one number or n/a.
_RECORD_LABELS = {
    "roic_years_above_20": "ROIC years above 20%",
    "roic_years_counted": "ROIC years counted",
    "roic_min_5y": "ROIC worst of last 5 years",
    "revenue_cagr": "Revenue CAGR",
    "revenue_cagr_years": "Revenue CAGR years",
    "fcf_cagr_5y": "FCF CAGR 5y",
    "reinvestment_rate": "Reinvestment rate",
    "sustainable_growth": "Sustainable growth",
}
_INFLECTION_LABELS = {
    "ttm_vs_5y_revenue": "TTM revenue vs 5y average",
    "ttm_vs_5y_ebit": "TTM EBIT vs 5y average",
    "revenue_acceleration": "Revenue acceleration",
    "ebit_margin_change_4q": "EBIT margin change on a year earlier",
    "incremental_roic": "Incremental ROIC",
    "run_rate_value": "Run-rate value",
    "run_rate_g3": "Run-rate 3-year growth",
    "run_rate_decay": "Run-rate decay",
    "implied_g3": "Implied 3-year growth",
}
_COUNTS = {"roic_years_above_20", "roic_years_counted", "revenue_cagr_years"}
_IMPLIED = re.compile(r"Reverse DCF: today's price assumes\s*([+\-−]?\d+(?:\.\d+)?)%")


def _labelled(excerpt: str, label: str) -> float | None:
    """The number on a ``- Label value`` line: a percentage as a fraction; None for n/a or absent."""
    match = re.search(rf"^\s*-\s*{re.escape(label)}\s+([+\-−]?\d+(?:\.\d+)?)(%?)\s*$", excerpt, re.M)
    if not match:
        return None
    value = float(match.group(1).replace("−", "-"))
    return value / 100 if match.group(2) else value


def _read(excerpt: str, labels: dict) -> dict:
    out = {key: _labelled(excerpt, label) for key, label in labels.items()}
    return {k: int(v) if k in _COUNTS and v is not None else v for k, v in out.items()}


def worksheet_record(excerpt: str) -> dict:
    """The record figures from the Record and inflection lines, plus ``implied_growth`` from the
    worksheet's reverse-DCF line; percentages as fractions, None where n/a or absent."""
    out = _read(excerpt, _RECORD_LABELS)
    match = _IMPLIED.search(excerpt)
    out["implied_growth"] = float(match.group(1).replace("−", "-")) / 100 if match else None
    return out


def worksheet_inflection(excerpt: str) -> dict:
    """The inflection and run-rate figures from the Record and inflection lines; percentages as
    fractions, None where n/a or absent."""
    return _read(excerpt, _INFLECTION_LABELS)


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
