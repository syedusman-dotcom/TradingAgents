"""Provider claude_code against the real Claude Code CLI: uses your login and your plan's usage.
Run by hand: .venv/bin/pytest -q -m integration tests/test_claude_code_integration.py"""

import pytest
from pydantic import BaseModel

from tradingagents.agents import tools as toolbox
from tradingagents.llm_clients import claude_code_client as cc

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def llm():
    if not cc.auth_status().get("loggedIn"):
        pytest.skip("Claude Code is not logged in")
    return cc.ChatClaudeCode(model="claude-opus-5-5", wait_minutes=0, timeout_seconds=300)


def test_a_plain_call(llm):
    message = llm.invoke("Reply with the word pong and nothing else.")
    assert message.content.strip().lower() == "pong"
    assert message.usage_metadata["output_tokens"] > 0 and message.response_metadata["total_cost_usd"] > 0


class Verdict(BaseModel):
    rating: str
    price_target: float


def test_a_structured_call(llm):
    verdict = llm.with_structured_output(Verdict).invoke(
        "Invent a rating (Buy, Hold or Sell) and a price target for a fictional company ACME.")
    assert verdict.rating in {"Buy", "Hold", "Sell"} and verdict.price_target > 0


def test_a_tool_call_through_the_mcp_server(llm):
    """The analyst must call get_value_metrics for HRB; the reply quotes a figure only the tool knows."""
    llm.set_context("HRB", "2026-10-22", {"get_cashflow": "yfinance", "get_balance_sheet": "yfinance",
                                          "get_income_statement": "yfinance"})
    message = llm.bind_tools([toolbox.get_value_metrics]).invoke(
        "Call get_value_metrics with curr_date 2026-10-22, then state HRB's Piotroski F-Score "
        "as 'F-Score: N' and nothing else.")
    assert "F-Score:" in message.content and message.response_metadata["num_turns"] >= 2
