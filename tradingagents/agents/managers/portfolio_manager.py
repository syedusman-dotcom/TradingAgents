"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call. Its rating is the run's
``final_rating``, and the decision is rendered to markdown as
``final_trade_decision`` for the memory log, CLI display and saved reports.
When a provider does not expose structured output, the agent falls back to
free-text generation and the rating is read from that text.
"""

from __future__ import annotations

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.decision_math import decision_checks, worksheet_excerpt
from tradingagents.agents.rating import parse_rating
from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )
        premortem = state.get("premortem", "")
        premortem_block = (
            f"**Pre-mortem (the most likely way this loses money):**\n{premortem}\n"
            if premortem
            else ""
        )
        premortem_instruction = (
            " Answer the pre-mortem explicitly: say how the thesis survives that failure path, "
            "or why you accept the risk."
            if premortem
            else ""
        )
        excerpt = worksheet_excerpt(state.get("fundamentals_report", ""))
        worksheet_block = (
            f"**Valuation worksheet (formula value):**\n{excerpt}\n" if excerpt else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

{portfolio_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}
{premortem_block}{worksheet_block}
---

Ground every conclusion in specific evidence from the analysts. Decide as a disciplined value investor, scoring the company on three tests: Good business, Good management, Cheap (margin of safety).
- Buy requires all three to pass AND a probability-weighted margin of safety of at least 30% AND an upside/downside of at least 3 AND no serious distress or accounting red flag.
- Overweight: all three pass with a probability-weighted margin of safety of at least 15% and an upside/downside of at least 2, or the trend check is DOWNTREND.
- Hold: a good company at a fair price, or evidence too thin. A Hold on a name not held must give the entry price at which both the margin of safety and the upside/downside bars are met.
- Underweight/Sell: a test clearly fails, the stock is above intrinsic value, or the original thesis is broken.
- Never rate Buy in FREEFALL; wait for BASING.
Think in a 1-3 year horizon. Conflicting arguments alone are not a reason to Hold; the debate always contains conflict, so decide which side the evidence supports. Prefer doing nothing over frequent trading: only change the rating from a previous decision on this ticker if the facts changed, not because the price moved. For a stock already held, buying and selling have different bars: a margin of safety that has shrunk but is still positive means stop adding (Hold), not sell; rate a holding Underweight or Sell only when the price is above intrinsic value or the thesis is broken. Weigh the analysts on their merits, independent of speaking order.{premortem_instruction}
Express entry conditions as prices and verifiable facts. Use the earnings date only when the print is genuinely binary and say why. If you would buy at today's price after a clean print, say so and give the entry price. Start the value bridge from the worksheet's formula value and justify any growth above the base rate. The bear case may not assume more than 0.8× the target multiple and zero growth unless you say why.

## Output

Write these sections, in this order, starting with the rating on its own line:

- **Rating**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Executive Summary**: the call and how to act on it
- **Investment Thesis**: list business quality, management quality, intrinsic value estimate and margin of safety, F-Score/Z-Score, trend status, the main risk, and the specific evidence that would make you sell. State the margin of safety as a percentage (e.g. "Margin of safety: 32%")
- **Price Target**: the per-share intrinsic value estimate as one number (e.g. "Price Target: 62"), not a range
- **Time Horizon**: the holding period
- **Checklist**: three lines, "Good business — PASS: note", "Good management — FAIL: note", "Cheap — PASS: Margin of safety: 32%"
- **Red Flags**: one "- " bullet per serious red flag, or "none"
- **Kill Criteria**: 3 to 6 numbered, verifiable facts that would prove the thesis wrong
- **Entry Price**: the price at or below which to buy, as one number (required unless the rating is Sell)
- **Scenarios**: three lines "Bear 25%: 42.00 — basis", "Base 50%: …", "Bull 25%: …" with probabilities summing to 100%, then "Expected value: 56.00", which should equal the price target
- **Value Bridge**: "Formula value 80.33" from the worksheet, one line per adjustment with its sign ("-12.00 reason"), then "= Price Target 56.00"; the adjustments must sum to the price target
- **Pre-mortem Answer**: how the thesis survives the pre-mortem, or why you accept the risk

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        # The typed rating is the decision; the rendered text only carries it.
        # Read back from text, a rating the thesis quotes could replace it.
        decision = invoke_structured(structured_llm, prompt, "Portfolio Manager")
        if decision is not None:
            # No market price reaches this node, so the checks that need one skip.
            problems = decision_checks(decision, None, None)
            if problems:
                retry_prompt = (prompt + "\n\nFix these problems and return the full decision again:\n"
                                + "\n".join(f"- {p}" for p in problems))
                retried = invoke_structured(structured_llm, retry_prompt, "Portfolio Manager")
                if retried is not None:
                    decision, problems = retried, decision_checks(retried, None, None)
            final_trade_decision = render_pm_decision(decision)
            if problems:
                final_trade_decision += "\n\n**Unresolved Checks**:\n" + "\n".join(
                    f"- {p}" for p in problems)
            final_rating = decision.rating.value
        else:
            final_trade_decision = llm.invoke(prompt).content
            final_rating = parse_rating(final_trade_decision)

        new_risk_debate_state = {
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "final_rating": final_rating,
        }

    return portfolio_manager_node
