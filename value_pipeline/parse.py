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


def consensus_view(decision: str) -> str:
    """Where the decision stands against consensus; "" when absent or 'not provided'."""
    text = section(decision, "Consensus").strip()
    return "" if text.lower() == "not provided" else text


def thesis_summary(decision: str, limit: int = 300) -> str:
    text = " ".join(section(decision, "Investment Thesis").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


# --- the checklist, scenarios and value bridge (absent from older decisions) --------

_DASH = r"\s*[—–-]\s*"
_SIGNED = r"([+\-−])\s*(\d[\d,]*(?:\.\d+)?)"


def entry_price(decision: str) -> float | None:
    """The Entry Price field as a number; None when absent or 'not provided'."""
    return _dollar_amount(section(decision, "Entry Price").split("\n")[0])


def red_flags(decision: str) -> list[str]:
    """The Red Flags bullets; empty for 'none' or an older decision."""
    return [m.group(1).strip() for m in re.finditer(r"^\s*[-*]\s+(.+)$", section(decision, "Red Flags"), re.M)]


def kill_criteria(decision: str) -> list[str]:
    """The numbered Kill Criteria."""
    return [m.group(1).strip() for m in re.finditer(r"^\s*\d+[.)]\s+(.+)$", section(decision, "Kill Criteria"), re.M)]


_CHECKS = {"good business": "good_business", "good management": "good_management",
           "still investing": "still_investing", "cheap": "cheap"}


def checklist(decision: str) -> dict[str, dict]:
    """{"good_business": {"passed": True, "note": "..."}, ...} for each test that has a verdict."""
    out = {}
    pattern = rf"^\s*(Good business|Good management|Still investing|Cheap){_DASH}(PASS|FAIL)\s*:?\s*(.*)$"
    for m in re.finditer(pattern, section(decision, "Checklist"), re.M | re.I):
        out[_CHECKS[m.group(1).lower()]] = {"passed": m.group(2).upper() == "PASS", "note": m.group(3).strip()}
    return out


def scenarios(decision: str) -> dict | None:
    """Bear/base/bull values and probabilities, the expected value and upside/downside; None when
    the decision has no scenarios."""
    text = section(decision, "Scenarios")
    out: dict = {}
    pattern = rf"^\s*(Bear|Base|Bull)\s+(\d+(?:\.\d+)?)\s*%\s*:\s*\$?(-?[\d,]+(?:\.\d+)?)(?:{_DASH}(.*))?$"
    for m in re.finditer(pattern, text, re.M | re.I):
        out[m.group(1).lower()] = {"value": _float(m.group(3)), "probability": float(m.group(2)) / 100,
                                   "basis": (m.group(4) or "").strip()}
    if not out:
        return None
    ev = re.search(r"Expected value\s*:\s*\$?(-?[\d,]+(?:\.\d+)?)", text, re.I)
    ratio = re.search(r"upside/downside\s+(\d+(?:\.\d+)?)\s*x", text, re.I)
    out["expected_value"] = _float(ev.group(1)) if ev else None
    out["upside_downside"] = float(ratio.group(1)) if ratio else None
    return out


def value_bridge(decision: str) -> list[dict]:
    """The bridge's adjustments, [{"reason", "amount"}], between the formula value and the target.
    A leading ASCII hyphen-minus or a Unicode minus both read as negative."""
    out = []
    for m in re.finditer(rf"^\s*{_SIGNED}\s+(.+)$", section(decision, "Value Bridge"), re.M):
        amount = _float(m.group(2))
        if amount is not None:
            out.append({"reason": m.group(3).strip(), "amount": -amount if m.group(1) != "+" else amount})
    return out


# --- the investment lane (absent from older decisions, which read as the value lane) --------

LANES = ("value", "compounder", "inflection")


def _lane_parts(decision: str) -> tuple[str, str]:
    text = section(decision, "Lane").split("\n")[0].strip()
    m = re.match(rf"({'|'.join(LANES)})\b(?:\s+lane\b)?\s*(?:[—–:-]\s*)?(.*)$", text, re.I)
    return (m.group(1).lower(), m.group(2).strip()) if m else ("value", "")


def lane(decision: str) -> str:
    """The decision's lane: value, compounder or inflection; "value" when absent or unrecognised."""
    return _lane_parts(decision)[0]


def lane_evidence(decision: str) -> list[str]:
    """The numbers the lane rests on, the ``; ``-separated items after the lane's name."""
    return [item.strip() for item in _lane_parts(decision)[1].split(";") if item.strip()]
