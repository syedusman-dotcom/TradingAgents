"""Paper Buy rules (pure; no network)."""

import pytest

from value_pipeline import paper

pytestmark = pytest.mark.unit


def _row(mos=0.05, rating="Hold", business=True, management=True, investing=True, **over):
    checklist = {"good_business": {"passed": business, "note": ""},
                 "good_management": {"passed": management, "note": ""}}
    if investing is not None:
        checklist["still_investing"] = {"passed": investing, "note": ""}
    return {"ticker": "AAA", "rating": rating, "error": None, "margin_of_safety": mos,
            "checklist": checklist, **over}


def test_fair_price_at_exactly_zero():
    assert paper.flags(_row(mos=0.0)) == ["fair_price", "fair_price_investing"]
    assert paper.flags(_row(mos=-0.001)) == []


def test_discount_at_exactly_fifteen_percent():
    assert paper.flags(_row(mos=0.15)) == ["fair_price", "fair_price_investing", "discount_15"]
    assert "discount_15" not in paper.flags(_row(mos=0.1499))


def test_still_investing_failed_or_missing_keeps_fair_price():
    assert paper.flags(_row(investing=False)) == ["fair_price"]
    assert paper.flags(_row(investing=None, mos=0.2)) == ["fair_price", "discount_15"]


@pytest.mark.parametrize("row", [
    _row(business=False), _row(management=False), _row(rating="Underweight"), _row(rating="Sell"),
    _row(error="boom"), _row(mos=None), _row(mos=True), _row(mos=float("nan")),
    {"ticker": "AAA", "rating": "Hold", "margin_of_safety": 0.2},                 # no checklist
    {"ticker": "AAA", "rating": "Hold", "margin_of_safety": 0.2, "checklist": "x"},
    _row(business="true"),                                                        # a string is not True
    {"ticker": "AAA", "rating": "Hold", "margin_of_safety": 0.2,
     "checklist": {"good_business": True, "good_management": {"passed": True}}},  # not a dict
])
def test_rows_that_flag_nothing(row):
    assert paper.flags(row) == []


def test_buy_and_overweight_pass_the_gate():
    assert paper.flags(_row(rating="Buy"))[0] == "fair_price"
    assert paper.flags(_row(rating="Overweight"))[0] == "fair_price"


def test_rules_keys_in_order():
    assert list(paper.RULES) == ["fair_price", "fair_price_investing", "discount_15"]
