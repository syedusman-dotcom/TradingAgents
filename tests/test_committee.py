"""The investment committee: one deep call across every reviewed stock, checked against the rules."""

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from value_pipeline.actions import Decision, plan
from value_pipeline.config import DEFAULTS, Paths

SIZING = DEFAULTS["sizing"]


@pytest.fixture(autouse=True)
def _no_company_name_lookup(monkeypatch):
    from tradingagents.agents import context

    monkeypatch.setattr(context, "resolve_instrument_identity", lambda ticker: {})


def _committee(*ideas, swaps=(), concentration=("Two of three in Industrials",), summary="Own the cheapest."):
    from value_pipeline.committee import Committee, RankedIdea, Swap

    return Committee(
        ranking=[RankedIdea(ticker=t, rank=i, action=a, weight=0.1, expected_return=0.12,
                            downside="bear case -30%", rationale=f"Why {t}.")
                 for i, (t, a) in enumerate(ideas, 1)],
        swaps=[Swap(sell=s, buy=b, reason="better return") for s, b in swaps],
        concentration=list(concentration), summary=summary)


def _decisions():
    return [
        Decision(ticker="HRB", rating="Hold", price=50.0, held_shares=1, sector="Industrials",
                 expected_return=0.08, kill_criteria=["Revenue falls"], upside_downside=1.5),
        Decision(ticker="AAPL", rating="Sell", price=200.0, sector="Tech", expected_return=float("nan")),
        Decision(ticker="MSFT", rating="Buy", price=400.0, sector="Tech", expected_return=0.15,
                 entry_price=380.0, intrinsic_value=500.0),
    ]


@pytest.mark.unit
def test_payload_is_json_serialisable_and_carries_every_ticker():
    from value_pipeline import portfolio as pfmod
    from value_pipeline.committee import build_payload

    decisions = _decisions()
    results = [{"ticker": d.ticker, "rating": d.rating, "checklist": {"cheap": {"passed": True}},
                "scenarios": {"weighted_mos": 0.2}, "red_flags": ["DSO up"], "kill_criteria": d.kill_criteria,
                "entry_price": d.entry_price, "error": None} for d in decisions]
    pf = pfmod.Portfolio(cash=500.0, positions=[pfmod.Position("HRB", 1, 40.0, "2026-01-01"),
                                                 pfmod.Position("KO", 3, 60.0, "2026-01-01")])
    actions, summary = plan(decisions, pf.cash, SIZING)
    payload = build_payload(decisions, results, pf, summary, SIZING)
    json.dumps(payload, allow_nan=False)
    stocks = {s["ticker"]: s for s in payload["stocks"]}
    assert set(stocks) == {"HRB", "AAPL", "MSFT"}
    assert stocks["HRB"]["held_weight"] == pytest.approx(50.0 / summary["total_value"])
    assert stocks["HRB"]["kill_criteria"] == ["Revenue falls"] and stocks["HRB"]["sector"] == "Industrials"
    assert stocks["AAPL"]["expected_return"] is None and stocks["AAPL"]["rating"] == "Sell"
    assert stocks["MSFT"]["checklist"] == {"cheap": {"passed": True}} and stocks["MSFT"]["red_flags"] == ["DSO up"]
    assert stocks["MSFT"]["entry_price"] == 380.0 and stocks["HRB"]["upside_downside"] == 1.5
    assert stocks["MSFT"]["scenarios"] == {"weighted_mos": 0.2}
    assert payload["cash"] == 500.0 and payload["sizing"]["max_position_weight"] == 0.10
    assert [h["ticker"] for h in payload["other_holdings"]] == ["KO"]


@pytest.mark.unit
def test_committee_never_overrides_a_sell():
    from value_pipeline.committee import reconcile

    actions, _ = plan(_decisions(), 500.0, SIZING)
    committee = _committee(("MSFT", "BUY"), ("AAPL", "BUY"), ("HRB", "HOLD"), swaps=[("HRB", "AAPL")])
    kept, notes = reconcile(committee, actions)
    assert [r.ticker for r in kept.ranking] == ["MSFT", "HRB"]
    assert kept.swaps == []
    assert "committee wanted to buy AAPL but its review rated Sell: the review stands" in notes


@pytest.mark.unit
def test_reconcile_lists_disagreements():
    from value_pipeline.committee import reconcile

    actions, _ = plan(_decisions(), 500.0, SIZING)
    rules = {a.ticker: a for a in actions}
    committee = _committee(("MSFT", "SKIP"), ("HRB", "HOLD"), ("AAPL", "SKIP"))
    _, notes = reconcile(committee, actions)
    assert notes == [f"MSFT: committee says SKIP, rules say {rules['MSFT'].action} ({rules['MSFT'].reason})"]
    # HOLD is the committee's word for the rules' KEEP; AAPL's SKIP matches the rules
    assert rules["HRB"].action == "KEEP" and rules["AAPL"].action == "SKIP"


@pytest.mark.unit
def test_committee_buy_on_a_holding_matches_the_rules_add():
    from value_pipeline.committee import reconcile

    decisions = [Decision(ticker="HRB", rating="Buy", price=50.0, held_shares=1, sector="Industrials",
                          expected_return=0.12, intrinsic_value=80.0, margin_of_safety=0.4, trend="UPTREND"),
                 Decision(ticker="MSFT", rating="Buy", price=400.0, sector="Tech", expected_return=0.15,
                          intrinsic_value=500.0)]
    actions, _ = plan(decisions, 5000.0, SIZING)
    rules = {a.ticker: a for a in actions}
    assert rules["HRB"].action == "ADD" and rules["HRB"].held
    _, notes = reconcile(_committee(("HRB", "BUY"), ("MSFT", "BUY")), actions)
    assert notes == []
    _, notes = reconcile(_committee(("HRB", "TRIM"), ("MSFT", "BUY")), actions)
    assert notes == [f"HRB: committee says TRIM, rules say ADD ({rules['HRB'].reason})"]


def _weighted(*weights):
    from value_pipeline.committee import Committee, RankedIdea

    return Committee(ranking=[RankedIdea(ticker=f"T{i}", rank=i, action="BUY", weight=w, expected_return=None,
                                         downside="-", rationale="r") for i, w in enumerate(weights, 1)],
                     swaps=[], concentration=[], summary="s")


@pytest.mark.unit
def test_committee_weights_given_as_percentages_are_read_as_percentages():
    from value_pipeline.committee import render_committee

    md = "\n".join(render_committee(_weighted(8.0, 0.05), [], None))
    assert "| 1 | T1 | BUY | 8% | – | r |" in md
    assert "| 2 | T2 | BUY | 5% | – | r |" in md


@pytest.mark.unit
def test_committee_weights_above_the_cap_are_shown_at_the_cap():
    from value_pipeline.committee import render_committee

    md = "\n".join(render_committee(_weighted(0.15, 25.0, 0.10), [], None))
    assert "| 1 | T1 | BUY | 10% (capped from 15%) | – | r |" in md
    assert "| 2 | T2 | BUY | 10% (capped from 25%) | – | r |" in md
    assert "| 3 | T3 | BUY | 10% | – | r |" in md
    md = "\n".join(render_committee(_weighted(0.15), [], None, max_weight=0.20))
    assert "| 1 | T1 | BUY | 15% | – | r |" in md


@pytest.mark.unit
def test_actions_render_the_committee_section_before_holdings():
    from value_pipeline import render

    actions, summary = plan(_decisions(), 500.0, SIZING)
    committee = _committee(("MSFT", "BUY"), ("HRB", "HOLD"), swaps=[("HRB", "MSFT")])
    md = render.actions_markdown("2026-11-07", actions, summary, reports={}, comparison=None, cost=0.0,
                                 failures=[], dry_run=False, names={"MSFT": "Microsoft"},
                                 committee=(committee, ["MSFT: committee says BUY, rules say SKIP (why)"]))
    assert md.index("## Investment committee") < md.index("## Holdings")
    assert "| Rank | Ticker | Action | Weight | Expected return | Rationale |" in md
    assert "| 1 | Microsoft (MSFT) | BUY | 10% | 12%/yr | Why MSFT. |" in md
    assert "Own the cheapest." in md
    for heading in ("### Swaps", "### Concentration", "### Where the committee and the rules disagree"):
        assert heading in md
    assert "- Sell HRB, buy MSFT: better return" in md and "- Two of three in Industrials" in md
    assert "- MSFT: committee says BUY, rules say SKIP (why)" in md
    plain = render.actions_markdown("2026-11-07", actions, summary, reports={}, comparison=None, cost=0.0,
                                    failures=[], dry_run=False)
    assert "Investment committee" not in plain


# --- the review ---------------------------------------------------------------------

class _Graph:
    """A TradingAgentsGraph that needs no model: fixed decisions, and the given deep LLM."""

    def __init__(self, deep_llm):
        self.deep_thinking_llm = deep_llm
        self.quick_thinking_llm = MagicMock()
        self.config = {"llm_provider": "claude_code"}

    def settle_all_pending(self):
        pass

    def propagate(self, ticker, day, portfolio=None):
        rating = {"HRB": "Buy", "MSFT": "Overweight"}.get(ticker, "Hold")
        return {"final_trade_decision": f"**Rating**: {rating}\n\n**Price Target**: 100"}, rating

    def save_reports(self, state, ticker, save_path, html):
        pass


def _structured_llm(result=None, error=None):
    structured = MagicMock()
    if error:
        structured.invoke.side_effect = error
    else:
        structured.invoke.return_value = result
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _review(tmp_path, monkeypatch, deep_llm, *, committee=True, dry_run=False, tickers=("HRB", "MSFT")):
    import copy

    from value_pipeline import market_data, operations as ops, review

    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    monkeypatch.setattr(review, "make_graph", lambda *a, **k: _Graph(deep_llm))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {t: {"ticker": t, "trend": "UPTREND", "sector": "X"}
                                                               for t in tickers})
    monkeypatch.setattr(market_data, "latest_price", lambda t: 50.0)
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: pd.Series(dtype=float))
    cfg = copy.deepcopy(DEFAULTS)
    cfg["review"]["committee"] = committee
    out = review.run(paths, cfg, yes=True, dry_run=dry_run, today="2026-10-23", only=list(tickers))
    return out.parent


class _FixedTally:
    """Every measured stretch (a ticker's runs, the committee call) used 10 tokens in, 5 out."""

    def reset(self):
        pass

    def snapshot(self):
        return {"calls": 1, "tokens_in": 10, "tokens_out": 5, "nominal_cost_usd": 0.5}


@pytest.mark.unit
def test_review_skips_the_committee_when_disabled_or_dry_run(tmp_path, monkeypatch):
    llm = _structured_llm(_committee(("HRB", "BUY"), ("MSFT", "BUY")))
    off = _review(tmp_path / "off", monkeypatch, llm, committee=False)
    assert not (off / "committee.json").exists() and "Investment committee" not in (off / "ACTIONS.md").read_text()
    dry = _review(tmp_path / "dry", monkeypatch, llm, dry_run=True)
    assert not (dry / "committee.json").exists()
    one = _review(tmp_path / "one", monkeypatch, llm, tickers=("HRB",))
    assert not (one / "committee.json").exists()
    one_md = (one / "ACTIONS.md").read_text()
    assert "Investment committee: skipped (fewer than 2 successful reviews)" in one_md
    assert one_md.index("Investment committee: skipped") < one_md.index("## Holdings")
    llm.with_structured_output.return_value.invoke.assert_not_called()

    from tradingagents.llm_clients import usage

    monkeypatch.setattr(usage, "UsageTally", _FixedTally)
    on = _review(tmp_path / "on", monkeypatch, llm)
    saved = json.loads((on / "committee.json").read_text())
    assert [r["ticker"] for r in saved["ranking"]] == ["HRB", "MSFT"] and saved["summary"] == "Own the cheapest."
    assert set(saved) == {"ranking", "swaps", "concentration", "summary", "notes"}
    md = (on / "ACTIONS.md").read_text()
    assert "## Investment committee" in md
    assert "~$1.50 nominal (30 tokens in, 15 out;" in md  # two runs and the committee call
    prompt = llm.with_structured_output.return_value.invoke.call_args[0][0]
    assert "investment committee of a concentrated long-term value portfolio" in prompt and '"HRB"' in prompt


@pytest.mark.unit
def test_a_failed_committee_call_does_not_fail_the_review(tmp_path, monkeypatch):
    out = _review(tmp_path, monkeypatch, _structured_llm(error=RuntimeError("overloaded")))
    md = (out / "ACTIONS.md").read_text()
    assert "Investment committee: not available (" in md and (out / "results.json").exists()
    assert not (out / "committee.json").exists()
    assert len(json.loads((out / "results.json").read_text())) == 2


@pytest.mark.unit
def test_payload_values_a_pence_holding_in_dollars_and_names_its_currency():
    from value_pipeline import portfolio as pfmod
    from value_pipeline.committee import build_payload

    d = Decision(ticker="DNLM.L", rating="Hold", price=886.0, held_shares=10, currency="GBp", fx_to_usd=0.0133)
    pf = pfmod.Portfolio(cash=1000.0, positions=[pfmod.Position("DNLM.L", 10, 880.0, "2026-01-01")])
    _, summary = plan([d], pf.cash, SIZING)
    stock = build_payload([d], [{"ticker": "DNLM.L", "error": None}], pf, summary, SIZING)["stocks"][0]
    assert stock["currency"] == "GBp"
    assert stock["held_weight"] == pytest.approx(10 * 886 * 0.0133 / (1000 + 10 * 886 * 0.0133))


@pytest.mark.unit
def test_payload_lists_errored_reviews_apart_and_the_prompt_says_null_is_unknown():
    from value_pipeline import portfolio as pfmod
    from value_pipeline.committee import PROMPT, RankedIdea, build_payload

    decisions = _decisions()
    results = [{"ticker": d.ticker, "error": "RuntimeError: x" if d.ticker == "AAPL" else None}
               for d in decisions]
    pf = pfmod.Portfolio(cash=500.0, positions=[])
    _, summary = plan(decisions, pf.cash, SIZING)
    payload = build_payload(decisions, results, pf, summary, SIZING)
    assert [s["ticker"] for s in payload["stocks"]] == ["HRB", "MSFT"]
    assert payload["not_reviewed"] == [{"ticker": "AAPL", "error": "RuntimeError: x"}]
    assert ("A null field is unknown. Never estimate an expected return, downside or sector yourself; "
            "say it is unknown and rank that stock on what is known.") in PROMPT
    assert "Never invent one" in RankedIdea.model_fields["expected_return"].description


@pytest.mark.unit
def test_a_committee_weight_of_exactly_one_is_read_as_one_percent():
    from value_pipeline.committee import render_committee

    md = "\n".join(render_committee(_weighted(1.0), [], None))
    assert "| 1 | T1 | BUY | 1% | – | r |" in md


@pytest.mark.unit
def test_committee_payload_carries_lanes():
    from value_pipeline import portfolio as pfmod
    from value_pipeline.committee import build_payload, committee_prompt

    decisions = [Decision(ticker="INF", rating="Buy", price=10.0, lane="inflection"),
                 Decision(ticker="HRB", rating="Hold", price=50.0, held_shares=1)]
    results = [{"ticker": "INF", "lane_evidence": ["backlog +40%"]}, {"ticker": "HRB"}]
    pf = pfmod.Portfolio(cash=100.0)
    _, summary = plan(decisions, 100.0, SIZING)
    stocks = {s["ticker"]: s for s in build_payload(decisions, results, pf, summary, SIZING)["stocks"]}
    assert stocks["INF"]["lane"] == "inflection" and stocks["INF"]["lane_evidence"] == ["backlog +40%"]
    assert stocks["HRB"]["lane"] == "value" and stocks["HRB"]["lane_evidence"] == []
    prompt = committee_prompt(SIZING)
    assert "inflection at 20%" in prompt and "cut inflection positions first" in prompt
    caps = committee_prompt({**SIZING, "lane_caps": {"compounder": 0.25, "inflection": 0.15}})
    assert "compounder positions are capped at 25% and inflection at 15%" in caps


@pytest.mark.unit
def test_committee_payload_carries_the_consensus_growth_and_target():
    from value_pipeline import portfolio as pfmod
    from value_pipeline.committee import build_payload

    decisions = [Decision(ticker="HRB", rating="Hold", price=50.0), Decision(ticker="KO", rating="Hold", price=60.0)]
    results = [{"ticker": "HRB", "consensus_growth": 0.06, "consensus_target": 60.0}, {"ticker": "KO"}]
    pf = pfmod.Portfolio(cash=100.0)
    _, summary = plan(decisions, 100.0, SIZING)
    stocks = {s["ticker"]: s for s in build_payload(decisions, results, pf, summary, SIZING)["stocks"]}
    assert stocks["HRB"]["consensus_growth"] == 0.06 and stocks["HRB"]["consensus_target"] == 60.0
    assert stocks["KO"]["consensus_growth"] is None and stocks["KO"]["consensus_target"] is None
