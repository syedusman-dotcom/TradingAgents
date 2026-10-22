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
