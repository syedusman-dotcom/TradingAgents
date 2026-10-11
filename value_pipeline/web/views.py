"""Data assembled for the website's pages, from the pipeline's files."""

from __future__ import annotations

from datetime import date, timedelta

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
