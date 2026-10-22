"""The analysts' data tools, served to a headless Claude Code session over MCP (stdio).

``ChatClaudeCode`` starts this per analyst call through ``--mcp-config`` and
tells it, in the environment, the run's ticker (``TA_COMPANY``), date
(``TA_TRADE_DATE``), which tools to serve (``TA_TOOLS``) and the graph's
statement vendors (``TA_TOOL_VENDORS``). Each MCP tool is the LangChain tool of
the same name with its ``InjectedState`` parameters filled from that state,
exactly as the graph's ToolNode fills them, so a dated tool still never serves
data past the trade date.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

from tradingagents.agents import tools as toolbox
from tradingagents.dataflows.config import set_config

SERVER_NAME = "ta"


def injected_fields(tool) -> dict[str, str]:
    """Parameter name -> graph-state key for each ``InjectedState`` parameter of a tool."""
    out = {}
    for name, field in tool.args_schema.model_fields.items():
        for marker in field.metadata:
            if type(marker).__name__ == "InjectedState":
                out[name] = marker.field
    return out


def exposed(tool, state: dict) -> Callable[..., str]:
    """A plain function with the tool's callable parameters, filling the injected ones from ``state``."""
    injected = injected_fields(tool)
    fields = tool.tool_call_schema.model_fields

    def call(**kwargs) -> str:
        args = {**{k: v for k, v in kwargs.items() if v is not None}, **{name: state.get(key, "") for name, key in injected.items()}}
        try:
            return str(tool.invoke(args))
        except Exception as exc:  # noqa: BLE001 - the model reads the failure and carries on
            return f"{tool.name} failed: {type(exc).__name__}: {exc}"

    call.__name__ = tool.name
    call.__doc__ = tool.description
    call.__signature__ = inspect.Signature([
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                          annotation=field.annotation if field.annotation is not None else str,
                          default=inspect.Parameter.empty if field.is_required() else field.default)
        for name, field in fields.items()
    ])
    return call


def build_server(names: list[str], state: dict, tool_vendors: dict | None = None) -> MCPServer:
    if tool_vendors:
        set_config({"tool_vendors": tool_vendors})
    server = MCPServer(SERVER_NAME)
    for name in names:
        tool = getattr(toolbox, name, None)
        if tool is None or not hasattr(tool, "args_schema"):
            raise ValueError(f"unknown tool {name}")
        server.add_tool(exposed(tool, state), name=name, description=tool.description)
    return server


def main() -> None:
    names = [n for n in os.environ.get("TA_TOOLS", "").split(",") if n]
    state = {"company_of_interest": os.environ.get("TA_COMPANY", ""),
             "trade_date": os.environ.get("TA_TRADE_DATE", "")}
    vendors = json.loads(os.environ.get("TA_TOOL_VENDORS") or "{}")
    build_server(names, state, vendors).run("stdio")


if __name__ == "__main__":
    main()
