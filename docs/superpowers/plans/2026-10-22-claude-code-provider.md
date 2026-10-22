# Claude Code Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every TradingAgents agent call as a headless Claude Code session on the user's Claude subscription, selectable as provider `claude_code`, with nothing downstream of the model changed.

**Architecture:** A new LangChain chat model (`ChatClaudeCode`) runs `claude -p` per call: free text, `--json-schema` for the structured agents, and a local stdio MCP server (`tradingagents.mcp_server`) that serves the analysts' existing data tools so the CLI runs the tool loop itself. The provider is registered in the existing factory; the graph gets one duck-typed hook to pass the ticker and date to the model; `value_pipeline` records measured token usage.

**Tech Stack:** Python 3.12+, LangChain core (`BaseChatModel`), the `claude` CLI 2.1.305 (`-p` mode), `mcp>=2,<3` (`mcp.server.mcpserver.MCPServer`), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-22-claude-code-provider-design.md` (read it first; its "Verified facts" table is the ground truth for CLI flags).

## Global Constraints

- Repo root: `/Users/syedusman/Developer/ValueTrading`. Run everything from there with `.venv/bin/python`, `.venv/bin/pytest -q`, `.venv/bin/ruff check --fix .`. Never run `ruff format`.
- Commit messages: Conventional Commits, ending with the line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- The tooling never places a trade. Nothing in this plan touches `actions.py`, prompts, schemas or report files.
- Child environment for every CLI call: copy of `os.environ` **minus** `ANTHROPIC_API_KEY`, `CLAUDECODE` and every variable starting `CLAUDE_CODE_`.
- CLI flags, exactly (verified): `-p --output-format json --model <m> --tools "" --no-session-persistence --setting-sources "" --permission-mode dontAsk --max-turns <n>`; never `--bare` (it drops the login).
- Tool calls: `--mcp-config <inline json> --strict-mcp-config --allowedTools mcp__ta__<name>,...`; MCP server name is `ta`.
- Defaults: `claude_code_deep_model = "claude-fable-5-1"`, `claude_code_quick_model = "claude-opus-5-5"`, `claude_code_wait_minutes = 360`, `claude_code_timeout_seconds = 900`. Usage-limit retry interval: 600 seconds.
- The ACTIONS line keeps the prefix `Cost of this review: ~$` so `web/views.py`'s regex keeps matching.
- Tests marked `@pytest.mark.unit` run without network; the real-CLI tests are `@pytest.mark.integration` and are run once by hand in Task 10.
- No `TBD`; every task ends with a green `.venv/bin/pytest -q` and clean ruff.

## Review Focus

1. A non-limit CLI error (e.g. "Not logged in") must raise at once, never wait six hours. Pinned in Task 4 (`test_a_plain_error_is_raised_without_waiting`).
2. `ANTHROPIC_API_KEY` loaded from `.env` must never reach the child process, or the key is billed. Pinned in Task 1 (`test_the_child_environment_has_no_api_key_and_no_session_variables`).
3. Message content that is a list of text blocks (LangChain may pass blocks) must render as text, not `[{'type': ...}]`. Pinned in Task 1 (`test_block_content_renders_as_text`).
4. A data tool that raises inside the MCP server must return the error as the tool's text, not kill the server. Pinned in Task 6 (`test_a_failing_tool_returns_its_error_as_text`).
5. A structured call whose JSON came back as text but without `structured_output` must raise so the existing free-text fallback runs. Pinned in Task 2 (`test_missing_structured_output_raises`).

---

### Task 0: Merge upstream 0.7.0

**Files:**
- Modify: whatever `git merge` touches; expected conflicts only in `pyproject.toml`, `tradingagents/default_config.py`, `tests/test_graph_end_to_end.py`.

**Interfaces:**
- Produces: the post-merge `tradingagents/llm_clients/factory.py` (with `REASONING_EFFORT_PROVIDERS` and `create_tier_client`), `api_key_env.py` (with `"chatgpt": None`), `validators.py` (with `"chatgpt"` in `_ANY_MODEL_PROVIDERS`), `cli/prompts.py` (`_llm_provider_table`). Later tasks edit these versions.

- [ ] **Step 1: Confirm a clean tree and fetch**

Run: `git status --short && git fetch upstream && git rev-list --count HEAD..upstream/main`
Expected: no output from status; a count of 11 (or more if upstream moved).

- [ ] **Step 2: Merge**

Run: `git merge --no-edit upstream/main`
Expected: either a clean merge, or `CONFLICT` lines naming files among `pyproject.toml`, `tradingagents/default_config.py`, `tests/test_graph_end_to_end.py`.

- [ ] **Step 3: Resolve conflicts, keeping both sides**

For each conflicted file, open it and keep both upstream's additions and the fork's. Rules:
- `pyproject.toml`: keep the fork's extra dependencies (fastapi, jinja2, lxml, pyyaml, fred, etc.) and upstream's new version floors; the result must list each package once.
- `tradingagents/default_config.py`: keep every key both sides added to `_ENV_OVERRIDES` and `DEFAULT_CONFIG`.
- `tests/test_graph_end_to_end.py`: keep both sides' tests.
Then `git add` each resolved file.

- [ ] **Step 4: Install, lint, test**

Run: `uv sync 2>/dev/null || .venv/bin/pip install -q -e . && .venv/bin/ruff check . && .venv/bin/pytest -q 2>&1 | tail -3`
Expected: ruff clean; `N passed, 1 skipped` with no failures. If upstream's new tests fail because of the fork's behaviour (for example a changed prompt), fix the test to the fork's behaviour and note it in the commit body.

- [ ] **Step 5: Commit the merge**

```bash
git commit -m "chore: merge upstream v0.7.0 (ChatGPT-plan provider, memory scoring fix, CLI update check)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
(If Step 2 merged cleanly, `git merge` already committed; verify with `git log --oneline -1`.)

---

### Task 1: The chat model core: prompt rendering, flags, child environment, usage

**Files:**
- Create: `tradingagents/llm_clients/claude_code_client.py`
- Test: `tests/test_claude_code_client.py`

**Interfaces:**
- Produces:
  - `ChatClaudeCode(BaseChatModel)` with fields `model: str`, `timeout_seconds: float = 900`, `wait_minutes: float = 360`, `effort: str | None = None`, `tools: list = []`, `json_schema: dict | None = None`, `runner`, `clock`, `sleep` (injectable), and `set_context(company: str, trade_date: str, tool_vendors: dict | None = None) -> None`.
  - `render_prompt(messages: list[BaseMessage]) -> tuple[str, str]` (system text, prompt text).
  - `child_env(env: Mapping[str, str]) -> dict[str, str]`.
  - `run_cli(args: list[str], prompt: str, env: dict, cwd: str, timeout: float) -> dict` (the default runner).
  - `ClaudeCodeError(RuntimeError)`, `UsageLimitReached(ClaudeCodeError)`.
  - The runner is called as `runner(args, prompt, env, cwd, timeout)` and returns the CLI's result dict.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_code_client.py`:

```python
"""Agents on the Claude subscription: each call is a headless Claude Code session (no network)."""

import json
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tradingagents.llm_clients import claude_code_client as cc


def _ok(text="pong", **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, "num_turns": 1,
            "duration_ms": 1200, "total_cost_usd": 0.0398,
            "usage": {"input_tokens": 2, "output_tokens": 4, "cache_creation_input_tokens": 10,
                      "cache_read_input_tokens": 100}, **extra}


class Runner:
    """Records every CLI call and answers from a queue."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args, prompt, env, cwd, timeout):
        self.calls.append({"args": args, "prompt": prompt, "env": env, "cwd": cwd, "timeout": timeout})
        return self.results.pop(0)


def _model(runner, **kw):
    return cc.ChatClaudeCode(model="claude-opus-5-5", runner=runner, sleep=lambda s: None, **kw)


@pytest.mark.unit
def test_a_single_human_message_is_the_prompt_and_system_messages_the_system_prompt():
    system, prompt = cc.render_prompt([SystemMessage(content="Be terse."), HumanMessage(content="Rate ACME.")])
    assert (system, prompt) == ("Be terse.", "Rate ACME.")


@pytest.mark.unit
def test_a_history_is_written_out_as_a_transcript():
    _, prompt = cc.render_prompt([HumanMessage(content="Hi"), AIMessage(content="Hello"), HumanMessage(content="Go")])
    assert prompt == "Human: Hi\n\nAssistant: Hello\n\nHuman: Go"


@pytest.mark.unit
def test_block_content_renders_as_text():
    _, prompt = cc.render_prompt([HumanMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])])
    assert prompt == "a\nb"


@pytest.mark.unit
def test_a_plain_call_passes_the_verified_flags_and_returns_the_text_with_usage():
    runner = Runner(_ok())
    message = _model(runner).invoke([SystemMessage(content="Be terse."), HumanMessage(content="ping")])

    call = runner.calls[0]
    assert call["prompt"] == "ping"
    args = call["args"]
    assert args[:3] == ["-p", "--output-format", "json"]
    for flag, value in (("--model", "claude-opus-5-5"), ("--tools", ""), ("--setting-sources", ""),
                        ("--permission-mode", "dontAsk"), ("--system-prompt", "Be terse."), ("--max-turns", "4")):
        assert args[args.index(flag) + 1] == value
    assert "--no-session-persistence" in args
    assert "--bare" not in args and "--mcp-config" not in args and "--json-schema" not in args
    assert call["timeout"] == 900
    assert message.content == "pong"
    assert message.usage_metadata == {"input_tokens": 112, "output_tokens": 4, "total_tokens": 116}
    assert message.response_metadata["total_cost_usd"] == 0.0398
    assert message.response_metadata["model"] == "claude-opus-5-5"


@pytest.mark.unit
def test_effort_is_passed_when_set():
    runner = Runner(_ok())
    _model(runner, effort="high").invoke("ping")
    args = runner.calls[0]["args"]
    assert args[args.index("--effort") + 1] == "high"


@pytest.mark.unit
def test_the_child_environment_has_no_api_key_and_no_session_variables():
    env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-secret", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x",
           "CLAUDE_CODE_ENTRYPOINT": "cli", "FRED_API_KEY": "fred"}
    assert cc.child_env(env) == {"PATH": "/bin", "FRED_API_KEY": "fred"}


@pytest.mark.unit
def test_each_call_runs_with_a_scrubbed_environment_in_an_empty_directory(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    runner = Runner(_ok())
    _model(runner).invoke("ping")
    call = runner.calls[0]
    assert "ANTHROPIC_API_KEY" not in call["env"] and "CLAUDECODE" not in call["env"]
    assert os.path.isdir(call["cwd"]) and os.listdir(call["cwd"]) == []


@pytest.mark.unit
def test_run_cli_parses_the_json_even_when_the_exit_code_is_nonzero(monkeypatch):
    class Proc:
        returncode, stdout, stderr = 1, json.dumps(_ok("limit")), ""

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: Proc())
    assert cc.run_cli(["-p"], "x", {}, "/tmp", 5)["result"] == "limit"


@pytest.mark.unit
def test_run_cli_reports_a_missing_binary_and_a_timeout(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(cc.subprocess, "run", missing)
    with pytest.raises(cc.ClaudeCodeError, match="not found"):
        cc.run_cli(["-p"], "x", {}, "/tmp", 5)

    def slow(*a, **k):
        raise cc.subprocess.TimeoutExpired("claude", 5)

    monkeypatch.setattr(cc.subprocess, "run", slow)
    with pytest.raises(cc.ClaudeCodeError, match="timed out"):
        cc.run_cli(["-p"], "x", {}, "/tmp", 5)


@pytest.mark.unit
def test_run_cli_reports_output_that_is_not_json(monkeypatch):
    class Proc:
        returncode, stdout, stderr = 2, "", "boom"

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(cc.ClaudeCodeError, match="boom"):
        cc.run_cli(["-p"], "x", {}, "/tmp", 5)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py 2>&1 | tail -3`
Expected: `ImportError` / `ModuleNotFoundError: No module named 'tradingagents.llm_clients.claude_code_client'`.

- [ ] **Step 3: Write the module**

Create `tradingagents/llm_clients/claude_code_client.py`:

```python
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
USAGE_LIMIT_SIGNS = ("usage limit", "rate limit", "rate limited", "limit reached", "overloaded", "529")
_DROPPED_VARS = ("ANTHROPIC_API_KEY", "CLAUDECODE")
_DROPPED_PREFIXES = ("CLAUDE_CODE_",)
_PLAIN_TURNS, _TOOL_TURNS = 4, 30


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
    return result.get("api_error_status") == 429 or any(sign in text for sign in USAGE_LIMIT_SIGNS)


def mcp_config(tool_names: Sequence[str], context: Mapping[str, Any]) -> dict:
    """The inline ``--mcp-config``: this interpreter serving the named tools for the run's ticker and date."""
    env = {"TA_COMPANY": str(context.get("company", "")), "TA_TRADE_DATE": str(context.get("trade_date", "")),
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
                raise ClaudeCodeError(f"claude -p failed: {text[:300]}")
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
        proc = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30, check=False)
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
            raise ClaudeCodeError("Claude Code is not logged in: run `claude login` (or install the CLI) "
                                  "before using provider claude_code")
        fields = {k: v for k, v in self.kwargs.items() if k in self._FIELDS}
        return ChatClaudeCode(model=self.model, **fields)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py 2>&1 | tail -3`
Expected: all 10 pass. If `usage_metadata` comparison fails because LangChain adds keys, compare the three keys individually.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check --fix . && git add tradingagents/llm_clients/claude_code_client.py tests/test_claude_code_client.py && git commit -m "feat(llm): a chat model that runs each call as a headless Claude Code session

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Structured output through `--json-schema`

**Files:**
- Modify: `tradingagents/llm_clients/claude_code_client.py` (already has `with_structured_output`; this task proves it)
- Test: `tests/test_claude_code_client.py`

**Interfaces:**
- Consumes: `ChatClaudeCode.with_structured_output(schema)` from Task 1.
- Produces: a runnable returning an instance of `schema`, raising `ValueError` when the CLI returned no `structured_output`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_claude_code_client.py`)

```python
from pydantic import BaseModel


class Verdict(BaseModel):
    rating: str
    price_target: float


@pytest.mark.unit
def test_a_structured_call_passes_the_schema_and_returns_the_model():
    runner = Runner(_ok('{"rating":"Hold","price_target":87.5}', structured_output={"rating": "Hold", "price_target": 87.5}))
    verdict = _model(runner).with_structured_output(Verdict).invoke("Rate ACME")
    assert verdict == Verdict(rating="Hold", price_target=87.5)
    args = runner.calls[0]["args"]
    assert json.loads(args[args.index("--json-schema") + 1])["properties"].keys() == {"rating", "price_target"}


@pytest.mark.unit
def test_missing_structured_output_raises():
    runner = Runner(_ok("Hold, target 87.5"))
    with pytest.raises(ValueError, match="structured_output"):
        _model(runner).with_structured_output(Verdict).invoke("Rate ACME")


@pytest.mark.unit
def test_the_structured_fallback_helper_falls_back_to_free_text():
    from tradingagents.agents.structured import bind_structured, invoke_structured_or_freetext

    runner = Runner(_ok("no json here"), _ok("Free text answer"))
    llm = _model(runner)
    structured = bind_structured(llm, Verdict, "test")
    text = invoke_structured_or_freetext(structured, llm, "Rate ACME", lambda v: f"{v.rating} {v.price_target}", "test")
    assert text == "Free text answer"
    assert len(runner.calls) == 2 and "--json-schema" not in runner.calls[1]["args"]


@pytest.mark.unit
def test_a_bound_copy_sees_a_context_set_later_on_the_original():
    runner = Runner(_ok())
    llm = _model(runner)
    structured = llm.with_structured_output(Verdict)      # bound at graph build time
    llm.set_context("HRB", "2026-10-22", {"get_cashflow": "yfinance"})
    bound = structured.first if hasattr(structured, "first") else structured.steps[0]
    assert bound._context == {"company": "HRB", "trade_date": "2026-10-22", "tool_vendors": {"get_cashflow": "yfinance"}}
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py 2>&1 | tail -3`
Expected: the first three pass already (Task 1 wrote the code); the fourth may fail if `RunnableSequence` exposes the first step under another name. If so, change the test to `structured.steps[0]` only. All four must pass before Step 3.

- [ ] **Step 3: Commit**

```bash
.venv/bin/ruff check --fix . && git add tests/test_claude_code_client.py && git commit -m "test(llm): structured output through claude -p --json-schema, with the free-text fallback

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Tool calls: the MCP config and the allow-list

**Files:**
- Modify: `tradingagents/llm_clients/claude_code_client.py` (already has `bind_tools`, `mcp_config`; this task proves them)
- Test: `tests/test_claude_code_client.py`

**Interfaces:**
- Consumes: `mcp_config(tool_names, context)`, `ChatClaudeCode.bind_tools(tools)` from Task 1.
- Produces: the contract Task 6's server relies on: env keys `TA_COMPANY`, `TA_TRADE_DATE`, `TA_TOOLS` (comma-separated), `TA_TOOL_VENDORS` (JSON object); command `sys.executable -m tradingagents.mcp_server`.

- [ ] **Step 1: Write the failing tests** (append)

```python
import sys

from tradingagents.agents import tools as toolbox


@pytest.mark.unit
def test_a_tool_call_serves_the_named_tools_over_mcp_for_the_run_s_ticker_and_date():
    runner = Runner(_ok("Fundamentals report"))
    llm = _model(runner)
    llm.set_context("HRB", "2026-10-22", {"get_cashflow": "yfinance"})
    message = llm.bind_tools([toolbox.get_value_metrics, toolbox.get_annual_report]).invoke(
        [SystemMessage(content="You are the fundamentals analyst."), HumanMessage(content="Report on HRB.")])

    assert message.content == "Fundamentals report" and message.tool_calls == []
    args = runner.calls[0]["args"]
    assert args[args.index("--max-turns") + 1] == "30"
    assert "--strict-mcp-config" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__ta__get_value_metrics,mcp__ta__get_annual_report"
    server = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["ta"]
    assert server["command"] == sys.executable and server["args"] == ["-m", "tradingagents.mcp_server"]
    assert server["env"] == {"TA_COMPANY": "HRB", "TA_TRADE_DATE": "2026-10-22",
                             "TA_TOOLS": "get_value_metrics,get_annual_report",
                             "TA_TOOL_VENDORS": '{"get_cashflow": "yfinance"}'}


@pytest.mark.unit
def test_the_analyst_turn_ends_after_one_round_with_the_report():
    """turn.take_turn treats a reply without tool_calls as the finished report."""
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    from tradingagents.agents.analysts.turn import take_turn

    runner = Runner(_ok("The report."))
    prompt = ChatPromptTemplate.from_messages([("system", "Analyst. Tools: {tool_names}"), MessagesPlaceholder("messages")])
    prompt = prompt.partial(tool_names="get_value_metrics")
    result, report = take_turn(prompt, _model(runner), [toolbox.get_value_metrics], [HumanMessage(content="HRB")])
    assert report == "The report." and result.tool_calls == []
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py 2>&1 | tail -3`
Expected: both pass with Task 1's code. If `take_turn`'s signature differs from `(prompt, llm, tools, messages)`, read `tradingagents/agents/analysts/turn.py` and adapt the test, not the code.

- [ ] **Step 3: Commit**

```bash
git add tests/test_claude_code_client.py && git commit -m "test(llm): analysts' tools reach a headless session over a local MCP server

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Errors: usage-limit wait, plain errors, login check

**Files:**
- Modify: `tradingagents/llm_clients/claude_code_client.py` (code exists from Task 1; this task proves it and fixes what the tests find)
- Test: `tests/test_claude_code_client.py`

**Interfaces:**
- Consumes: `ChatClaudeCode._run`, `ClaudeCodeClient.get_llm`, `UsageLimitReached`, `ClaudeCodeError`, `usage_limited`.
- Produces: `ClaudeCodeClient(model, **kwargs)` honours kwargs `auth_check` (callable returning a dict with `loggedIn`), `runner`, `wait_minutes`, `timeout_seconds`, `effort`, `callbacks`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _limited(text="You've hit your usage limit · resets 3pm"):
    return {"type": "result", "subtype": "error", "is_error": True, "result": text, "usage": {}}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.unit
def test_a_usage_limit_is_waited_out_and_the_same_call_retried():
    clock = Clock()
    runner = Runner(_limited(), _limited(), _ok("pong"))
    llm = cc.ChatClaudeCode(model="opus", runner=runner, clock=clock, sleep=clock.sleep, wait_minutes=60)
    assert llm.invoke("ping").content == "pong"
    assert len(runner.calls) == 3 and clock.now == 2 * cc.RETRY_SECONDS
    assert runner.calls[0]["prompt"] == runner.calls[2]["prompt"]


@pytest.mark.unit
def test_the_wait_gives_up_after_wait_minutes():
    clock = Clock()
    runner = Runner(*[_limited()] * 10)
    llm = cc.ChatClaudeCode(model="opus", runner=runner, clock=clock, sleep=clock.sleep, wait_minutes=25)
    with pytest.raises(cc.UsageLimitReached):
        llm.invoke("ping")
    assert len(runner.calls) == 3          # 0, 10 and 20 minutes; a fourth would pass 25


@pytest.mark.unit
def test_wait_minutes_zero_fails_at_once():
    runner = Runner(_limited())
    with pytest.raises(cc.UsageLimitReached):
        _model(runner, wait_minutes=0).invoke("ping")
    assert len(runner.calls) == 1


@pytest.mark.unit
def test_a_plain_error_is_raised_without_waiting():
    runner = Runner({"is_error": True, "result": "Not logged in · Please run /login"})
    with pytest.raises(cc.ClaudeCodeError, match="Not logged in"):
        _model(runner).invoke("ping")
    assert len(runner.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize("result,limited", [
    ({"result": "usage limit reached"}, True),
    ({"result": "Rate limited, try later"}, True),
    ({"result": "x", "api_error_status": 429}, True),
    ({"result": "Not logged in"}, False),
    ({"result": "Invalid API key"}, False),
])
def test_what_counts_as_a_usage_limit(result, limited):
    assert cc.usage_limited(result) is limited


@pytest.mark.unit
def test_the_client_refuses_to_build_a_model_when_not_logged_in():
    client = cc.ClaudeCodeClient("opus", auth_check=lambda: {"loggedIn": False})
    with pytest.raises(cc.ClaudeCodeError, match="claude login"):
        client.get_llm()


@pytest.mark.unit
def test_the_client_builds_a_model_with_its_settings():
    runner = Runner(_ok())
    client = cc.ClaudeCodeClient("claude-fable-5-1", auth_check=lambda: {"loggedIn": True}, runner=runner,
                                 wait_minutes=5, timeout_seconds=30, effort="high", callbacks=[])
    llm = client.get_llm()
    assert (llm.model, llm.wait_minutes, llm.timeout_seconds, llm.effort) == ("claude-fable-5-1", 5, 30, "high")
    assert llm.invoke("ping").content == "pong"
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py 2>&1 | tail -5`
Expected: all pass with Task 1's code. If `test_the_wait_gives_up_after_wait_minutes` counts 4 calls, the deadline check is off by one: it must be `clock() + RETRY_SECONDS > deadline` evaluated after a failed call, before sleeping.

- [ ] **Step 3: Commit**

```bash
git add tests/test_claude_code_client.py tradingagents/llm_clients/claude_code_client.py && git commit -m "test(llm): usage-limit waits, plain errors and the login check for provider claude_code

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Register the provider: config, factory, validators, key map, CLI menu, report header

**Files:**
- Modify: `tradingagents/default_config.py` (`_ENV_OVERRIDES`, `DEFAULT_CONFIG`)
- Modify: `tradingagents/llm_clients/factory.py` (`create_llm_client`, `build_llm_kwargs`, `create_tier_client`)
- Modify: `tradingagents/llm_clients/validators.py` (`_ANY_MODEL_PROVIDERS`)
- Modify: `tradingagents/llm_clients/api_key_env.py` (`PROVIDER_API_KEY_ENV`)
- Modify: `cli/prompts.py` (`_llm_provider_table`)
- Modify: `tradingagents/reporting.py` (`_header`)
- Test: `tests/test_claude_code_provider.py`

**Interfaces:**
- Consumes: `ClaudeCodeClient` from Task 1.
- Produces: config keys `claude_code_deep_model`, `claude_code_quick_model`, `claude_code_wait_minutes` (int), `claude_code_timeout_seconds` (int); env names `TRADINGAGENTS_CLAUDE_CODE_DEEP_MODEL`, `..._QUICK_MODEL`, `..._WAIT_MINUTES`, `..._TIMEOUT_SECONDS`; `create_tier_client(config, tier)` returns a `ClaudeCodeClient` with model from the `claude_code_<tier>_model` key.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_code_provider.py`:

```python
"""Provider claude_code is registered like any other, with its own model pair and no API key."""

import pytest

from tradingagents import default_config
from tradingagents.llm_clients import factory
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.claude_code_client import ClaudeCodeClient
from tradingagents.llm_clients.validators import validate_model


def _config(**overrides):
    config = {**default_config.DEFAULT_CONFIG, "llm_provider": "claude_code",
              "quick_think_llm": "claude-sonnet-5-5", "deep_think_llm": "claude-opus-5-5"}
    config.update(overrides)
    return config


@pytest.mark.unit
def test_defaults_and_env_overrides():
    cfg = default_config.DEFAULT_CONFIG
    assert cfg["claude_code_deep_model"] == "claude-fable-5-1"
    assert cfg["claude_code_quick_model"] == "claude-opus-5-5"
    assert cfg["claude_code_wait_minutes"] == 360 and cfg["claude_code_timeout_seconds"] == 900
    for env, key in (("TRADINGAGENTS_CLAUDE_CODE_DEEP_MODEL", "claude_code_deep_model"),
                     ("TRADINGAGENTS_CLAUDE_CODE_QUICK_MODEL", "claude_code_quick_model"),
                     ("TRADINGAGENTS_CLAUDE_CODE_WAIT_MINUTES", "claude_code_wait_minutes"),
                     ("TRADINGAGENTS_CLAUDE_CODE_TIMEOUT_SECONDS", "claude_code_timeout_seconds")):
        assert default_config._ENV_OVERRIDES[env] == key


@pytest.mark.unit
def test_the_factory_builds_the_client_and_needs_no_key():
    client = factory.create_llm_client("claude_code", "opus", auth_check=lambda: {"loggedIn": True})
    assert isinstance(client, ClaudeCodeClient)
    assert get_api_key_env("claude_code") is None
    assert validate_model("claude_code", "anything") is True


@pytest.mark.unit
def test_each_tier_uses_the_claude_code_model_pair_not_the_api_pair(monkeypatch):
    made = []
    monkeypatch.setattr(factory, "create_llm_client",
                        lambda provider, model, base_url=None, **kw: made.append((provider, model, kw)))
    factory.create_tier_client(_config(anthropic_effort="high"), "deep")
    factory.create_tier_client(_config(claude_code_quick_model="sonnet"), "quick")
    assert made[0][:2] == ("claude_code", "claude-fable-5-1")
    assert made[1][:2] == ("claude_code", "sonnet")
    kw = made[0][2]
    assert (kw["effort"], kw["wait_minutes"], kw["timeout_seconds"]) == ("high", 360.0, 900.0)
    assert "temperature" not in kw and "max_tokens" not in kw and "max_retries" not in kw


@pytest.mark.unit
def test_the_api_tiers_are_untouched(monkeypatch):
    made = []
    monkeypatch.setattr(factory, "create_llm_client",
                        lambda provider, model, base_url=None, **kw: made.append((provider, model)))
    factory.create_tier_client(_config(llm_provider="anthropic"), "deep")
    assert made[0] == ("anthropic", "claude-opus-5-5")


@pytest.mark.unit
def test_the_cli_menu_lists_the_provider():
    from cli.prompts import _llm_provider_table

    assert ("Claude Code (your Claude plan)", "claude_code", None) in _llm_provider_table()


@pytest.mark.unit
def test_the_report_header_names_the_claude_code_models():
    from tradingagents.reporting import _header

    settings = {**_config(), "version": "0.7.0", "analysts": ["market"]}
    text = _header("HRB", {}, settings)
    assert "claude_code, deep claude-fable-5-1, quick claude-opus-5-5" in text
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest -q tests/test_claude_code_provider.py 2>&1 | tail -3`
Expected: 6 failures (`KeyError: 'claude_code_deep_model'`, `ValueError: Unsupported LLM provider`, etc.).

- [ ] **Step 3: Config keys**

In `tradingagents/default_config.py`, add to `_ENV_OVERRIDES` after the `TRADINGAGENTS_ANTHROPIC_EFFORT` row:

```python
    # Provider claude_code: the agents run through the Claude Code CLI on your plan.
    "TRADINGAGENTS_CLAUDE_CODE_DEEP_MODEL":      "claude_code_deep_model",
    "TRADINGAGENTS_CLAUDE_CODE_QUICK_MODEL":     "claude_code_quick_model",
    "TRADINGAGENTS_CLAUDE_CODE_WAIT_MINUTES":    "claude_code_wait_minutes",
    "TRADINGAGENTS_CLAUDE_CODE_TIMEOUT_SECONDS": "claude_code_timeout_seconds",
```

and to `DEFAULT_CONFIG` after `"anthropic_effort": None,`:

```python
        # Provider claude_code (the Claude Code CLI on your subscription) has its own
        # model pair, so switching provider never changes the API pair above. On the
        # plan the models cost nothing extra: the strongest for the managers.
        "claude_code_deep_model": "claude-fable-5-1",
        "claude_code_quick_model": "claude-opus-5-5",
        # When the plan's usage limit is hit, a call retries every 10 minutes for this
        # long before failing; 0 fails at once. Timeout per call, in seconds.
        "claude_code_wait_minutes": 360,
        "claude_code_timeout_seconds": 900,
```

- [ ] **Step 4: Factory**

In `tradingagents/llm_clients/factory.py`, in `create_llm_client` before the `from .openai_client import ...` line:

```python
    if provider_lower == "claude_code":
        from .claude_code_client import ClaudeCodeClient
        return ClaudeCodeClient(model, base_url, **kwargs)
```

In `build_llm_kwargs`, after the `elif provider == "anthropic":` block:

```python
    elif provider == "claude_code":
        effort = config.get("anthropic_effort")
        if effort:
            kwargs["effort"] = effort
        kwargs["wait_minutes"] = float(config["claude_code_wait_minutes"])
        kwargs["timeout_seconds"] = float(config["claude_code_timeout_seconds"])
        return kwargs       # the CLI takes no temperature, retry budget or token cap
```

In `create_tier_client`, replace the final `return` with:

```python
    model = config[f"{tier}_think_llm"]
    if provider == "claude_code":
        model = config.get(f"claude_code_{tier}_model") or model
    return create_llm_client(provider=provider, model=model, base_url=base_url, **kwargs, **extra)
```

- [ ] **Step 5: Validators, key map, CLI menu, report header**

`tradingagents/llm_clients/validators.py`: add `"claude_code"` to `_ANY_MODEL_PROVIDERS`.

`tradingagents/llm_clients/api_key_env.py`: after the `"chatgpt": None,` row add:

```python
    # Claude Code runs on the CLI's own login, not a key.
    "claude_code": None,
```

`cli/prompts.py`, in `_llm_provider_table()` after the Anthropic row:

```python
        ("Claude Code (your Claude plan)", "claude_code", None),
```

`tradingagents/reporting.py`, in `_header`, replace the two lines that build `models` so each tier's model name comes from a helper:

```python
        def model_of(tier, provider):
            return s(f"claude_code_{tier}_model", "?") if provider == "claude_code" else s(f"{tier}_think_llm", "?")

        if deep_provider == quick_provider:
            models = f"{deep_provider}, deep {model_of('deep', deep_provider)}, quick {model_of('quick', quick_provider)}"
        else:   # each tier on its own provider (#1440)
            models = (f"deep {deep_provider} {model_of('deep', deep_provider)}, "
                      f"quick {quick_provider} {model_of('quick', quick_provider)}")
```

- [ ] **Step 6: Run the new tests, then the whole suite**

Run: `.venv/bin/pytest -q tests/test_claude_code_provider.py 2>&1 | tail -3 && .venv/bin/pytest -q 2>&1 | tail -3`
Expected: 6 pass; whole suite green. A CLI test that counts the provider menu entries needs its count raised by one.

- [ ] **Step 7: Commit**

```bash
.venv/bin/ruff check --fix . && git add -A tradingagents cli tests && git commit -m "feat(llm): provider claude_code, with its own model pair and no API key

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: The MCP server that serves the data tools

**Files:**
- Create: `tradingagents/mcp_server.py`
- Modify: `pyproject.toml` (dependency `mcp>=2,<3`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: env contract from Task 3 (`TA_COMPANY`, `TA_TRADE_DATE`, `TA_TOOLS`, `TA_TOOL_VENDORS`).
- Produces: `injected_fields(tool) -> dict[str, str]`, `exposed(tool, state: dict) -> Callable`, `build_server(names: list[str], state: dict, tool_vendors: dict | None = None) -> MCPServer`, `main()`; the module runs as `python -m tradingagents.mcp_server`.

- [ ] **Step 1: Add the dependency and install it**

In `pyproject.toml` `dependencies`, after `"markdown-it-py>=4.0",` add `"mcp>=2,<3",`.

Run: `uv sync 2>/dev/null || .venv/bin/pip install -q -e . ; .venv/bin/python -c "from mcp.server.mcpserver import MCPServer; print('mcp ok')"`
Expected: `mcp ok`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
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
```

- [ ] **Step 3: Run them**

Run: `.venv/bin/pytest -q tests/test_mcp_server.py 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'tradingagents.mcp_server'`.

- [ ] **Step 4: Write the server**

Create `tradingagents/mcp_server.py`:

```python
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
        args = {**kwargs, **{name: state.get(key, "") for name, key in injected.items()}}
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
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest -q tests/test_mcp_server.py 2>&1 | tail -3`
Expected: 6 pass. If `input_schema` still lists an injected parameter, `tool_call_schema` did not drop it: build `fields` from `tool.args_schema.model_fields` minus `injected` instead.

- [ ] **Step 6: Prove the server starts as a process**

Run:
```bash
cd /Users/syedusman/Developer/ValueTrading && TA_COMPANY=HRB TA_TRADE_DATE=2026-10-22 TA_TOOLS=get_value_metrics .venv/bin/python - <<'EOF'
import json, subprocess, sys, os
p = subprocess.Popen([sys.executable, "-m", "tradingagents.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
try:
    out, err = p.communicate(json.dumps(init) + "\n", timeout=20)
except subprocess.TimeoutExpired:
    p.kill(); out, err = p.communicate()
print("OK" if '"serverInfo"' in out else "FAILED", err[-300:])
EOF
```
Expected: `OK`.

- [ ] **Step 7: Clean-install smoke test, lint, commit**

Run: `tmp=$(mktemp -d) && python3 -m venv "$tmp/venv" && "$tmp/venv/bin/pip" install -q . && (cd "$tmp" && "$tmp/venv/bin/python" -c "import tradingagents, cli.main, tradingagents.mcp_server") ; rm -rf "$tmp"`
Expected: no output (imports succeed).

```bash
.venv/bin/ruff check --fix . && git add pyproject.toml uv.lock tradingagents/mcp_server.py tests/test_mcp_server.py && git commit -m "feat(llm): serve the analysts' data tools to Claude Code over a local MCP server

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
(`uv.lock` only if it changed.)

---

### Task 7: The graph tells the model which run it is

**Files:**
- Modify: `tradingagents/graph/trading_graph.py` (`_run_graph`)
- Test: `tests/test_claude_code_client.py`

**Interfaces:**
- Consumes: `ChatClaudeCode.set_context(company, trade_date, tool_vendors)`.
- Produces: `TradingAgentsGraph._run_graph` calls `set_context` on `self.quick_thinking_llm` and `self.deep_thinking_llm` when they have it, before the graph runs.

- [ ] **Step 1: Write the failing test** (append to `tests/test_claude_code_client.py`)

```python
@pytest.mark.unit
def test_the_graph_passes_the_ticker_date_and_vendors_to_the_models(monkeypatch):
    from tradingagents.graph import trading_graph

    told = []

    class Model:
        def set_context(self, company, trade_date, tool_vendors=None):
            told.append((company, trade_date, tool_vendors))

    graph = trading_graph.TradingAgentsGraph.__new__(trading_graph.TradingAgentsGraph)
    graph.quick_thinking_llm, graph.deep_thinking_llm = Model(), object()
    graph.config = {"tool_vendors": {"get_cashflow": "yfinance"}}
    graph.tell_models("HRB", "2026-10-22")
    assert told == [("HRB", "2026-10-22", {"get_cashflow": "yfinance"})]
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py -k tell_models 2>&1 | tail -3`
Expected: `AttributeError: ... has no attribute 'tell_models'`.

- [ ] **Step 3: Add the hook**

In `tradingagents/graph/trading_graph.py`, add a method to `TradingAgentsGraph` just above `_run_graph`:

```python
    def tell_models(self, company_name: str, trade_date: str) -> None:
        """A model that serves tools itself (provider claude_code) needs the run's ticker,
        date and vendors; the API models have no such hook and are left alone."""
        for llm in (self.quick_thinking_llm, self.deep_thinking_llm):
            set_context = getattr(llm, "set_context", None)
            if callable(set_context):
                set_context(company_name, trade_date, self.config.get("tool_vendors"))
```

and in `_run_graph`, as its first statement before `init_agent_state = ...`:

```python
        self.tell_models(company_name, trade_date)
```

- [ ] **Step 4: Run the test and the suite**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py tests/test_graph_end_to_end.py 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/graph/trading_graph.py tests/test_claude_code_client.py && git commit -m "feat(graph): tell a tool-serving model which ticker and date it is analysing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Measured usage in the review, the ACTIONS line and results.json

**Files:**
- Create: `tradingagents/llm_clients/usage.py`
- Modify: `value_pipeline/review.py` (`RunResult`, `make_graph`, `run_agents`, `_run`)
- Modify: `value_pipeline/render.py` (`actions_markdown` cost line)
- Test: `tests/test_claude_code_client.py` (tally), `tests/test_value_pipeline.py` (render and results)

**Interfaces:**
- Consumes: `AIMessage.usage_metadata` and `response_metadata["total_cost_usd"]` from Task 1; `TradingAgentsGraph(..., callbacks=[...])`.
- Produces: `UsageTally()` with `reset()`, `snapshot() -> {"calls", "tokens_in", "tokens_out", "nominal_cost_usd"}`; `RunResult` fields `tokens_in: int = 0`, `tokens_out: int = 0`, `nominal_cost_usd: float = 0.0`; `make_graph(analysts, statement_vendor=None, tally=None)`; `run_agents(ta, ticker, day, portfolio_ctx, out_dir, tally=None)`; `render.actions_markdown(..., usage: dict | None = None)` where `usage = {"tokens_in", "tokens_out", "nominal_cost_usd", "on_plan": bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_code_client.py`:

```python
@pytest.mark.unit
def test_the_usage_tally_sums_tokens_and_nominal_cost_across_calls():
    from tradingagents.llm_clients.usage import UsageTally

    tally = UsageTally()
    runner = Runner(_ok(), _ok())
    llm = cc.ChatClaudeCode(model="opus", runner=runner, sleep=lambda s: None, callbacks=[tally])
    llm.invoke("a")
    llm.invoke("b")
    assert tally.snapshot() == {"calls": 2, "tokens_in": 224, "tokens_out": 8, "nominal_cost_usd": pytest.approx(0.0796)}
    tally.reset()
    assert tally.snapshot()["calls"] == 0
```

Append to `tests/test_value_pipeline.py`, next to `test_unreviewed_holdings_above_the_cap_are_named_in_the_summary` (around line 541), which already imports `plan`, `_new` and `SIZING`; the summary comes from `plan()` the same way:

```python
@pytest.mark.unit
def test_the_cost_line_shows_measured_usage_and_says_who_is_billed():
    _, summary = plan([_new("Hold")], cash=1_000.0, sizing=SIZING)
    plan_usage = {"tokens_in": 120000, "tokens_out": 8000, "nominal_cost_usd": 12.4, "on_plan": True}
    md = render.actions_markdown("2026-11-07", [], summary, reports={}, comparison=None, cost=4.0, failures=[],
                                 dry_run=False, usage=plan_usage)
    assert "- Cost of this review: ~$12.40 nominal (120,000 tokens in, 8,000 out; billed to your Claude plan, not the API)" in md

    api_usage = {**plan_usage, "on_plan": False}
    md = render.actions_markdown("2026-11-07", [], summary, reports={}, comparison=None, cost=4.0, failures=[],
                                 dry_run=False, usage=api_usage)
    assert "- Cost of this review: ~$4.00 (estimate; 120,000 tokens in, 8,000 out)" in md

    md = render.actions_markdown("2026-11-07", [], summary, reports={}, comparison=None, cost=4.0, failures=[], dry_run=False)
    assert "- Cost of this review: ~$4.00\n" in md


@pytest.mark.unit
def test_run_agents_records_the_tally_for_the_ticker(tmp_path):
    from value_pipeline import review

    class Tally:
        def __init__(self):
            self.resets = 0

        def reset(self):
            self.resets += 1

        def snapshot(self):
            return {"calls": 3, "tokens_in": 10, "tokens_out": 5, "nominal_cost_usd": 0.5}

    class Graph:
        def propagate(self, ticker, day, portfolio=None):
            return {"final_trade_decision": "**Rating**: Hold"}, "Hold"

        def save_reports(self, state, ticker, save_path, html):
            pass

    tally = Tally()
    r = review.run_agents(Graph(), "HRB", "2026-10-22", None, tmp_path, tally)
    assert (r.tokens_in, r.tokens_out, r.nominal_cost_usd, tally.resets) == (10, 5, 0.5, 1)
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest -q tests/test_claude_code_client.py -k tally; .venv/bin/pytest -q tests/test_value_pipeline.py -k "cost_line or records_the_tally" 2>&1 | tail -3`
Expected: `ModuleNotFoundError: tradingagents.llm_clients.usage`; `TypeError: unexpected keyword argument 'usage'`; `TypeError: run_agents() takes 5 positional arguments`.

- [ ] **Step 3: The tally**

Create `tradingagents/llm_clients/usage.py`:

```python
"""What a run spent: tokens from every model reply, and the nominal API-price cost a
Claude Code session reports. The CLI's stats handler shows the same for the terminal."""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


class UsageTally(BaseCallbackHandler):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.calls, self.tokens_in, self.tokens_out, self.nominal_cost_usd = 0, 0, 0, 0.0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage):
                    continue
                usage = message.usage_metadata or {}
                with self._lock:
                    self.calls += 1
                    self.tokens_in += int(usage.get("input_tokens", 0))
                    self.tokens_out += int(usage.get("output_tokens", 0))
                    self.nominal_cost_usd += float(message.response_metadata.get("total_cost_usd") or 0.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                    "nominal_cost_usd": self.nominal_cost_usd}
```

- [ ] **Step 4: The review**

In `value_pipeline/review.py`:

`RunResult` gains, after `error: str | None = None`:

```python
    tokens_in: int = 0
    tokens_out: int = 0
    nominal_cost_usd: float = 0.0   # what the calls would have cost at API prices (measured, not the estimate)
```

`make_graph`:

```python
def make_graph(analysts: list[str], statement_vendor: str | None = None, tally=None):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(selected_analysts=tuple(analysts), config=graph_config(statement_vendor),
                              callbacks=[tally] if tally else None)
```

`run_agents` gains a `tally=None` parameter; inside the `try`, before `ta.propagate`, add `if tally: tally.reset()`, and build the success result as:

```python
            used = tally.snapshot() if tally else {}
            return RunResult(ticker, "REVIEW" if is_review(rating) else rating,
                             final_state.get("final_trade_decision", ""), f"{ticker}/complete_report.html",
                             tokens_in=used.get("tokens_in", 0), tokens_out=used.get("tokens_out", 0),
                             nominal_cost_usd=used.get("nominal_cost_usd", 0.0))
```

In `_run`, replace the agent block with:

```python
    usage = None
    if dry_run:
        results = [fake_run(t, i, t in owned, prices[t]) for i, t in enumerate(tickers)]
    else:
        from tradingagents.llm_clients.usage import UsageTally

        logging.getLogger("tradingagents.llm_clients.claude_code_client").setLevel(logging.INFO)
        if not logging.getLogger().handlers:
            logging.basicConfig(format="%(levelname)s %(message)s")
        tally = UsageTally()
        ta = make_graph(rcfg["analysts"], rcfg.get("statement_vendor"), tally)
        print("Scoring past decisions (memory log)...")
        ta.settle_all_pending()
        ctx = pf.to_context()
        for i, t in enumerate(tickers, 1):
            print(f"[{i}/{len(tickers)}] {t}...", flush=True)
            results.append(run_agents(ta, t, today, ctx, out_dir, tally))
        usage = {"tokens_in": sum(r.tokens_in for r in results), "tokens_out": sum(r.tokens_out for r in results),
                 "nominal_cost_usd": sum(r.nominal_cost_usd for r in results),
                 "on_plan": str(ta.config.get("llm_provider", "")).lower() == "claude_code"}
```

(`import logging` at the top of the module if missing.) Pass `usage=usage` to `render.actions_markdown(...)`, and add to each results.json row:

```python
         "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "nominal_cost_usd": round(r.nominal_cost_usd, 4),
```

- [ ] **Step 5: The cost line**

In `value_pipeline/render.py`, `actions_markdown` gains a keyword parameter `usage: dict | None = None`, and the cost line becomes:

```python
    if usage and usage.get("on_plan"):
        lines.append(f"- Cost of this review: ~{_money(usage['nominal_cost_usd'])} nominal "
                     f"({usage['tokens_in']:,} tokens in, {usage['tokens_out']:,} out; "
                     "billed to your Claude plan, not the API)")
    elif usage:
        lines.append(f"- Cost of this review: ~{_money(cost)} (estimate; {usage['tokens_in']:,} tokens in, "
                     f"{usage['tokens_out']:,} out)")
    else:
        lines.append(f"- Cost of this review: ~{_money(cost)}")
```

- [ ] **Step 6: Run the tests and the suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: green. `tests/test_value_web.py` still parses `$12.40` from the new line (the regex stops at the first non-money character).

- [ ] **Step 7: Commit**

```bash
.venv/bin/ruff check --fix . && git add tradingagents/llm_clients/usage.py value_pipeline/review.py value_pipeline/render.py tests && git commit -m "feat(value_pipeline): record measured token usage and the nominal cost of a review

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: The `/review` skill and the docs

**Files:**
- Create: `.claude/skills/review/SKILL.md`
- Modify: `.env.example`, `README.md`, `value_pipeline/README.md`, `VALUE_INVESTING.md`

**Interfaces:**
- Consumes: `python -m value_pipeline review --tickers A,B --yes` (existing CLI), the review lock file `value_pipeline/data/.review.lock`, the log lines from Task 1.

- [ ] **Step 1: The skill**

Create `.claude/skills/review/SKILL.md`:

```markdown
---
name: review
description: Run the value_pipeline agent review for one or more tickers from inside Claude Code, relay each agent as it finishes, then summarise the rating, price target, margin of safety and planned action. Nothing is traded.
disable-model-invocation: true
---

Run the agent review for the tickers in `$ARGUMENTS` (comma or space separated, upper-cased).
The agents run on the Claude plan through headless sessions when `.env` sets
`TRADINGAGENTS_LLM_PROVIDER=claude_code`; you watch agents complete, not the model think.

1. From the repo root, refuse if a review is already running:
   `.venv/bin/python -c "from value_pipeline import lock; from value_pipeline.config import Paths; import pathlib; print(lock.held(Paths(pathlib.Path('value_pipeline/data')), 'review'))"`
   prints `True` → say the scheduled run or the website holds the lock and stop.
2. Start it in the background, logging to `value_pipeline/data/logs/review_<today>.log`:
   `.venv/bin/python -m value_pipeline review --tickers <TICKERS> --yes >> value_pipeline/data/logs/review_<today>.log 2>&1`
3. While it runs, read the log every minute or two and relay the new lines that matter:
   `[n/N] TICKER...`, `INFO claude-...: 41s, 23,000 tokens in, 900 out` (one per finished agent call),
   `WARNING usage limit reached ... retrying in 10 minutes`, and any `failed` line.
   Do not paste the whole log.
4. When the process exits, read `value_pipeline/data/reports/<latest folder>/ACTIONS.md` and, for each
   ticker, `<folder>/<TICKER>/complete_report.md`. Report per ticker: rating, price target, margin of
   safety, the planned action and its reason, and the report path. Then stop and let the user ask.
5. If the run failed, show the last 20 log lines and the likely cause (not logged in → `claude login`;
   usage limit → when it resets; a vendor error → which tool).
```

- [ ] **Step 2: `.env.example`**

After the ChatGPT-plan lines (around line 38–39) add:

```
# Claude plan (Pro or Max): no key; the agents run through the Claude Code CLI you are logged into.
# Its own model pair, so the API pair below stays as it is. Runs count toward the plan's usage limit.
#TRADINGAGENTS_LLM_PROVIDER=claude_code
#TRADINGAGENTS_CLAUDE_CODE_DEEP_MODEL=claude-fable-5-1
#TRADINGAGENTS_CLAUDE_CODE_QUICK_MODEL=claude-opus-5-5
#TRADINGAGENTS_CLAUDE_CODE_WAIT_MINUTES=360
```

- [ ] **Step 3: `README.md`**

After the paragraph that begins "To run on your ChatGPT Plus or Pro plan" add:

```markdown
To run on your Claude plan instead of an API key, install [Claude Code](https://claude.com/claude-code), run `claude login` once, and set `TRADINGAGENTS_LLM_PROVIDER=claude_code`. Each agent call is then a headless Claude Code session: the analysts get their data tools over a local MCP server, the structured agents keep their schemas, and the API key is never passed to the session. The provider has its own model pair (`TRADINGAGENTS_CLAUDE_CODE_DEEP_MODEL`, `TRADINGAGENTS_CLAUDE_CODE_QUICK_MODEL`, Fable and Opus by default) so the API pair is untouched. Runs count toward the plan's usage limit; when it is hit, a call waits and retries every 10 minutes for up to `TRADINGAGENTS_CLAUDE_CODE_WAIT_MINUTES`. `temperature`, `max_tokens` and `llm_max_retries` do not apply.
```

Also add "and your own Claude plan through Claude Code" to the sentence under "Implementation Details" that lists providers.

- [ ] **Step 4: `value_pipeline/README.md` and `VALUE_INVESTING.md`**

In `value_pipeline/README.md`, after the sentence about `cost_per_run` and `max_cost_per_review` (item 3 of the review steps), add:

```markdown
   With `TRADINGAGENTS_LLM_PROVIDER=claude_code` the agents run on your Claude plan through Claude Code, so the cap is a usage budget rather than a bill; the ACTIONS report then shows the measured tokens and what the run would have cost at API prices. The website runs reviews in its own process: restart it after changing `.env`. Inside Claude Code, `/review HRB` runs one stock and relays the agents as they finish.
```

In `VALUE_INVESTING.md`, after the paragraph "The deep-thinking model matters most..." add:

```markdown
On a Claude plan the per-token cost disappears: set `TRADINGAGENTS_LLM_PROVIDER=claude_code` and the agents run through the Claude Code CLI with Fable for the managers and Opus for everyone else (their own two settings, so the API pair stays as it is). What you spend instead is the plan's usage limit; a 15-stock review may hit it and wait.
```

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check . && git add .claude/skills/review/SKILL.md .env.example README.md value_pipeline/README.md VALUE_INVESTING.md && git commit -m "docs: provider claude_code, its settings, and the /review skill

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Integration tests against the real CLI, then the verify checks

**Files:**
- Create: `tests/test_claude_code_integration.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the integration tests**

Create `tests/test_claude_code_integration.py`:

```python
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
```

- [ ] **Step 2: Run them for real**

Run: `cd /Users/syedusman/Developer/ValueTrading && .venv/bin/pytest -q -m integration tests/test_claude_code_integration.py 2>&1 | tail -5`
Expected: 3 passed (a minute or two; the tool test fetches HRB's statements from Yahoo). If the tool test fails with the model saying it has no tool, the MCP server failed to start: run it by hand as in Task 6 Step 6 with the same environment and read its stderr. If it fails because `FRED_API_KEY` or the vendors are missing inside the server, the CLI did not pass the parent environment to the server: add the needed variables (`FRED_API_KEY`, `SEC_EDGAR_USER_AGENT`, `TRADINGAGENTS_*`) to `mcp_config`'s `env` from `os.environ` and extend the Task 3 test accordingly.

- [ ] **Step 3: The verify checks**

Run, from the repo root, all four and report each:
```bash
.venv/bin/ruff check .
.venv/bin/pytest -q 2>&1 | tail -2
TZ=America/New_York .venv/bin/pytest -q 2>&1 | tail -2
tmp=$(mktemp -d) && python3 -m venv "$tmp/venv" && "$tmp/venv/bin/pip" install -q . && (cd "$tmp" && "$tmp/venv/bin/python" -c "import tradingagents, cli.main") ; rm -rf "$tmp"
```
Expected: ruff clean; both suites green (the integration tests are deselected by default); the smoke test prints nothing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_claude_code_integration.py && git commit -m "test(llm): provider claude_code against the real CLI (integration)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Switch the user's setup over and compare one review

**Files:**
- Modify: `.env` (gitignored; the model lines only)

- [ ] **Step 1: Switch the provider**

In `.env`, set `TRADINGAGENTS_LLM_PROVIDER=claude_code`. Leave `TRADINGAGENTS_DEEP_THINK_LLM` and `QUICK_THINK_LLM` as they are (the API pair). Do not touch any key line.

Run: `cd /Users/syedusman/Developer/ValueTrading && .venv/bin/python -c "from tradingagents.default_config import DEFAULT_CONFIG as c; print(c['llm_provider'], c['claude_code_deep_model'], c['claude_code_quick_model'])"`
Expected: `claude_code claude-fable-5-1 claude-opus-5-5`.

- [ ] **Step 2: Restart the website**

Run: `pkill -f "value_pipeline web"; sleep 2; cd /Users/syedusman/Developer/ValueTrading && (nohup .venv/bin/python -m value_pipeline web >> value_pipeline/data/logs/web.log 2>&1 &) ; sleep 5; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/`
Expected: `200`.

- [ ] **Step 3: One real review of HRB**

Run: `cd /Users/syedusman/Developer/ValueTrading && .venv/bin/python -m value_pipeline review --tickers HRB --yes 2>&1 | tail -30`
Expected: twelve `INFO claude-...` lines, a rating for HRB, an ACTIONS report whose cost line says "billed to your Claude plan", and `complete_report.md` whose header reads `claude_code, deep claude-fable-5-1, quick claude-opus-5-5`. Compare the rating and price target with `value_pipeline/data/reports/2026-10-15/HRB/complete_report.md` and report both to the user.
