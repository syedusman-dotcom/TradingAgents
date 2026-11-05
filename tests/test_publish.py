"""The daily status file: the day's buys, sells and trims for the Claude routine."""
from __future__ import annotations

import json

import pytest

from value_pipeline import publish
from value_pipeline.actions import Decision, action_rows, plan
from value_pipeline.config import DEFAULTS, Paths

TODAY = "2026-11-05"


def _paths(tmp_path) -> Paths:
    paths = Paths(tmp_path)
    paths.reports.mkdir(parents=True)
    return paths


def _write(paths: Paths, folder: str, rows: list[dict]) -> None:
    d = paths.reports / folder
    d.mkdir()
    (d / "actions.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.mark.unit
def test_action_rows_carry_the_quote_unit_and_the_name():
    uk = Decision("DNLM.L", "Buy", 900.0, margin_of_safety=0.41, currency="GBp", fx_to_usd=0.013464,
                  entry_price=950.0)
    held = Decision("INCY", "Sell", 60.0, held_shares=10.0)
    actions, _ = plan([uk, held], cash=50_000.0, sizing=DEFAULTS["sizing"])
    rows = {r["ticker"]: r for r in action_rows(actions, {"DNLM.L": "Dunelm Group"})}
    assert rows["DNLM.L"]["name"] == "Dunelm Group"
    assert rows["DNLM.L"]["action"] == "BUY"
    assert rows["DNLM.L"]["currency"] == "GBp"
    assert rows["DNLM.L"]["price"] == 900.0
    assert rows["DNLM.L"]["entry_price"] == 950.0
    assert rows["DNLM.L"]["amount"] > 0 and rows["DNLM.L"]["shares"] > 0
    assert rows["INCY"]["action"] == "SELL" and rows["INCY"]["held"] is True
    assert rows["INCY"]["name"] is None


@pytest.mark.unit
def test_summary_lists_buys_and_sells_in_the_quote_unit(tmp_path):
    paths = _paths(tmp_path)
    _write(paths, TODAY, [
        {"ticker": "DNLM.L", "name": "Dunelm Group", "held": False, "rating": "Buy", "action": "BUY",
         "reason": "", "amount": 1017.4, "shares": 84.0, "lane": "value", "flags": [],
         "price": 900.0, "currency": "GBp", "entry_price": 950.0},
        {"ticker": "INCY", "name": "Incyte", "held": True, "rating": "Sell", "action": "SELL",
         "reason": "", "amount": 600.0, "shares": 10.0, "lane": "value", "flags": [],
         "price": 60.0, "currency": None, "entry_price": None},
        {"ticker": "IT", "name": "Gartner", "held": False, "rating": "Hold", "action": "SKIP",
         "reason": "", "amount": None, "shares": None, "lane": "value", "flags": [],
         "price": 200.0, "currency": "USD", "entry_price": 181.0},
    ])
    text = publish.summary(paths, TODAY)
    assert "- Dunelm Group (DNLM.L): BUY 84 sh ($1,017), rated Buy, price 900.00 GBp, buy below 950.00 GBp" in text
    assert "- Incyte (INCY): SELL 10 sh ($600), rated Sell, price $60.00" in text
    assert "IT Hold" in text
    assert "cash" not in text.lower()


@pytest.mark.unit
def test_summary_without_a_review_says_so(tmp_path):
    paths = _paths(tmp_path)
    assert f"No review ran on {TODAY}." in publish.summary(paths, TODAY)
    _write(paths, TODAY, [])
    text = publish.summary(paths, TODAY)
    assert "## Buys\n\nNone." in text and "## Sells and trims\n\nNone." in text


@pytest.mark.unit
def test_latest_review_prefers_the_newest_folder_and_ignores_dry_runs(tmp_path):
    paths = _paths(tmp_path)
    _write(paths, f"{TODAY}-dry-run", [])
    assert publish.latest_review(paths, TODAY) is None
    _write(paths, TODAY, [])
    _write(paths, f"{TODAY}-2", [])
    assert publish.latest_review(paths, TODAY).name == f"{TODAY}-2"


@pytest.mark.unit
def test_publish_without_a_clone_writes_the_file_and_warns(tmp_path, caplog):
    paths = _paths(tmp_path)
    _write(paths, TODAY, [])
    with caplog.at_level("WARNING"):
        out = publish.publish(paths, TODAY, repo_dir=tmp_path / "status")
    assert out.read_text(encoding="utf-8").startswith(f"# Value pipeline: {TODAY}")
    assert "pushed nothing" in caplog.text
