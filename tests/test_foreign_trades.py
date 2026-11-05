"""Positions and trades in the quote currency (pence, euro, ...) with dollars as the base."""

import csv

import pytest

from value_pipeline import benchmark, market_data, operations as ops, portfolio as pfmod, review
from value_pipeline.config import Paths
from value_pipeline.operations import portfolio_view, realised_gains, trade_fx
from value_pipeline.portfolio import (
    Portfolio,
    Position,
    TradeError,
    apply_buy,
    apply_sell,
    read_trades,
    row_fx,
)


@pytest.mark.unit
def test_pence_buy_debits_dollars_and_keeps_pence():
    pf = Portfolio(cash=10_000.0)
    held = apply_buy(pf, "DNLM.L", 100, 900.0, fees=2.0, currency="GBp", fx=0.0135)
    assert pf.cash == pytest.approx(10_000 - 1_217.0)
    assert held.currency == "GBp" and held.avg_fx == pytest.approx(0.0135)
    assert held.avg_price == pytest.approx(900 + 2.0 / 0.0135 / 100)
    assert held.cost_usd == pytest.approx(1_217.0)


@pytest.mark.unit
def test_add_reweights_price_and_rate_so_cost_is_dollars_paid():
    pf = Portfolio(cash=10_000.0)
    apply_buy(pf, "DNLM.L", 100, 900.0, fees=2.0, currency="GBp", fx=0.0135)
    held = apply_buy(pf, "DNLM.L", 100, 1_000.0, currency="GBp", fx=0.0130)
    assert held.shares == 200
    assert held.cost_usd == pytest.approx(1_217.0 + 1_300.0)


@pytest.mark.unit
def test_sale_returns_the_dollar_gain():
    pf = Portfolio(cash=10_000.0)
    apply_buy(pf, "DNLM.L", 100, 900.0, fees=2.0, currency="GBp", fx=0.0135)
    apply_buy(pf, "DNLM.L", 100, 1_000.0, currency="GBp", fx=0.0130)
    gain = apply_sell(pf, "DNLM.L", 200, 1_100.0, fees=1.0, fx=0.014)
    assert gain == pytest.approx(3_079.0 - 2_517.0)
    assert pf.cash == pytest.approx(10_000 - 2_517.0 + 3_079.0)
    assert pf.position("DNLM.L") is None


@pytest.mark.unit
def test_dollar_trades_are_unchanged():
    pf = Portfolio(cash=1_000.0)
    held = apply_buy(pf, "HRB", 10, 45.0, fees=1.0)
    assert held.currency is None and held.avg_fx == 1.0 and held.avg_price == pytest.approx(45.1)
    assert apply_sell(pf, "HRB", 10, 50.0) == pytest.approx(49.0)


@pytest.mark.unit
def test_nonpositive_fx_is_refused():
    with pytest.raises(TradeError):
        apply_buy(Portfolio(cash=1_000.0), "X", 1, 1.0, fx=0.0)


@pytest.mark.unit
def test_old_portfolio_json_loads():
    pf = Portfolio.from_dict({"cash": 5.0, "positions": [
        {"ticker": "HRB", "shares": 2, "avg_price": 45.0, "first_bought": "2026-10-01"}]})
    held = pf.position("HRB")
    assert held.currency is None and held.avg_fx == 1.0 and held.cost_usd == pytest.approx(90.0)


@pytest.mark.unit
def test_trade_fx_reads_the_market_and_refuses_an_unknown_rate():
    assert trade_fx("DNLM.L", None, fx_fn=lambda t: ("GBp", 0.0135)) == ("GBp", 0.0135)
    assert trade_fx("DNLM.L", 0.014, fx_fn=lambda t: ("GBp", 0.0135)) == ("GBp", 0.014)
    assert trade_fx("HRB", None, fx_fn=lambda t: ("USD", 1.0)) == ("USD", 1.0)
    assert trade_fx("HRB", None, fx_fn=lambda t: (None, 1.0)) == (None, 1.0)
    with pytest.raises(TradeError, match="--fx"):
        trade_fx("XYZ.WA", None, fx_fn=lambda t: ("PLN", None))
    assert trade_fx("XYZ.WA", 0.25, fx_fn=lambda t: ("PLN", None)) == ("PLN", 0.25)
    held = Position("DNLM.L", 1, 900.0, "2026-11-05", currency="GBp")
    assert trade_fx("DNLM.L", 0.014, position=held, fx_fn=lambda t: (None, 1.0)) == ("GBp", 0.014)


@pytest.mark.unit
def test_record_buy_writes_currency_and_fx_and_upgrades_an_old_log(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 10_000.0, on="2026-10-01")
    paths.trades.write_text(
        "date,ticker,action,shares,price,fees,note\n2026-10-02,HRB,BUY,10,45.59,0,first\n",
        encoding="utf-8")
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("GBp", 0.0135))
    monkeypatch.setattr(review, "latest_result", lambda *a, **k: None)
    ops.record_buy(paths, "DNLM.L", 100, 900.0, fx=None, on="2026-11-05")
    rows = read_trades(paths)
    assert rows[0]["ticker"] == "HRB" and row_fx(rows[0]) == 1.0 and rows[0]["price"] == "45.59"
    assert rows[1]["currency"] == "GBp" and row_fx(rows[1]) == pytest.approx(0.0135)
    with paths.trades.open(newline="", encoding="utf-8") as f:
        assert csv.DictReader(f).fieldnames == pfmod.TRADE_FIELDS
    assert not list(tmp_path.glob("*.tmp"))


def _foreign_paths(tmp_path):
    paths = Paths(tmp_path)
    pfmod.save(paths, Portfolio(cash=1_000.0, positions=[
        Position("DNLM.L", 100, 900.0, "2026-11-05", currency="GBp", avg_fx=0.0135)]))
    return paths


@pytest.mark.unit
def test_portfolio_view_values_pence_in_dollars_and_falls_back_to_cost(tmp_path):
    paths = _foreign_paths(tmp_path)
    view = portfolio_view(paths, price_fn=lambda t: 1_000.0, benchmark_fn=lambda *a: None,
                          fx_fn=lambda t: ("GBp", 0.014))
    row = view["positions"][0]
    assert row["value"] == pytest.approx(1_400.0) and row["cost"] == pytest.approx(1_215.0)
    assert row["gain"] == pytest.approx(1_400 / 1_215 - 1)
    assert row["price"] == 1_000.0 and row["currency"] == "GBp" and row["fx"] == 0.014
    assert view["total"] == pytest.approx(2_400.0)
    view = portfolio_view(paths, price_fn=lambda t: 1_000.0, benchmark_fn=lambda *a: None,
                          fx_fn=lambda t: ("GBp", None))
    assert view["positions"][0]["value"] == pytest.approx(1_215.0)
    view = portfolio_view(paths, price_fn=lambda t: None, benchmark_fn=lambda *a: None,
                          fx_fn=lambda t: ("GBp", 0.014))
    assert view["positions"][0]["gain"] is None
    assert view["positions"][0]["value"] == pytest.approx(1_215.0)


@pytest.mark.unit
def test_benchmark_and_realised_gains_use_the_trade_rate():
    trades = [{"date": "2026-11-05", "ticker": "DNLM.L", "action": "BUY", "shares": "100", "price": "900",
               "fees": "2", "currency": "GBp", "fx": "0.0135"},
              {"date": "2026-11-20", "ticker": "DNLM.L", "action": "SELL", "shares": "100", "price": "1000",
               "fees": "0", "currency": "GBp", "fx": "0.014"}]
    events = benchmark.cash_flows([], trades)
    assert events[0] == ("2026-11-05", "buy", pytest.approx(1_217.0))
    assert events[1] == ("2026-11-20", "sell", pytest.approx(1_400.0))
    assert realised_gains(trades) == [None, pytest.approx(183.0)]
