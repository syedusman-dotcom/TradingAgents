"""Positions and trades in the quote currency (pence, euro, ...) with dollars as the base."""

import csv
from io import StringIO

import pytest
from rich.console import Console

from value_pipeline import (
    benchmark,
    cli,
    market_data,
    operations as ops,
    portfolio as pfmod,
    review,
)
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
    with pytest.raises(TradeError, match="--fx"):
        trade_fx("XYZ.WA", None, fx_fn=lambda t: ("PLN", None))
    assert trade_fx("XYZ.WA", 0.25, fx_fn=lambda t: ("PLN", None)) == ("PLN", 0.25)
    held = Position("DNLM.L", 1, 900.0, "2026-11-05", currency="GBp")
    assert trade_fx("DNLM.L", 0.014, position=held, fx_fn=lambda t: (None, 1.0)) == ("GBp", 0.014)


@pytest.mark.unit
def test_trade_fx_refuses_a_foreign_position_when_the_currency_is_unreadable():
    held = Position("DNLM.L", 1, 900.0, "2026-11-05", currency="GBp")
    with pytest.raises(TradeError, match="--fx"):
        trade_fx("DNLM.L", None, position=held, fx_fn=lambda t: (None, 1.0))


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
    view = portfolio_view(paths, price_fn=lambda t: 1_000.0, benchmark_fn=lambda *a: None,
                          fx_fn=lambda t: (None, 1.0))      # unreadable currency: not dollars
    assert view["positions"][0]["value"] == pytest.approx(1_215.0)
    assert view["positions"][0]["fx"] is None
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


@pytest.mark.unit
def test_trade_line_states_both_units():
    assert cli.trade_line("BUY", 84, "DNLM.L", 900.0, "GBp", 0.01348) \
        == "BUY 84 DNLM.L @ 900.00 GBp (fx 0.01348) = $1,019.09"
    assert cli.trade_line("SELL", 10, "HRB", 50.0, None, 1.0) == "SELL 10 HRB @ $50.00 = $500.00"


def _gbp_portfolio(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("GBp", 0.013))
    monkeypatch.setattr(review, "latest_result", lambda *a, **k: None)
    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 10_000.0, on="2026-10-01")
    return paths


@pytest.mark.unit
def test_cli_buy_passes_fx(monkeypatch, tmp_path, capsys):
    paths = _gbp_portfolio(tmp_path, monkeypatch)
    cli.main(["--data-dir", str(tmp_path), "buy", "DNLM.L", "84", "900", "--fx", "0.0135"])
    held = pfmod.load(paths).position("DNLM.L")
    assert held.currency == "GBp" and held.avg_fx == pytest.approx(0.0135)
    assert "BUY 84 DNLM.L @ 900.00 GBp (fx 0.0135) = $1,020.60" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_buy_without_fx_uses_the_market_rate_and_sell_prints_the_line(monkeypatch, tmp_path, capsys):
    paths = _gbp_portfolio(tmp_path, monkeypatch)
    cli.main(["--data-dir", str(tmp_path), "buy", "DNLM.L", "84", "900"])
    assert pfmod.load(paths).position("DNLM.L").avg_fx == pytest.approx(0.013)
    cli.main(["--data-dir", str(tmp_path), "sell", "DNLM.L", "84", "1000", "--fx", "0.014"])
    assert "SELL 84 DNLM.L @ 1,000.00 GBp (fx 0.014) = $1,176.00" in capsys.readouterr().out
    assert pfmod.load(paths).position("DNLM.L") is None


@pytest.mark.unit
def test_cli_buy_without_a_known_rate_stops_naming_fx(monkeypatch, tmp_path):
    paths = _gbp_portfolio(tmp_path, monkeypatch)
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("PLN", None))
    with pytest.raises(SystemExit, match="--fx"):
        cli.main(["--data-dir", str(tmp_path), "buy", "XYZ.WA", "1", "10"])
    assert pfmod.load(paths).position("XYZ.WA") is None


@pytest.mark.unit
def test_status_shows_the_quote_unit(monkeypatch, tmp_path):
    paths = _gbp_portfolio(tmp_path, monkeypatch)
    ops.record_buy(paths, "DNLM.L", 84, 900.0, fx=0.0135)
    monkeypatch.setattr(market_data, "latest_price", lambda t: 900.0)
    monkeypatch.setattr(benchmark, "compare", lambda *a, **k: None)
    monkeypatch.setattr(ops, "portfolio_view", lambda paths, **kw: portfolio_view(
        paths, price_fn=lambda t: 900.0, benchmark_fn=lambda *a: None, fx_fn=lambda t: ("GBp", 0.013), **kw))
    out = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=200))
    cli.main(["--data-dir", str(tmp_path), "status"])
    assert "900.00 GBp" in out.getvalue()


# --- An unreadable currency is not a dollar rate (final review F1, F4, M1) ---

def _dnlm(**kw):
    return Position("DNLM.L", 84, 900.0, "2026-11-05", currency="GBp", avg_fx=0.01348, **kw)


@pytest.mark.unit
def test_position_rate_refuses_the_placeholder_for_a_foreign_position():
    assert pfmod.position_rate(_dnlm(), (None, 1.0)) is None
    assert pfmod.position_rate(_dnlm(), ("GBp", 0.013)) == 0.013
    assert pfmod.position_rate(_dnlm(), ("GBp", None)) is None
    hrb = Position("HRB", 10, 50.0, "2026-10-01")
    assert pfmod.position_rate(hrb, (None, 1.0)) == 1.0            # a dollar position: 1.0 is its rate
    paid_in_kronor = Position("X", 1, 280.0, "2026-10-01", avg_fx=0.102)   # no currency kept, a rate paid
    assert pfmod.position_rate(paid_in_kronor, (None, 1.0)) is None
    assert pfmod.position_rate(None, (None, 1.0)) == 1.0


@pytest.mark.unit
def test_holding_value_of_a_foreign_holding_with_an_unreadable_currency_is_its_cost():
    assert review._holding_value(_dnlm(), 910.0, (None, 1.0)) == pytest.approx(84 * 900.0 * 0.01348)
    assert review._holding_value(_dnlm(), 910.0, ("GBp", 0.013)) == pytest.approx(84 * 910.0 * 0.013)


@pytest.mark.unit
def test_parking_with_an_unreadable_currency_is_unpriced():
    fund = Position("0P0001K6NL.ST", 24, 280.0, "2026-10-01", currency="SEK", avg_fx=0.102)
    cfg = {"ticker": "0P0001K6NL.ST", "share": 0.7, "min_trade_amount": 250}
    value, settings, info = review._parking(fund, "0P0001K6NL.ST", 285.0, (None, 1.0), cfg)
    assert value == pytest.approx(24 * 280.0 * 0.102) and "price_usd" not in settings
    assert info["priced"] is False
    value, settings, info = review._parking(None, "0P0001K6NL.ST", 285.0, (None, 1.0), cfg)
    assert value == 0.0 and "price_usd" not in settings and info["priced"] is False   # not held yet
    _, settings, info = review._parking(None, "0P0001K6NL.ST", 285.0, ("SEK", 0.105), cfg)
    assert settings["price_usd"] == pytest.approx(285.0 * 0.105) and info["priced"] is True


def _review_paths(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 10_000.0, on="2026-09-01")
    pf = pfmod.load(paths)
    apply_buy(pf, "HRB", 10, 50.0, on="2026-09-02").last_review_date = "2026-10-01"
    apply_buy(pf, "DNLM.L", 84, 900.0, on="2026-09-02", currency="GBp", fx=0.01348)
    pfmod.save(paths, pf)
    prices = {"HRB": 50.0, "DNLM.L": 910.0}
    monkeypatch.setattr(market_data, "latest_price", lambda t: prices.get(t))
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("USD", 1.0) if t == "HRB" else (None, 1.0))
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: __import__("pandas").Series(dtype=float))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    monkeypatch.setattr(review, "screen_row_for", lambda *a, **k: None)
    return paths


@pytest.mark.unit
def test_review_values_an_unreviewed_foreign_holding_at_cost_when_the_currency_is_unreadable(
        tmp_path, monkeypatch):
    import copy

    from value_pipeline.config import DEFAULTS
    paths = _review_paths(tmp_path, monkeypatch)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["parking"]["ticker"] = ""
    md = review.run(paths, cfg, yes=True, dry_run=True, only=["HRB"], today="2026-11-05").read_text()
    assert "- Value: **$10,000.00**" in md          # not 84 x 910 "dollars" = $76,440 for DNLM.L


@pytest.mark.unit
def test_review_sizes_a_reviewed_foreign_holding_by_hand_when_the_currency_is_unreadable(
        tmp_path, monkeypatch):
    import copy

    from value_pipeline.config import DEFAULTS
    paths = _review_paths(tmp_path, monkeypatch)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["parking"]["ticker"] = ""
    out = review.run(paths, cfg, yes=True, dry_run=True, today="2026-11-05")
    md = out.read_text()
    assert "quoted in GBp: size by hand" in md
    assert "$76,440" not in md and "76,440" not in md
    rows = {r["ticker"]: r for r in __import__("json").loads((out.parent / "results.json").read_text())}
    assert rows["DNLM.L"]["currency"] == "GBp" and rows["DNLM.L"]["fx_to_usd"] is None


@pytest.mark.unit
def test_trade_fx_refuses_an_unreadable_currency_for_a_first_buy():
    with pytest.raises(TradeError, match="could not read the quote currency"):
        trade_fx("0P0001K6NL.ST", None, fx_fn=lambda t: (None, 1.0))
    assert trade_fx("0P0001K6NL.ST", 0.105, fx_fn=lambda t: (None, 1.0)) == (None, 0.105)
    hrb = Position("HRB", 10, 50.0, "2026-10-01")
    assert trade_fx("HRB", None, position=hrb, fx_fn=lambda t: (None, 1.0)) == (None, 1.0)   # a dollar holding
    with pytest.raises(TradeError, match="Rate field"):
        trade_fx("0P0001K6NL.ST", None, fx_fn=lambda t: (None, 1.0), how="fill in the Rate field")


@pytest.mark.unit
def test_trade_fx_refuses_a_rate_for_a_dollar_stock_and_one_far_from_the_market():
    with pytest.raises(TradeError, match="US dollars"):
        trade_fx("AAPL", 0.01348, fx_fn=lambda t: ("USD", 1.0))
    assert trade_fx("AAPL", 1.0, fx_fn=lambda t: ("USD", 1.0)) == ("USD", 1.0)
    with pytest.raises(TradeError, match=r"0\.0135.*0\.105|0\.105.*0\.0135"):
        trade_fx("0P0001K6NL.ST", 0.0135, fx_fn=lambda t: ("SEK", 0.105))
    assert trade_fx("0P0001K6NL.ST", 0.11, fx_fn=lambda t: ("SEK", 0.105)) == ("SEK", 0.11)   # within 20%


@pytest.mark.unit
def test_portfolio_view_shows_no_gain_without_a_rate(tmp_path):
    paths = _foreign_paths(tmp_path)
    for fx in (("GBp", None), (None, 1.0)):
        row = portfolio_view(paths, price_fn=lambda t: 1_000.0, benchmark_fn=lambda *a: None,
                             fx_fn=lambda t, fx=fx: fx)["positions"][0]
        assert row["value"] == pytest.approx(1_215.0) and row["gain"] is None
