"""The price-blind valuer: price-free inputs, a valuation in the statement currency converted to
the quote unit, and a node that runs only in the independent arm (no network)."""

import pandas as pd
import pytest

from tradingagents.agents import schemas
from tradingagents.agents.analysts import blind_valuer
from tradingagents.dataflows.blind import blind_fundamentals_text
from tradingagents.dataflows.vendors.yahoo import value_metrics as yahoo_vm
from value_pipeline import parse

FORBIDDEN = ("price", "Price", "market cap", "enterprise value", "EV/", "P/E", "P/B", "yield",
             "52-week", "target", "formula value", "worksheet", "margin of safety", "buyback",
             "repurchase", "123.45")

KEYS = {"name", "sector", "years", "reported", "quote", "cross", "factor", "same_basis",
        "statement_shares", "revenue_usd", "discount_rate"}


def _ticker(quote="SEK", reported="EUR", price=123.45, shares=1e8):
    """A company quoted in ``quote`` reporting in ``reported``, buying back shares every year."""
    cols = [pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31"),
            pd.Timestamp("2022-12-31")]

    def frame(rows):
        return pd.DataFrame({c: {k: v[i] for k, v in rows.items()} for i, c in enumerate(cols)})

    class FakeTicker:
        info = {"currency": quote, "financialCurrency": reported, "currentPrice": price,
                "regularMarketPrice": price, "marketCap": price * shares, "sharesOutstanding": shares,
                "longName": "Blindco AB", "sector": "Industrials",
                "fiftyTwoWeekHigh": 150.0, "targetMeanPrice": 140.0, "trailingPE": 18.0}
        income_stmt = frame({"Total Revenue": [2.0e9, 1.9e9, 1.8e9, 1.7e9],
                             "Gross Profit": [8.0e8, 7.6e8, 7.2e8, 6.8e8],
                             "EBIT": [3.0e8, 2.8e8, 2.6e8, 2.4e8],
                             "Net Income": [2.2e8, 2.1e8, 1.9e8, 1.8e8]})
        balance_sheet = frame({"Total Assets": [3e9] * 4, "Current Assets": [1.2e9] * 4,
                               "Current Liabilities": [6e8] * 4, "Net PPE": [9e8] * 4,
                               "Cash And Cash Equivalents": [2e8] * 4, "Total Debt": [5e8] * 4,
                               "Stockholders Equity": [1.5e9] * 4,
                               "Ordinary Shares Number": [shares, shares * 1.02, shares * 1.04,
                                                          shares * 1.06]})
        cashflow = frame({"Operating Cash Flow": [3.2e8, 3.0e8, 2.8e8, 2.6e8],
                          "Capital Expenditure": [-8e7] * 4,
                          "Free Cash Flow": [2.4e8, 2.2e8, 2.0e8, 1.8e8],
                          "Cash Dividends Paid": [-6e7] * 4,
                          "Repurchase Of Capital Stock": [-1.2345e8] * 4})

        def history(self, **kw):
            raise AssertionError("the blind inputs read no price history")

    return FakeTicker


def _patch(monkeypatch, fake, rates):
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: fake())
    monkeypatch.setattr(yahoo_vm, "is_historical", lambda d: False)
    monkeypatch.setattr(yahoo_vm, "current_discount_rate", lambda: {"rate": 0.09, "treasury_10y": None})
    monkeypatch.setattr(yahoo_vm, "usd_rate", lambda c: rates.get(c))


@pytest.fixture
def sek_inputs(monkeypatch):
    _patch(monkeypatch, _ticker(), {"SEK": 0.1, "EUR": 1.0})
    return yahoo_vm.blind_inputs("BLIND.ST", "2026-11-07")


@pytest.mark.unit
def test_blind_inputs_for_a_dollar_company(monkeypatch):
    _patch(monkeypatch, _ticker("USD", "USD"), {"USD": 1.0})
    inputs = yahoo_vm.blind_inputs("BLND", "2026-11-07")
    assert set(inputs) == KEYS
    assert inputs["cross"] == 1.0 and inputs["factor"] == 1.0 and inputs["same_basis"] is True
    assert inputs["reported"] == inputs["quote"] == "USD"
    assert inputs["statement_shares"] == 1e8 and inputs["revenue_usd"] == 2.0e9
    assert inputs["discount_rate"] == 0.09 and inputs["name"] == "Blindco AB"
    assert len(inputs["years"]) == 4


@pytest.mark.unit
def test_blind_inputs_convert_at_the_cross_rate_and_drop_an_unconvertible_quote(monkeypatch, sek_inputs):
    assert sek_inputs["cross"] == pytest.approx(0.1) and sek_inputs["same_basis"] is True
    assert sek_inputs["revenue_usd"] == pytest.approx(2.0e9)
    _patch(monkeypatch, _ticker(), {"EUR": 1.0})                      # no SEK rate
    assert yahoo_vm.blind_inputs("BLIND.ST", "2026-11-07")["cross"] is None


@pytest.mark.unit
def test_blind_inputs_are_not_available_for_a_past_date(monkeypatch):
    monkeypatch.setattr(yahoo_vm.yf, "Ticker", lambda *a: pytest.fail("no request for a past date"))
    with pytest.raises(ValueError, match="past"):
        yahoo_vm.blind_inputs("KO", "2020-01-02")


@pytest.mark.unit
def test_blind_text_shows_the_accounts_and_nothing_priced(sek_inputs):
    text = blind_fundamentals_text(sek_inputs)
    for needed in ("Revenue", "Free cash flow", "Shares outstanding", "EUR", "Blindco AB",
                   "Base rates", "Discount rate"):
        assert needed in text, needed
    for banned in FORBIDDEN:
        assert banned not in text, banned


@pytest.mark.unit
def test_blind_text_does_not_move_with_the_price(monkeypatch, sek_inputs):
    _patch(monkeypatch, _ticker(price=234.56), {"SEK": 0.1, "EUR": 1.0})
    again = yahoo_vm.blind_inputs("BLIND.ST", "2026-11-07")
    assert blind_fundamentals_text(again) == blind_fundamentals_text(sek_inputs)


@pytest.mark.unit
@pytest.mark.parametrize("change, reason", [({"same_basis": False}, "share"),
                                            ({"cross": None}, "rate"),
                                            ({"statement_shares": None}, "share count")])
def test_blind_text_refuses_a_basis_it_cannot_convert(sek_inputs, change, reason):
    with pytest.raises(ValueError, match=reason):
        blind_fundamentals_text({**sek_inputs, **change})


VALUATION = schemas.BlindValuation(value_per_share=10.0, low=8.0, high=12.0, growth_5y=0.05,
                                   discount_rate=0.09, method="Owner earnings over 9%.",
                                   reasoning="Steady compounder.")


@pytest.mark.unit
def test_render_and_parse_round_trip_in_the_quote_unit():
    text = schemas.render_blind_valuation(VALUATION, 0.1)
    assert text.splitlines()[0] == "**Value per share**: 1.00 (range 0.80–1.20) in the quote unit"
    assert "**Growth assumed**: 5.0%" in text and "**Discount rate**: 9.0%" in text
    assert "**Method**: Owner earnings" in text and "**Reasoning**: Steady" in text
    assert schemas.blind_values(text) == pytest.approx(
        {"value": 1.0, "low": 0.8, "high": 1.2, "growth": 0.05, "discount": 0.09})
    assert parse.blind_value(text) == pytest.approx(1.0)
    assert parse.blind_value("skipped: no rate") is None and parse.blind_value("") is None
    assert schemas.blind_values("skipped: no rate") is None


@pytest.mark.unit
def test_a_long_method_is_cut_to_400_characters():
    v = VALUATION.model_copy(update={})
    assert len(schemas.BlindValuation(**{**v.model_dump(), "method": "x" * 900}).method) == 400


class _Scripted:
    """A model whose structured call returns ``result`` and records the prompt."""

    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.prompts = result, fail, []

    def with_structured_output(self, schema, **kw):
        assert schema is schemas.BlindValuation
        return self

    def invoke(self, prompt):
        if self.fail:
            raise AssertionError("the current arm calls no model")
        self.prompts.append(prompt)
        return self.result


def _state(arm=None):
    s = {"company_of_interest": "BLIND.ST", "trade_date": "2026-11-07"}
    if arm:
        s["agent_round"] = arm
    return s


@pytest.mark.unit
@pytest.mark.parametrize("arm", [None, "current"])
def test_the_current_arm_calls_no_model(sek_inputs, arm):
    node = blind_valuer.create_blind_valuer(_Scripted(fail=True), inputs_fn=lambda t, d: sek_inputs)
    out = node(_state(arm))
    assert out["blind_valuation"] == ""
    assert "Base rates" in out["base_rates"]


@pytest.mark.unit
def test_the_independent_arm_values_in_statement_units_and_converts(sek_inputs):
    llm = _Scripted(VALUATION)
    node = blind_valuer.create_blind_valuer(llm, inputs_fn=lambda t, d: sek_inputs)
    out = node(_state("independent"))
    # 10 EUR at 0.1 EUR per SEK is 100 SEK.
    assert out["blind_valuation"] == schemas.render_blind_valuation(VALUATION, 10.0)
    assert out["blind_valuation"].startswith("**Value per share**: 100.00 (range 80.00–120.00)")
    assert out["base_rates"].startswith("Base rates")
    prompt = llm.prompts[0]
    assert "EUR" in prompt and "Base rates" in prompt and "must not guess" in prompt
    assert "123.45" not in prompt and "SEK" not in prompt


@pytest.mark.unit
def test_a_pence_quote_gets_the_value_in_pence(monkeypatch):
    _patch(monkeypatch, _ticker("GBp", "GBP", price=886.0), {})
    inputs = yahoo_vm.blind_inputs("BLND.L", "2026-11-07")
    assert inputs["cross"] == 1.0 and inputs["factor"] == 100.0
    node = blind_valuer.create_blind_valuer(_Scripted(VALUATION), inputs_fn=lambda t, d: inputs)
    assert node(_state("independent"))["blind_valuation"].startswith(
        "**Value per share**: 1000.00 (range 800.00–1200.00)")


@pytest.mark.unit
def test_the_valuer_is_skipped_when_its_inputs_fail(sek_inputs):
    def boom(t, d):
        raise RuntimeError("Yahoo down")

    out = blind_valuer.create_blind_valuer(_Scripted(fail=True), inputs_fn=boom)(_state("independent"))
    assert out["blind_valuation"].startswith("skipped: ") and "Yahoo down" in out["blind_valuation"]
    assert out["base_rates"] == ""
    adr = {**sek_inputs, "same_basis": False}
    out = blind_valuer.create_blind_valuer(_Scripted(fail=True), inputs_fn=lambda t, d: adr)(
        _state("independent"))
    assert out["blind_valuation"].startswith("skipped: ") and out["base_rates"]


@pytest.mark.unit
def test_a_structured_miss_is_a_skip(sek_inputs):
    node = blind_valuer.create_blind_valuer(_Scripted(None), inputs_fn=lambda t, d: sek_inputs)
    assert node(_state("independent"))["blind_valuation"].startswith("skipped: ")
