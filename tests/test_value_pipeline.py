"""The value pipeline's arithmetic and rules on fixed sample data (no network)."""

import json
import math
from datetime import date

import pytest

from value_pipeline import benchmark, metrics, parse, portfolio as pfmod, render, screener
from value_pipeline.actions import Decision, base_action, plan, target_weight
from value_pipeline.config import DEFAULTS, Paths

SIZING = DEFAULTS["sizing"]


@pytest.fixture(autouse=True)
def _no_company_name_lookup(monkeypatch):
    """run_agents looks the company name up; never from Yahoo in a test."""
    from tradingagents.agents import context

    monkeypatch.setattr(context, "resolve_instrument_identity", lambda ticker: {})


# --- portfolio math ---------------------------------------------------------------

@pytest.mark.unit
def test_buy_sets_cash_and_cost_basis_including_fees():
    pf = pfmod.Portfolio(cash=1000.0)
    pfmod.apply_buy(pf, "abc", 2.5, 100.0, fees=5.0, on="2026-01-05")
    pos = pf.position("ABC")
    assert pf.cash == pytest.approx(745.0)
    assert pos.shares == 2.5
    assert pos.avg_price == pytest.approx(102.0)      # (250 + 5) / 2.5
    assert pos.first_bought == "2026-01-05"


@pytest.mark.unit
def test_second_buy_averages_the_price_and_keeps_first_date():
    pf = pfmod.Portfolio(cash=1000.0)
    pfmod.apply_buy(pf, "ABC", 1, 100.0, on="2026-01-05")
    pfmod.apply_buy(pf, "ABC", 3, 60.0, fees=4.0, on="2026-02-05")
    pos = pf.position("ABC")
    assert pos.shares == 4
    assert pos.avg_price == pytest.approx((100 + 180 + 4) / 4)
    assert pos.first_bought == "2026-01-05"
    assert pf.cash == pytest.approx(1000 - 100 - 184)


@pytest.mark.unit
def test_sell_adds_proceeds_keeps_average_and_closes_at_zero():
    pf = pfmod.Portfolio(cash=0.0)
    pf.cash = 500.0
    pfmod.apply_buy(pf, "ABC", 4, 100.0)
    gain = pfmod.apply_sell(pf, "ABC", 1.5, 120.0, fees=1.0)
    assert gain == pytest.approx(1.5 * 120 - 1 - 1.5 * 100)
    assert pf.position("ABC").avg_price == pytest.approx(100.0)
    assert pf.cash == pytest.approx(100 + 179)
    pfmod.apply_sell(pf, "ABC", 2.5, 90.0)
    assert pf.position("ABC") is None
    assert pf.cash == pytest.approx(279 + 225)


@pytest.mark.unit
@pytest.mark.parametrize("trade", [
    lambda pf: pfmod.apply_sell(pf, "ABC", 5, 10.0),      # more than held
    lambda pf: pfmod.apply_sell(pf, "XYZ", 1, 10.0),      # not held
    lambda pf: pfmod.apply_buy(pf, "ABC", 100, 10.0),     # more than the cash
    lambda pf: pfmod.apply_buy(pf, "ABC", -1, 10.0),
])
def test_impossible_trades_are_refused_and_change_nothing(trade):
    pf = pfmod.Portfolio(cash=100.0)
    pfmod.apply_buy(pf, "ABC", 2, 10.0)
    before = pf.to_dict()
    with pytest.raises(pfmod.TradeError):
        trade(pf)
    assert pf.to_dict() == before


@pytest.mark.unit
def test_save_backs_up_the_previous_files(tmp_path):
    paths = Paths(tmp_path)
    pf = pfmod.Portfolio(cash=100.0)
    pfmod.save(paths, pf)
    pfmod.append_trade(paths, {"date": "2026-01-05", "ticker": "ABC", "action": "BUY", "shares": 1, "price": 10})
    pf.cash = 90.0
    pfmod.save(paths, pf)
    assert pfmod.load(paths).cash == 90.0
    backups = list(paths.backups.iterdir())
    assert len(backups) == 1
    assert (backups[0] / "trades.csv").exists()
    assert '"cash": 100.0' in (backups[0] / "portfolio.json").read_text()
    assert pfmod.read_trades(paths)[0]["ticker"] == "ABC"


# --- action rules -------------------------------------------------------------------

def _held(rating, **kw):
    return Decision(ticker="HLD", rating=rating, price=kw.pop("price", 100.0), held_shares=kw.pop("shares", 10), **kw)


def _new(rating, **kw):
    return Decision(ticker="NEW", rating=rating, price=kw.pop("price", 50.0), **kw)


@pytest.mark.unit
@pytest.mark.parametrize("rating, action", [
    ("Buy", "KEEP"), ("Overweight", "KEEP"), ("Hold", "KEEP"), ("Underweight", "TRIM"), ("Sell", "SELL"),
])
def test_held_rating_rules(rating, action):
    a = base_action(_held(rating))
    assert a.action == action


@pytest.mark.unit
def test_underweight_twice_in_a_row_sells_and_other_ratings_reset_the_streak():
    first = base_action(_held("Underweight"))
    assert (first.action, first.consecutive_underweight) == ("TRIM", 1)
    second = base_action(_held("Underweight", consecutive_underweight=1))
    assert (second.action, second.consecutive_underweight) == ("SELL", 2)
    assert base_action(_held("Hold", consecutive_underweight=1)).consecutive_underweight == 0


@pytest.mark.unit
def test_price_above_intrinsic_value_is_flagged():
    a = base_action(_held("Hold", price=120.0, intrinsic_value=110.0))
    assert a.action == "KEEP"
    assert any("REACHED VALUE" in f for f in a.flags)
    assert not base_action(_held("Hold", price=100.0, intrinsic_value=110.0)).flags


@pytest.mark.unit
@pytest.mark.parametrize("rating, action", [("Buy", "BUY"), ("Overweight", "BUY"), ("Hold", "SKIP"), ("Sell", "SKIP")])
def test_new_candidate_rules(rating, action):
    assert base_action(_new(rating)).action == action


@pytest.mark.unit
def test_never_buy_or_add_in_freefall():
    assert base_action(_new("Buy", trend="FREEFALL")).action == "SKIP"
    actions, _ = plan([_held("Buy", shares=1, trend="FREEFALL")], cash=10_000, sizing=SIZING)
    assert actions[0].action == "KEEP"


@pytest.mark.unit
def test_target_weight_is_equal_weight_capped_at_the_max():
    assert target_weight(1, SIZING) == pytest.approx(0.10)   # 1/5 capped at 10%
    assert target_weight(20, SIZING) == pytest.approx(0.05)
    assert target_weight(1, {**SIZING, "max_position_weight": 0.5}) == pytest.approx(0.20)  # min 5 holdings


@pytest.mark.unit
def test_buy_sizes_full_and_overweight_half_with_fractional_shares():
    decisions = [_new("Buy", price=40.0), Decision("OVW", "Overweight", price=30.0)]
    actions, summary = plan(decisions, cash=10_000.0, sizing=SIZING)
    buy, ovw = actions
    assert summary["target_weight"] == pytest.approx(0.10)
    assert buy.amount == pytest.approx(1000.0) and buy.shares == pytest.approx(25.0)
    assert ovw.amount == pytest.approx(500.0) and ovw.shares == pytest.approx(16.6667)


@pytest.mark.unit
def test_buys_never_exceed_cash_and_tiny_buys_are_skipped():
    decisions = [Decision("A", "Buy", price=10.0, margin_of_safety=0.5),
                 Decision("B", "Buy", price=10.0, margin_of_safety=0.3)]
    # Total 1,010: each target is 101; only 120 cash, so A gets 101, B the 19 left (below the 25 minimum).
    actions, summary = plan(decisions, cash=120.0, sizing=SIZING, others=[("H", 890.0, None)])
    by = {a.ticker: a for a in actions}
    assert by["A"].action == "BUY" and by["A"].amount == pytest.approx(101.0)
    assert by["B"].action == "SKIP" and "not enough cash" in by["B"].reason
    assert summary["cash_after"] >= 0


@pytest.mark.unit
def test_sale_proceeds_fund_buys_and_gaps_below_the_minimum_are_left():
    decisions = [_held("Sell", shares=10, price=100.0),                       # frees 1,000
                 Decision("NEWBUY", "Buy", price=10.0),                        # target 10% of 1,100 = 110
                 Decision("ADDME", "Buy", price=50.0, held_shares=2)]          # holds 100: gap 10 < 25 minimum
    actions, summary = plan(decisions, cash=0.0, sizing=SIZING)
    sell, new, add = actions
    assert sell.action == "SELL" and sell.amount == pytest.approx(1000.0)
    assert new.action == "BUY" and new.amount == pytest.approx(110.0) and new.shares == pytest.approx(11.0)
    assert add.action == "KEEP"
    assert summary["cash_after"] == pytest.approx(890.0)


@pytest.mark.unit
def test_add_fills_the_gap_to_target():
    decisions = [Decision("ADDME", "Buy", price=50.0, held_shares=2)]  # 100 held, total 10,100
    actions, _ = plan(decisions, cash=10_000.0, sizing=SIZING)
    assert actions[0].action == "ADD"
    assert actions[0].amount == pytest.approx(1010.0 - 100.0)


# --- metrics ---------------------------------------------------------------------------

PREV = {"revenue": 1000, "gross_profit": 400, "ebit": 150, "net_income": 80, "total_assets": 1000, "current_assets": 400,
            "current_liabilities": 250, "long_term_debt": 300, "shares": 100, "operating_cash_flow": 90}
CUR = {"revenue": 1200, "gross_profit": 500, "ebit": 200, "net_income": 110, "total_assets": 1050, "current_assets": 450,
           "current_liabilities": 240, "long_term_debt": 250, "shares": 98, "operating_cash_flow": 150}


@pytest.mark.unit
def test_piotroski_all_nine_points_pass():
    fs = metrics.piotroski(CUR, PREV)
    assert fs["points"] == dict.fromkeys(metrics.FSCORE_POINTS, True)
    assert (fs["score"], fs["missing"]) == (9, 0)


@pytest.mark.unit
def test_piotroski_each_point_can_fail():
    bad = dict(CUR, net_income=-10, operating_cash_flow=-20, long_term_debt=400, current_assets=300,
               shares=120, gross_profit=300, revenue=900)
    fs = metrics.piotroski(bad, PREV)
    assert fs["score"] == 0, fs["points"]


@pytest.mark.unit
def test_piotroski_missing_data_is_counted_not_guessed():
    fs = metrics.piotroski(dict(CUR, shares=None, gross_profit=None), PREV)
    assert fs["points"]["no_dilution"] is None and fs["points"]["gross_margin_rising"] is None
    assert (fs["score"], fs["missing"]) == (7, 2)


@pytest.mark.unit
def test_piotroski_missing_long_term_debt_is_zero_only_for_a_debt_free_company():
    assert metrics.piotroski(dict(CUR, long_term_debt=None, total_debt=0), PREV)["points"]["leverage_falling"] is True
    assert metrics.piotroski(dict(CUR, long_term_debt=None, total_debt=900), PREV)["points"]["leverage_falling"] is None


@pytest.mark.unit
def test_share_count_prefers_market_cap_over_price_to_one_class_count():
    # FOX: Yahoo's sharesOutstanding is class B only (220M); market cap / price covers every class.
    assert metrics.share_count(23_567e6, 55.89, statement=439e6, reported=220e6) == pytest.approx(421.67e6, rel=1e-3)
    assert metrics.share_count(None, 55.89, statement=439e6, reported=220e6) == 439e6
    assert metrics.share_count(None, None, statement=None, reported=220e6) == 220e6
    assert metrics.share_count(0, 0, statement=None, reported=None) is None


@pytest.mark.unit
def test_screen_values_per_share_on_market_cap_over_price():
    year = {"fiscal_year_end": "2025-12-31", "ebit": 200, "free_cash_flow": 150, "current_assets": 400,
            "cash": 0, "current_liabilities": 200, "net_ppe": 100, "shares": 440, "total_debt": 0}
    data = {"ticker": "FOX", "years": [year, year], "market_cap": 8800, "price": 20, "shares": 220, "closes": []}
    row = screener.evaluate(data)
    assert row["shares"] == pytest.approx(440)
    assert row["value_base"] == pytest.approx(metrics.formula_value([year, year], 20, 440, 0)["base"])


@pytest.mark.unit
def test_roic_floors_working_capital_at_zero_and_excludes_cash():
    assert metrics.roic({"ebit": 100, "current_assets": 500, "cash": 100, "current_liabilities": 200, "net_ppe": 300}) == \
        pytest.approx(100 / (200 + 300))
    assert metrics.roic({"ebit": 100, "current_assets": 100, "cash": 50, "current_liabilities": 400, "net_ppe": 200}) == \
        pytest.approx(0.5)


@pytest.mark.unit
def test_earnings_yield_and_fcf_yield():
    ev = metrics.enterprise_value(1000, 300, 100)
    assert ev == 1200
    assert metrics.earnings_yield(120, ev) == pytest.approx(0.10)
    assert metrics.earnings_yield(120, -5) is None
    assert metrics.fcf_yield(50, 1000) == pytest.approx(0.05)


@pytest.mark.unit
def test_magic_formula_sums_ranks_and_breaks_ties_with_fcf_yield():
    rows = [
        {"ticker": "A", "earnings_yield": 0.15, "roic": 0.20, "fcf_yield": 0.03},  # ranks 1 + 3 = 4
        {"ticker": "B", "earnings_yield": 0.10, "roic": 0.50, "fcf_yield": 0.08},  # ranks 3 + 1 = 4
        {"ticker": "C", "earnings_yield": 0.12, "roic": 0.30, "fcf_yield": 0.05},  # ranks 2 + 2 = 4
        {"ticker": "D", "earnings_yield": 0.05, "roic": 0.10, "fcf_yield": 0.20},  # 4 + 4 = 8
        {"ticker": "E", "earnings_yield": None, "roic": 0.9},                      # left out
    ]
    ranked = metrics.magic_formula_rank(rows)
    assert [r["ticker"] for r in ranked] == ["B", "C", "A", "D"]
    assert ranked[0]["mf_score"] == 4


@pytest.mark.unit
def test_altman_z_matches_the_formula():
    year = {"total_assets": 1000, "total_liabilities": 400, "current_assets": 500, "current_liabilities": 200,
                "retained_earnings": 300, "ebit": 150, "revenue": 1200}
    expected = 1.2 * 0.3 + 1.4 * 0.3 + 3.3 * 0.15 + 0.6 * (2000 / 400) + 1.0 * 1.2
    assert metrics.altman_z(year, 2000) == pytest.approx(expected)
    assert metrics.altman_z(dict(year, retained_earnings=None), 2000) is None


@pytest.mark.unit
def test_interest_coverage_and_debt_to_equity():
    assert metrics.interest_coverage({"ebit": 100, "interest_expense": -20}) == 5
    assert metrics.interest_coverage({"ebit": 100, "interest_expense": None, "total_debt": 0}) == math.inf
    assert metrics.interest_coverage({"ebit": 100, "interest_expense": None}) == math.inf
    # Debt with no interest row is unknown coverage, not infinite: Yahoo drops the row for some borrowers.
    assert metrics.interest_coverage({"ebit": 100, "interest_expense": None, "total_debt": 8_500}) is None
    assert metrics.debt_to_equity({"total_debt": 50, "equity": 100}) == 0.5
    assert metrics.debt_to_equity({"total_debt": 50, "equity": -1}) is None


def _series(*legs):
    """Concatenate linear price legs: (start, end, days)."""
    out = []
    for start, end, days in legs:
        out += [start + (end - start) * i / (days - 1) for i in range(days)]
    return out


@pytest.mark.unit
@pytest.mark.parametrize("closes, label", [
    (_series((50, 100, 400)), "UPTREND"),
    (_series((100, 50, 400)), "FREEFALL"),                         # falling, at new lows
    (_series((100, 50, 360), (50, 56, 40)), "DOWNTREND"),          # bounced off the low, still under a falling SMA
    (_series((100, 60, 200), (60, 60, 200)), "BASING"),            # flat after a decline
])
def test_trend_classification(closes, label):
    assert metrics.trend(closes)["trend"] == label


@pytest.mark.unit
def test_trend_needs_enough_history():
    assert metrics.trend([10.0] * 100)["trend"] is None


# --- screen filters -----------------------------------------------------------------------

GOOD = {"ticker": "GOOD", "market_cap": 5e9, "fscore": 7, "altman_z": 3.0, "free_cash_flow": 1e8,
        "interest_coverage": 10, "earnings_yield_avg": 0.1, "roic_ranked": 0.3, "roic_min": 0.2,
        "dilution_per_year": 0.0, "fcf_yield": 0.05, "trend": "UPTREND"}


@pytest.mark.unit
def test_hard_filters():
    f = DEFAULTS["filters"]
    assert screener.failed_filters(GOOD, f) == []
    bad = dict(GOOD, market_cap=1e9, fscore=5, altman_z=1.5, free_cash_flow=-1, interest_coverage=3)
    assert screener.failed_filters(bad, f) == ["market cap", "F-Score", "Altman Z", "FCF", "interest coverage"]


@pytest.mark.unit
def test_screen_rows_carry_accounting_columns_and_optional_filters():
    year = {"fiscal_year_end": "2025-12-31", "revenue": 1000, "ebit": 200, "net_income": 150,
            "total_assets": 2000, "operating_cash_flow": 220, "current_assets": 400, "cash": 0,
            "current_liabilities": 200, "net_ppe": 100, "shares": 100, "total_debt": 0}
    row = screener.evaluate({"ticker": "ACC", "years": [year, year], "market_cap": 2000, "price": 20, "closes": []})
    assert {"accruals_ratio", "beneish_m", "accounting_flags"} <= set(row)
    f = DEFAULTS["filters"]
    assert "accruals" in screener.failed_filters(dict(GOOD, accruals_ratio=0.0), {**f, "max_accruals_ratio": -1.0})
    assert "accruals" not in screener.failed_filters(dict(GOOD, accruals_ratio=0.0), f)
    assert "M-score" in screener.failed_filters(dict(GOOD, beneish_m=-1.0), {**f, "max_beneish_m": -1.78})
    assert "M-score" not in screener.failed_filters(dict(GOOD, beneish_m=None), {**f, "max_beneish_m": -1.78})


@pytest.mark.unit
def test_rank_drops_owned_and_freefall_from_the_top_list():
    rows = [dict(GOOD, ticker="A", earnings_yield_avg=0.2, fail_reasons=[]),
            dict(GOOD, ticker="B", earnings_yield_avg=0.15, trend="FREEFALL", fail_reasons=[]),
            dict(GOOD, ticker="C", earnings_yield_avg=0.1, fail_reasons=[]),
            dict(GOOD, ticker="D", fail_reasons=["F-Score"])]
    res = screener.rank(rows, DEFAULTS["filters"], owned={"A"}, top_n=10)
    assert [r["ticker"] for r in res["top"]] == ["C"]
    assert [r["ticker"] for r in res["knives"]] == ["B"]
    assert len(res["ranked"]) == 3


# --- parsing, benchmark, rendering ----------------------------------------------------------

DECISION = """**Rating**: Buy

**Executive Summary**: Start a 10% position.

**Investment Thesis**: High ROIC business at 12x EBIT. Intrinsic value about $180 per share; Margin of safety: 32%.

**Price Target**: 182.5

**Time Horizon**: 2-3 years"""


@pytest.mark.unit
def test_parse_value_and_margin_of_safety():
    assert parse.intrinsic_value(DECISION) == 182.5
    no_target = DECISION.replace("182.5", "not provided")
    assert parse.intrinsic_value(no_target) == 180.0
    assert parse.margin_of_safety(DECISION) == pytest.approx(0.32)
    assert parse.thesis_summary(DECISION).startswith("High ROIC business")
    assert parse.intrinsic_value("no numbers here") is None


@pytest.mark.unit
def test_intrinsic_value_fallback_skips_growth_rates_and_prefers_the_base_case():
    assert parse.intrinsic_value("Intrinsic value (3% growth scenario): $142 per share") == 142.0
    assert parse.intrinsic_value("intrinsic value at 3% growth is $62") == 62.0
    assert parse.intrinsic_value("intrinsic value range $120-$160, base $140") == 140.0
    assert parse.intrinsic_value("**Price Target**: ~$62\n") == 62.0
    assert parse.intrinsic_value("intrinsic value fell 15% this year") is None


@pytest.mark.unit
def test_margin_of_safety_fallback_skips_the_required_threshold():
    text = "we require a margin of safety of at least 30% and today it is 12%"
    assert parse.margin_of_safety(text) == pytest.approx(0.12)
    assert parse.margin_of_safety("the margin of safety (minimum 30%) is not met") is None


@pytest.mark.unit
def test_spy_shadow_replays_the_same_cash_flows():
    deposits = [{"date": "2026-01-02", "amount": 1000}]
    trades = [{"date": "2026-01-05", "action": "BUY", "shares": 5, "price": 100, "fees": 0},
              {"date": "2026-03-02", "action": "SELL", "shares": 2, "price": 150, "fees": 0}]
    spy = {"2026-01-05": 500.0, "2026-03-02": 600.0}
    out = benchmark.replay(benchmark.cash_flows(deposits, trades), spy.__getitem__, price_now=550.0)
    # bought 1 SPY with 500, sold 300/600 = 0.5 SPY; 0.5 SPY + 800 cash
    assert out["value"] == pytest.approx(0.5 * 550 + 800)
    assert out["invested"] == 1000


@pytest.mark.unit
def test_action_page_is_self_contained():
    actions, summary = plan([_held("Hold"), _new("Buy")], cash=50_000.0, sizing=SIZING)
    md = render.actions_markdown("2026-11-07", actions, summary, reports={"NEW": "NEW/complete_report.html"},
                                 comparison=None, cost=4.0, failures=[], dry_run=False)
    assert "| HLD | Hold | KEEP |" in md and "| NEW | Buy |" in md
    page = render.actions_html(md, "Value review")
    assert "<script" not in page and "http" not in page.replace('href="NEW/complete_report.html"', "")
    assert 'name="viewport"' in page


@pytest.mark.unit
def test_config_reads_numbers_yaml_leaves_as_strings(tmp_path):
    from value_pipeline.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("filters:\n  min_market_cap: 2.0e9\n  min_fscore: 7\n")
    cfg = load_config(path)
    assert cfg["filters"]["min_market_cap"] == 2.0e9
    assert cfg["filters"]["min_fscore"] == 7
    assert cfg["filters"]["min_altman_z"] == DEFAULTS["filters"]["min_altman_z"]


@pytest.mark.unit
def test_review_reads_statements_from_the_configured_vendor_only():
    from tradingagents.default_config import DEFAULT_CONFIG
    from value_pipeline.review import STATEMENT_TOOLS, graph_config

    cfg = graph_config("yfinance")
    assert all(cfg["tool_vendors"][t] == "yfinance" for t in STATEMENT_TOOLS)
    assert "get_insider_transactions" not in cfg["tool_vendors"]
    assert DEFAULT_CONFIG["tool_vendors"].get("get_balance_sheet") is None   # the shared default is untouched
    assert graph_config(None)["tool_vendors"] == DEFAULT_CONFIG["tool_vendors"]


@pytest.mark.unit
def test_agents_value_far_from_the_formula_is_flagged():
    from value_pipeline.actions import value_gap_flag

    assert value_gap_flag(_new("Buy", intrinsic_value=120.0, formula_value=100.0)) is None
    flag = value_gap_flag(_new("Buy", intrinsic_value=150.0, formula_value=100.0))
    assert flag and "+50%" in flag
    assert any("vs formula" in f for f in base_action(_held("Hold", intrinsic_value=50.0, formula_value=100.0)).flags)


@pytest.mark.unit
def test_normalized_figures_average_years_and_measure_dilution():
    years = [
        {"ebit": 300, "current_assets": 500, "cash": 100, "current_liabilities": 200, "net_ppe": 300, "shares": 121},
        {"ebit": 100, "current_assets": 500, "cash": 100, "current_liabilities": 200, "net_ppe": 300, "shares": 110},
        {"ebit": 200, "current_assets": 500, "cash": 100, "current_liabilities": 200, "net_ppe": 300, "shares": 100},
    ]
    n = metrics.normalized(years)
    assert n["ebit_avg"] == pytest.approx(200)
    assert n["roic_avg"] == pytest.approx(200 / 500) and n["roic_min"] == pytest.approx(100 / 500)
    assert n["dilution_per_year"] == pytest.approx(0.10)      # 100 -> 121 over two years


@pytest.mark.unit
def test_screen_ranks_on_averages_and_caps_roic():
    peak = {"fiscal_year_end": "2025-12-31", "ebit": 500, "current_assets": 400, "cash": 0,
            "current_liabilities": 200, "net_ppe": 100, "shares": 100, "total_debt": 0}
    trough = dict(peak, ebit=100)
    data = {"ticker": "CYC", "years": [peak, trough, trough, trough, trough], "market_cap": 2000,
            "enterprise_value": 2000, "price": 20, "closes": []}
    row = screener.evaluate(data, normalize_years=5, roic_cap=1.0)
    assert row["earnings_yield"] == pytest.approx(0.25)            # the peak year alone
    assert row["earnings_yield_avg"] == pytest.approx(180 / 2000)  # what the rank uses
    assert row["roic_avg"] == pytest.approx((500 / 300 + 4 * 100 / 300) / 5)
    assert row["roic_ranked"] == pytest.approx(row["roic_avg"])    # 60%: under the cap
    asset_light = dict(data, years=[dict(peak, net_ppe=1)] * 5)
    assert screener.evaluate(asset_light, roic_cap=1.0)["roic_ranked"] == 1.0


@pytest.mark.unit
def test_screen_values_at_the_given_discount_rate():
    year = {"fiscal_year_end": "2025-12-31", "ebit": 200, "free_cash_flow": 150, "current_assets": 400,
            "cash": 0, "current_liabilities": 200, "net_ppe": 100, "shares": 100, "total_debt": 0}
    data = {"ticker": "ABC", "years": [year, year], "market_cap": 2000, "price": 20, "closes": []}
    at_9, at_10 = screener.evaluate(data), screener.evaluate(data, rate=0.10)
    assert at_10["discount_rate"] == 0.10 and at_9["discount_rate"] == 0.09
    assert at_10["value_base"] < at_9["value_base"]


@pytest.mark.unit
def test_unstable_roic_and_dilution_fail_the_filters():
    f = DEFAULTS["filters"]
    assert "ROIC unstable" in screener.failed_filters(dict(GOOD, roic_min=0.02), f)
    assert "dilution" in screener.failed_filters(dict(GOOD, dilution_per_year=0.06), f)
    assert screener.failed_filters(dict(GOOD, roic_min=None, dilution_per_year=None), f) == []


@pytest.mark.unit
def test_holding_above_the_max_weight_is_trimmed_back_to_the_cap():
    big = Decision("BIG", "Hold", price=100.0, held_shares=500)              # 50,000 of 51,000
    actions, _ = plan([big], cash=1_000.0, sizing=SIZING)                    # cap 10% = 5,100
    a = actions[0]
    assert a.action == "TRIM" and "above the 10% max position" in a.reason
    assert a.shares == pytest.approx(449) and a.amount == pytest.approx(44_900.0)
    # Just over the cap by less than the minimum trade: left alone.
    small = Decision("BIG", "Hold", price=100.0, held_shares=50.1)           # 5,010 of 50,010 = 10.02%
    assert plan([small], cash=45_000.0, sizing=SIZING)[0][0].action == "KEEP"
    # A Buy-rated holding over the cap is trimmed too, never added to.
    hot = Decision("HOT", "Buy", price=100.0, held_shares=500)
    assert plan([hot], cash=1_000.0, sizing=SIZING)[0][0].action == "TRIM"


@pytest.mark.unit
def test_plan_counts_holdings_that_were_not_reviewed():
    sizing = {**SIZING, "max_sector_weight": 0.30}
    others = [("A", 90_000.0, "Tech"), ("B", 5_000.0, "Health")]              # unreviewed, 95,000
    buy = Decision("NEW", "Buy", price=10.0, sector="Health")
    actions, summary = plan([buy], cash=5_000.0, sizing=sizing, others=others)
    assert summary["total_value"] == pytest.approx(100_000.0)
    assert actions[0].amount == pytest.approx(5_000.0)                        # target 10% of 100k, cash-limited
    tech = Decision("NEW", "Buy", price=10.0, sector="Tech")                  # Tech already at 90%
    actions, _ = plan([tech], cash=5_000.0, sizing=sizing, others=others)
    assert actions[0].action == "SKIP" and "sector cap" in actions[0].reason


@pytest.mark.unit
def test_buy_rating_above_the_agents_own_value_is_not_bought():
    a = base_action(_new("Buy", price=10.0, intrinsic_value=8.0))
    assert a.action == "SKIP" and "above" in a.reason
    assert base_action(_new("Overweight", price=10.0, margin_of_safety=-0.25)).action == "SKIP"
    assert base_action(_new("Buy", price=10.0, intrinsic_value=15.0)).action == "BUY"


@pytest.mark.unit
def test_underweight_again_within_three_weeks_is_not_a_repeat():
    rerun = base_action(_held("Underweight", consecutive_underweight=1, days_since_last_review=3))
    assert rerun.action == "KEEP" and rerun.consecutive_underweight == 1 and "re-run" in rerun.reason
    later = base_action(_held("Underweight", consecutive_underweight=1, days_since_last_review=30))
    assert later.action == "SELL" and later.consecutive_underweight == 2
    first = base_action(_held("Underweight", consecutive_underweight=0, days_since_last_review=3))
    assert first.action == "TRIM" and first.consecutive_underweight == 1


@pytest.mark.unit
def test_a_stated_zero_margin_ranks_ahead_of_no_margin():
    a = Decision("A", "Buy", price=10.0, margin_of_safety=None)
    z = Decision("Z", "Buy", price=10.0, margin_of_safety=0.0)
    actions, _ = plan([a, z], cash=130.0, sizing=SIZING, others=[("H", 1_000.0, None)])   # targets 113: cash for one
    by = {x.ticker: x for x in actions}
    assert by["Z"].action == "BUY" and by["A"].action == "SKIP"


@pytest.mark.unit
def test_unreviewed_holdings_above_the_cap_are_named_in_the_summary():
    _, summary = plan([_new("Hold")], cash=1_000.0, sizing=SIZING, others=[("BIG", 9_000.0, None), ("OK", 500.0, None)])
    assert summary["over_cap"] == [("BIG", 0.8571)]
    md = render.actions_markdown("2026-11-07", [], summary, reports={}, comparison=None, cost=0.0, failures=[], dry_run=False)
    assert "BIG at 86%" in md and "not reviewed" in md


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


class _GrowingTally:
    def __init__(self):
        self.resets, self.snaps = 0, 0

    def reset(self):
        self.resets += 1

    def snapshot(self):
        self.snaps += 1
        return {"calls": 1, "tokens_in": 10 * self.snaps, "tokens_out": 5 * self.snaps,
                "nominal_cost_usd": 0.5 * self.snaps}


class _FlakyGraph:
    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def propagate(self, ticker, day, portfolio=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        return {"final_trade_decision": "**Rating**: Hold"}, "Hold"

    def save_reports(self, state, ticker, save_path, html):
        pass


@pytest.mark.unit
def test_run_agents_usage_counts_a_failed_first_attempt(tmp_path):
    from value_pipeline import review

    tally = _GrowingTally()
    r = review.run_agents(_FlakyGraph(1), "HRB", "2026-10-22", None, tmp_path, tally)
    assert r.error is None and tally.resets == 1
    assert (r.tokens_in, r.tokens_out, r.nominal_cost_usd) == (10, 5, 0.5)


@pytest.mark.unit
def test_run_agents_usage_is_kept_when_both_attempts_fail(tmp_path):
    from value_pipeline import review

    tally = _GrowingTally()
    r = review.run_agents(_FlakyGraph(99), "HRB", "2026-10-22", None, tmp_path, tally)
    assert r.error and tally.resets == 1
    assert (r.tokens_in, r.tokens_out, r.nominal_cost_usd) == (10, 5, 0.5)


@pytest.mark.unit
def test_candidates_are_dropped_to_fit_the_cost_cap():
    from value_pipeline.review import fit_to_cap

    tickers = ["H1", "H2", "C1", "C2", "C3"]
    assert fit_to_cap(tickers, n_held=2, cost_per_run=2.0, limit=40.0) == (tickers, 3, 0)
    assert fit_to_cap(tickers, n_held=2, cost_per_run=2.0, limit=7.0) == (["H1", "H2", "C1"], 1, 2)
    assert fit_to_cap(tickers, n_held=2, cost_per_run=2.0, limit=3.0) == (["H1", "H2"], 0, 3)  # holdings alone over


@pytest.mark.unit
def test_sector_falls_back_to_the_cached_fundamentals(tmp_path):
    from value_pipeline import market_data
    from value_pipeline.review import _sector

    paths = Paths(tmp_path)
    paths.cache.mkdir(parents=True)
    (paths.cache / "HRB.json").write_text('{"ticker": "HRB", "sector": "Consumer Cyclical"}')
    assert market_data.cached_sector("HRB", paths) == "Consumer Cyclical"
    assert _sector(paths, "HRB", {"sector": "Industrials"}) == "Industrials"
    assert _sector(paths, "HRB", None) == "Consumer Cyclical"
    assert _sector(paths, "XYZ", None) is None


@pytest.mark.unit
def test_sector_cap_limits_buys_and_counts_sales():
    sizing = {**SIZING, "max_sector_weight": 0.30, "max_position_weight": 0.5}
    held = [Decision("T1", "Hold", price=100.0, held_shares=25, sector="Tech")]       # 2,500 of 10,000
    new = [Decision("T2", "Buy", price=10.0, sector="Tech"), Decision("H1", "Buy", price=10.0, sector="Health")]
    actions, _ = plan([*held, *new], cash=7_500.0, sizing=sizing)
    by = {a.ticker: a for a in actions}
    assert by["T2"].amount == pytest.approx(500.0) and "sector cap" in by["T2"].reason   # 3,000 - 2,500
    assert by["H1"].amount == pytest.approx(2_000.0)                                     # target 1/5 of 10,000
    # Selling the Tech holding frees the cap for the new Tech buy.
    actions, _ = plan([Decision("T1", "Sell", price=100.0, held_shares=25, sector="Tech"), new[0]],
                      cash=7_500.0, sizing=sizing)
    assert actions[1].amount == pytest.approx(2_000.0)


@pytest.mark.unit
def test_weekly_watch_alerts():
    from value_pipeline.watch import alerts

    holdings = [{"ticker": "RICH", "intrinsic_value": 100.0}, {"ticker": "FALL", "intrinsic_value": 100.0},
                {"ticker": "OK", "intrinsic_value": 100.0}]
    ideas = [{"ticker": "CHEAP", "rating": "Buy", "intrinsic_value": 100.0},
             {"ticker": "KNIFE", "rating": "Buy", "intrinsic_value": 100.0},
             {"ticker": "HOLDER", "rating": "Hold", "intrinsic_value": 100.0},
             {"ticker": "FAIR", "rating": "Overweight", "intrinsic_value": 100.0}]
    prices = {"RICH": 110.0, "FALL": 60.0, "OK": 90.0, "CHEAP": 65.0, "KNIFE": 50.0, "HOLDER": 50.0, "FAIR": 80.0}
    trends = {"FALL": "FREEFALL", "KNIFE": "FREEFALL"}
    got = {(a.ticker, a.kind) for a in alerts(holdings, ideas, prices, trends, buy_mos=0.30)}
    assert got == {("RICH", "REACHED VALUE"), ("FALL", "FREEFALL"), ("CHEAP", "BUY RANGE")}


@pytest.mark.unit
def test_watch_ignores_buy_ratings_older_than_the_limit():
    from value_pipeline.watch import alerts

    ideas = [{"ticker": "OLD", "rating": "Buy", "intrinsic_value": 100.0, "date": "2026-01-10"},
             {"ticker": "NEW", "rating": "Buy", "intrinsic_value": 100.0, "date": "2026-09-20"}]
    prices = {"OLD": 60.0, "NEW": 60.0}
    got = {a.ticker for a in alerts([], ideas, prices, {}, buy_mos=0.30, today=date(2026, 10, 17), max_age_days=120)}
    assert got == {"NEW"}


@pytest.mark.unit
def test_latest_results_carry_their_review_date(tmp_path):
    from value_pipeline.watch import _latest_results

    paths = Paths(tmp_path)
    for folder, ticker in (("2026-09-05", "A"), ("2026-10-11", "B")):
        (paths.reports / folder).mkdir(parents=True)
        (paths.reports / folder / "results.json").write_text(
            f'[{{"ticker": "{ticker}", "rating": "Buy", "intrinsic_value": 10, "error": null}}]')
    assert {r["ticker"]: r["date"] for r in _latest_results(paths)} == {"A": "2026-09-05", "B": "2026-10-11"}


@pytest.mark.unit
def test_review_folders_newest_first_without_dry_runs(tmp_path):
    from value_pipeline.review import review_dirs

    paths = Paths(tmp_path)
    for name in ("2026-09-05", "2026-10-11", "2026-10-11-2", "2026-10-11-10", "2026-10-11-dry-run"):
        (paths.reports / name).mkdir(parents=True)
    (paths.reports / "watch_2026-10-17.md").write_text("x")
    assert [f.name for f in review_dirs(paths)] == ["2026-10-11-10", "2026-10-11-2", "2026-10-11", "2026-09-05"]


@pytest.mark.unit
def test_labelled_margin_of_safety_beats_a_threshold_in_prose():
    text = ("A ~20% margin of safety, not 30%+, so Overweight.\\n\\n"
            "**Investment Thesis**: ... Margin of safety: 20% at $44 ...")
    assert parse.margin_of_safety(text) == pytest.approx(0.20)
    assert parse.margin_of_safety("a margin of safety near 25% here") == pytest.approx(0.25)


@pytest.mark.unit
def test_review_runs_holdings_and_candidates_or_only_the_named_tickers():
    from value_pipeline.review import select_tickers

    assert select_tickers(["HRB"], ["CI", "G"], None) == (["HRB", "CI", "G"], 1, 2)
    assert select_tickers(["HRB"], [], ["hog", " ingr", "HRB", "hog"]) == (["HOG", "INGR", "HRB"], 1, 2)


@pytest.mark.unit
def test_screen_ranks_passers_by_expected_return_in_er_rank():
    rows = [dict(GOOD, ticker="A", earnings_yield_avg=0.2, expected_return=0.05, fail_reasons=[]),
            dict(GOOD, ticker="B", earnings_yield_avg=0.1, expected_return=0.12, fail_reasons=[]),
            dict(GOOD, ticker="C", earnings_yield_avg=0.1, expected_return=None, fail_reasons=[]),
            dict(GOOD, ticker="D", expected_return=0.30, fail_reasons=["F-Score"])]
    screener.rank(rows, DEFAULTS["filters"], owned=set(), top_n=10)
    by = {r["ticker"]: r.get("er_rank") for r in rows}
    assert by == {"A": 2, "B": 1, "C": None, "D": None}


@pytest.mark.unit
def test_actions_show_expected_return_for_candidates():
    actions, summary = plan([_new("Hold", expected_return=0.09)], cash=50_000.0, sizing=SIZING)
    md = render.actions_markdown("2026-11-07", actions, summary, reports={}, comparison=None,
                                 cost=0.0, failures=[], dry_run=False)
    assert "expected return 9%/yr" in md
    buy, _ = plan([_new("Buy", expected_return=0.09)], cash=50_000.0, sizing=SIZING)
    md = render.actions_markdown("2026-11-07", buy, summary, reports={}, comparison=None,
                                 cost=0.0, failures=[], dry_run=False)
    assert "| Expected return |" in md and "9%/yr" in md


@pytest.mark.unit
def test_sector_median_ev_ebit_sets_the_reversion_target_and_reorders_er_rank():
    def yr(ebit):
        return {"fiscal_year_end": "2025-12-31", "ebit": ebit, "free_cash_flow": 150, "stock_based_comp": 0, "current_assets": 400,
                "cash": 0, "current_liabilities": 200, "net_ppe": 300, "shares": 100, "total_debt": 0,
                "revenue": 1000}
    inputs, rows = {}, []
    for t, ebit in (("A", 100), ("B", 200), ("C", 400)):         # EV/EBIT 20, 10, 5 at EV 2000
        inputs[t] = ([yr(ebit)] * 5, 20.0, 2000, 100)
        row = {"ticker": t, "sector": "Tech", "fail_reasons": []}
        row.update(screener.evaluate({"ticker": t, "years": [yr(ebit)] * 5, "market_cap": 2000,
                                      "enterprise_value": 2000, "price": 20.0, "closes": []}))
        row["sector"] = "Tech"
        rows.append(row)
    assert {r["er_target_multiple"] for r in rows} == {12.0}
    screener.apply_sector_targets(rows, inputs)
    assert {r["er_target_multiple"] for r in rows} == {10.0}      # median of 20, 10, 5
    assert [r["ev_ebit"] for r in rows] == pytest.approx([20.0, 10.0, 5.0])
    screener.rank(rows, DEFAULTS["filters"], owned=set(), top_n=10)
    # C trades at 5x against a 10x sector median, so reversion lifts it most
    ranked = sorted(rows, key=lambda r: r["expected_return"], reverse=True)
    assert [r["ticker"] for r in ranked] == ["C", "B", "A"]
    assert {r["ticker"]: r["er_rank"] for r in rows} == {"C": 1, "B": 2, "A": 3}


@pytest.mark.unit
def test_value_gap_flag_prints_the_bridge():
    from value_pipeline.actions import value_gap_flag

    bridge = [{"reason": "peak-cycle margins", "amount": -12.0}, {"reason": "net debt", "amount": -12.33}]
    flag = value_gap_flag(_new("Buy", intrinsic_value=56.0, formula_value=80.33, value_bridge=bridge))
    assert flag.endswith("; bridge: 80.33 → -12.00 (peak-cycle margins) → -12.33 (net debt) = 56.00")
    assert "bridge" not in value_gap_flag(_new("Buy", intrinsic_value=56.0, formula_value=80.33))


@pytest.mark.unit
def _review_one(tmp_path, monkeypatch, decision, price):
    """A dry-run review of HRB whose agents return ``decision``: the results.json row and the Decision."""
    import json

    import pandas as pd

    from tradingagents.agents.schemas import render_pm_decision
    from value_pipeline import market_data, operations as ops, review

    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    text = render_pm_decision(decision)
    monkeypatch.setattr(review, "fake_run", lambda t, i, held, price: review.RunResult(t, "Overweight", text))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {"HRB": {"ticker": "HRB", "value_base": 80.33,
                                                                        "trend": "UPTREND", "sector": "X"}})
    monkeypatch.setattr(market_data, "latest_price", lambda t: price)
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: pd.Series(dtype=float))
    planned = {}
    real_plan = review.plan

    def plan(decisions, *args):
        planned["d"] = decisions
        return real_plan(decisions, *args)

    monkeypatch.setattr(review, "plan", plan)
    out = review.run(paths, DEFAULTS, yes=True, dry_run=True, today="2026-10-23", only=["HRB"])
    return json.loads((out.parent / "results.json").read_text())[0], planned["d"][0]


@pytest.mark.unit
def test_results_json_carries_the_checklist_fields(tmp_path, monkeypatch):
    from tests.test_decision_math import _good_decision

    row, _ = _review_one(tmp_path, monkeypatch, _good_decision(), 50.0)
    assert row["price"] == 50.0 and row["formula_value"] == 80.33 and row["entry_price"] == 46.0
    assert len(row["kill_criteria"]) == 3 and row["checklist"]["cheap"]["passed"] is True
    assert row["scenarios"]["expected_value"] == pytest.approx(56.0)
    assert row["value_bridge"][1] == {"reason": "net debt", "amount": -12.33}
    assert row["red_flags"] == []


@pytest.mark.unit
def test_results_json_measures_the_scenarios_at_the_review_price(tmp_path, monkeypatch):
    from tests.test_decision_math import _decision
    from tradingagents.agents.schemas import Scenario, Scenarios

    scen = Scenarios(bear=Scenario(value=40, probability=0.25, basis="b"),
                     base=Scenario(value=56, probability=0.5, basis="m"),
                     bull=Scenario(value=70, probability=0.25, basis="u"))
    row, d = _review_one(tmp_path, monkeypatch, _decision(scenarios=scen), 50.0)
    sc = row["scenarios"]
    assert sc["upside_downside"] == pytest.approx(2.0) and sc["weighted_mos"] == pytest.approx((55.5 - 50) / 55.5)
    assert sc["expected_value"] == pytest.approx(55.5) and sc["price_basis"] == "review price"
    assert d.upside_downside == pytest.approx(2.0) and d.weighted_mos == pytest.approx((55.5 - 50) / 55.5)


@pytest.mark.unit
def test_without_a_review_price_the_scenarios_keep_the_entry_price_figures(tmp_path, monkeypatch):
    from tests.test_decision_math import _good_decision

    row, d = _review_one(tmp_path, monkeypatch, _good_decision(), None)
    sc = row["scenarios"]
    assert sc["price_basis"] == "entry price" and sc["upside_downside"] == pytest.approx(6.0)
    assert sc["weighted_mos"] is None and d.upside_downside == pytest.approx(6.0)


@pytest.mark.unit
def test_actions_list_entry_price_for_candidates_and_kill_criteria_for_holdings():
    held = _held("Hold", kill_criteria=["Revenue falls two years running", "Net debt above 3x EBITDA"])
    cand = Decision(ticker="000270.KS", rating="Hold", price=130_000.0, entry_price=115_000.0,
                    expected_return=0.11)
    bare = Decision(ticker="BARE", rating="Hold", price=10.0)
    actions, summary = plan([held, cand, bare], cash=50_000.0, sizing=SIZING)
    md = render.actions_markdown("2026-11-07", actions, summary, reports={"000270.KS": "x.html"},
                                 comparison=None, cost=0.0, failures=[], dry_run=False)
    assert "- 000270.KS: " in md and " — buy below 115,000.00; expected return 11%/yr ([report](x.html))" in md
    bare_line = next(line for line in md.splitlines() if line.startswith("- BARE:"))
    assert "buy below" not in bare_line and "expected return" not in bare_line
    assert "### Kill criteria" in md
    assert "- HLD: 1. Revenue falls two years running; 2. Net debt above 3x EBITDA" in md
    md = render.actions_markdown("2026-11-07", plan([_held("Hold")], cash=1.0, sizing=SIZING)[0], summary,
                                 reports={}, comparison=None, cost=0.0, failures=[], dry_run=False)
    assert "Kill criteria" not in md


@pytest.mark.unit
def test_watch_alerts_on_entry_price():
    from value_pipeline.watch import alerts

    ideas = [{"ticker": "AT", "rating": "Hold", "intrinsic_value": 100.0, "entry_price": 60.0, "date": "2026-10-01"},
             {"ticker": "ABOVE", "rating": "Buy", "intrinsic_value": 100.0, "entry_price": 50.0, "date": "2026-10-01"},
             {"ticker": "SELL", "rating": "Sell", "intrinsic_value": 100.0, "entry_price": 60.0, "date": "2026-10-01"},
             {"ticker": "KNIFE", "rating": "Hold", "intrinsic_value": 100.0, "entry_price": 60.0, "date": "2026-10-01"},
             {"ticker": "OLD", "rating": "Hold", "intrinsic_value": 100.0, "entry_price": 60.0, "date": "2026-01-01"}]
    prices = {"AT": 60.0, "ABOVE": 55.0, "SELL": 40.0, "KNIFE": 40.0, "OLD": 40.0}
    got = [a for a in alerts([], ideas, prices, {"KNIFE": "FREEFALL"}, buy_mos=0.90,
                             today=date(2026, 10, 17), max_age_days=120) if a.kind == "ENTRY PRICE"]
    assert [(a.ticker, a.message) for a in got] == [
        ("AT", "AT at 60.00 is at or below the agents' entry price 60.00 (rated Hold on 2026-10-01)")]


@pytest.mark.unit
def test_watch_handles_results_without_entry_price():
    from value_pipeline.watch import alerts

    ideas = [{"ticker": "OLDROW", "rating": "Hold", "intrinsic_value": 100.0, "date": "2026-10-01"},
             {"ticker": "NONE", "rating": "Hold", "intrinsic_value": 100.0, "entry_price": None}]
    got = alerts([], ideas, {"OLDROW": 10.0, "NONE": 10.0}, {}, buy_mos=0.99, today=date(2026, 10, 17))
    assert [a for a in got if a.kind == "ENTRY PRICE"] == []


@pytest.mark.unit
def test_watch_markdown_lists_the_holdings_kill_criteria(tmp_path, monkeypatch):
    from value_pipeline import watch

    paths = Paths(tmp_path)
    pf = pfmod.Portfolio(cash=0.0)
    pfmod.apply_deposit(pf, 1000.0, on="2026-10-01")
    pfmod.apply_buy(pf, "HRB", 1, 40.0, on="2026-10-02")
    pf.position("HRB").kill_criteria = ["Revenue falls two years running", "Dividend cut"]
    pfmod.save(paths, pf)
    monkeypatch.setattr(watch.market_data, "latest_price", lambda t: 40.0)
    monkeypatch.setattr(watch.market_data, "closes", lambda t: [])
    _, out = watch.run(paths, DEFAULTS, notify=False)
    text = out.read_text()
    assert "Kill criteria to check" in text
    assert "- HRB: 1. Revenue falls two years running; 2. Dividend cut" in text


@pytest.mark.unit
def test_old_portfolio_file_loads_with_empty_kill_criteria(tmp_path):
    paths = Paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.portfolio.write_text('{"cash": 5.0, "positions": [{"ticker": "OLD", "shares": 1, '
                               '"avg_price": 2.0, "first_bought": "2026-01-01"}]}')
    pf = pfmod.load(paths)
    assert pf.position("OLD").kill_criteria == [] and pf.position("OLD").entry_price is None
    pfmod.save(paths, pf)
    saved = json.loads(paths.portfolio.read_text())["positions"][0]
    assert saved["kill_criteria"] == [] and saved["entry_price"] is None


@pytest.mark.unit
def test_recording_a_buy_stores_kill_criteria_and_entry_price(tmp_path):
    from value_pipeline import operations as ops

    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    folder = paths.reports / "2026-10-11"
    folder.mkdir(parents=True)
    (folder / "results.json").write_text(json.dumps([
        {"ticker": "HRB", "rating": "Overweight", "error": None, "thesis": "t", "intrinsic_value": 55.0,
         "entry_price": 46.0, "kill_criteria": ["Revenue falls", "Dividend cut", "Debt up"]},
        {"ticker": "OLD", "rating": "Overweight", "error": None, "thesis": "t", "intrinsic_value": 5.0}]))
    held, _ = ops.record_buy(paths, "HRB", 1, 44.0, on="2026-10-12")
    assert held.kill_criteria == ["Revenue falls", "Dividend cut", "Debt up"] and held.entry_price == 46.0
    assert pfmod.load(paths).position("HRB").entry_price == 46.0
    old, _ = ops.record_buy(paths, "OLD", 1, 4.0, on="2026-10-12")
    assert old.kill_criteria == [] and old.entry_price is None


@pytest.mark.unit
def test_a_review_stores_a_holdings_kill_criteria_and_entry_price(tmp_path):
    from value_pipeline import operations as ops, review

    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    ops.record_buy(paths, "HRB", 1, 40.0, on="2026-10-02")
    pf = pfmod.load(paths)
    pf.position("HRB").kill_criteria = ["stale"]
    pfmod.save(paths, pf)
    d = Decision(ticker="HRB", rating="Hold", price=44.0, held_shares=1, entry_price=41.0,
                 kill_criteria=["Revenue falls", "Dividend cut", "Debt up"])
    actions, _ = plan([d], cash=960.0, sizing=SIZING)
    review._record_ratings(paths, pf, actions, "2026-11-07")
    held = pfmod.load(paths).position("HRB")
    assert held.kill_criteria == ["Revenue falls", "Dividend cut", "Debt up"] and held.entry_price == 41.0
    # a review that returned none keeps the earlier list
    bare = Decision(ticker="HRB", rating="Hold", price=44.0, held_shares=1)
    review._record_ratings(paths, pf, plan([bare], cash=960.0, sizing=SIZING)[0], "2026-12-05")
    assert pfmod.load(paths).position("HRB").kill_criteria == ["Revenue falls", "Dividend cut", "Debt up"]
