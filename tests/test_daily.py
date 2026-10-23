from datetime import date

import pytest

from value_pipeline.config import DEFAULTS
from value_pipeline.daily import KINDS, Trigger, select, triggers

pytestmark = pytest.mark.unit

TODAY = date(2026, 10, 23)
CFG = {"daily": dict(DEFAULTS["daily"])}


def holding(**kw):
    base = {"ticker": "HRB", "intrinsic_value": 55.0, "entry_price": None, "next_earnings_date": None,
            "last_review_date": "2026-10-01", "last_price": None, "last_trend": "UPTREND"}
    return {**base, **kw}


def idea(**kw):
    base = {"ticker": "DNLM.L", "date": "2026-10-01", "price": 800.0, "trend": "UPTREND",
            "entry_price": 750.0, "intrinsic_value": 1000.0, "earnings_date": None}
    return {**base, **kw}


def kinds(ts):
    return [t.kind for t in ts]


def test_entry_price_fires_for_ideas_and_holdings_with_entry():
    ts = triggers([holding(ticker="A", entry_price=40.0)], [idea()],
                  {"A": 39.0, "DNLM.L": 740.0}, {}, TODAY, CFG)
    assert [(t.ticker, t.kind) for t in ts] == [("A", "ENTRY PRICE"), ("DNLM.L", "ENTRY PRICE")]
    assert ts[1].message == "DNLM.L at 740.00 is at or below its entry price 750.00"


def test_entry_price_quiet_when_above_or_missing():
    assert triggers([], [idea()], {"DNLM.L": 760.0}, {}, TODAY, CFG) == []
    assert triggers([], [idea(entry_price=None)], {"DNLM.L": 700.0}, {}, TODAY, CFG) == []
    assert triggers([holding()], [], {"HRB": 30.0}, {}, TODAY, CFG) == []
    assert triggers([], [idea()], {}, {}, TODAY, CFG) == []


def test_reached_value_fires_for_holdings_only():
    ts = triggers([holding()], [idea(intrinsic_value=700.0, entry_price=None)],
                  {"HRB": 56.10, "DNLM.L": 800.0}, {}, TODAY, CFG)
    assert [(t.ticker, t.kind) for t in ts] == [("HRB", "REACHED VALUE")]
    assert ts[0].message == "HRB at 56.10 is above its value 55.00"


def test_reached_value_quiet_below_or_missing():
    assert triggers([holding()], [], {"HRB": 54.0}, {}, TODAY, CFG) == []
    assert triggers([holding(intrinsic_value=None)], [], {"HRB": 99.0}, {}, TODAY, CFG) == []


def test_freefall_fires_only_on_a_new_freefall():
    ts = triggers([holding()], [], {"HRB": 50.0}, {"HRB": "FREEFALL"}, TODAY, CFG)
    assert kinds(ts) == ["FREEFALL"]
    assert triggers([holding(last_trend="FREEFALL")], [], {"HRB": 50.0},
                    {"HRB": "FREEFALL"}, TODAY, CFG) == []
    assert triggers([holding()], [], {"HRB": 50.0}, {"HRB": "DOWNTREND"}, TODAY, CFG) == []
    assert triggers([holding()], [], {"HRB": 50.0}, {}, TODAY, CFG) == []
    assert kinds(triggers([], [idea(trend=None)], {"DNLM.L": 800.0},
                          {"DNLM.L": "FREEFALL"}, TODAY, CFG)) == ["FREEFALL"]


def test_earnings_fires_once_per_report():
    h = holding(next_earnings_date="2026-10-22", last_review_date="2026-10-01")
    ts = triggers([h], [], {"HRB": 50.0}, {}, TODAY, CFG)
    assert kinds(ts) == ["EARNINGS"]
    assert ts[0].message == "HRB reported on 2026-10-22 (last review 2026-10-01)"
    # reviewed since the report: quiet
    assert triggers([holding(next_earnings_date="2026-10-22", last_review_date="2026-10-23")], [],
                    {"HRB": 50.0}, {}, TODAY, CFG) == []
    # reports today or later: not yet
    assert triggers([holding(next_earnings_date="2026-10-23")], [], {"HRB": 50.0}, {}, TODAY, CFG) == []
    assert triggers([holding(next_earnings_date=None)], [], {"HRB": 50.0}, {}, TODAY, CFG) == []
    assert kinds(triggers([], [idea(earnings_date="2026-10-10")], {"DNLM.L": 800.0},
                          {}, TODAY, CFG)) == ["EARNINGS"]


def test_big_move_fires_at_threshold_either_way():
    ts = triggers([holding(last_price=85.28, intrinsic_value=None)], [], {"HRB": 70.0}, {}, TODAY, CFG)
    assert kinds(ts) == ["BIG MOVE"]
    assert ts[0].message == "HRB moved -18% since its review at 85.28"
    assert kinds(triggers([holding(last_price=100.0, intrinsic_value=None)], [], {"HRB": 115.0}, {}, TODAY, CFG)) == ["BIG MOVE"]
    assert triggers([holding(last_price=100.0, intrinsic_value=None)], [], {"HRB": 110.0}, {}, TODAY, CFG) == []
    assert triggers([holding(last_price=None, intrinsic_value=None)], [], {"HRB": 110.0}, {}, TODAY, CFG) == []


def test_cooldown_holds_back_except_value_and_earnings():
    ts = [Trigger("A", "BIG MOVE", ""), Trigger("B", "REACHED VALUE", ""),
          Trigger("C", "EARNINGS", ""), Trigger("D", "ENTRY PRICE", "")]
    last = dict.fromkeys("ABCD", "2026-10-20")
    last["D"] = "2026-10-18"  # 5 days ago: outside the cooldown
    chosen, deferred = select(ts, last, TODAY, CFG)
    assert chosen == ["D", "B", "C"]
    assert deferred == [("A", "cooldown: reviewed on 2026-10-20")]


def test_selection_order_and_cap():
    ts = [Trigger("Z", "ENTRY PRICE", ""), Trigger("Y", "BIG MOVE", ""), Trigger("X", "FREEFALL", ""),
          Trigger("W", "EARNINGS", ""), Trigger("Y", "REACHED VALUE", ""), Trigger("A", "FREEFALL", "")]
    chosen, deferred = select(ts, {}, TODAY, CFG)
    assert chosen == ["Z", "Y", "W"]
    assert deferred == [("A", "over the daily cap of 3"), ("X", "over the daily cap of 3")]
    assert KINDS[0] == "ENTRY PRICE"
