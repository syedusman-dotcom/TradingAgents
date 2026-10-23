"""The theme scan: where demand is inflecting for the themes you name, which listed suppliers
benefit, and which of them the numbers confirm (inflection candidates for the review).

Nothing here trades. The model only proposes tickers; each one is fetched and evaluated like
a screen row, and only names whose TTM figures show the inflection join the review."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from value_pipeline.config import Paths

log = logging.getLogger(__name__)

# A theme idea joins the review when either number shows the inflection (looser than the
# screen's inflection hint: the theme is the first half of the evidence).
MIN_TTM_VS_5Y_EBIT, MIN_REVENUE_ACCELERATION = 1.3, 0.10

PROMPT = """Today is {today}. Theme: {theme}.

Where is demand inflecting for this theme, and which listed suppliers with pricing power benefit?
Name up to 8 tickers with one line of evidence each; label unverified claims.

For each idea give the theme exactly as named above, the exchange ticker as Yahoo Finance spells
it (e.g. CLS, 6857.T, ASML.AS), the company name, one line of evidence (customer capex, backlog,
orders or pricing, with its source and date), and verified = true only when that evidence comes
from a filing, an earnings call or a named source you can cite; otherwise verified = false
(unverified)."""


class ThemeIdea(BaseModel):
    theme: str = Field(description="The theme exactly as named in the prompt.")
    ticker: str = Field(description="The listed ticker as Yahoo Finance spells it.")
    name: str = Field(description="The company's name.")
    evidence: str = Field(description="One line: the demand evidence, with its source and date.")
    verified: bool = Field(description="True only when the evidence has a citable source.")


class ThemeScan(BaseModel):
    ideas: list[ThemeIdea] = Field(default_factory=list, description="Up to 8 ideas for the theme.")


def scan(llm, themes: list[str], today: str) -> list[ThemeIdea]:
    """One structured quick-tier call per theme; a theme whose call fails gives no ideas (the
    others still count). No model (a dry run) gives none."""
    if llm is None or not themes:
        return []
    from tradingagents.agents.structured import bind_structured, invoke_structured

    structured = bind_structured(llm, ThemeScan, "Theme scan")
    ideas: list[ThemeIdea] = []
    for theme in themes:
        try:
            result = invoke_structured(structured, PROMPT.format(today=today, theme=theme), "Theme scan")
        except Exception as exc:  # noqa: BLE001 - one theme failing must not stop the others
            log.warning("theme scan failed for %r: %s: %s", theme, type(exc).__name__, exc)
            continue
        if result is None:
            log.warning("theme scan gave no structured answer for %r", theme)
            continue
        for idea in result.ideas[:8]:
            ticker = idea.ticker.strip().upper()
            if ticker:
                ideas.append(idea.model_copy(update={"theme": theme, "ticker": ticker}))
    return ideas


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) and v == v else None


def _inflecting(row: dict) -> bool:
    ebit, accel = _num(row, "ttm_vs_5y_ebit"), _num(row, "revenue_acceleration")
    return (ebit is not None and ebit >= MIN_TTM_VS_5Y_EBIT) or (
        accel is not None and accel >= MIN_REVENUE_ACCELERATION)


def _figures(row: dict) -> str:
    ebit, accel = _num(row, "ttm_vs_5y_ebit"), _num(row, "revenue_acceleration")
    return (f"TTM EBIT {'–' if ebit is None else f'{ebit:.2f}x'} the 5-year average, revenue growth "
            f"{'–' if accel is None else f'{accel * 100:+.0f} points'} over four quarters")


def candidates(paths: Paths, cfg: dict, ideas: list[ThemeIdea],
               owned: set[str]) -> tuple[list[dict], list[str]]:
    """The ideas the numbers confirm, as screen rows hinted ``inflection`` (best expected return
    first, at most ``lanes.inflection_candidates``), and a note for every ticker left out.
    Each distinct, not-owned ticker is fetched and evaluated once; one that cannot be is noted."""
    from value_pipeline import review

    cap = int(cfg.get("lanes", {}).get("inflection_candidates", 3))
    screen = review._screen_rows(paths)
    passing, skipped = [], []
    for ticker in dict.fromkeys(i.ticker for i in ideas):
        if ticker in owned:
            continue
        try:
            row = review.screen_row_for(ticker, paths, cfg, screen)
        except Exception as exc:  # noqa: BLE001 - fail open: the idea is noted, not reviewed
            log.warning("theme idea %s: %s", ticker, exc)
            row = None
        if row is None:
            skipped.append(f"{ticker}: no figures (fundamentals unavailable)")
        elif not _inflecting(row):
            skipped.append(f"{ticker}: not inflecting ({_figures(row)})")
        else:
            passing.append({**row, "lane_hint": "inflection"})
    passing.sort(key=lambda r: (_num(r, "expected_return") is None, -(_num(r, "expected_return") or 0.0)))
    kept = passing[:cap]
    skipped += [f"{r['ticker']}: over the cap of {cap} inflection candidates" for r in passing[cap:]]
    return kept, skipped


def report_lines(ideas: list[ThemeIdea], kept: list[dict], skipped: list[str]) -> list[str]:
    """The theme sections, the candidates added and the skipped ideas (markdown lines); other
    discovery sections (the dislocation scan) append their own to the same report."""
    lines: list[str] = []
    for theme in dict.fromkeys(i.theme for i in ideas):
        lines += [f"## {theme}", ""]
        lines += [f"- **{i.ticker}** {i.name}: {i.evidence} ({'verified' if i.verified else 'unverified'})"
                  for i in ideas if i.theme == theme]
        lines.append("")
    lines += ["## Candidates added", ""]
    lines += [f"- {r['ticker']}: {_figures(r)}; expected return "
              f"{'–' if _num(r, 'expected_return') is None else format(_num(r, 'expected_return'), '.0%')}/yr"
              for r in kept] or ["None."]
    lines += ["", "## Skipped", ""]
    lines += [f"- {s}" for s in skipped] or ["None."]
    return lines + [""]


def report_path(paths: Paths, today: str) -> Path:
    """``reports/themes_<today>.md``, beside the watch and daily reports (not a review folder)."""
    return paths.reports / f"themes_{today}.md"


def write_report(paths: Paths, today: str, ideas: list[ThemeIdea], kept: list[dict],
                 skipped: list[str], extra: list[str] | None = None) -> Path:
    """``reports/themes_<today>.md``: each theme's ideas, then the candidates added and skipped.
    ``extra`` lines (another discovery section, e.g. the dislocation scan's) are appended after."""
    paths.reports.mkdir(parents=True, exist_ok=True)
    head = [f"# Theme scan {today}", "",
            "Ideas the model named for each theme in `lanes.themes`; unverified claims are labelled. "
            "Only names whose TTM figures show the inflection join the review.", ""]
    if not ideas:
        head += ["No ideas (no themes named, or the scan gave none).", ""]
    out = report_path(paths, today)
    out.write_text("\n".join(head + report_lines(ideas, kept, skipped) + (extra or [])), encoding="utf-8")
    return out
