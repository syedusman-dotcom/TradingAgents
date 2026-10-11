"""Recording trades, undo and the portfolio view, shared by the CLI and the website."""

from datetime import date

import pandas as pd
import pytest

from value_pipeline import history, operations as ops, portfolio as pfmod, review
from value_pipeline.config import DEFAULTS, Paths


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path)
    ops.init_portfolio(p, 1000.0, on="2026-10-01")
    return p


@pytest.mark.unit
def test_tickers_are_normalised_and_recorded_with_the_trade(paths):
    ops.record_buy(paths, " hrb ", 2, 44.0, fees=1.0, on="2026-10-02", note="first")
    pf = pfmod.load(paths)
    assert [p.ticker for p in pf.positions] == ["HRB"]
    trade = pfmod.read_trades(paths)[0]
    assert (trade["ticker"], trade["action"], trade["note"]) == ("HRB", "BUY", "first")
    ops.record_buy(paths, "HRB", 1, 40.0, on="2026-10-03")
    assert len(pfmod.load(paths).positions) == 1


@pytest.mark.unit
def test_init_refuses_to_overwrite_without_force(paths):
    with pytest.raises(ops.PortfolioExists):
        ops.init_portfolio(paths, 50.0)
    assert ops.init_portfolio(paths, 50.0, force=True).cash == 50.0


@pytest.mark.unit
def test_undo_restores_the_state_before_the_last_entry(paths):
    ops.record_buy(paths, "HRB", 2, 44.0, on="2026-10-02")
    ops.record_sell(paths, "HRB", 1, 50.0, on="2026-10-03")
    assert ops.undo_info(paths)["description"].startswith("SELL 1 HRB")
    ops.undo_last(paths)
    pf = pfmod.load(paths)
    assert pf.position("HRB").shares == 2 and pf.cash == pytest.approx(912.0)
    assert [t["action"] for t in pfmod.read_trades(paths)] == ["BUY"]
    assert ops.undo_info(paths) is None           # one level only
    with pytest.raises(ops.UndoUnavailable):
        ops.undo_last(paths)


@pytest.mark.unit
def test_undo_of_the_first_trade_removes_the_trade_log(paths):
    ops.record_buy(paths, "HRB", 2, 44.0)
    ops.undo_last(paths)
    assert pfmod.read_trades(paths) == [] and pfmod.load(paths).cash == 1000.0


@pytest.mark.unit
def test_undo_refuses_after_something_else_changed_the_files(paths):
    ops.record_buy(paths, "HRB", 2, 44.0)
    pf = pfmod.load(paths)
    pf.position("HRB").last_rating = "Hold"        # e.g. a review stored a rating
    pfmod.save(paths, pf)
    with pytest.raises(ops.UndoUnavailable, match="changed"):
        ops.undo_last(paths)
    assert pfmod.load(paths).position("HRB").last_rating == "Hold"


@pytest.mark.unit
def test_portfolio_view_values_positions_with_given_prices(paths):
    ops.record_buy(paths, "HRB", 2, 40.0, on="2026-10-02")
    pf = pfmod.load(paths)
    pf.position("HRB").intrinsic_value_estimate = 55.0
    pfmod.save(paths, pf)
    view = ops.portfolio_view(paths, price_fn={"HRB": 44.0}.get, benchmark_fn=lambda *a: None)
    pos = view["positions"][0]
    assert view["total"] == pytest.approx(920 + 88)
    assert pos["value"] == pytest.approx(88) and pos["gain"] == pytest.approx(0.10)
    assert pos["weight"] == pytest.approx(88 / 1008)
    assert pos["margin_of_safety"] == pytest.approx((55 - 44) / 55)


@pytest.mark.unit
def test_realised_gains_follow_average_cost_with_fees():
    trades = [
        {"date": "2026-01-02", "ticker": "A", "action": "BUY", "shares": "2", "price": "10", "fees": "2"},
        {"date": "2026-01-03", "ticker": "A", "action": "BUY", "shares": "2", "price": "20", "fees": "0"},
        {"date": "2026-01-04", "ticker": "A", "action": "SELL", "shares": "1", "price": "30", "fees": "1"},
    ]
    gains = ops.realised_gains(trades)
    assert gains[:2] == [None, None]
    assert gains[2] == pytest.approx(30 - 1 - (62 / 4))


@pytest.mark.unit
def test_review_preview_counts_runs_and_applies_the_cap(paths):
    ops.record_buy(paths, "HRB", 1, 44.0)
    cfg = {**DEFAULTS, "review": {**DEFAULTS["review"], "cost_per_run": 2.0, "max_cost_per_review": 5.0}}
    p = review.preview(paths, cfg, only=["hog", "ingr"])
    assert (p["tickers"], p["n_held"], p["n_new"], p["estimate"], p["blocked"]) == (["HOG", "INGR"], 0, 2, 4.0, False)
    p = review.preview(paths, cfg, only=["HRB", "A", "B"])
    assert p["blocked"] and p["estimate"] == 6.0
    p = review.preview(paths, cfg)          # no screen yet: holdings only
    assert p["tickers"] == ["HRB"]


@pytest.mark.unit
def test_value_series_replays_cash_shares_and_the_spy_shadow():
    deposits = [{"date": "2026-01-02", "amount": 1000}]
    trades = [{"date": "2026-01-05", "ticker": "A", "action": "BUY", "shares": "10", "price": "50", "fees": "0"}]
    idx = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 9)]
    closes = {"A": pd.Series([50.0, 50.0, 60.0], index=idx)}
    spy = pd.Series([100.0, 100.0, 110.0], index=idx)
    s = history.value_series(deposits, trades, closes, spy, idx)
    assert [round(x["portfolio"], 2) for x in s] == [1000.0, 1000.0, 1100.0]
    assert [round(x["spy"], 2) for x in s] == [1000.0, 1000.0, 1050.0]   # 500 of SPY at 100 -> 110
    assert s[0]["date"] == "2026-01-02"


@pytest.mark.unit
def test_review_ratings_do_not_wipe_a_trade_recorded_while_it_ran(paths):
    """A review loads the portfolio at the start and stores ratings an hour later."""
    from value_pipeline.actions import Action, Decision

    ops.record_buy(paths, "HRB", 1, 44.0)
    stale = pfmod.load(paths)                         # what the review loaded
    ops.record_buy(paths, "ADBE", 0.5, 240.0)         # recorded on the website meanwhile
    d = Decision("HRB", "Hold", price=44.0, held_shares=1, intrinsic_value=55.0)
    review._record_ratings(paths, stale, [Action("HRB", True, "Hold", "KEEP", "", decision=d)], "2026-10-11")
    pf = pfmod.load(paths)
    assert pf.position("ADBE") is not None and pf.cash == pytest.approx(1000 - 44 - 120)
    assert pf.position("HRB").last_rating == "Hold" and pf.position("HRB").intrinsic_value_estimate == 55.0


@pytest.mark.unit
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_trades_refuse_non_finite_numbers(paths, bad):
    before = pfmod.load(paths).to_dict()
    for trade in (lambda: ops.record_buy(paths, "HRB", bad, 44.0), lambda: ops.record_buy(paths, "HRB", 1, 44.0, fees=bad),
                  lambda: ops.record_deposit(paths, bad)):
        with pytest.raises(pfmod.TradeError):
            trade()
    assert pfmod.load(paths).to_dict() == before


@pytest.mark.unit
def test_undo_refuses_when_its_backup_is_gone(paths):
    import shutil

    ops.record_buy(paths, "HRB", 2, 44.0)
    ops.record_buy(paths, "HRB", 1, 44.0)
    shutil.rmtree(paths.backups)
    with pytest.raises(ops.UndoUnavailable, match="backup"):
        ops.undo_last(paths)
    assert paths.portfolio.exists() and len(pfmod.read_trades(paths)) == 2


@pytest.mark.unit
def test_config_with_a_wrong_shape_is_rejected(tmp_path):
    from value_pipeline.config import load_config, validate_config

    for text in ("review: 5\n", "sizing:\n  max_position_weight: [1]\n", "screen:\n  top_n: ten\n"):
        f = tmp_path / "c.yaml"
        f.write_text(text)
        with pytest.raises((ValueError, TypeError)):
            validate_config(load_config(f))
    f.write_text("review:\n  cost_per_run: 3\n")
    validate_config(load_config(f))                    # an int where a float is expected is fine
