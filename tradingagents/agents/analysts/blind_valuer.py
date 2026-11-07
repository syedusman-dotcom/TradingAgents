"""Blind Valuer: a per-share value from the accounts alone, never shown the price.

It runs from START beside the analysts. In the independent arm it makes one structured call on
the quick tier, with no tools, over ``blind_fundamentals_text``; the value comes back in the
statement currency and is converted here to the quote unit, and ``base_rates`` gets the base-rate
block for the company's size. In the current arm it fetches nothing and calls no model.

It reads none of the shared context (the instrument context carries the pipeline note, with price
moves): its prompt names the company and gives the accounts, nothing else.
"""

from __future__ import annotations

import logging

from tradingagents.agents.context import arm_of, get_language_instruction
from tradingagents.agents.schemas import BlindValuation, render_blind_valuation
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured
from tradingagents.dataflows.blind import blind_fundamentals_text
from tradingagents.dataflows.value_metrics import base_rate_block
from tradingagents.dataflows.vendors.yahoo.value_metrics import blind_inputs

logger = logging.getLogger(__name__)


def _reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text if isinstance(exc, ValueError) and text else f"{type(exc).__name__}: {text}"


def _prompt(ticker: str, inputs: dict, accounts: str) -> str:
    cur = inputs["reported"]
    return f"""You are valuing {inputs['name']} (`{ticker}`) from its accounts alone. You will not be shown the share price and you must not guess it, recall it or reason from it: this value has to be independent of what the market pays today.

Value the business per share:
1. Start the five-year growth rate from the base-rate median for the company's size given below (add about two points for inflation, since the base rates are real). Any higher rate needs a reason in the accounts; say what it is.
2. Use the discount rate given ({inputs['discount_rate']:.1%}).
3. Give a range: low at most 80% and high at least 120% of your value.
4. Every per-share figure (value_per_share, low, high) is in {cur} per share, the statement currency, on the latest share count shown. growth_5y and discount_rate are fractions (0.05 for 5%).
5. In the method, name the base rate you started from and the earnings figure you capitalised.

{NO_EXTERNAL_TOOLS}

{accounts}""" + get_language_instruction()


def create_blind_valuer(llm, inputs_fn=None):
    """The node; ``inputs_fn(ticker, date)`` gives the ``blind_inputs`` dict (this module's
    ``blind_inputs`` when None, looked up at call time)."""
    structured_llm = bind_structured(llm, BlindValuation, "Blind Valuer")

    def blind_valuer_node(state) -> dict:
        if arm_of(state) != "independent":
            return {"blind_valuation": "", "base_rates": ""}
        ticker, day = state["company_of_interest"], state["trade_date"]
        try:
            inputs = (inputs_fn or blind_inputs)(ticker, day)
        except Exception as exc:  # noqa: BLE001 - the review goes on without a blind value
            logger.info("Blind Valuer: no inputs for %s: %s", ticker, _reason(exc))
            return {"blind_valuation": f"skipped: {_reason(exc)}", "base_rates": ""}
        base_rates = base_rate_block(inputs.get("revenue_usd")) or ""
        try:
            accounts = blind_fundamentals_text(inputs)
        except ValueError as exc:
            return {"blind_valuation": f"skipped: {_reason(exc)}", "base_rates": base_rates}
        result = invoke_structured(structured_llm, _prompt(ticker, inputs, accounts), "Blind Valuer")
        if result is None:
            return {"blind_valuation": "skipped: the model returned no structured valuation",
                    "base_rates": base_rates}
        # cross is statement units per major quote unit; factor 100 for a minor-unit quote.
        quote_per_statement = inputs.get("factor", 1.0) / inputs["cross"]
        return {"blind_valuation": render_blind_valuation(result, quote_per_statement),
                "base_rates": base_rates}

    return blind_valuer_node
