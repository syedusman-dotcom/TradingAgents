"""Data assembled for the website's pages, from the pipeline's files."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from value_pipeline import portfolio as pfmod, review as reviewmod, screener
from value_pipeline.config import Paths


def next_review_date(today: date) -> date:
    """The first Saturday of this month if it is still ahead (or today), else next month's."""
    def first_saturday(year: int, month: int) -> date:
        d = date(year, month, 1)
        return d + timedelta(days=(5 - d.weekday()) % 7)

    this = first_saturday(today.year, today.month)
    if today <= this:
        return this
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return first_saturday(year, month)


def latest_watch(paths: Paths) -> tuple[str, str] | None:
    """The newest weekly watch: (date, markdown)."""
    files = sorted(paths.reports.glob("watch_????-??-??.md")) if paths.reports.exists() else []
    if not files:
        return None
    return files[-1].stem.removeprefix("watch_"), files[-1].read_text(encoding="utf-8")


_COST = re.compile(r"Cost of this review: ~?(\$[\d,.]+)")


def _results(folder: Path) -> list[dict]:
    f = folder / "results.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def reviews_index(paths: Paths) -> list[dict]:
    """Every real review, newest first, with its results and cost."""
    out = []
    for folder in reviewmod.review_dirs(paths):
        actions = folder / "ACTIONS.md"
        cost = _COST.search(actions.read_text(encoding="utf-8")) if actions.exists() else None
        out.append({"folder": folder.name, "results": _results(folder), "cost": cost.group(1) if cost else None})
    return out


def stock_history(paths: Paths, ticker: str) -> dict:
    """One ticker's ratings across reviews (newest first), its latest screen row and its trades."""
    ticker = ticker.strip().upper()
    reviews = [{"folder": folder.name, **r} for folder in reviewmod.review_dirs(paths)
               for r in _results(folder) if r["ticker"] == ticker]
    screen = None
    latest = screener.latest_screen(paths)
    if latest is not None:
        df = pd.read_csv(latest)
        match = df[df["ticker"] == ticker]
        if not match.empty:
            screen = {k: (None if pd.isna(v) else v) for k, v in match.iloc[0].to_dict().items()}
    trades = [t for t in pfmod.read_trades(paths) if t["ticker"] == ticker]
    return {"ticker": ticker, "reviews": reviews, "screen": screen, "trades": trades}


def safe_report_path(paths: Paths, rel: str) -> Path | None:
    """The file under data/reports that ``rel`` names, or None if it is missing or outside it."""
    root = paths.reports.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target
