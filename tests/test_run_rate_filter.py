import copy

import pandas as pd
import pytest

from value_pipeline import market_data, operations as ops, portfolio as pfmod, review
from value_pipeline.config import DEFAULTS, Paths
from value_pipeline.screener import run_rate_skips, top_candidates

pytestmark = pytest.mark.unit

ROWS = [("THO", 1, 0.50), ("AAA", 2, 1.10), ("PPC", 3, 0.52), ("BBB", 4, None), ("CCC", 5, 0.80)]


def _screen(paths, rows=ROWS, column=True, hints=None):
    hints = hints or {}
    df = pd.DataFrame([{"ticker": t, "mf_position": pos, "trend": "BASING", "ttm_vs_5y_ebit": ratio,
                        "lane_hint": hints.get(t, ""), "expected_return": 0.1}
                       for t, pos, ratio in rows])
    if not column:
        df = df.drop(columns="ttm_vs_5y_ebit")
    paths.screens.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.screens / "screen_2026-11-05.csv", index=False)


def _tickers(rows):
    return [r["ticker"] for r in rows]


def test_low_run_rate_gives_its_place_to_the_next_name(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert _tickers(top_candidates(paths, set(), 3, min_run_rate=0.8)) == ["AAA", "BBB", "CCC"]
    assert run_rate_skips(paths, set(), 3, 0.8) == [{"ticker": "THO", "ttm_vs_5y_ebit": 0.50},
                                                    {"ticker": "PPC", "ttm_vs_5y_ebit": 0.52}]


def test_zero_turns_the_filter_off(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths)
    assert _tickers(top_candidates(paths, set(), 3)) == ["THO", "AAA", "PPC"]
    assert run_rate_skips(paths, set(), 3, 0.0) == []


def test_a_screen_without_the_column_passes_everything(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths, column=False)
    assert _tickers(top_candidates(paths, set(), 3, min_run_rate=0.8)) == ["THO", "AAA", "PPC"]
    assert run_rate_skips(paths, set(), 3, 0.8) == []


def test_hinted_lanes_ignore_the_filter(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths, rows=ROWS + [("DDD", 9, 0.40)], hints={"DDD": "dislocation"})
    got = top_candidates(paths, set(), 2, dislocations=1, min_run_rate=0.8)
    assert _tickers(got) == ["AAA", "BBB", "DDD"]


def test_skips_stop_at_the_last_pick(tmp_path):
    paths = Paths(tmp_path)
    _screen(paths, rows=[("AAA", 1, 1.0), ("THO", 2, 0.5), ("BBB", 3, 1.0), ("PPC", 4, 0.5)])
    assert _tickers(top_candidates(paths, set(), 2, min_run_rate=0.8)) == ["AAA", "BBB"]
    assert run_rate_skips(paths, set(), 2, 0.8) == [{"ticker": "THO", "ttm_vs_5y_ebit": 0.5}]


def test_review_prints_the_left_out_names(tmp_path, capsys, monkeypatch):
    paths = Paths(tmp_path)
    ops.init_portfolio(paths, 20_000.0, on="2026-09-01")
    pf = pfmod.load(paths)
    pfmod.apply_buy(pf, "HRB", 10, 50.0, on="2026-09-02", intrinsic_value=55.0)
    pfmod.save(paths, pf)
    _screen(paths)
    monkeypatch.setattr(market_data, "latest_price", lambda t: 50.0)
    monkeypatch.setattr(market_data, "quote_fx", lambda t: ("USD", 1.0))
    monkeypatch.setattr(market_data, "closes_between", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(review, "_screen_rows", lambda paths: {})
    cfg = copy.deepcopy(DEFAULTS)
    cfg["review"]["candidates"] = 3
    review.run(paths, cfg, yes=True, dry_run=True, today="2026-11-05")
    out = capsys.readouterr().out
    left = "Left out, profit running below the five-year average: THO 0.50, PPC 0.52"
    assert left in out
    assert out.index(left) < out.index("Estimated cost")
