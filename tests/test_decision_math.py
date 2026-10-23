"""The Portfolio Manager's checklist, scenarios and value bridge: the maths, the checks, and the
rendered text the pipeline reads back."""

import pytest

from tradingagents.agents.decision_math import (
    decision_checks,
    scenario_stats,
    worksheet_base,
    worksheet_excerpt,
    worksheet_price,
)
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


@pytest.mark.unit
def test_decision_checks_flag_missing_scenarios_and_verdicts_except_on_a_sell():
    assert len(decision_checks(_decision(scenarios=None), None, None)) == 1
    problems = decision_checks(_decision(good_management=None, cheap=None), None, None)
    assert len(problems) == 2 and any("Cheap" in p for p in problems)
    assert decision_checks(_decision(rating=PortfolioRating.SELL, scenarios=None, good_business=None,
                                     entry_price=None), None, None) == []


@pytest.mark.unit
def test_worksheet_base_reads_the_base_value_or_none():
    excerpt = ("## Valuation worksheet (per share; 5-year averages)\n"
               "- **Range 60.00 to 95.00, base (3% growth) 1,080.33; margin of safety at base 30%**")
    assert worksheet_base(excerpt) == pytest.approx(1080.33)
    assert worksheet_base("## Valuation worksheet\nnothing here") is None
    assert worksheet_base("") is None


@pytest.mark.unit
def test_rendered_scenarios_measure_upside_downside_at_todays_price_else_the_entry():
    scen = Scenarios(bear=Scenario(value=40, probability=0.25, basis="b"),
                     base=Scenario(value=80, probability=0.5, basis="m"),
                     bull=Scenario(value=120, probability=0.25, basis="u"))
    d = _decision(scenarios=scen, entry_price=60.0)
    assert "upside/downside 1.0x at price 80.00" in render_pm_decision(d, price=80.0)
    assert "upside/downside 3.0x at entry price 60.00" in render_pm_decision(d)
    assert "upside/downside n/a" in render_pm_decision(_decision(scenarios=scen, entry_price=None))


@pytest.mark.unit
def test_worksheet_price_reads_todays_price_line_or_none():
    excerpt = "## Valuation worksheet (per share)\n- Price today 1,886.50\n- **Range 1 to 2, base (3% growth) 3**"
    assert worksheet_price(excerpt) == pytest.approx(1886.5)
    assert worksheet_price("## Valuation worksheet\nnothing") is None


@pytest.mark.unit
def test_the_worksheet_carries_todays_price_inside_its_section():
    from tests.test_value_metrics import _year
    from tradingagents.dataflows import value_metrics as vm

    m = vm.company_metrics([_year(), _year()], price=25.0, market_cap=2500.0, shares=100)
    excerpt = worksheet_excerpt(vm.render_report("X", "2026-10-23", m))
    assert worksheet_price(excerpt) == 25.0


@pytest.mark.unit
def test_a_sell_is_not_asked_for_kill_criteria_and_the_entry_wording_is_grammatical():
    assert decision_checks(_decision(rating=PortfolioRating.SELL, kill_criteria=[]), None, None) == []
    problems = decision_checks(_decision(rating=PortfolioRating.UNDERWEIGHT, entry_price=None), None, None)
    assert problems == ["No entry price: an Underweight rating needs the price at which to buy."]
    problems = decision_checks(_decision(rating=PortfolioRating.HOLD, entry_price=None), None, None)
    assert problems == ["No entry price: a Hold rating needs the price at which to buy."]


# --- investment lanes ---------------------------------------------------------

_SHEET = """## Valuation worksheet (per share; 5-year averages)

- Price today 108.00
- **Range 70.00 to 140.00, base (3% growth) 100.00; margin of safety at base -8.0%**
- Reverse DCF: today's price assumes 6.0% perpetual FCF growth at a 9.0% discount rate"""


def _record(**values):
    """A ``## Record and inflection`` section in the worksheet's format; a compounder's by default."""
    lines = {"ROIC years above 20%": "5", "ROIC years counted": "5", "ROIC worst of counted years": "24.1%",
             "ROIC incl. goodwill years above 15%": "5", "ROIC incl. goodwill worst of counted years": "18.0%",
             "Revenue CAGR": "11.2%", "Revenue CAGR years": "10", "Earned growth": "10.0%",
             "FCF CAGR": "12.0%", "FCF CAGR years": "4",
             "Reinvestment rate": "62%", "Sustainable growth": "14.0%", "TTM revenue vs 5y average": "1.20",
             "TTM EBIT vs 5y average": "1.30", "Revenue acceleration": "+2%",
             "EBIT margin change on a year earlier": "+0.5%", "Incremental ROIC": "25%",
             "Run-rate value": "120.00", "Run-rate 3-year growth": "10%", "Run-rate decay": "4%",
             "Implied 3-year growth": "8%"}
    lines.update(values)
    return ("\n\n## Record and inflection\n\n" + "\n".join(f"- {k} {v}" for k, v in lines.items())
            + "\n\nRecord: ROIC ≥ 20% in 5/5 years.\nInflection: n/a.\nRun-rate: n/a.")


_INFLECTING = {"TTM EBIT vs 5y average": "2.40", "Revenue acceleration": "+21%", "Incremental ROIC": "38%"}


def _compounder(**overrides):
    """A compounder Buy at the worksheet's price 108 and base 100: upside/downside 4.0x."""
    scen = Scenarios(bear=Scenario(value=90, probability=0.25, basis="multiple compresses"),
                     base=Scenario(value=125, probability=0.5, basis="sustainable growth"),
                     bull=Scenario(value=180, probability=0.25, basis="reinvestment holds"))
    return _decision(**{"rating": PortfolioRating.BUY, "lane": "compounder", "scenarios": scen,
                        "formula_value": 100.0, "price_target": 130.0,
                        "value_bridge": [Adjustment(reason="growth the record earns", amount=30.0)],
                        "lane_evidence": ["ROIC ≥ 20% 5/5 years", "revenue 11.2%/yr 10y",
                                          "sustainable growth 14%", "implied growth 6%", "price 1.08× base"],
                        **overrides})


def _inflection(**overrides):
    """An inflection Buy at the worksheet's price 108: weighted margin of safety 26%, upside/downside 4.0x."""
    scen = Scenarios(bear=Scenario(value=80, probability=0.30, basis="growth stops now"),
                     base=Scenario(value=150, probability=0.45, basis="run-rate"),
                     bull=Scenario(value=220, probability=0.25, basis="demand holds"))
    return _decision(**{"rating": PortfolioRating.BUY, "lane": "inflection", "scenarios": scen,
                        "formula_value": 100.0, "price_target": 146.5,
                        "value_bridge": [Adjustment(reason="run-rate growth", amount=46.5)],
                        "kill_criteria": ["ROIC below 12% two years running", "net debt above 3x EBIT",
                                          "Backlog falls two quarters running"],
                        "lane_evidence": ["TTM EBIT 2.40× the 5-year average",
                                          "verified: Microsoft raised FY27 capex guidance, 2026-07-30 call"],
                        **overrides})


@pytest.mark.unit
def test_lane_and_its_evidence_round_trip_through_render_and_parse():
    d = _compounder()
    text = render_pm_decision(d)
    assert ("**Lane**: compounder — ROIC ≥ 20% 5/5 years; revenue 11.2%/yr 10y; sustainable growth 14%; "
            "implied growth 6%; price 1.08× base") in text
    assert text.index("**Checklist**:") < text.index("**Lane**:") < text.index("**Red Flags**:")
    assert parse.lane(text) == "compounder" and parse.lane_evidence(text) == d.lane_evidence
    assert parse.checklist(text)["cheap"]["passed"] is True and parse.red_flags(text) == []


@pytest.mark.unit
def test_a_decision_without_a_lane_reads_as_value():
    assert _good_decision().lane == "value" and _good_decision().lane_evidence == []
    text = render_pm_decision(_good_decision())
    assert "**Lane**: value\n" in text
    assert parse.lane(text) == "value" and parse.lane_evidence(text) == []
    old = "**Rating**: Buy\n\n**Checklist**:\nCheap — PASS: x\n\n**Red Flags**: none"
    assert parse.lane(old) == "value" and parse.lane_evidence(old) == []
    assert parse.lane("**Lane**: something else") == "value"
    assert parse.lane_evidence("**Lane**: Inflection—verified: backlog up 40%; TTM EBIT 2.4×") == [
        "verified: backlog up 40%", "TTM EBIT 2.4×"]


@pytest.mark.unit
def test_worksheet_record_and_inflection_read_the_labelled_lines():
    from tradingagents.agents.decision_math import worksheet_inflection, worksheet_record

    sheet = _SHEET + _record(**{"Incremental ROIC": "n/a", "EBIT margin change on a year earlier": "-1.5%"})
    rec = worksheet_record(sheet)
    assert rec["roic_years_above_20"] == 5 and rec["roic_years_counted"] == 5
    assert rec["roic_min_5y"] == pytest.approx(0.241) and rec["revenue_cagr"] == pytest.approx(0.112)
    assert rec["revenue_cagr_years"] == 10 and rec["sustainable_growth"] == pytest.approx(0.14)
    assert rec["reinvestment_rate"] == pytest.approx(0.62) and rec["implied_growth"] == pytest.approx(0.06)
    assert rec["roic_gw_years_above_15"] == 5 and rec["roic_gw_min"] == pytest.approx(0.18)
    assert rec["fcf_cagr"] == pytest.approx(0.12) and rec["fcf_cagr_years"] == 4
    assert rec["earned_growth"] == pytest.approx(0.10)
    inf = worksheet_inflection(sheet)
    assert inf["ttm_vs_5y_ebit"] == pytest.approx(1.3) and inf["revenue_acceleration"] == pytest.approx(0.02)
    assert inf["ebit_margin_change_4q"] == pytest.approx(-0.015) and inf["incremental_roic"] is None
    assert inf["run_rate_value"] == pytest.approx(120.0) and inf["implied_g3"] == pytest.approx(0.08)
    assert set(worksheet_record("").values()) == {None} and set(worksheet_inflection("").values()) == {None}


@pytest.mark.unit
def test_worksheet_record_reads_the_real_rendered_section():
    from tests.test_value_metrics import _year
    from tradingagents.agents.decision_math import (
        record_excerpt,
        worksheet_inflection,
        worksheet_record,
    )
    from tradingagents.dataflows import value_metrics as vm

    m = vm.company_metrics([_year() for _ in range(6)], price=25.0, market_cap=2500.0, shares=100)
    report = vm.render_report("X", "2026-10-23", m)
    excerpt = record_excerpt(report)
    assert excerpt.startswith("## Record and inflection")
    rec, inf = worksheet_record(excerpt), worksheet_inflection(excerpt)
    assert rec["roic_years_counted"] == 5 and rec["roic_years_above_20"] is not None
    assert set(rec) >= {"revenue_cagr", "revenue_cagr_years", "sustainable_growth"}
    assert set(inf) >= {"ttm_vs_5y_ebit", "revenue_acceleration", "incremental_roic"}
    from tradingagents.agents.decision_math import worksheet_expected_return

    er = worksheet_expected_return(worksheet_excerpt(report))
    assert er is not None and er["yield"] + er["growth"] + er["reversion"] == pytest.approx(
        m["expected_return"]["expected_return"], abs=0.002)
    assert worksheet_expected_return(_SHEET) is None
    minus = worksheet_expected_return("Expected return (7-year): 4.0%/yr = yield 3.0% + growth 2.0% + "
                                      "multiple reversion −1.0% (bear 1.0% / base 4.0% / bull 6.0%)")
    assert minus == {"yield": pytest.approx(0.03), "growth": pytest.approx(0.02),
                     "reversion": pytest.approx(-0.01)}


@pytest.mark.unit
def test_a_qualifying_compounder_passes():
    assert decision_checks(_compounder(), None, None, worksheet=_SHEET + _record()) == []
    four = _record(**{"ROIC incl. goodwill years above 15%": "4", "ROIC years counted": "4"})
    assert decision_checks(_compounder(), None, None, worksheet=_SHEET + four) == []


@pytest.mark.unit
def test_a_buyback_funded_compounder_passes_on_its_free_cash_flow_and_earned_growth():
    # Reinvestment 0% (payouts exceed owner earnings): sustainable growth 0%, so the ER line's
    # growth is 0% too; the record still earns 10% (revenue 11.9% over 18 years, held to the cap).
    sheet = (_SHEET + "\n\nExpected return (7-year): 6.0%/yr = yield 8.0% + growth 0.0% + multiple "
             "reversion -2.0% (bear 1.0% / base 6.0% / bull 9.0%)"
             + _record(**{"Reinvestment rate": "0%", "Sustainable growth": "0.0%", "Revenue CAGR": "11.9%",
                          "Revenue CAGR years": "18", "FCF CAGR": "9.0%", "Earned growth": "10.0%"}))
    assert decision_checks(_compounder(), None, None, worksheet=sheet) == []
    dear = sheet.replace("assumes 6.0%", "assumes 11.0%")
    assert decision_checks(_compounder(), None, None, worksheet=dear) == [
        "The compounder lane needs the price's implied growth (11.0%, reverse DCF) at or below the "
        "earned growth (10.0%) or the sustainable growth (0.0%), whichever is higher."]
    no_label = sheet.replace("- Earned growth 10.0%\n", "")    # falls back to the ER line's 0% growth
    assert any("expected return of at least 12%" in p
               for p in decision_checks(_compounder(), None, None, worksheet=no_label))


@pytest.mark.unit
def test_a_compounder_needs_roic_including_goodwill_in_every_counted_year():
    three = _record(**{"ROIC incl. goodwill years above 15%": "3", "ROIC years counted": "4"})
    assert decision_checks(_compounder(), None, None, worksheet=_SHEET + three) == [
        "The compounder lane needs ROIC including goodwill ≥ 15% in every counted year (at least "
        "four); the worksheet shows 3/4."]
    short = _record(**{"ROIC incl. goodwill years above 15%": "3", "ROIC years counted": "3"})
    assert len(decision_checks(_compounder(), None, None, worksheet=_SHEET + short)) == 1
    slow_fcf = _record(**{"FCF CAGR": "4.0%"})
    assert decision_checks(_compounder(), None, None, worksheet=_SHEET + slow_fcf) == [
        "The compounder lane needs free cash flow growing at least 5%/yr over three or more years; "
        "the worksheet shows 4.0% over 4 years."]
    no_fcf = _record(**{"FCF CAGR": "n/a", "FCF CAGR years": "n/a"})
    assert decision_checks(_compounder(), None, None, worksheet=_SHEET + no_fcf) == [
        "The compounder lane needs free cash flow growing at least 5%/yr over three or more years; "
        "the worksheet shows none."]


@pytest.mark.unit
def test_compounder_claim_without_the_record_is_sent_back():
    problems = decision_checks(_compounder(), None, None, worksheet=_SHEET)
    assert problems and all("compounder" in p for p in problems)
    assert any("ROIC including goodwill ≥ 15% in every counted year" in p for p in problems)
    weak = _record(**{"ROIC incl. goodwill years above 15%": "3", "Revenue CAGR": "6.0%", "FCF CAGR": "2.0%"})
    problems = decision_checks(_compounder(), None, None, worksheet=_SHEET + weak)
    assert len(problems) == 3
    assert any("3/5" in p for p in problems) and any("6.0%" in p for p in problems)
    assert any("2.0%" in p for p in problems)
    short = _record(**{"Revenue CAGR years": "4"})
    assert len(decision_checks(_compounder(), None, None, worksheet=_SHEET + short)) == 1
    dear_growth = _SHEET.replace("assumes 6.0%", "assumes 15.0%") + _record()
    assert any("implied" in p for p in decision_checks(_compounder(), None, None, worksheet=dear_growth))


@pytest.mark.unit
def test_a_compounder_buy_is_held_to_a_modest_premium_over_the_base():
    dear = _SHEET.replace("Price today 108.00", "Price today 120.00") + _record()
    problems = decision_checks(_compounder(), None, None, worksheet=dear)
    assert problems == ["The compounder lane buys at no more than 1.15× the worksheet base (100.00); "
                        "today's price 120.00 is 1.20×."]
    assert decision_checks(_compounder(rating=PortfolioRating.OVERWEIGHT), None, None, worksheet=dear) == []
    assert decision_checks(_compounder(rating=PortfolioRating.HOLD), None, 130.0, worksheet=dear) == []
    assert len(decision_checks(_compounder(rating=PortfolioRating.OVERWEIGHT), None, 130.0,
                               worksheet=dear)) == 1


@pytest.mark.unit
def test_inflection_claim_needs_verified_demand_and_a_demand_kill_criterion():
    sheet = _SHEET + _record(**_INFLECTING)
    assert decision_checks(_inflection(), None, None, worksheet=sheet) == []
    unverified = _inflection(lane_evidence=["TTM EBIT 2.40×", "unverified: channel checks"],
                             kill_criteria=["ROIC below 12% two years running", "net debt above 3x EBIT",
                                            "the CEO sells half his stake"])
    problems = decision_checks(unverified, None, None, worksheet=sheet)
    assert len(problems) == 2
    assert any("verified:" in p for p in problems) and any("backlog" in p for p in problems)
    bear = Scenarios(bear=Scenario(value=80, probability=0.2, basis="b"),
                     base=Scenario(value=150, probability=0.55, basis="m"),
                     bull=Scenario(value=192, probability=0.25, basis="u"))
    problems = decision_checks(_inflection(scenarios=bear), None, None, worksheet=sheet)
    assert len(problems) == 1 and "30%" in problems[0]


@pytest.mark.unit
def test_inflection_claim_without_the_inflection_numbers_is_sent_back():
    problems = decision_checks(_inflection(), None, None, worksheet=_SHEET + _record())
    assert len(problems) == 2            # EBIT only 1.30x, acceleration only +2%
    assert any("1.30" in p for p in problems) and any("2%" in p for p in problems)
    problems = decision_checks(_inflection(), None, None, worksheet="")
    assert len(problems) == 3 and all("inflection" in p for p in problems)


@pytest.mark.unit
def test_lane_checks_skip_the_value_lane_and_a_sell():
    assert decision_checks(_good_decision(), None, None, worksheet="") == []
    assert decision_checks(_compounder(rating=PortfolioRating.SELL), None, None, worksheet="") == []


def _scenarios(bear, base, bull, p=(0.25, 0.5, 0.25)):
    return Scenarios(bear=Scenario(value=bear, probability=p[0], basis="b"),
                     base=Scenario(value=base, probability=p[1], basis="m"),
                     bull=Scenario(value=bull, probability=p[2], basis="u"))


def _target(scen):
    """The fields that keep the bridge and the expected value consistent with ``scen``."""
    ev = scenario_stats(scen, None)["expected_value"]
    return {"scenarios": scen, "price_target": ev,
            "value_bridge": [Adjustment(reason="r", amount=round(ev - 100.0, 6))]}


@pytest.mark.unit
def test_a_compounder_buy_needs_an_upside_downside_of_one_and_a_half():
    sheet = _SHEET + _record()                                  # price 108
    thin = _scenarios(90, 125, 130)                             # (130-108)/(108-90) = 1.22x
    problems = decision_checks(_compounder(**_target(thin)), None, None, worksheet=sheet)
    assert problems == ["The compounder lane's Buy needs an upside/downside of at least 1.5× at today's "
                        "price 108.00; the scenarios give 1.2×."]
    assert decision_checks(_compounder(**_target(_scenarios(90, 125, 140))), None, None, worksheet=sheet) == []
    below_bear = _scenarios(110, 125, 140)                      # price under the bear case: a pass
    assert decision_checks(_compounder(**_target(below_bear)), None, None, worksheet=sheet) == []
    above_bull = _scenarios(90, 100, 105)
    assert len(decision_checks(_compounder(**_target(above_bull)), None, None, worksheet=sheet)) == 1
    hold = _compounder(rating=PortfolioRating.HOLD, **_target(thin))
    assert decision_checks(hold, None, None, worksheet=sheet) == []
    no_price = sheet.replace("- Price today 108.00\n", "")        # only the premium check asks for it
    assert decision_checks(_compounder(**_target(thin)), None, None, worksheet=no_price) == [
        "The compounder lane's Buy needs today's price and the worksheet base to measure the premium; "
        "one is missing."]


@pytest.mark.unit
def test_a_compounder_needs_the_worksheets_expected_return():
    def sheet(line):
        return _SHEET + "\n\n" + line + _record()

    def er(total, y, g, r):
        return (f"Expected return (7-year): {total}%/yr = yield {y}% + growth {g}% + multiple "
                f"reversion {r}% (bear 1.0% / base 2.0% / bull 3.0%)")

    good = sheet(er("11.0", "4.0", "10.0", "-3.0"))             # 4 + 10 - 3 = 11%
    problems = decision_checks(_compounder(), None, None, worksheet=good)
    assert problems == ["The compounder lane's Buy needs an expected return of at least 12%/yr with no "
                        "multiple expansion; the worksheet gives 11.0% (yield 4.0% + earned growth 10.0% + "
                        "reversion -3.0%)."]
    assert decision_checks(_compounder(rating=PortfolioRating.OVERWEIGHT), None, None, worksheet=good) == []
    weak = (_SHEET + "\n\n" + er("9.0", "3.0", "8.0", "-2.0")
            + _record(**{"Earned growth": "8.0%"}))
    assert len(decision_checks(_compounder(rating=PortfolioRating.OVERWEIGHT), None, None, worksheet=weak)) == 1
    expansion = (_SHEET + "\n\n" + er("14.0", "3.0", "8.0", "3.0")   # expansion is not counted: 11%
                 + _record(**{"Earned growth": "8.0%"}))
    assert len(decision_checks(_compounder(), None, None, worksheet=expansion)) == 1
    strong = sheet(er("13.0", "4.0", "10.0", "-1.0"))
    assert decision_checks(_compounder(), None, None, worksheet=strong) == []
    assert decision_checks(_compounder(rating=PortfolioRating.HOLD), None, None, worksheet=weak) == []


@pytest.mark.unit
def test_an_inflection_buy_needs_a_margin_of_safety_and_an_upside_downside():
    sheet = _SHEET + _record(**_INFLECTING)                     # price 108
    p = (0.3, 0.45, 0.25)
    thin = _scenarios(80, 125, 150, p)        # EV 117.75: margin of safety 8%; (150-108)/28 = 1.5x
    problems = decision_checks(_inflection(**_target(thin)), None, None, worksheet=sheet)
    assert len(problems) == 2
    assert any("margin of safety of at least 20%" in p for p in problems)
    assert any("upside/downside of at least 2.0×" in p for p in problems)
    lopsided = _scenarios(100, 125, 160, p)   # EV 126.25: 14%; (160-108)/8 = 6.5x
    problems = decision_checks(_inflection(**_target(lopsided)), None, None, worksheet=sheet)
    assert len(problems) == 1 and "14%" in problems[0]
    hold = _inflection(rating=PortfolioRating.HOLD, **_target(thin))
    assert decision_checks(hold, None, None, worksheet=sheet) == []
    overweight = decision_checks(_inflection(rating=PortfolioRating.OVERWEIGHT, **_target(thin)), None, None,
                                 worksheet=sheet)                     # the same bars for an Overweight
    assert len(overweight) == 2 and any("lane's Overweight needs an upside/downside" in p for p in overweight)
    no_price = sheet.replace("- Price today 108.00\n", "")
    assert decision_checks(_inflection(**_target(thin)), None, None, worksheet=no_price) == []


@pytest.mark.unit
def test_a_lane_buy_allows_no_red_flag_and_needs_the_verdicts():
    comp, infl = _SHEET + _record(), _SHEET + _record(**_INFLECTING)
    flagged = _compounder(red_flags=["receivables up 40% on revenue up 10%"])
    assert decision_checks(flagged, None, None, worksheet=comp) == [
        "The compounder lane allows no red flag; the decision lists 1: resolve it or choose the value lane."]
    two = _inflection(rating=PortfolioRating.OVERWEIGHT, red_flags=["a", "b"])
    assert "The inflection lane allows no red flag; the decision lists 2: resolve it or choose the value lane." \
        in decision_checks(two, None, None, worksheet=infl)
    # The compounder's price test is its own (premium, expected return, upside/downside): Cheap may fail.
    lane_priced = _compounder(cheap=Verdict(passed=False, note="Margin of safety: 5%; price 1.08× base"))
    assert decision_checks(lane_priced, None, None, worksheet=comp) == []
    weak_comp = _compounder(good_business=Verdict(passed=False, note="x"))
    assert decision_checks(weak_comp, None, None, worksheet=comp) == [
        "The compounder lane needs the Good business test to pass; it is FAIL."]
    # The inflection lane does not ask for Cheap, but does ask for the business and management.
    assert decision_checks(_inflection(cheap=Verdict(passed=False, note="x")), None, None, worksheet=infl) == []
    weak = _inflection(good_management=Verdict(passed=False, note="dilutes"))
    assert decision_checks(weak, None, None, worksheet=infl) == [
        "The inflection lane needs the Good management test to pass; it is FAIL."]
    held = _compounder(rating=PortfolioRating.HOLD, red_flags=["x"], cheap=Verdict(passed=False, note="x"))
    assert decision_checks(held, None, 130.0, worksheet=comp) == []        # a Hold is not a lane buy


@pytest.mark.unit
def test_the_value_lane_renders_without_evidence():
    d = _decision(lane="value", lane_evidence=["margin of safety 32%"])
    text = render_pm_decision(d)
    assert "**Lane**: value\n" in text and "margin of safety 32%" not in text
    assert parse.lane(text) == "value" and parse.lane_evidence(text) == []


@pytest.mark.unit
def test_a_free_text_lane_line_parses():
    text = ("**Rating**: Buy\n\n**Checklist**:\nGood business — PASS: ROIC 30%\n\n"
            "**Lane**: Compounder lane — ROIC ≥ 20% 5/5 years; sustainable growth 14%\n\n**Red Flags**: none")
    assert parse.lane(text) == "compounder"
    assert parse.lane_evidence(text) == ["ROIC ≥ 20% 5/5 years", "sustainable growth 14%"]
    assert parse.lane("**Lane**: inflection lane: verified: backlog") == "inflection"
    assert parse.lane_evidence("**Lane**: inflection lane: verified: backlog") == ["verified: backlog"]


# --- consensus ------------------------------------------------------------------

CONSENSUS_SECTION = (
    "## Consensus (Yahoo estimates)\n\n"
    "- Consensus revenue growth next year 6.2%\n- Consensus revenue growth following year -1.5%\n"
    "- Consensus EPS growth next year n/a\n- Consensus analysts 12\n- Consensus price target median 60.00\n\n"
    "Consensus sees 6.2% revenue growth next year against 3.0% implied by the price.")
EMPTY_CONSENSUS = ("## Consensus (Yahoo estimates)\n\n- Consensus revenue growth next year n/a\n"
                   "- Consensus revenue growth following year n/a\n- Consensus EPS growth next year n/a\n"
                   "- Consensus analysts n/a\n- Consensus price target median n/a")


@pytest.mark.unit
def test_worksheet_consensus_reads_the_five_labels():
    from tradingagents.agents.decision_math import consensus_excerpt, worksheet_consensus

    got = worksheet_consensus(CONSENSUS_SECTION)
    assert got == {"revenue_growth_next": pytest.approx(0.062), "revenue_growth_following": pytest.approx(-0.015),
                   "eps_growth_next": None, "analysts": 12, "target_median": 60.0}
    assert isinstance(got["analysts"], int)
    assert set(worksheet_consensus(EMPTY_CONSENSUS).values()) == {None}
    report = "prose\n## Record and inflection\n- Revenue CAGR 5.0%\n\n" + CONSENSUS_SECTION + "\n## Next\nx"
    assert consensus_excerpt(report) == CONSENSUS_SECTION
    assert consensus_excerpt("no section") == ""


@pytest.mark.unit
def test_decision_checks_ask_for_a_consensus_view_only_when_the_worksheet_has_estimates():
    problem = ("The worksheet shows consensus estimates; say where your thesis differs from them and why "
               "(Consensus field)")
    assert problem in decision_checks(_good_decision(), 80.33, 50.0, CONSENSUS_SECTION)
    assert decision_checks(_decision(consensus_view="We sit below the 60 median: margins revert."),
                           80.33, 50.0, CONSENSUS_SECTION) == []
    assert decision_checks(_good_decision(), 80.33, 50.0, EMPTY_CONSENSUS) == []
    assert decision_checks(_good_decision(), 80.33, 50.0, "") == []


@pytest.mark.unit
def test_consensus_view_renders_after_the_premortem_and_parses_back():
    text = render_pm_decision(_decision(consensus_view="Below consensus: we doubt the 6% growth."))
    assert text.endswith("**Pre-mortem Answer**: Margins revert, but the bear case covers it.\n\n"
                         "**Consensus**: Below consensus: we doubt the 6% growth.")
    assert parse.consensus_view(text) == "Below consensus: we doubt the 6% growth."
    blank = render_pm_decision(_good_decision())
    assert blank.endswith("**Consensus**: not provided") and parse.consensus_view(blank) == ""
    assert parse.consensus_view(blank + "\n\n**Unresolved Checks**:\n- x") == ""
    assert parse.consensus_view("**Rating**: Hold") == ""
    # The first five sections are unchanged.
    assert blank.startswith("**Rating**: Overweight\n\n**Executive Summary**: Buy below 46.\n\n"
                            "**Investment Thesis**: Durable franchise. Margin of safety: 18%\n\n"
                            "**Price Target**: 56.0\n\n**Time Horizon**: 1-3 years\n\n**Checklist**:")
