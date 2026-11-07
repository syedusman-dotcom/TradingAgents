"""The independent arm: separate cases, the bear's own evidence and a randomised judge order.

In the independent arm the bull and bear each open from the analyst reports without the other's
text, the bear also reads an adverse evidence pack, the three risk analysts each speak once
without the others' text, and the two judges read the cases under their own headings in an order
seeded by ticker and date. The current arm keeps today's interleaved debate.
"""
from __future__ import annotations

import random
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.context import judge_order
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers import adverse
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, ResearchPlan
from tradingagents.graph.conditional_logic import ConditionalLogic

DATE = "2026-01-09"
BULL_TEXT = "Bull Analyst: the moat is widening"
BEAR_TEXT = "Bear Analyst: the margins are fading"
RISK_TEXT = {
    "aggressive": "Aggressive Analyst: lean in hard",
    "conservative": "Conservative Analyst: protect the capital",
    "neutral": "Neutral Analyst: split the difference",
}


def _state(arm=None, ticker="AAPL", **extra):
    state = {
        "company_of_interest": ticker, "trade_date": DATE, "asset_type": "stock",
        "market_report": "m", "sentiment_report": "s", "news_report": "n",
        "fundamentals_report": "f", "investment_plan": "plan", "trader_investment_plan": "tplan",
        "investment_debate_state": {
            "history": "\n" + BULL_TEXT, "bull_history": "\n" + BULL_TEXT,
            "bear_history": "\n" + BEAR_TEXT, "current_response": BULL_TEXT, "count": 1,
        },
        "risk_debate_state": {
            "history": "\n" + "\n".join(RISK_TEXT.values()),
            "aggressive_history": "\n" + RISK_TEXT["aggressive"],
            "conservative_history": "\n" + RISK_TEXT["conservative"],
            "neutral_history": "\n" + RISK_TEXT["neutral"],
            "current_aggressive_response": RISK_TEXT["aggressive"],
            "current_conservative_response": RISK_TEXT["conservative"],
            "current_neutral_response": RISK_TEXT["neutral"],
            "latest_speaker": "Neutral", "count": 2,
        },
        **extra,
    }
    if arm is not None:
        state["agent_round"] = arm
    return state


def _llm(captured: dict, result=None):
    def record(prompt):
        captured["prompt"] = str(prompt)
        return MagicMock(content="argument")

    llm = MagicMock()
    llm.invoke.side_effect = record
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: captured.__setitem__("prompt", str(prompt)) or result
    llm.with_structured_output.return_value = structured
    return llm


_PLAN = ResearchPlan(recommendation=PortfolioRating.HOLD, rationale="r", strategic_actions="a")
_DECISION = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="y")


# --- judge order ------------------------------------------------------------

@pytest.mark.unit
def test_judge_order_is_seeded_and_both_orders_occur():
    tickers = [f"T{i}" for i in range(200)]
    research = [judge_order(t, DATE, "research") for t in tickers]
    assert research == [judge_order(t, DATE, "research") for t in tickers]
    assert {tuple(o) for o in research} == {("bear", "bull"), ("bull", "bear")}
    for t, order in zip(tickers, research, strict=True):
        bear_first = random.Random(f"{t}:{DATE}:research").random() < 0.5
        assert order == (["bear", "bull"] if bear_first else ["bull", "bear"])

    risk = [judge_order(t, DATE, "risk") for t in tickers]
    assert risk == [judge_order(t, DATE, "risk") for t in tickers]
    assert all(sorted(o) == ["aggressive", "conservative", "neutral"] for o in risk)
    assert len({tuple(o) for o in risk}) == 6
    assert risk[0] == random.Random(f"T0:{DATE}:risk").sample(
        ["aggressive", "conservative", "neutral"], 3)


# --- adverse pack -----------------------------------------------------------

@pytest.mark.unit
def test_adverse_pack_has_both_sections_and_truncates():
    seen = {}

    def news(ticker, start, end):
        seen["news"] = (ticker, start, end)
        return "N" * 7000

    def insiders(ticker, date):
        seen["insiders"] = (ticker, date)
        return "CEO sold 10,000 shares"

    pack = adverse.adverse_pack("AAPL", DATE, fetch_news=news, fetch_insiders=insiders)
    assert pack.startswith("**Adverse evidence pack**")
    assert "## Insider transactions\nCEO sold 10,000 shares" in pack
    assert "## News, last 90 days\n" + "N" * 6000 in pack
    assert "N" * 6001 not in pack
    assert seen["news"] == ("AAPL", "2025-10-11", DATE)
    assert seen["insiders"] == ("AAPL", DATE)


@pytest.mark.unit
def test_adverse_pack_marks_a_failed_fetch():
    def broken(*args):
        raise RuntimeError("vendor down")

    pack = adverse.adverse_pack("AAPL", DATE, fetch_news=lambda *a: "headline",
                                fetch_insiders=broken)
    assert "## Insider transactions\nnot retrieved: vendor down" in pack
    assert "## News, last 90 days\nheadline" in pack


@pytest.mark.unit
def test_adverse_pack_defaults_route_through_the_vendor_router(monkeypatch):
    calls = []
    monkeypatch.setattr(adverse, "route_to_vendor",
                        lambda method, *args: calls.append((method, args)) or f"{method} text")
    pack = adverse.adverse_pack("AAPL", DATE)
    assert ("get_news", ("AAPL", "2025-10-11", DATE)) in calls
    assert ("get_insider_transactions", ("AAPL", DATE)) in calls
    assert "get_news text" in pack and "get_insider_transactions text" in pack


# --- bull and bear ----------------------------------------------------------

def _pack(ticker, date):
    return f"**Adverse evidence pack** for {ticker} on {date}\n## Insider transactions\nsold"


@pytest.mark.unit
def test_independent_bear_reads_the_pack_and_not_the_bull():
    captured, packs = {}, []
    node = create_bear_researcher(_llm(captured), pack_fn=lambda t, d: packs.append((t, d)) or _pack(t, d))
    node(_state("independent"))
    prompt = captured["prompt"]
    assert packs == [("AAPL", DATE)]
    assert "Adverse evidence pack" in prompt
    assert "has not spoken yet" in prompt
    assert "the moat is widening" not in prompt
    assert "cite" in prompt.lower()


@pytest.mark.unit
@pytest.mark.parametrize("arm", [None, "current"])
def test_current_bear_reads_the_bull_and_no_pack(arm):
    captured, packs = {}, []
    node = create_bear_researcher(_llm(captured), pack_fn=lambda t, d: packs.append(t) or _pack(t, d))
    node(_state(arm))
    assert packs == []
    assert "the moat is widening" in captured["prompt"]
    assert "Adverse evidence pack" not in captured["prompt"]


@pytest.mark.unit
def test_independent_bull_opens_without_history_or_pack():
    captured = {}
    state = _state("independent")
    state["investment_debate_state"]["current_response"] = BEAR_TEXT
    state["investment_debate_state"]["history"] = "\n" + BEAR_TEXT
    create_bull_researcher(_llm(captured))(state)
    assert "has not spoken yet" in captured["prompt"]
    assert "the margins are fading" not in captured["prompt"]
    assert "Adverse evidence pack" not in captured["prompt"]


# --- research manager ---------------------------------------------------------

def _ticker_with(first: str) -> str:
    return next(f"T{i}" for i in range(200) if judge_order(f"T{i}", DATE, "research")[0] == first)


@pytest.mark.unit
@pytest.mark.parametrize("first", ["bull", "bear"])
def test_independent_research_manager_reads_two_cases_in_seeded_order(first):
    captured = {}
    ticker = _ticker_with(first)
    update = create_research_manager(_llm(captured, _PLAN))(_state("independent", ticker=ticker))
    prompt = captured["prompt"]
    assert "Debate History" not in prompt
    bull_at, bear_at = prompt.index("**The Bull case**"), prompt.index("**The Bear case**")
    assert (bull_at < bear_at) == (first == "bull")
    assert prompt.index("the moat is widening") > bull_at
    assert prompt.index("the margins are fading") > bear_at
    assert update["judge_order"] == f"{first}-first"


@pytest.mark.unit
def test_current_research_manager_keeps_the_debate_history():
    captured = {}
    update = create_research_manager(_llm(captured, _PLAN))(_state("current"))
    assert "**Debate History:**" in captured["prompt"]
    assert "The Bull case" not in captured["prompt"]
    assert "judge_order" not in update


# --- risk analysts ------------------------------------------------------------

_RISK = [("aggressive", create_aggressive_debator), ("conservative", create_conservative_debator),
         ("neutral", create_neutral_debator)]


@pytest.mark.unit
@pytest.mark.parametrize("own,factory", _RISK)
def test_independent_risk_analyst_sees_no_other_view(own, factory):
    captured = {}
    factory(_llm(captured))(_state("independent"))
    prompt = captured["prompt"]
    for name, text in RISK_TEXT.items():
        assert text.split(": ")[1] not in prompt, name
    assert prompt.count("has not spoken yet") == 2
    assert "conversation history" not in prompt


@pytest.mark.unit
@pytest.mark.parametrize("own,factory", _RISK)
def test_current_risk_analyst_sees_the_others(own, factory):
    captured = {}
    factory(_llm(captured))(_state("current"))
    for name, text in RISK_TEXT.items():
        if name != own:
            assert text.split(": ")[1] in captured["prompt"]
    assert "Here is the current conversation history:" in captured["prompt"]


# --- portfolio manager ----------------------------------------------------------

@pytest.mark.unit
def test_independent_portfolio_manager_reads_three_views_in_seeded_order():
    captured = {}
    create_portfolio_manager(_llm(captured, _DECISION))(_state("independent"))
    prompt = captured["prompt"]
    assert "Risk Analysts Debate History" not in prompt
    assert "**Risk views**" in prompt
    order = judge_order("AAPL", DATE, "risk")
    at = [prompt.index(f"**{name.capitalize()} view**") for name in order]
    assert at == sorted(at)
    for name in order:
        assert RISK_TEXT[name] in prompt


@pytest.mark.unit
def test_current_portfolio_manager_keeps_the_risk_history():
    captured = {}
    create_portfolio_manager(_llm(captured, _DECISION))(_state("current"))
    assert "**Risk Analysts Debate History:**" in captured["prompt"]
    assert "Risk views" not in captured["prompt"]


# --- routing --------------------------------------------------------------------

@pytest.mark.unit
def test_independent_debate_ends_after_two_openings():
    logic = ConditionalLogic(max_debate_rounds=2, max_risk_discuss_rounds=2)
    debate = {"current_response": "Bear Analyst: x", "count": 2}
    assert logic.should_continue_debate(
        {"agent_round": "independent", "investment_debate_state": debate}) == "Research Manager"
    assert logic.should_continue_debate(
        {"agent_round": "current", "investment_debate_state": debate}) == "Bull Researcher"
    assert logic.should_continue_debate({"investment_debate_state": debate}) == "Bull Researcher"


@pytest.mark.unit
def test_independent_risk_round_ends_after_three_views():
    logic = ConditionalLogic(max_debate_rounds=2, max_risk_discuss_rounds=2)
    risk = {"latest_speaker": "Neutral", "count": 3}
    assert logic.should_continue_risk_analysis(
        {"agent_round": "independent", "risk_debate_state": risk}) == "Pre-mortem"
    assert logic.should_continue_risk_analysis(
        {"agent_round": "current", "risk_debate_state": risk}) == "Aggressive Analyst"
    assert logic.should_continue_risk_analysis({"risk_debate_state": risk}) == "Aggressive Analyst"


# --- current-arm invariance -------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("arm", [None, "current"])
def test_current_arm_prompts_keep_their_old_anchors(arm):
    prompts = {}
    for key, factory in [("bull", create_bull_researcher), ("bear", create_bear_researcher),
                         ("aggressive", create_aggressive_debator),
                         ("conservative", create_conservative_debator),
                         ("neutral", create_neutral_debator)]:
        captured = {}
        factory(_llm(captured))(_state(arm))
        prompts[key] = captured["prompt"]
    for key, factory, result in [("rm", create_research_manager, _PLAN),
                                 ("pm", create_portfolio_manager, _DECISION)]:
        captured = {}
        factory(_llm(captured, result))(_state(arm))
        prompts[key] = captured["prompt"]

    assert "the moat is widening" in prompts["bear"]
    assert "Conversation history of the debate:" in prompts["bull"]
    assert "**Debate History:**" in prompts["rm"]
    assert "**Risk Analysts Debate History:**" in prompts["pm"]
    for key in ("aggressive", "conservative", "neutral"):
        assert "Here is the current conversation history:" in prompts[key]
    for prompt in prompts.values():
        for new in ("Adverse evidence pack", "The Bull case", "The Bear case", "Risk views"):
            assert new not in prompt
