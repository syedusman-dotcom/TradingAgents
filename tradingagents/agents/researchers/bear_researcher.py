from tradingagents.agents.context import (
    arm_of,
    get_instrument_context_from_state,
    get_language_instruction,
    opponent_argument_or_opening,
    report_or_absent,
)
from tradingagents.agents.researchers.adverse import adverse_pack


def create_bear_researcher(llm, pack_fn=adverse_pack):
    """The Value-Trap Hunter. ``pack_fn(ticker, date)`` fetches its adverse evidence pack in the
    independent arm; the current arm never calls it."""

    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

        # The independent arm opens from the reports and its own evidence pack, without the
        # debate history or the bull's text.
        independent = arm_of(state) == "independent"
        current_response = opponent_argument_or_opening(
            "" if independent else investment_debate_state.get("current_response", ""),
            "bull analyst",
        )
        asset_type = state.get("asset_type", "stock")
        target_label = "stock" if asset_type == "stock" else "asset"
        if independent:
            counterpoints = (
                "- Independence: You have not seen the Bull's case; make your own case from the "
                "evidence in the reports and from your adverse evidence pack."
            )
            closing = (
                "Use this information to deliver a compelling bear argument that demonstrates "
                f"the risks and weaknesses of investing in the {target_label}."
            )
            pack = pack_fn(state["company_of_interest"], state.get("trade_date", ""))
            debate_lines = (
                f"{pack}\n"
                "The adverse evidence pack above is your own evidence: the bull has not seen it. "
                "Cite from it by section, or say plainly that it holds nothing adverse.\n"
                f"Last bull argument: {current_response}"
            )
        else:
            counterpoints = (
                "- Bull Counterpoints: Rebut the Bull point by point with data.\n"
                "- Engagement: Present your argument in a conversational style, directly engaging "
                "with the bull analyst's points and debating effectively rather than simply "
                "listing facts."
            )
            closing = (
                "Use this information to deliver a compelling bear argument, refute the bull's "
                "claims, and engage in a dynamic debate that demonstrates the risks and weaknesses "
                f"of investing in the {target_label}."
            )
            debate_lines = (
                f"Conversation history of the debate: {history}\n"
                f"Last bull argument: {current_response}"
            )
        market_research_report = report_or_absent(state["market_report"], "market")
        sentiment_report = report_or_absent(state["sentiment_report"], "sentiment")
        news_report = report_or_absent(state["news_report"], "news")
        fundamentals_report = report_or_absent(state["fundamentals_report"], "fundamentals")
        instrument_context = get_instrument_context_from_state(state)
        fundamentals_label = (
            "Company fundamentals report"
            if asset_type == "stock"
            else "Asset fundamentals report (may be unavailable for crypto)"
        )

        prompt = f"""You are the Value-Trap Hunter, making the case against investing in the {target_label}. Argue that this stock is cheap for a good reason or is not actually cheap.

Key points to test:

- Deteriorating Business: Declining returns on capital or margins; a disappearing competitive advantage.
- Distress: Heavy debt or distress risk (low Altman Z-Score, low Piotroski F-Score).
- Management: Poor capital allocation or shareholder dilution.
- Accounting: Red flags in the financial statements.
- Valuation: An intrinsic value estimate that relies on optimistic assumptions.
- Trend: A FREEFALL or DOWNTREND that may signal bad news not yet in the numbers.
{counterpoints}

Resources available:

{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
{debate_lines}
{closing}
""" + get_language_instruction()

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
