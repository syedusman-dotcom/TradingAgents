"""Agents on the schema-only structured-output path must not invite tool calls (#1130).

`with_structured_output` binds exactly one tool (the schema). A prompt that
primes tool use makes models emit an unknown `web_search` call, which discards
the structured attempt and forces a free-text retry — an extra LLM round trip
and the loss of typed output.

These assert the constraint reaches the *rendered* prompt each agent actually
sends, not merely that the constant is referenced in the module.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import tradingagents.agents.analysts.sentiment_analyst as sentiment
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS
from tradingagents.agents.trader.trader import create_trader


def _capturing_llm(captured: dict, result):
    """LLM whose structured binding records the prompt it was handed."""
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or result
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _prompt_text(prompt) -> str:
    """Flatten a captured prompt (str, message list, or objects) to text."""
    if isinstance(prompt, str):
        return prompt
    parts = []
    for m in prompt:
        parts.append(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
    return "\n".join(str(p) for p in parts)


@pytest.mark.unit
def test_trader_prompt_states_constraint():
    from tradingagents.agents.schemas import TraderAction, TraderProposal

    captured = {}
    llm = _capturing_llm(captured, TraderProposal(action=TraderAction.BUY, reasoning="x"))
    create_trader(llm)({
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy",
        "market_report": "Current price $189.5; ATR 4.2.",
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
def test_research_manager_prompt_states_constraint():
    from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

    captured = {}
    llm = _capturing_llm(
        captured,
        ResearchPlan(
            recommendation=PortfolioRating.BUY, rationale="x", strategic_actions="y"
        ),
    )
    create_research_manager(llm)({
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "count": 1,
        },
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])
    assert ("Also judge Still investing (R&D and growth capex held or rising, product evidence from the "
            "filings) and say whether the company is investing or harvesting; it does not change these "
            "bars.") in " ".join(_prompt_text(captured["prompt"]).split())
    assert ("Underweight/Sell: Good business, Good management or Cheap clearly fails, the stock is above "
            "intrinsic value") in " ".join(_prompt_text(captured["prompt"]).split())


@pytest.mark.unit
def test_portfolio_manager_prompt_states_constraint():
    from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

    captured = {}
    llm = _capturing_llm(
        captured,
        PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="x",
            investment_thesis="y",
        ),
    )
    risk = {
        "history": "h", "aggressive_history": "a", "conservative_history": "c",
        "neutral_history": "n", "current_aggressive_response": "",
        "current_conservative_response": "", "current_neutral_response": "",
        "latest_speaker": "Neutral", "count": 1,
    }
    create_portfolio_manager(llm)({
        "company_of_interest": "NVDA",
        "risk_debate_state": risk,
        "investment_plan": "plan",
        "trader_investment_plan": "trader plan",
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
def test_sentiment_prompt_states_constraint(monkeypatch):
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    # Pre-fetched sources are stubbed so the prompt builds without network I/O.
    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
    monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)

    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.BULLISH, overall_score=7.5,
        confidence="high", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-01-15",
        "asset_type": "stock", "messages": [],
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    # This agent binds no tools, so tool-range wording must not reappear.
    assert "tool-call date ranges" not in text


@pytest.mark.unit
def test_tool_using_analysts_keep_their_date_guidance():
    # The analysts that really do call tools keep the wording that anchors their
    # tool date ranges (#836) — this fix is scoped to no-tool agents.
    import tradingagents.agents.analysts.market_analyst as market
    import tradingagents.agents.analysts.news_analyst as news
    for module in (market, news):
        assert "tool-call date ranges" in inspect.getsource(module)


@pytest.mark.unit
def test_constraint_text_is_unambiguous():
    assert "do not call external tools" in NO_EXTERNAL_TOOLS.lower()
    # No template braces: it is embedded in ChatPromptTemplate strings, where
    # braces would be parsed as input variables.
    assert "{" not in NO_EXTERNAL_TOOLS and "}" not in NO_EXTERNAL_TOOLS


@pytest.mark.unit
def test_the_sentiment_prompt_names_the_subreddits_searched(monkeypatch):
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
    monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)
    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.NEUTRAL, overall_score=5.0, confidence="low", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "BTC-USD", "trade_date": "2026-01-15",
        "asset_type": "crypto", "messages": [],
    })
    text = _prompt_text(captured["prompt"])
    assert "r/Bitcoin, r/CryptoCurrency, r/CryptoMarkets" in text
    assert "wallstreetbets" not in text


@pytest.mark.unit
def test_prompts_carry_innovation_lines(monkeypatch):
    from tradingagents.agents import create_premortem
    from tradingagents.agents.analysts import fundamentals_analyst
    from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

    prompts = {}

    def take_turn(prompt, llm, tools, messages):
        prompts["f"] = prompt.format(messages=[])
        return None, ""

    monkeypatch.setattr(fundamentals_analyst, "take_turn", take_turn)
    monkeypatch.setattr(fundamentals_analyst, "get_instrument_context_from_state", lambda s: "")
    fundamentals_analyst.create_fundamentals_analyst(None)(
        {"trade_date": "2026-10-23", "company_of_interest": "KO", "messages": []})
    flat = " ".join(prompts["f"].split())
    assert "Innovation and reinvestment: state the new-product, launch and pipeline evidence" in flat
    assert "conclude 'investing' or 'harvesting'" in flat
    assert "Good management (1-5), Still investing (investing/harvesting), Cheap (margin of safety %)" in flat

    risk = {
        "history": "h", "aggressive_history": "a", "conservative_history": "c",
        "neutral_history": "n", "current_aggressive_response": "",
        "current_conservative_response": "", "current_neutral_response": "",
        "latest_speaker": "Neutral", "count": 1,
    }
    state = {"company_of_interest": "NVDA", "risk_debate_state": risk,
             "investment_plan": "plan", "trader_investment_plan": "trader plan"}
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="x")
    create_premortem(llm)(state)
    premortem = llm.invoke.call_args.args[0]
    assert ("4. The disruption exposure: who or what could make the product unnecessary, and the "
            "signal visible first that it is happening.") in premortem
    assert "about 300 words" in premortem

    captured = {}
    pm = _capturing_llm(captured, PortfolioDecision(rating=PortfolioRating.HOLD,
                                                    executive_summary="x", investment_thesis="y"))
    create_portfolio_manager(pm)(state)
    text = " ".join(_prompt_text(captured["prompt"]).split())
    assert "Still investing must PASS for the compounder lane." in text
    assert ("When R&D is not reported or the return on incremental capital reads n/a, judge on growth "
            "capex, acquisitions and the filings' product evidence; n/a is not a FAIL.") in text
    assert ("one kill criterion names the reinvestment level (R&D or capex as a share of revenue) "
            "below which the thesis is wrong") in text
    assert "four tests: Good business, Good management, Still investing, Cheap" in text
    assert '"Still investing — PASS: note"' in text
    assert "Underweight/Sell: Good business, Good management or Cheap clearly fails, the stock" in text
