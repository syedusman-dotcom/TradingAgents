from tradingagents.agents.context import (
    arm_of,
    get_instrument_context_from_state,
    get_language_instruction,
    opponent_argument_or_opening,
    report_or_absent,
)


def create_bull_researcher(llm):
    def bull_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bull_history = investment_debate_state.get("bull_history", "")

        # The independent arm opens from the reports alone: no debate history, no bear text.
        independent = arm_of(state) == "independent"
        current_response = opponent_argument_or_opening(
            "" if independent else investment_debate_state.get("current_response", ""),
            "bear analyst",
        )
        if independent:
            counterpoints = (
                "- Bear Counterpoints: You will not see the Bear's case; name the strongest "
                "objections a value-trap hunter would raise and answer each with data.\n"
                "- Independence: Write your own opening case; a judge reads it side by side with "
                "a Bear case written without yours."
            )
            debate_lines = f"Last bear argument: {current_response}"
        else:
            counterpoints = (
                "- Bear Counterpoints: Rebut the Bear point by point with data.\n"
                "- Engagement: Present your argument in a conversational style, engaging directly "
                "with the bear analyst's points and debating effectively rather than just listing "
                "data."
            )
            debate_lines = (
                f"Conversation history of the debate: {history}\n"
                f"Last bear argument: {current_response}"
            )
        market_research_report = report_or_absent(state["market_report"], "market")
        sentiment_report = report_or_absent(state["sentiment_report"], "sentiment")
        news_report = report_or_absent(state["news_report"], "news")
        fundamentals_report = report_or_absent(state["fundamentals_report"], "fundamentals")
        instrument_context = get_instrument_context_from_state(state)
        asset_type = state.get("asset_type", "stock")
        target_label = "stock" if asset_type == "stock" else "asset"
        fundamentals_label = (
            "Company fundamentals report"
            if asset_type == "stock"
            else "Asset fundamentals report (may be unavailable for crypto)"
        )

        prompt = f"""You are the Value Bull, advocating for investing in the {target_label}. Argue that this is a good business, run by good management, available at a price meaningfully below its intrinsic value.

Key points to focus on:
- Good Business: Use the fundamentals report's return on capital, margin stability and cash flow to show the business is high quality.
- Cheap: Use the fundamentals report's intrinsic value estimate and margin of safety; explain why the reason the stock is cheap is temporary.
- Compounding: Show why the business can compound value over 3-5 years.
- No Momentum Arguments: Do not use short-term price momentum or social media sentiment as arguments.
{counterpoints}

Resources available:
{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
{debate_lines}
Use this information to deliver a compelling bull argument, refute the bear's concerns, and engage in a dynamic debate that demonstrates the strengths of the bull position.
""" + get_language_instruction()

        response = llm.invoke(prompt)

        argument = f"Bull Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
