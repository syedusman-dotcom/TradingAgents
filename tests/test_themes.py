"""Discovery: the theme scan, inflection candidates, lane hints on the screen (no network, no model)."""

import copy
import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from value_pipeline import screener
from value_pipeline.config import DEFAULTS, Paths


def _scan_llm(*answers):
    """A quick model whose structured calls return ``answers`` in turn (an exception is raised)."""
    structured = MagicMock()
    structured.invoke.side_effect = list(answers)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _idea(theme, ticker, verified=True):
    from value_pipeline.themes import ThemeIdea

    return ThemeIdea(theme=theme, ticker=ticker, name=f"{ticker} Inc", evidence=f"{ticker} backlog up",
                     verified=verified)


@pytest.mark.unit
def test_theme_scan_extracts_ideas_with_a_stub_model():
    from value_pipeline import themes

    llm = _scan_llm(themes.ThemeScan(ideas=[_idea("AI infrastructure", " cls "), _idea("x", "vrt", False)]),
                    RuntimeError("overloaded"),
                    themes.ThemeScan(ideas=[_idea("grid", "ETN")]))
    ideas = themes.scan(llm, ["AI infrastructure", "robots", "grid equipment"], "2026-10-23")
    assert [(i.theme, i.ticker, i.verified) for i in ideas] == [
        ("AI infrastructure", "CLS", True), ("AI infrastructure", "VRT", False), ("grid equipment", "ETN", True)]
    llm.with_structured_output.assert_called_with(themes.ThemeScan)
    prompts = [c.args[0] for c in llm.with_structured_output.return_value.invoke.call_args_list]
    assert len(prompts) == 3 and "AI infrastructure" in prompts[0] and "2026-10-23" in prompts[0]
    assert "up to 8 tickers" in prompts[0] and "unverified" in prompts[0] and "pricing power" in prompts[0]
    assert themes.scan(None, ["AI"], "2026-10-23") == []           # no model (a dry run): nothing
    assert themes.scan(llm, [], "2026-10-23") == []


def _row(ticker, er, ttm=None, accel=None, **kw):
    """A screen row passing the hard filters a theme candidate is held to."""
    return {"ticker": ticker, "expected_return": er, "ttm_vs_5y_ebit": ttm, "revenue_acceleration": accel,
            "sector": "Industrials", "market_cap": 5e9, "altman_z": 3.0, "free_cash_flow": 100.0,
            "interest_coverage": 10.0, **kw}


@pytest.mark.unit
def test_theme_candidates_are_held_to_the_hard_filters(tmp_path, monkeypatch):
    from value_pipeline import review, themes

    rows = {"CLS": _row("CLS", 0.12, ttm=2.4, fscore=4, roic_min=0.02),     # F-Score and ROIC floor not applied
            "WEAK": _row("WEAK", 0.30, ttm=2.0, altman_z=1.2, free_cash_flow=-5.0),
            "TINY": _row("TINY", 0.25, ttm=2.0, market_cap=None)}
    monkeypatch.setattr(review, "offscreen_row", lambda t, paths, cfg, screen: dict(rows[t]))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    ideas = [_idea("AI infrastructure", t) for t in ("CLS", "WEAK", "TINY")]
    kept, skipped = themes.candidates(Paths(tmp_path), copy.deepcopy(DEFAULTS), ideas, owned=set())
    assert [r["ticker"] for r in kept] == ["CLS"]
    assert kept[0]["passes_filters"] is True and kept[0]["fail_reasons"] == ""
    assert kept[0]["theme"] == "AI infrastructure" and kept[0]["hints"] == "inflection"
    assert "WEAK: fails Altman Z; FCF" in skipped and "TINY: fails market cap" in skipped


@pytest.mark.unit
def test_theme_candidates_skip_unfetchable_tickers_and_cap(tmp_path, monkeypatch):
    from value_pipeline import review, themes

    rows = {"CLS": _row("CLS", 0.12, ttm=2.4), "VRT": _row("VRT", 0.20, accel=0.15),
            "ETN": _row("ETN", 0.30, ttm=1.0, accel=0.02), "FN": _row("FN", None, ttm=1.3),
            "ANET": _row("ANET", 0.05, accel=0.10), "HRB": _row("HRB", 0.5, ttm=3.0)}
    fetched = []

    def offscreen(t, paths, cfg, screen):
        fetched.append(t)
        return dict(rows[t]) if t in rows else None

    monkeypatch.setattr(review, "offscreen_row", offscreen)
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    cfg = copy.deepcopy(DEFAULTS)
    ideas = [_idea("AI", t) for t in ("CLS", "VRT", "BAD", "ETN", "FN", "ANET", "HRB", "CLS")]
    kept, skipped = themes.candidates(Paths(tmp_path), cfg, ideas, owned={"HRB"})
    assert [r["ticker"] for r in kept] == ["VRT", "CLS", "ANET"]          # by expected return, capped at 3
    assert all(r["lane_hint"] == "inflection" for r in kept)
    assert fetched.count("CLS") == 1 and "HRB" not in fetched           # each ticker once; owned not fetched
    joined = " ".join(skipped)
    assert "BAD" in joined and "no figures" in joined
    assert "ETN" in joined and "not inflecting" in joined
    assert "FN" in joined                                                 # over the cap
    cfg["lanes"]["inflection_candidates"] = 1
    kept, _ = themes.candidates(Paths(tmp_path), cfg, ideas, owned=set())
    assert [r["ticker"] for r in kept] == ["HRB"]


@pytest.mark.unit
def test_theme_report_lists_ideas_candidates_and_skips(tmp_path):
    from value_pipeline import themes

    ideas = [_idea("AI infrastructure", "CLS"), _idea("AI infrastructure", "VRT", False), _idea("grid", "ETN")]
    out = themes.write_report(Paths(tmp_path), "2026-10-23", ideas, [_row("CLS", 0.12, ttm=2.4)],
                              ["ETN: not inflecting"], extra=["## Sector scares", "", "- Software: ADBE, CRM"])
    assert out == Paths(tmp_path).reports / "themes_2026-10-23.md"       # beside watch_/daily_, not a review
    from value_pipeline.review import review_dirs

    assert review_dirs(Paths(tmp_path)) == []
    md = out.read_text()
    assert md.index("## AI infrastructure") < md.index("## grid") < md.index("## Candidates added")
    assert "CLS" in md and "VRT backlog up" in md and "unverified" in md
    assert md.index("## Candidates added") < md.index("## Skipped") and "ETN: not inflecting" in md
    assert md.index("## Skipped") < md.index("## Sector scares") and "- Software: ADBE, CRM" in md


@pytest.mark.unit
def test_lane_hint_column():
    good = {"roic_gw_min": 0.16, "roic_years_counted": 4, "revenue_cagr": 0.11, "revenue_cagr_years": 10,
            "fcf_cagr": 0.09, "fcf_cagr_years": 3, "sustainable_growth": 0.0,   # a buyback compounder
            "ttm_vs_5y_ebit": 1.1, "revenue_acceleration": 0.0, "passes_filters": True}
    assert screener.lane_hint(good) == "compounder"
    assert screener.lane_hint({**good, "roic_years_counted": 3}) == ""
    assert screener.lane_hint({**good, "roic_gw_min": 0.14}) == ""
    assert screener.lane_hint({**good, "roic_min": 0.5, "roic_gw_min": None}) == ""
    assert screener.lane_hint({**good, "revenue_cagr_years": 4}) == ""
    assert screener.lane_hint({**good, "fcf_cagr": 0.04}) == ""
    assert screener.lane_hint({**good, "fcf_cagr_years": 2}) == ""
    assert screener.lane_hint({**good, "passes_filters": False}) == ""
    hot = {"roic_min": 0.1, "ttm_vs_5y_ebit": 1.6, "revenue_acceleration": 0.12, "passes_filters": True}
    assert screener.lane_hint(hot) == "inflection"
    assert screener.lane_hint({**hot, "revenue_acceleration": 0.05}) == ""
    assert screener.lane_hint({**hot, "ttm_vs_5y_ebit": float("nan")}) == ""
    assert screener.lane_hint({**hot, "passes_filters": "False"}) == ""


def _write_screen(paths: Paths, rows: list[dict], day="2026-10-20"):
    paths.screens.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(paths.screens / f"screen_{day}.csv", index=False)


def _screen_rows():
    def r(t, mf, er, hint="", trend="UPTREND"):
        return {"ticker": t, "mf_position": mf, "expected_return": er, "lane_hint": hint, "trend": trend,
                "sector": "Industrials", "value_base": 100.0, "fscore": 7}
    return [r("AAA", 1, 0.10), r("BBB", 2, 0.09), r("CCC", 3, 0.08, "compounder"),
            r("DDD", 4, 0.07), r("CMP1", 9, 0.15, "compounder"), r("CMP2", None, 0.20, "compounder"),
            r("CMP3", 12, 0.13, "compounder", trend="FREEFALL"), r("CMP4", 14, 0.11, "compounder"),
            r("OWN", 15, 0.30, "compounder")]


@pytest.mark.unit
def test_top_candidates_add_compounders(tmp_path):
    paths = Paths(tmp_path)
    _write_screen(paths, _screen_rows())
    picks = screener.top_candidates(paths, {"OWN"}, 3, compounders=2)
    assert [p["ticker"] for p in picks] == ["AAA", "BBB", "CCC", "CMP2", "CMP1"]
    rows = _screen_rows()
    for r in rows:                     # the compounder return ranks them: CMP4 first, CMP2 last
        r["compounder_return"] = {"CMP1": 0.13, "CMP2": 0.11, "CMP4": 0.16}.get(r["ticker"])
    _write_screen(paths, rows, day="2026-10-20")
    picks = screener.top_candidates(paths, {"OWN"}, 3, compounders=2)
    assert [p["ticker"] for p in picks] == ["AAA", "BBB", "CCC", "CMP4", "CMP1"]
    assert [p["ticker"] for p in screener.top_candidates(paths, {"OWN"}, 3)] == ["AAA", "BBB", "CCC"]
    _write_screen(paths, [{k: v for k, v in r.items() if k != "lane_hint"} for r in _screen_rows()],
                  day="2026-10-21")                                     # a screen from before the column
    assert [p["ticker"] for p in screener.top_candidates(paths, {"OWN"}, 2, compounders=2)] == ["AAA", "BBB"]


@pytest.mark.unit
def test_run_all_includes_theme_and_compounder_candidates(tmp_path, monkeypatch):
    from value_pipeline import market_data, operations as ops, review, themes

    paths = Paths(tmp_path)
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("USD", 1.0))
    ops.init_portfolio(paths, 1000.0, on="2026-10-01")
    ops.record_buy(paths, "OWN", 1, 10.0, on="2026-10-02")
    _write_screen(paths, _screen_rows())
    monkeypatch.setattr(market_data, "latest_price", lambda t: 50.0)
    monkeypatch.setattr(market_data, "closes", lambda t, days=420: [])
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(market_data, "cached_sector", lambda t, p: "Industrials")
    monkeypatch.setattr(review, "next_earnings_date", lambda t: None)
    monkeypatch.setattr(review, "fake_run", lambda t, i, held, price: review.RunResult(t, "Hold", ""))
    built = {"CLS": _row("CLS", 0.12, ttm=2.4, value_base=90.0), "NAMED": _row("NAMED", 0.1)}
    monkeypatch.setattr(review, "offscreen_row", lambda t, p, c, s: dict(built[t]) if t in built else None)
    scans = []
    monkeypatch.setattr(themes, "scan", lambda llm, names, today: scans.append((llm, names)) or [
        _idea("AI infrastructure", "CLS"), _idea("AI infrastructure", "AAA"), _idea("AI infrastructure", "BAD")])
    decisions = {}
    real_plan = review.plan
    monkeypatch.setattr(review, "plan", lambda d, *a: decisions.setdefault("d", d) and real_plan(d, *a))
    cfg = copy.deepcopy(DEFAULTS)
    cfg["review"]["candidates"] = 2
    cfg["review"]["committee"] = False
    cfg["lanes"].update(themes=["AI infrastructure"], tickers=["named"])

    out = review.run(paths, cfg, yes=True, dry_run=True, today="2026-10-23", discover=True)
    rows = json.loads((out.parent / "results.json").read_text())
    assert [r["ticker"] for r in rows] == ["OWN", "NAMED", "AAA", "BBB", "CMP2", "CMP1", "CLS"]
    hints = {r["ticker"]: r["lane_hint"] for r in rows}
    assert hints["CLS"] == "inflection" and hints["CMP2"] == "compounder" and hints["AAA"] == ""
    assert scans == [(None, ["AI infrastructure"])]                     # dry run: no model
    d = {x.ticker: x for x in decisions["d"]}
    assert d["CLS"].formula_value == 90.0 and d["CLS"].lane_hint == "inflection"
    md = out.read_text()
    assert "CLS" in md and "(hint: inflection)" in md and "(hint: compounder)" in md
    report = (paths.reports / "themes_2026-10-23-dry-run.md").read_text()
    assert "## AI infrastructure" in report and "BAD" in report
    assert "Theme scan: [themes_2026-10-23-dry-run.md](../themes_2026-10-23-dry-run.md)" in md
    assert not (paths.reports / "2026-10-23").exists()

    scans.clear()
    out = review.run(paths, cfg, yes=True, dry_run=True, today="2026-10-24")   # plain review: no scan
    assert scans == [] and "CLS" not in [r["ticker"] for r in json.loads((out.parent / "results.json").read_text())]
    assert "Theme scan:" not in out.read_text()


@pytest.mark.unit
def test_cli_themes_and_run_all_discover(tmp_path, monkeypatch):
    from value_pipeline import cli, review, themes

    seen = {}
    monkeypatch.setattr(review, "run", lambda paths, cfg, **kw: seen.setdefault("kw", kw) and None or _md(tmp_path))
    monkeypatch.setattr(cli, "cmd_screen", lambda *a: None)
    args = cli.build_parser().parse_args(["run-all", "--yes", "--dry-run"])
    cli.cmd_run_all(args, Paths(tmp_path), copy.deepcopy(DEFAULTS))
    assert seen["kw"]["discover"] is True

    graph = MagicMock()
    monkeypatch.setattr(review, "make_graph", lambda *a, **k: graph)
    monkeypatch.setattr(themes, "scan", lambda llm, names, today: [_idea(names[0], "CLS")] if llm is
                        graph.quick_thinking_llm else [])
    monkeypatch.setattr(themes, "candidates", lambda paths, cfg, ideas, owned: ([], ["CLS: not inflecting"]))
    cfg = copy.deepcopy(DEFAULTS)
    cfg["lanes"]["themes"] = ["AI infrastructure"]
    args = cli.build_parser().parse_args(["themes"])
    cli.cmd_themes(args, Paths(tmp_path), cfg)
    md = next((Paths(tmp_path).reports).glob("themes_????-??-??.md")).read_text()
    assert "CLS" in md and "not inflecting" in md
    with pytest.raises(SystemExit, match="lanes.themes"):
        cli.cmd_themes(args, Paths(tmp_path), copy.deepcopy(DEFAULTS))


def _md(tmp_path):
    f = tmp_path / "ACTIONS.md"
    f.write_text("# Value review")
    return f


@pytest.mark.unit
def test_screen_page_shows_and_filters_the_lane_hint(tmp_path):
    from fastapi.testclient import TestClient

    from value_pipeline.web import create_app, views

    assert "lane_hint" in views._SCREEN_COLUMNS
    paths = Paths(tmp_path / "data")
    app = create_app(paths, config_path=tmp_path / "config.yaml", price_fn=lambda t: None,
                     series_fn=lambda p: [], benchmark_fn=lambda *a: None,
                     allowed_hosts=("127.0.0.1", "localhost", "testserver"))
    _write_screen(paths, _screen_rows())
    (paths.screens / "screen_2026-10-20_excluded.csv").write_text("ticker,name,sector\n")
    (paths.screens / "screen_2026-10-20_skipped.csv").write_text("ticker,reason\n")
    text = TestClient(app).get("/screen").text
    assert "<th>Lane hint</th>" in text and 'id="f-hint"' in text
    assert '<option value="compounder">compounder</option>' in text and "inflection" in text
    assert 'data-hint="compounder"' in text and 'data-hint=""' in text
    assert "f-hint" in text.split("<script>")[1]                        # the filter is wired


@pytest.mark.unit
def test_a_failed_theme_scan_still_writes_the_sector_scares(tmp_path, monkeypatch):
    from value_pipeline import review, themes

    paths = Paths(tmp_path)
    scares = {"Software": [{"ticker": "DS1", "drawdown_from_high": -0.4}]}

    def boom(llm, names, today):
        raise RuntimeError("model down")

    monkeypatch.setattr(themes, "scan", boom)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["lanes"]["themes"] = ["AI infrastructure"]
    kept, out = review.discover_themes(paths, cfg, None, "2026-10-23", set(), scares=scares)
    assert kept == [] and out is not None
    assert "## Sector scares" in out.read_text() and "DS1 (-40%)" in out.read_text()
    kept, out = review.discover_themes(paths, cfg, None, "2026-10-24", set(), scares={})
    assert kept == [] and out is None                                   # no scares: nothing to write
