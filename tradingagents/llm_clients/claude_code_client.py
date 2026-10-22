"""The agents on your Claude subscription: each call is a headless Claude Code session.

``claude -p`` runs with the CLI's own login, so nothing is billed to an API key.
The child never sees ``ANTHROPIC_API_KEY`` (the CLI would bill it) nor the
variables of a Claude Code session this process may run inside (they would
stop a nested session). ``--bare`` is never used: it drops the login. Settings
and hooks are not loaded and no built-in tool is available, so an agent can
only talk and, for the analysts, call the data tools served over MCP.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field, PrivateAttr

from .base_client import BaseLLMClient

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "ta"
RETRY_SECONDS = 600
# What the CLI says when the plan's rolling limit is hit; matched case-insensitively.
USAGE_LIMIT_SIGNS = ("usage limit", "rate limit", "rate limited", "limit reached", "resets", "overloaded")
# Wordings that name a limit only together with the word "limit" ("You've hit your limit").
_LIMIT_LEAD_INS = ("hit your", "reached your")
_DROPPED_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")
_DROPPED_PREFIXES = ("CLAUDE_CODE_",)
_SERVER_PREFIXES = ("TRADINGAGENTS_", "TYPESAFE_")
_SERVER_VARS = ("SEC_EDGAR_USER_AGENT", "OLLAMA_BASE_URL", "PATH", "HOME")
_PLAIN_TURNS, _TOOL_TURNS = 4, 50


class ClaudeCodeError(RuntimeError):
    """The CLI could not complete a call."""


class UsageLimitReached(ClaudeCodeError):
    """The plan's usage limit stayed in force for the whole wait."""


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment for ``claude -p``: no API key, no session variables."""
    return {k: v for k, v in env.items() if k not in _DROPPED_VARS and not k.startswith(_DROPPED_PREFIXES)}


def run_cli(args: list[str], prompt: str, env: dict, cwd: str, timeout: float) -> dict:
    """Run ``claude <args>`` with the prompt on stdin; its result JSON, whatever the exit code."""
    try:
        proc = subprocess.run(["claude", *args], input=prompt, capture_output=True, text=True,
                              env=env, cwd=cwd, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ClaudeCodeError("the `claude` command was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeError(f"claude -p timed out after {timeout:.0f}s") from exc
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise ClaudeCodeError(f"claude -p exited {proc.returncode} without a JSON result: {tail}") from exc


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") if isinstance(c, dict) else str(c) for c in content]
    return "\n".join(p for p in parts if p)


def render_prompt(messages: Sequence[BaseMessage]) -> tuple[str, str]:
    """(system prompt, prompt): one human message as is, a longer history as a labelled transcript."""
    system = "\n\n".join(_text(m) for m in messages if isinstance(m, SystemMessage))
    rest = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(rest) == 1:
        return system, _text(rest[0])
    lines = []
    for m in rest:
        label = "Human" if isinstance(m, HumanMessage) else "Assistant" if isinstance(m, AIMessage) \
            else type(m).__name__.removesuffix("Message")
        lines.append(f"{label}: {_text(m)}")
    return system, "\n\n".join(lines)


def usage_of(result: dict) -> dict:
    u = result.get("usage") or {}
    tokens_in = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    tokens_out = int(u.get("output_tokens") or 0)
    return {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}


def usage_limited(result: dict) -> bool:
    text = str(result.get("result") or "").lower()
    if result.get("api_error_status") in (429, 529):
        return True
    if any(sign in text for sign in USAGE_LIMIT_SIGNS):
        return True
    return "limit" in text and any(lead in text for lead in _LIMIT_LEAD_INS)


def _server_env(source: Mapping[str, str]) -> dict[str, str]:
    """The slice of the environment the MCP server needs: vendor keys and settings, never the API key."""
    return {k: v for k, v in child_env(source).items()
            if k.endswith("_API_KEY") or k.startswith(_SERVER_PREFIXES) or k in _SERVER_VARS}


def _failure(result: dict) -> str:
    """Why an ``is_error`` result failed: its subtype, text and error list (the max-turns result has no text)."""
    parts = [str(result.get("result") or "")[:300], *(str(e) for e in result.get("errors") or [])]
    subtype = result.get("subtype")
    return f"claude -p failed{f' ({subtype})' if subtype else ''}: {'; '.join(p for p in parts if p)[:400]}"


def mcp_config(tool_names: Sequence[str], context: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict:
    """The inline ``--mcp-config``: this interpreter serving the named tools for the run's ticker and date.

    The server starts in an empty directory with no ``.env``, so it is handed the data-vendor keys and
    settings of this process (``ANTHROPIC_API_KEY`` stays out).
    """
    env = _server_env(os.environ if env is None else env) | {"TA_COMPANY": str(context.get("company", "")), "TA_TRADE_DATE": str(context.get("trade_date", "")),
           "TA_TOOLS": ",".join(tool_names), "TA_TOOL_VENDORS": json.dumps(context.get("tool_vendors") or {})}
    return {"mcpServers": {MCP_SERVER_NAME: {"command": sys.executable, "args": ["-m", "tradingagents.mcp_server"],
                                             "env": env}}}


class ChatClaudeCode(BaseChatModel):
    """A LangChain chat model whose every call is one ``claude -p`` run."""

    model: str
    timeout_seconds: float = 900
    wait_minutes: float = 360
    effort: str | None = None
    tools: list = Field(default_factory=list)
    json_schema: dict | None = None
    runner: Callable[..., dict] = Field(default=run_cli, exclude=True)
    clock: Callable[[], float] = Field(default=time.monotonic, exclude=True)
    sleep: Callable[[float], None] = Field(default=time.sleep, exclude=True)
    _context: dict = PrivateAttr(default_factory=dict)
    _workdir: str | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "claude_code"

    @property
    def _identifying_params(self) -> dict:
        return {"model": self.model}

    # --- what the graph tells the model ---------------------------------------------

    def set_context(self, company: str, trade_date: str, tool_vendors: dict | None = None) -> None:
        """The run's ticker, date and statement vendors, for the tools; shared with every bound copy."""
        self._context.clear()
        self._context.update(company=company, trade_date=trade_date, tool_vendors=tool_vendors or {})

    def _copy(self, **update) -> ChatClaudeCode:
        copy = self.model_copy(update=update)
        copy._context = self._context      # the same dict: set_context on the original reaches the copy
        copy._workdir = self._workdir
        return copy

    def bind_tools(self, tools, **kwargs):
        return self._copy(tools=list(tools))

    def with_structured_output(self, schema, **kwargs):
        bound = self._copy(json_schema=schema.model_json_schema())

        def parse(message: AIMessage):
            parsed = message.response_metadata.get("structured_output")
            if parsed is None:
                raise ValueError("claude -p returned no structured_output")
            return schema.model_validate(parsed)

        return bound | RunnableLambda(parse)

    # --- the call ------------------------------------------------------------------------

    def _args(self, system: str) -> list[str]:
        args = ["-p", "--output-format", "json", "--model", self.model, "--tools", "",
                "--no-session-persistence", "--setting-sources", "", "--permission-mode", "dontAsk",
                "--max-turns", str(_TOOL_TURNS if self.tools else _PLAIN_TURNS)]
        if system:
            args += ["--system-prompt", system]
        if self.effort:
            args += ["--effort", self.effort]
        if self.json_schema:
            args += ["--json-schema", json.dumps(self.json_schema)]
        if self.tools:
            names = [t.name for t in self.tools]
            args += ["--mcp-config", json.dumps(mcp_config(names, self._context)), "--strict-mcp-config",
                     "--allowedTools", ",".join(f"mcp__{MCP_SERVER_NAME}__{n}" for n in names)]
        return args

    def _cwd(self) -> str:
        if self._workdir is None or not os.path.isdir(self._workdir):
            self._workdir = tempfile.mkdtemp(prefix="claude-code-agent-")
        return self._workdir

    def _run(self, args: list[str], prompt: str) -> dict:
        deadline = self.clock() + self.wait_minutes * 60
        while True:
            started = self.clock()
            result = self.runner(args, prompt, child_env(os.environ), self._cwd(), self.timeout_seconds)
            if not result.get("is_error"):
                usage = usage_of(result)
                log.info("%s: %.0fs, %d tokens in, %d out, %d turn(s)", self.model, self.clock() - started,
                         usage["input_tokens"], usage["output_tokens"], int(result.get("num_turns") or 0))
                return result
            text = str(result.get("result") or "")
            if not usage_limited(result):
                raise ClaudeCodeError(_failure(result))
            if self.clock() + RETRY_SECONDS > deadline:
                raise UsageLimitReached(f"usage limit still in force after {self.wait_minutes:.0f} minutes: {text[:200]}")
            log.warning("usage limit reached (%s); retrying in %d minutes", text[:120], RETRY_SECONDS // 60)
            self.sleep(RETRY_SECONDS)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        system, prompt = render_prompt(messages)
        result = self._run(self._args(system), prompt)
        message = AIMessage(
            content=str(result.get("result") or ""),
            usage_metadata=usage_of(result),
            response_metadata={"model": self.model, "total_cost_usd": float(result.get("total_cost_usd") or 0.0),
                               "num_turns": int(result.get("num_turns") or 0),
                               "duration_ms": int(result.get("duration_ms") or 0),
                               "structured_output": result.get("structured_output")},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


# --- the client the factory builds ---------------------------------------------------

def auth_status() -> dict:
    """``claude auth status`` as a dict; ``{"loggedIn": False}`` when the CLI is missing."""
    try:
        proc = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30, check=False,
                              env=child_env(os.environ))
        return json.loads(proc.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return {"loggedIn": False}


class ClaudeCodeClient(BaseLLMClient):
    """Provider ``claude_code``: the model runs through the Claude Code CLI on your subscription."""

    provider = "claude_code"
    _FIELDS = ("timeout_seconds", "wait_minutes", "effort", "runner", "callbacks")

    def validate_model(self) -> bool:
        return True                 # the CLI accepts full names and aliases such as opus or fable

    def get_llm(self) -> ChatClaudeCode:
        check = self.kwargs.get("auth_check", auth_status)
        if not check().get("loggedIn"):
            raise ClaudeCodeError("Claude Code is not logged in: run `claude auth login` (or install the CLI) "
                                  "before using provider claude_code")
        fields = {k: v for k, v in self.kwargs.items() if k in self._FIELDS}
        return ChatClaudeCode(model=self.model, **fields)
