from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.decision_math import consensus_excerpt, record_excerpt, worksheet_excerpt
from tradingagents.agents.tools import (
    get_annual_report,
    get_balance_sheet,
    get_cashflow,
    get_earnings_release,
    get_financial_history,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
    get_peer_comparison,
    get_quarterly_report,
    get_value_metrics,
)
from tradingagents.dataflows.router import route_to_vendor

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_value_metrics,
    get_peer_comparison,
    get_financial_history,
    get_annual_report,
    get_quarterly_report,
    get_earnings_release,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_insider_transactions,
)


def tool_metrics(messages, ticker: str, trade_date: str) -> str:
    """The latest ``get_value_metrics`` result in ``messages``, else fetched again (a provider that
    runs its tools itself leaves no tool results in the state); empty when there is none."""
    for message in reversed(messages or []):
        if isinstance(message, ToolMessage) and message.name == get_value_metrics.name:
            return message.content if isinstance(message.content, str) else ""
    try:
        return route_to_vendor("get_value_metrics", ticker, trade_date) or ""
    except Exception:  # noqa: BLE001 - the report stands without it
        return ""


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are a value-investing analyst. Judge the business over the long term, not the past week."
            " Call `get_value_metrics` first. Its figures (ROIC, margins, free cash flow, share-count change, buybacks,"
            " dividends, leverage, yields, the Piotroski F-Score point by point, the Altman Z-Score and the formula valuation)"
            " are computed in code from the statements: quote them and do not recompute or estimate them yourself."
            " The worksheet's base value is the formula value every later agent refers to; start your valuation"
            " from it and state each adjustment. Quote the worksheet's expected-return line and the sensitivity grid."
            " Quote the Record and inflection lines and the run-rate value; the run-rate value applies to"
            " inflection candidates only; otherwise value on the worksheet base."
            " Use the eight-quarter table for the trend; annual figures for the level."
            " If it is not available for this date, compute what you can from the statements and say so."
            " Then call `get_income_statement`, `get_balance_sheet` and `get_cashflow` with freq='annual'"
            " to explain one-offs, plus freq='quarterly' for the latest trend, `get_fundamentals` for the profile,"
            " and `get_insider_transactions`. Call `get_financial_history` for up to 20 years of revenue, margins and"
            " cash flow, to judge the business across a full cycle, and `get_annual_report` for management's own account"
            " of its risks and results; cite it for events such as lawsuits, tax disputes or acquisitions."
            " Call `get_quarterly_report` for the latest quarter's drivers and management's guidance;"
            " quote it with the filing date."
            " `get_earnings_release` gives the latest results announcement and its outlook, as filed."
            " `get_financial_history`, `get_annual_report`, `get_quarterly_report` and `get_earnings_release`"
            " answer only for US SEC filers; if they say so, do not retry them."
            " Never state a figure or event from memory as fact; if you rely on background knowledge, label it as unverified."
            "\n\nAnswer three questions with numbers:"
            "\n1. GOOD BUSINESS? Return on invested capital (EBIT / (net working capital + net fixed assets)) and ROE for each year;"
            " stability of gross and operating margins; revenue and free-cash-flow growth; free cash flow vs net income (earnings quality);"
            " signs of a durable competitive advantage (pricing power, high stable margins, high returns without heavy reinvestment)."
            "\n2. GOOD MANAGEMENT? Start from the Capital allocation table in `get_value_metrics`: where the cash went over"
            " five years, the prices paid for buybacks against today's price, acquisitions and debt; then insider buying vs"
            " selling and stock-based compensation as % of revenue."
            "\n3. CHEAP? Earnings yield EBIT/EV, free-cash-flow yield, P/E, EV/EBIT, P/B against the company's own history where data allows."
            " Start from the formula valuation's range: say which growth scenario fits the business and why, adjust the"
            " normalized cash flow only for identified one-offs or a cyclical peak or trough, and state each adjustment and"
            " its effect. Give your intrinsic value per share and the margin of safety = (value - price) / value."
            " For a growing company, also give the PEG ratio."
            " Call `get_peer_comparison`: say whether it is cheap relative to peers and why (quote the table)."
            "\n\nSAFETY CHECKS: report the Piotroski F-Score (0-9, list each point), Altman Z-Score, debt/equity,"
            " interest coverage and current ratio from `get_value_metrics`. Flag accounting red flags: receivables or inventory growing much faster than revenue,"
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
        if report:
            # Later agents read the formula value and today's price from the worksheet, appended
            # when the analyst's prose lacks it. The lane checks gate on the tool's Record and
            # inflection numbers, never the analyst's transcription, so that section is always
            # appended (read back as the report's last) unless the report already ends with it.
            metrics = tool_metrics(state["messages"], state["company_of_interest"], current_date)
            sheet, record = worksheet_excerpt(metrics), record_excerpt(metrics)
            consensus = consensus_excerpt(metrics)
            if sheet and not worksheet_excerpt(report):
                report = f"{report}\n\n{sheet}"
            if record and record_excerpt(report) != record:
                report = f"{report}\n\n{record}"
            # Likewise the tool's consensus lines, which the Portfolio Manager's check reads.
            if consensus and consensus_excerpt(report) != consensus:
                report = f"{report}\n\n{consensus}"

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
