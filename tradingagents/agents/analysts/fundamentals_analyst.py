from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_insider_transactions,
)


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are a value-investing analyst. Judge the business over the long term, not the past week."
            " Call `get_income_statement`, `get_balance_sheet` and `get_cashflow` with freq='annual' to get as many years as available (aim for 5-10),"
            " plus freq='quarterly' for the latest trend, `get_fundamentals` for the profile, and `get_insider_transactions`."
            "\n\nAnswer three questions with numbers:"
            "\n1. GOOD BUSINESS? Return on invested capital (EBIT / (net working capital + net fixed assets)) and ROE for each year;"
            " stability of gross and operating margins; revenue and free-cash-flow growth; free cash flow vs net income (earnings quality);"
            " signs of a durable competitive advantage (pricing power, high stable margins, high returns without heavy reinvestment)."
            "\n2. GOOD MANAGEMENT? Capital allocation from the cash flow statement: buybacks vs share dilution (share count trend),"
            " dividends, acquisitions, debt taken on; insider buying vs selling; stock-based compensation as % of revenue."
            "\n3. CHEAP? Earnings yield EBIT/EV, free-cash-flow yield, P/E, EV/EBIT, P/B against the company's own history where data allows."
            " Estimate a conservative intrinsic value range (e.g. normalized owner earnings or FCF times a sensible multiple)"
            " and state the margin of safety = (value - price) / value. For a growing company, also give the PEG ratio."
            "\n\nSAFETY CHECKS: compute an approximate Piotroski F-Score (0-9, list each point), Altman Z-Score, debt/equity,"
            " interest coverage, current ratio. Flag accounting red flags: receivables or inventory growing much faster than revenue,"
            " falling cash conversion, frequent one-off charges."
            "\n\nSay plainly when data is missing or conflicting instead of guessing."
            " End with a Markdown table: Good business (score 1-5), Good management (1-5), Cheap (margin of safety %),"
            " F-Score, Z-Score, key red flags."
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
