"""The Portfolio Manager's checklist, scenarios and value bridge: the maths, the checks, and the
rendered text the pipeline reads back."""

import pytest

from tradingagents.agents.decision_math import decision_checks, scenario_stats, worksheet_excerpt
from tradingagents.agents.schemas import (
    Adjustment,
    PortfolioDecision,
    PortfolioRating,
    Scenario,
    Scenarios,
    Verdict,
    render_pm_decision,
)
from value_pipeline import parse


def _scen(bear_p, base_p, bull_p):
    return Scenarios(bear=Scenario(value=42, probability=bear_p, basis="0.8x multiple, no growth"),
                     base=Scenario(value=56, probability=base_p, basis="formula value less adjustments"),
                     bull=Scenario(value=70, probability=bull_p, basis="margins hold"))


def _good_fields():
    return {
        "rating": PortfolioRating.OVERWEIGHT, "executive_summary": "Buy below 46.",
        "investment_thesis": "Durable franchise. Margin of safety: 18%", "price_target": 56.0,
        "time_horizon": "1-3 years", "formula_value": 80.33,
        "good_business": Verdict(passed=True, note="ROIC above 20% for ten years"),
        "good_management": Verdict(passed=True, note="buybacks below value"),
        "cheap": Verdict(passed=True, note="Margin of safety: 18%"),
        "red_flags": [],
        "kill_criteria": ["ROIC below 12% two years running", "net debt above 3x EBIT",
                          "the CEO sells more than half his stake"],
        "entry_price": 46.0,
        "value_bridge": [Adjustment(reason="peak-cycle margins", amount=-12.0),
                         Adjustment(reason="net debt", amount=-12.33)],
        "scenarios": _scen(0.25, 0.5, 0.25),
        "premortem_answer": "Margins revert, but the bear case covers it.",
    }


def _good_decision():
    return PortfolioDecision(**_good_fields())


def _decision(**overrides):
    return PortfolioDecision(**{**_good_fields(), **overrides})


@pytest.mark.unit
def test_scenario_stats_expected_value_and_upside_downside():
    s = Scenarios(bear=Scenario(value=40, probability=0.25, basis="b"),
                  base=Scenario(value=56, probability=0.5, basis="m"),
                  bull=Scenario(value=70, probability=0.25, basis="u"))
    st = scenario_stats(s, price=50)
    assert st["expected_value"] == pytest.approx(55.5) and st["upside_downside"] == pytest.approx(2.0)
    assert st["weighted_mos"] == pytest.approx((55.5 - 50) / 55.5)


@pytest.mark.unit
def test_scenario_stats_without_a_price_or_outside_the_range_has_no_upside_downside():
    s = _scen(0.25, 0.5, 0.25)
    assert scenario_stats(s, price=None) == {"expected_value": pytest.approx(56.0),
                                             "upside_downside": None, "weighted_mos": None}
    assert scenario_stats(s, price=80)["upside_downside"] is None     # above the bull case
    assert scenario_stats(s, price=40)["upside_downside"] is None     # below the bear case


@pytest.mark.unit
def test_decision_checks_catch_bridge_probabilities_entry_price_and_kill_criteria():
    d = _decision(rating=PortfolioRating.HOLD, price_target=56,
                  value_bridge=[Adjustment(reason="x", amount=-10)], scenarios=_scen(0.3, 0.5, 0.3),
                  entry_price=None, kill_criteria=["a"])
    problems = decision_checks(d, formula_value=80.33, price=50)
    assert len(problems) == 4 and decision_checks(_good_decision(), formula_value=80.33, price=50) == []


@pytest.mark.unit
def test_decision_checks_catch_an_expected_value_off_the_target():
    problems = decision_checks(_decision(price_target=65.0, value_bridge=[
        Adjustment(reason="r", amount=-15.33)]), formula_value=80.33, price=None)
    assert len(problems) == 1 and "expected value" in problems[0].lower()


@pytest.mark.unit
def test_bridge_check_uses_the_decisions_formula_value_or_skips():
    off = _decision(value_bridge=[])
    assert len(decision_checks(off, None, None)) == 1                 # the decision's own 80.33
    assert decision_checks(_decision(value_bridge=[], formula_value=None), None, None) == []
    assert decision_checks(off, formula_value=56.0, price=None) == []  # the pipeline's value wins


@pytest.mark.unit
def test_a_sell_needs_no_entry_price():
    assert decision_checks(_decision(rating=PortfolioRating.SELL, entry_price=None), None, None) == []


@pytest.mark.unit
def test_entry_price_takes_the_same_nullish_coercion_as_the_target():
    assert _decision(entry_price="N/A").entry_price is None
    assert _decision(entry_price="$1,046.50").entry_price == 1046.5


@pytest.mark.unit
def test_rendered_decision_round_trips_through_parse():
    text = render_pm_decision(_good_decision())
    assert parse.entry_price(text) == 46.0 and len(parse.kill_criteria(text)) == 3
    assert parse.checklist(text)["good_business"]["passed"] is True
    assert parse.scenarios(text)["expected_value"] == pytest.approx(56.0)
    assert parse.value_bridge(text)[0]["amount"] == -12.0
    assert parse.intrinsic_value(text) == 56.0 and parse.margin_of_safety(text) is not None   # old parsers still work


@pytest.mark.unit
def test_rendered_sections_follow_the_first_five_in_order():
    text = render_pm_decision(_good_decision())
    labels = ["**Rating**:", "**Executive Summary**:", "**Investment Thesis**:", "**Price Target**:",
              "**Time Horizon**:", "**Checklist**:", "**Red Flags**:", "**Kill Criteria**:",
              "**Entry Price**:", "**Scenarios**:", "**Value Bridge**:", "**Pre-mortem Answer**:"]
    positions = [text.index(label) for label in labels]
    assert positions == sorted(positions)
    assert text.startswith("**Rating**: Overweight\n\n**Executive Summary**: Buy below 46.\n\n"
                           "**Investment Thesis**: Durable franchise. Margin of safety: 18%\n\n"
                           "**Price Target**: 56.0\n\n**Time Horizon**: 1-3 years\n\n**Checklist**:")
    assert "Good business — PASS: ROIC above 20% for ten years" in text
    assert "Bear 25%: 42.00 — 0.8x multiple, no growth" in text
    assert "Formula value 80.33\n-12.00 peak-cycle margins\n-12.33 net debt\n= Price Target 56.00" in text
    assert "**Red Flags**: none" in text


@pytest.mark.unit
def test_parsed_checklist_scenarios_and_bridge_carry_every_field():
    text = render_pm_decision(_decision(good_management=Verdict(passed=False, note="empire building"),
                                        red_flags=["auditor changed twice"]))
    checks = parse.checklist(text)
    assert checks["good_management"] == {"passed": False, "note": "empire building"}
    assert set(checks) == {"good_business", "good_management", "cheap"}
    assert parse.red_flags(text) == ["auditor changed twice"]
    sc = parse.scenarios(text)
    assert sc["bear"]["value"] == 42.0 and sc["bear"]["probability"] == pytest.approx(0.25)
    assert sc["upside_downside"] == pytest.approx((70 - 46) / (46 - 42))   # measured at the entry price
    assert [b["reason"] for b in parse.value_bridge(text)] == ["peak-cycle margins", "net debt"]


@pytest.mark.unit
def test_the_parser_accepts_a_unicode_minus_in_the_bridge():
    text = "**Value Bridge**:\nFormula value 80.33\n−12.00 margins\n+3.50 buyback\n= Price Target 71.83"
    assert parse.value_bridge(text) == [{"reason": "margins", "amount": -12.0},
                                        {"reason": "buyback", "amount": 3.5}]


@pytest.mark.unit
def test_an_old_decision_parses_to_absences():
    text = render_pm_decision(PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="s",
                                                investment_thesis="t"))
    assert "Formula value not provided" in text and "**Entry Price**: not provided" in text
    assert parse.entry_price(text) is None and parse.kill_criteria(text) == []
    assert parse.scenarios(text) is None and parse.value_bridge(text) == [] and parse.red_flags(text) == []
    plain = "**Rating**: Buy\n\n**Price Target**: 50"
    assert parse.checklist(plain) == {} and parse.scenarios(plain) is None and parse.entry_price(plain) is None


@pytest.mark.unit
def test_worksheet_excerpt_is_the_valuation_section_only():
    report = ("## Verified metrics\nROIC 20%\n## Valuation worksheet (per share; 5-year averages)\n"
              "- **Range 60 base (3% growth) 80.33; margin of safety 30%**\n## Peers\nKO")
    assert worksheet_excerpt(report) == ("## Valuation worksheet (per share; 5-year averages)\n"
                                         "- **Range 60 base (3% growth) 80.33; margin of safety 30%**")
    assert worksheet_excerpt("## Valuation worksheet\nlast") == "## Valuation worksheet\nlast"
    assert worksheet_excerpt("no worksheet here") == ""
