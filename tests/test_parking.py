import copy
import json

import pytest

from value_pipeline import (
    cli,
    daily,
    market_data,
    metrics,
    operations as ops,
    portfolio as pfmod,
    review,
    watch,
)
from value_pipeline.actions import Decision, parking_move, plan
from value_pipeline.config import DEFAULTS, Paths, parking_ticker
from value_pipeline.render import actions_markdown

pytestmark = pytest.mark.unit

PARK = {"ticker": "SXR8.DE", "share": 0.7, "min_trade_amount": 250}
FUND = "SXR8.DE"


def test_parking_move_rules():
    assert parking_move(9_564.07, 0.0, 0.0, PARK) == {"action": "PARK", "amount": pytest.approx(6_694.85, abs=0.01)}
    assert parking_move(3_000.0, 0.0, 6_900.0, PARK) is None                 # target 6,930: 30 short, under 250
    assert parking_move(500.0, 550.0, 5_000.0, PARK) == {"action": "UNPARK", "amount": pytest.approx(50.0)}
    assert parking_move(1_000.0, 1_000.0, 0.0, PARK) is None                 # nothing idle
    assert parking_move(2_000.0, 0.0, 9_000.0, PARK) is None                 # above its share: never sold for that
    assert parking_move(400.0, 300.0, 0.0, {**PARK, "share": 1.0, "min_trade_amount": 50}) \
        == {"action": "PARK", "amount": pytest.approx(100.0)}                # capped at liquid - buys


def test_plan_without_parking_is_unchanged():
    held = Decision("HRB", "Hold", 54.81, held_shares=11.0)
    buy = Decision("AAA", "Buy", 100.0, margin_of_safety=0.4)
    a1, s1 = plan([held, buy], 9_564.07, DEFAULTS["sizing"])
    a2, s2 = plan([held, buy], 9_564.07, DEFAULTS["sizing"], parked=0.0, parking=None)
    assert [(a.ticker, a.action, a.amount, a.shares) for a in a1] == [(a.ticker, a.action, a.amount, a.shares) for a in a2]
    assert s2["parking"] is None and s2["parked"] == 0.0
    assert {k: s1[k] for k in ("total_value", "cash", "target_weight", "cash_after")} \
        == {k: s2[k] for k in ("total_value", "cash", "target_weight", "cash_after")}
    assert s1["cash_after"] == pytest.approx(9_564.07 - sum(a.amount or 0 for a in a1 if a.action == "BUY"))


def test_plan_parks_idle_money_after_the_buys():
    held = Decision("HRB", "Hold", 54.81, held_shares=11.0)
    _, s = plan([held], 9_564.07, DEFAULTS["sizing"], parked=0.0, parking={**PARK, "price_usd": 809.0})
    assert s["parking"]["action"] == "PARK" and s["parking"]["amount"] == pytest.approx(6_694.85, abs=0.01)
    assert s["parking"]["shares"] == pytest.approx(6_694.85 / 809.0, abs=0.001)
    assert s["cash_after"] == pytest.approx(9_564.07 - 6_694.85, abs=0.01)


def test_plan_sizes_buys_against_the_fund_and_unparks_the_shortfall():
    buy = Decision("AAA", "Buy", 100.0, margin_of_safety=0.4)
    actions, s = plan([buy], 500.0, DEFAULTS["sizing"], parked=5_000.0, parking={**PARK, "price_usd": 809.0})
    assert s["total_value"] == pytest.approx(5_500.0)
    assert actions[0].action == "BUY" and actions[0].amount == pytest.approx(550.0)   # 10% of 5,500
    assert s["parking"] == {"action": "UNPARK", "amount": pytest.approx(50.0), "shares": pytest.approx(50 / 809.0, abs=1e-4)}
    assert s["cash_after"] == pytest.approx(0.0)


def test_plan_without_a_fund_price_suggests_no_park():
    _, s = plan([], 5_000.0, DEFAULTS["sizing"], parked=0.0, parking=PARK)
    assert s["parking"] is None and s["cash_after"] == pytest.approx(5_000.0)


def test_plan_without_a_fund_price_still_unparks_for_the_buys():
    buy = Decision("AAA", "Buy", 100.0, margin_of_safety=0.4)
    actions, s = plan([buy], 500.0, DEFAULTS["sizing"], parked=5_000.0, parking=PARK)
    assert actions[0].amount == pytest.approx(550.0)
    assert s["parking"] == {"action": "UNPARK", "amount": pytest.approx(50.0), "shares": None}
    assert s["cash_after"] == pytest.approx(0.0)


def test_parking_ticker_helper():
    assert parking_ticker({"parking": {"ticker": " sxr8.de "}}) == "SXR8.DE"
    assert parking_ticker({"parking": {"ticker": ""}}) is None and parking_ticker({}) is None
    assert parking_ticker(DEFAULTS) == "SXR8.DE"


def _md(summary, fund, **kw):
    return actions_markdown("2026-11-05", [], summary, reports={}, comparison=None, cost=0.0, failures=[],
                            dry_run=False, fund=fund, **kw)


def test_actions_shows_the_index_fund_section():
    _, park = plan([], 9_564.07, DEFAULTS["sizing"], parked=0.0, parking={**PARK, "price_usd": 809.0})
    md = _md(park, {"ticker": FUND, "value": 0.0, "priced": True})
    assert "## Index fund" in md
    assert md.index("## New buys") < md.index("## Index fund")
    assert "Buy $6,694.85 of SXR8.DE (about 8.28 shares)" in md
    assert "Idle money sits in the index fund so it moves with the market and can fall; sell it only to fund a buy." in md
    assert "- Index fund" not in md                       # not held: no Portfolio line

    buy = Decision("AAA", "Buy", 100.0, margin_of_safety=0.4)
    _, unpark = plan([buy], 500.0, DEFAULTS["sizing"], parked=5_000.0, parking={**PARK, "price_usd": 809.0})
    md = _md(unpark, {"ticker": FUND, "value": 5_000.0, "priced": True})
    assert "Sell $50.00 of SXR8.DE to fund the buys; place it first" in md
    assert "- Index fund (SXR8.DE): $5,000.00 (91% of the portfolio)" in md

    _, none = plan([], 100.0, DEFAULTS["sizing"], parked=0.0, parking=PARK)
    assert "No change." in _md(none, {"ticker": FUND, "value": 0.0, "priced": True})
    assert "No suggestion: no dollar price for SXR8.DE today." in _md(none, {"ticker": FUND, "value": 3.0, "priced": False})
    assert "## Index fund" not in _md(park, None)

    _, unpriced = plan([buy], 500.0, DEFAULTS["sizing"], parked=5_000.0, parking=PARK)
    md = _md(unpriced, {"ticker": FUND, "value": 5_000.0, "priced": False})
    assert "Sell $50.00 of SXR8.DE to fund the buys; place it first" in md
    assert "No dollar price for SXR8.DE today: work out the shares at your broker." in md
    assert "No suggestion" not in md


def _portfolio(tmp_path, fund=True):
    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 20_000.0, on="2026-09-01")
    pf = pfmod.load(paths)
    hrb = pfmod.apply_buy(pf, "HRB", 10, 50.0, on="2026-09-02", intrinsic_value=55.0)
    hrb.last_review_date = "2026-10-01"
    if fund:
        pfmod.apply_buy(pf, FUND, 8, 700.0, on="2026-09-03", currency="EUR", fx=1.12)
    pfmod.save(paths, pf)
    return paths


def _quotes(monkeypatch, fund_price=800.0):
    prices = {"HRB": 50.0, FUND: fund_price}
    monkeypatch.setattr(market_data, "latest_price", lambda t: prices.get(t))
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("EUR", 1.10) if t == FUND else ("USD", 1.0))
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: __import__("pandas").Series(dtype=float))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    return prices


def test_review_never_runs_agents_on_the_fund(tmp_path, monkeypatch):
    paths = _portfolio(tmp_path)
    _quotes(monkeypatch)
    out = review.run(paths, copy.deepcopy(DEFAULTS), yes=True, dry_run=True, today="2026-11-05")
    rows = json.loads((out.parent / "results.json").read_text())
    assert [r["ticker"] for r in rows] == ["HRB"]
    md = out.read_text()
    fund_value = 8 * 800.0 * 1.10
    assert "## Index fund" in md and "SXR8.DE" in md
    assert f"- Index fund (SXR8.DE): ${fund_value:,.2f}" in md
    assert f"- Value: **${20_000 - 10 * 50 - 8 * 700 * 1.12 + fund_value + 10 * 50:,.2f}**" in md
    assert "| SXR8.DE" not in md                              # not a row of Holdings


def test_review_without_a_dollar_price_values_the_fund_at_cost(tmp_path, monkeypatch):
    paths = _portfolio(tmp_path)
    _quotes(monkeypatch, fund_price=None)
    md = review.run(paths, copy.deepcopy(DEFAULTS), yes=True, dry_run=True, today="2026-11-05").read_text()
    assert "No suggestion: no dollar price for SXR8.DE today." in md
    assert f"- Index fund (SXR8.DE): ${8 * 700 * 1.12:,.2f}" in md


def test_review_with_parking_off_has_no_fund_section(tmp_path, monkeypatch):
    paths = _portfolio(tmp_path, fund=False)
    _quotes(monkeypatch)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["parking"]["ticker"] = ""
    md = review.run(paths, cfg, yes=True, dry_run=True, today="2026-11-05").read_text()
    assert "## Index fund" not in md


def test_review_refuses_the_fund_by_name(tmp_path, monkeypatch):
    paths = _portfolio(tmp_path)
    _quotes(monkeypatch)
    with pytest.raises(SystemExit, match="index fund"):
        review.run(paths, copy.deepcopy(DEFAULTS), yes=True, dry_run=True, only=["hrb", "sxr8.de"])
    with pytest.raises(SystemExit, match="index fund"):
        cli.main(["--data-dir", str(tmp_path), "review", "--dry-run", "--yes", "--tickers", FUND])


def test_preview_refuses_a_list_naming_the_fund(tmp_path, monkeypatch):
    paths = _portfolio(tmp_path)
    with pytest.raises(SystemExit, match="index fund"):
        review.preview(paths, copy.deepcopy(DEFAULTS), only=["sxr8.de"])
    plan_ = review.preview(paths, copy.deepcopy(DEFAULTS), only=["HRB"])
    assert plan_["tickers"] == ["HRB"]
    cfg = copy.deepcopy(DEFAULTS)
    cfg["parking"]["ticker"] = ""
    assert review.preview(paths, cfg, only=[FUND])["tickers"] == [FUND]


def test_daily_and_watch_skip_the_fund(tmp_path, monkeypatch):
    paths = _portfolio(tmp_path)
    pf = pfmod.load(paths)
    fund = pf.position(FUND)
    fund.intrinsic_value_estimate, fund.entry_price = 100.0, 900.0   # would fire REACHED VALUE and ENTRY PRICE
    fund.kill_criteria = ["Fund closes"]
    pfmod.save(paths, pf)
    monkeypatch.setattr(market_data, "latest_price", lambda t: 800.0)
    monkeypatch.setattr(market_data, "closes", lambda t: [])
    monkeypatch.setattr(metrics, "trend", lambda closes: {"trend": "FREEFALL"})
    cfg = copy.deepcopy(DEFAULTS)
    fired, report, _ = daily.run(paths, cfg, dry_run=True, notify=False)
    assert FUND not in report.read_text() and all(t.ticker != FUND for t in fired)
    assert any(t.ticker == "HRB" for t in fired)
    alerts, out = watch.run(paths, cfg, notify=False)
    assert FUND not in out.read_text() and all(FUND not in a.message for a in alerts)
    cfg["parking"]["ticker"] = ""
    _, report, _ = daily.run(paths, cfg, dry_run=True, notify=False)
    assert FUND in report.read_text()                           # off: a holding like any other


def test_portfolio_view_labels_the_fund(tmp_path):
    paths = _portfolio(tmp_path)
    pf = pfmod.load(paths)
    pf.position(FUND).intrinsic_value_estimate = 900.0
    pfmod.save(paths, pf)
    kw = {"price_fn": lambda t: 800.0, "benchmark_fn": lambda *a: None,
          "fx_fn": lambda t: ("EUR", 1.10) if t == FUND else ("USD", 1.0)}
    rows = {r["ticker"]: r for r in ops.portfolio_view(paths, parking_ticker=FUND, **kw)["positions"]}
    assert rows[FUND]["last_rating"] == "index fund"
    assert rows[FUND]["margin_of_safety"] is None and rows[FUND]["intrinsic_value"] is None
    assert rows[FUND]["value"] == pytest.approx(8 * 800.0 * 1.10)
    assert rows["HRB"]["last_rating"] != "index fund"
    plain = {r["ticker"]: r for r in ops.portfolio_view(paths, **kw)["positions"]}
    assert plain[FUND]["margin_of_safety"] is not None
