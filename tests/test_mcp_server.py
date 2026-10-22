"""The analysts' data tools served to a headless Claude Code session over MCP (no network)."""

import asyncio

import pytest

from tradingagents import mcp_server
from tradingagents.agents import tools as toolbox


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.unit
def test_injected_parameters_map_to_state_keys():
    assert mcp_server.injected_fields(toolbox.get_value_metrics) == {"ticker": "company_of_interest",
                                                                      "trade_date": "trade_date"}
    assert mcp_server.injected_fields(toolbox.get_stock_data) == {"symbol": "company_of_interest",
                                                                   "trade_date": "trade_date"}


@pytest.mark.unit
def test_the_server_lists_the_named_tools_without_their_injected_parameters():
    server = mcp_server.build_server(["get_value_metrics", "get_stock_data"], {"company_of_interest": "HRB",
                                                                               "trade_date": "2026-10-22"})
    tools = {t.name: t for t in _run(server.list_tools())}
    assert set(tools) == {"get_value_metrics", "get_stock_data"}
    assert set(tools["get_value_metrics"].input_schema["properties"]) == {"curr_date"}
    assert set(tools["get_stock_data"].input_schema["properties"]) == {"start_date", "end_date"}
    assert "Piotroski" in tools["get_value_metrics"].description


@pytest.mark.unit
def test_a_call_fills_the_ticker_and_date_from_the_state(monkeypatch):
    seen = {}

    def fake_route(name, *args):
        seen[name] = args
        return "metrics text"

    monkeypatch.setattr(toolbox, "route_to_vendor", fake_route)
    server = mcp_server.build_server(["get_value_metrics"], {"company_of_interest": "HRB", "trade_date": "2026-10-22"})
    result = _run(server.call_tool("get_value_metrics", {"curr_date": "2026-12-01"}))
    assert result.content[0].text == "metrics text"
    assert seen["get_value_metrics"] == ("HRB", "2026-10-22")     # the date is clamped to the trade date


@pytest.mark.unit
def test_a_failing_tool_returns_its_error_as_text(monkeypatch):
    def broken(name, *args):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(toolbox, "route_to_vendor", broken)
    server = mcp_server.build_server(["get_value_metrics"], {"company_of_interest": "HRB", "trade_date": "2026-10-22"})
    result = _run(server.call_tool("get_value_metrics", {"curr_date": "2026-10-22"}))
    assert "get_value_metrics failed: RuntimeError: vendor down" in result.content[0].text


@pytest.mark.unit
def test_an_unknown_tool_name_is_refused():
    with pytest.raises(ValueError, match="unknown tool"):
        mcp_server.build_server(["get_rich_quick"], {})


@pytest.mark.unit
def test_tool_vendors_from_the_environment_reach_the_dataflows_config(monkeypatch):
    from tradingagents.dataflows.config import get_config

    mcp_server.build_server([], {}, {"get_cashflow": "yfinance"})
    assert get_config()["tool_vendors"]["get_cashflow"] == "yfinance"


@pytest.mark.unit
def test_omitted_optional_parameters_take_the_tools_default(monkeypatch):
    seen = []

    def fake_route(name, *args):
        seen.append((name, args))
        return "cashflow text"

    monkeypatch.setattr(toolbox, "route_to_vendor", fake_route)
    server = mcp_server.build_server(["get_cashflow"], {"company_of_interest": "HRB", "trade_date": "2026-10-22"})
    result = _run(server.call_tool("get_cashflow", {}))
    assert result.content[0].text == "cashflow text"
    assert len(seen) == 1 and seen[0][0] == "get_cashflow"
    seen.clear()
    result = _run(server.call_tool("get_cashflow", {"curr_date": "2026-10-01"}))
    assert result.content[0].text == "cashflow text"
    assert "2026-10-01" in seen[0][1]
