import copy

import pytest

from value_pipeline.actions import Decision, plan
from value_pipeline.config import DEFAULTS, validate_config
from value_pipeline.render import actions_markdown

pytestmark = pytest.mark.unit

BASE = {**DEFAULTS["sizing"], "max_position_weight": 0.15, "min_holdings": 10}   # target 10%
CAPPED = {**BASE, "max_stock_share": 0.2}
LIMIT = "stocks at the 20% cap of the account"


def _held(rating="Hold"):
    return Decision("HELD", rating, 50.0, held_shares=30.0, margin_of_safety=0.1)   # 1,500


def _buy(ticker, mos=0.4):
    return Decision(ticker, "Buy", 100.0, margin_of_safety=mos)


def test_the_cap_is_off_by_default():
    assert DEFAULTS["sizing"]["max_stock_share"] == 1.0
    actions, s = plan([_held(), _buy("AAA"), _buy("BBB", 0.3)], 8_500.0, BASE)
    assert [a.amount for a in actions if a.action == "BUY"] == [1_000.0, 1_000.0]
    assert all("cap of the account" not in a.reason for a in actions)
    assert s["stock_share"] == pytest.approx(0.15)


def test_a_buy_is_reduced_to_the_room_under_the_cap():
    actions, s = plan([_held(), _buy("AAA")], 8_500.0, CAPPED)
    buy = actions[1]
    assert buy.action == "BUY" and buy.amount == 500.0
    assert buy.reason.endswith(f"reduced, {LIMIT}")
    assert s["stock_share"] == pytest.approx(0.15)       # the share before the new buys


def test_the_second_buy_is_skipped_at_the_cap():
    actions, _ = plan([_held(), _buy("AAA"), _buy("BBB", 0.3)], 8_500.0, CAPPED)
    first, second = actions[1], actions[2]
    assert first.amount == 500.0
    assert second.action == "SKIP" and second.reason.endswith(f"; {LIMIT}")


def test_an_add_to_a_buy_rated_holding_is_capped():
    held = Decision("HELD", "Buy", 50.0, held_shares=10.0, margin_of_safety=0.1)     # 500 held
    others = [("OTH", 1_000.0, "Tech")]
    free = plan([held], 8_500.0, BASE, others=others)[0][0]
    assert free.action == "ADD" and free.amount == pytest.approx(500.0)
    add = plan([held], 8_500.0, {**BASE, "max_stock_share": 0.17}, others=others)[0][0]
    assert add.action == "ADD" and add.amount == pytest.approx(200.0)
    assert add.reason.count("reduced, stocks at the 17% cap of the account") == 1


def test_others_count_toward_the_stock_value_and_the_fund_does_not():
    others = [("OTH", 1_500.0, "Tech")]
    actions, s = plan([_buy("AAA")], 8_500.0, CAPPED, others=others)
    assert actions[0].amount == 500.0 and s["stock_share"] == pytest.approx(0.15)
    actions, s = plan([_buy("AAA")], 3_500.0, CAPPED, parked=5_000.0, others=[("OTH", 1_500.0, "Tech")])
    assert actions[0].amount == 500.0
    actions, s = plan([_buy("AAA")], 5_000.0, CAPPED, parked=5_000.0)
    assert actions[0].amount == 1_000.0 and s["stock_share"] == 0.0


def test_stock_share_is_zero_without_a_total():
    assert plan([], 0.0, CAPPED)[1]["stock_share"] == 0.0


def test_validate_config_bounds_the_share():
    cfg = copy.deepcopy(DEFAULTS)
    for bad in (0, 1.5, -0.1):
        cfg["sizing"]["max_stock_share"] = bad
        with pytest.raises(ValueError, match="max_stock_share"):
            validate_config(cfg)
    for good in (0.2, 1, 1.0):
        cfg["sizing"]["max_stock_share"] = good
        validate_config(cfg)


def test_the_actions_line_shows_only_below_one():
    def md(sizing):
        _, s = plan([_held()], 8_500.0, sizing)
        return actions_markdown("2026-11-07", [], s, reports={}, comparison=None, cost=0.0,
                                failures=[], dry_run=False)
    assert "Stocks: 15% of the account (cap 20%)" in md(CAPPED)
    assert "Stocks:" not in md(BASE)
