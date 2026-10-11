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


def intrinsic_value(decision: str) -> float | None:
    """The per-share intrinsic value: the Price Target field, else an 'intrinsic value ... $X' phrase."""
    words = section(decision, "Price Target").lstrip("$ ").split()
    target = _float(words[0]) if words else None   # "not provided" parses to None
    if target is not None and target > 0:
        return target
    m = re.search(r"intrinsic value[^$\d\n]{0,60}" + _NUMBER, decision, re.I)
    value = _float(m.group(1)) if m else None
    return value if value and value > 0 else None


def margin_of_safety(decision: str) -> float | None:
    """'Margin of safety: 32%' as 0.32."""
    m = re.search(r"margin of safety[^%\d\n-]{0,40}(-?\d+(?:\.\d+)?)\s*%", decision, re.I)
    return float(m.group(1)) / 100 if m else None


def thesis_summary(decision: str, limit: int = 300) -> str:
    text = " ".join(section(decision, "Investment Thesis").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
