"""The value pipeline's arithmetic and rules on fixed sample data (no network)."""

import math

import pytest

from value_pipeline import benchmark, metrics, parse, portfolio as pfmod, render, screener
from value_pipeline.actions import Decision, base_action, plan, target_weight
from value_pipeline.config import DEFAULTS, Paths

SIZING = DEFAULTS["sizing"]


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
    held = Decision("H", "Hold", price=10.0, held_shares=89)
    actions, summary = plan([held, *decisions], cash=120.0, sizing=SIZING)
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
    assert metrics.interest_coverage({"ebit": 100, "interest_expense": None}) == math.inf
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
    actions, summary = plan([_held("Hold"), _new("Buy")], cash=5000.0, sizing=SIZING)
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
def test_unstable_roic_and_dilution_fail_the_filters():
    f = DEFAULTS["filters"]
    assert "ROIC unstable" in screener.failed_filters(dict(GOOD, roic_min=0.02), f)
    assert "dilution" in screener.failed_filters(dict(GOOD, dilution_per_year=0.06), f)
    assert screener.failed_filters(dict(GOOD, roic_min=None, dilution_per_year=None), f) == []


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
def test_review_folders_newest_first_without_dry_runs(tmp_path):
    from value_pipeline.review import review_dirs

    paths = Paths(tmp_path)
    for name in ("2026-09-05", "2026-10-11", "2026-10-11-2", "2026-10-11-10", "2026-10-11-dry-run"):
        (paths.reports / name).mkdir(parents=True)
    (paths.reports / "watch_2026-10-17.md").write_text("x")
    assert [f.name for f in review_dirs(paths)] == ["2026-10-11-10", "2026-10-11-2", "2026-10-11", "2026-09-05"]
