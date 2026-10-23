"""Pre-mortem: after the risk debate, the most likely way the position loses money.

One plain call on the quick tier, with no tools and no structured output. The
Portfolio Manager reads the result and must answer it.
"""

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
    report_or_absent,
)


def create_premortem(llm):
    def premortem_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)
        fundamentals_report = report_or_absent(state.get("fundamentals_report", ""), "fundamentals")

        prompt = f"""{instrument_context}
{portfolio_context}
Company Fundamentals Report: {fundamentals_report}

Research Manager's investment plan:
{state["investment_plan"]}

Trader's transaction proposal:
{state["trader_investment_plan"]}

Risk Analysts Debate History:
{state["risk_debate_state"]["history"]}

It is three years on and this position has lost 40%. Write about 250 words: 1. The single most likely cause. 2. The evidence visible today that supports it. 3. The one metric that would show it first, and the level that confirms it.""" + get_language_instruction()

        response = llm.invoke(prompt)
        return {"premortem": "Pre-mortem: " + response.content}

    return premortem_node
