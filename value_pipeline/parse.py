"""Read the numbers the review needs out of the Portfolio Manager's decision text."""

from __future__ import annotations

import re

_NUMBER = r"\$?\s*(-?[\d,]+(?:\.\d+)?)"


def _float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def section(decision: str, label: str) -> str:
    """The text of a **Label**: section, up to the next bold label."""
    m = re.search(rf"\*\*{re.escape(label)}\*\*:?\s*(.*?)(?=\n\s*\*\*[A-Z][^*]*\*\*:|\Z)", decision, re.S)
    return m.group(1).strip() if m else ""


_AMOUNT = re.compile(r"(\$)?\s*(-?\d[\d,]*(?:\.\d+)?)(\s*%)?")
_THRESHOLD = re.compile(r"at least|require|minimum|need|threshold|hurdle|not \d", re.I)


def _dollar_amount(text: str) -> float | None:
    """The first money figure in a stretch of text: a $-prefixed number first, else a bare number;
    percentages (growth rates, margins) never count."""
    found = [(m.group(1), _float(m.group(2))) for m in _AMOUNT.finditer(text) if not m.group(3)]
    found = [(d, v) for d, v in found if v is not None and v > 0]
    with_dollar = [v for d, v in found if d]
    return with_dollar[0] if with_dollar else (found[0][1] if found else None)


def intrinsic_value(decision: str) -> float | None:
    """The per-share intrinsic value: the Price Target field, else an 'intrinsic value ... $X' phrase.
    A range reads as its stated base; growth rates ("at 3% growth") are skipped."""
    target = _dollar_amount(section(decision, "Price Target").split("\n")[0])   # "not provided" parses to None
    if target:
        return target
    m = re.search(r"intrinsic value([^\n]{0,120})", decision, re.I)
    if not m:
        return None
    window = m.group(1)
    base = re.search(r"\bbase\b([^\n]{0,40})", window, re.I)
    return (_dollar_amount(base.group(1)) if base else None) or _dollar_amount(window)


def margin_of_safety(decision: str) -> float | None:
    """'Margin of safety: 32%' as 0.32.

    The labelled form the Portfolio Manager is asked for wins; a looser phrase is
    the fallback, since prose also mentions thresholds ("a margin of safety, not 30%+").
    """
    m = re.search(r"margin of safety\s*[:=]\s*\**\s*(-?\d+(?:\.\d+)?)\s*%", decision, re.I)
    if m:
        return float(m.group(1)) / 100
    # Loose prose: the first percentage after the phrase that is not a threshold ("at least 30%").
    for phrase in re.finditer(r"margin of safety([^\n]{0,120})", decision, re.I):
        window = phrase.group(1)
        for pct in re.finditer(r"(-?\d+(?:\.\d+)?)\s*%", window):
            if not _THRESHOLD.search(window[max(0, pct.start() - 25):pct.start()]):
                return float(pct.group(1)) / 100
    return None


def thesis_summary(decision: str, limit: int = 300) -> str:
    text = " ".join(section(decision, "Investment Thesis").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
