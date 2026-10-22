"""Agents on the Claude subscription: each call is a headless Claude Code session (no network)."""

import json
import os
import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from tradingagents.agents import tools as toolbox
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
