"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tradingagents.agents.decision_math import scenario_stats

# LLMs sometimes write a placeholder string ("None", "N/A", ...) into an optional
# numeric field instead of omitting it. Coerce those to None so the structured
# call validates instead of erroring (#1058). Pydantic still parses real numeric
# strings ("189.5") to float.
_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    """Normalise an LLM-written optional numeric field before validation.

    Three shapes show up in practice: a placeholder string ("None", "N/A") in
    place of an omitted value (#1058); a percentage where a price was asked for
    ("15%", #1288); and a human-formatted price ("$1,234.50"). A percentage
    cannot be salvaged into an absolute level -- reading "15%" as 15 would put a
    stop at $15 on a $600 stock -- so it is dropped like a placeholder, leaving
    one bad field to null out instead of failing the whole proposal. A formatted
    price is reduced to its number.

    Anything that is not a single number is dropped the same way. A range
    ("150-160") or a hedge ("around 150") would otherwise reach pydantic, fail
    validation, and discard the whole decision, losing every field the model got
    right along with the price.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in _NULLISH_FLOAT or text.endswith("%"):
        return None
    cleaned = text.replace(",", "").lstrip("$€£¥").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(StrEnum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(StrEnum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Conflicting arguments alone are not a "
            "reason to Hold: commit to the stronger side, sized by how "
            "decisively it wins. Choose Hold only when the evidence is still "
            "balanced after weighing, or too thin to support a call."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including sizing guidance relative to a standard allocation. The "
            "research team does not see the caller's holdings; the trader and "
            "portfolio manager apply the actual position."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        description=(
            "Optional entry price target as an absolute number in the instrument's "
            "quote currency (e.g. 189.5), never a percentage or a range. Omit it "
            "if you cannot state a specific level."
        ),
    )
    stop_loss: float | None = Field(
        default=None,
        description=(
            "Optional stop-loss as an absolute price in the instrument's quote "
            "currency (e.g. 172.0), never a percentage. Convert a percentage "
            "distance to the price level it implies, or omit it."
        ),
    )
    position_sizing: str | None = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )

    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    # Named even when absent, so a reader can tell a level the trader chose not
    # to give from one the schema never asked for.
    for label, value in (("Entry Price", proposal.entry_price),
                         ("Stop Loss", proposal.stop_loss),
                         ("Position Sizing", proposal.position_sizing)):
        parts.extend(["", f"**{label}**: {value if value is not None and value != '' else 'not provided'}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class Verdict(BaseModel):
    """One test of the value checklist: whether it passes, and why."""

    passed: bool = Field(description="True when the test passes.")
    note: str = Field(description="One sentence of evidence for the verdict.")


class Adjustment(BaseModel):
    """One step of the value bridge from the formula value to the price target."""

    reason: str = Field(description="Why the value moves, in a few words.")
    amount: float = Field(description="Per-share change: negative lowers the value, positive raises it.")


class Scenario(BaseModel):
    value: float = Field(description="Per-share value in this scenario.")
    probability: float = Field(description="Probability as a fraction, e.g. 0.25.")
    basis: str = Field(description="The multiple and growth this value assumes.")


class Scenarios(BaseModel):
    bear: Scenario
    base: Scenario
    bull: Scenario


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate. "
            "Conflicting arguments alone are not a reason to Hold: commit to the "
            "stronger side, sized by how decisively it wins. Choose Hold only "
            "when the evidence is still balanced after weighing, or too thin to "
            "support a call."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: float | None = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )
    # Defaults throughout: a free-text fallback and an older decision still validate.
    good_business: Verdict | None = Field(
        default=None, description="Good business: durable returns on capital and a moat.")
    good_management: Verdict | None = Field(
        default=None, description="Good management: honest, shareholder-minded capital allocation.")
    cheap: Verdict | None = Field(
        default=None,
        description=(
            "Cheap: the price is well below the price target. The note states the margin of "
            "safety as a percentage, e.g. 'Margin of safety: 32%'."
        ),
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Serious distress, accounting or governance red flags; empty when there are none.",
    )
    kill_criteria: list[str] = Field(
        default_factory=list,
        description=(
            "3 to 6 verifiable facts that would prove the thesis wrong and mean selling, "
            "e.g. 'ROIC below 12% for two years'."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        description=(
            "The price at or below which to buy or add, as one number. Required unless the "
            "rating is Sell."
        ),
    )
    formula_value: float | None = Field(
        default=None,
        description=(
            "The formula value from the fundamentals report's valuation worksheet, copied as "
            "given; the value bridge starts here."
        ),
    )
    value_bridge: list[Adjustment] = Field(
        default_factory=list,
        description=(
            "The adjustments that take the formula value to the price target: start from the "
            "worksheet's formula value; the formula value plus the amounts must sum to the price "
            "target. Justify any growth above the base rate."
        ),
    )
    scenarios: Scenarios | None = Field(
        default=None,
        description=(
            "Bear, base and bull per-share values; the probabilities must sum to 1 and the "
            "probability-weighted value should equal the price target. The bear case may not "
            "assume more than 0.8x the target multiple and zero growth unless the basis says why."
        ),
    )
    premortem_answer: str = Field(
        default="",
        description=(
            "How the thesis survives the pre-mortem's failure path, or why the risk is accepted."
        ),
    )
    consensus_view: str = Field(
        default="",
        description=(
            "Where the thesis agrees with or differs from the worksheet's consensus estimates "
            "(revenue growth, price target) and why; a value far below consensus with no stated "
            "reason is a problem. Empty only when the worksheet shows no consensus."
        ),
    )
    lane: Literal["value", "compounder", "inflection"] = Field(
        default="value",
        description=(
            "The investment lane the evidence supports; value first. 'value': the margin-of-safety "
            "bars. For the compounder and inflection lanes the Cheap test is replaced by the lane's "
            "own price bars (Cheap may be FAIL; its note states the lane's price test). "
            "'compounder': good business and good management pass, ROIC including goodwill at least 15% in every counted year (at "
            "least four), revenue CAGR at least 8% (ten years, or five flagged), free cash flow "
            "growing at least 5%/yr over at least 3 years, the reverse-DCF implied growth no higher "
            "than the earned growth (revenue CAGR capped at the cohort base rate) or the sustainable "
            "growth, whichever is higher, expected return = yield + earned growth + reversion (at "
            "most 0) of at least 12%/yr, and for a Buy a price no more than 1.15x the worksheet "
            "base (1.25x for Overweight). 'inflection': TTM EBIT at least 1.5x its five-year average, "
            "revenue growth in the latest quarter at least 10 points above the last fiscal year's, "
            "incremental ROIC at least 20%, demand evidence the news report marks verified, a bear "
            "case of at least 30% probability and a kill criterion on a demand indicator (orders, "
            "backlog, customer capex)."
        ),
    )
    lane_evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Empty for the value lane; the other lanes quote their numbers. "
            "The numbers the lane rests on, quoted from the worksheet's Record and inflection lines, "
            "e.g. 'ROIC incl. goodwill ≥ 15% 5/5 years', 'earned growth 10.0%', 'price 1.08× base'. An "
            "inflection claim includes at least one item starting 'verified:' naming the demand "
            "source (a filing, a dated company statement, a customer's dated guidance)."
        ),
    )

    @field_validator("price_target", "entry_price", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_pm_decision(decision: PortfolioDecision, price: float | None = None) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    # Named even when absent: a missing line reads as a field nobody asked for,
    # so a reader cannot tell "no target" from "target not reported".
    target = decision.price_target if decision.price_target is not None else "not provided"
    parts.extend(["", f"**Price Target**: {target}"])
    parts.extend(["", f"**Time Horizon**: {decision.time_horizon or 'not provided'}"])
    parts.extend(["", "**Checklist**:"])
    for label, verdict in (("Good business", decision.good_business),
                           ("Good management", decision.good_management),
                           ("Cheap", decision.cheap)):
        if verdict is None:
            parts.append(f"{label} — not provided")
        else:
            parts.append(f"{label} — {'PASS' if verdict.passed else 'FAIL'}: {verdict.note}")
    # The value lane's bars are the checklist and scenarios above: its line carries no evidence.
    evidence = "; ".join(decision.lane_evidence) if decision.lane != "value" else ""
    parts.extend(["", f"**Lane**: {decision.lane}" + (f" — {evidence}" if evidence else "")])
    flags = [f"- {flag}" for flag in decision.red_flags]
    parts.extend(["", "**Red Flags**:" + ("\n" + "\n".join(flags) if flags else " none")])
    kills = [f"{i}. {k}" for i, k in enumerate(decision.kill_criteria, 1)]
    parts.extend(["", "**Kill Criteria**:" + ("\n" + "\n".join(kills) if kills else " not provided")])
    entry = decision.entry_price if decision.entry_price is not None else "not provided"
    parts.extend(["", f"**Entry Price**: {entry}"])
    parts.extend(["", "**Scenarios**:" + ("\n" + _render_scenarios(decision, price) if decision.scenarios
                                          else " not provided")])
    parts.extend(["", "**Value Bridge**:\n" + _render_bridge(decision)])
    parts.extend(["", f"**Pre-mortem Answer**: {decision.premortem_answer or 'not provided'}"])
    parts.extend(["", f"**Consensus**: {decision.consensus_view or 'not provided'}"])
    return "\n".join(parts)


def _money(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "not provided"


def _render_scenarios(decision: PortfolioDecision, price: float | None = None) -> str:
    s = decision.scenarios
    lines = [f"{name} {sc.probability * 100:.0f}%: {sc.value:.2f} — {sc.basis}"
             for name, sc in (("Bear", s.bear), ("Base", s.base), ("Bull", s.bull))]
    # Measured at today's price (the worksheet's) when known, else at the entry price, labelled.
    at, label = (price, "price") if price is not None else (decision.entry_price, "entry price")
    stats = scenario_stats(s, at)
    ratio = (f"{stats['upside_downside']:.1f}x at {label} {at:.2f}"
             if stats["upside_downside"] is not None else "n/a")
    ev = _money(stats["expected_value"])
    lines.append(f"Expected value: {ev}; upside/downside {ratio}")
    return "\n".join(lines)


def _render_bridge(decision: PortfolioDecision) -> str:
    """ASCII signs throughout; the pipeline's parser also accepts a Unicode minus."""
    lines = [f"Formula value {_money(decision.formula_value)}"]
    lines += [f"{a.amount:+.2f} {a.reason}" for a in decision.value_bridge]
    lines.append(f"= Price Target {_money(decision.price_target)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------


class SentimentBand(StrEnum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    overall_band: SentimentBand = Field(
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when all sources are genuinely silent or non-committal."
        ),
    )
    overall_score: float = Field(
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "Use 'low' when one or more sources returned a placeholder or fewer "
            "than 5 data points; 'medium' when data is present but sparse; "
            "'high' when all three sources returned substantive data."
        ),
    )
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite message "
            "counts, ratios, notable posts); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )


def render_sentiment_report(report: SentimentReport) -> str:
    """Render a SentimentReport to the markdown shape the rest of the system expects.

    The structured header (band + score + confidence) is prepended to the
    narrative so the saved report is both human-readable and machine-parseable
    without regex.
    """
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** "
        f"(Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])
