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
