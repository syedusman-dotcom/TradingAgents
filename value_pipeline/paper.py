"""Paper Buy rules: applied in code to what a review recorded, scored in dollars against SPY.

They are tracked only. Nothing sizes or trades on them, and no agent is told about them.
"""

from __future__ import annotations

import math

RULES = {
    "fair_price": "a good business with good management, rated Hold or better, "
                  "at or below the agents' own value",
    "fair_price_investing": "the same, and the company is still investing for growth",
    "discount_15": "a good business with good management, rated Hold or better, "
                   "at least 15% below the agents' value",
}
PRIMARY = "fair_price"
NOT_NEGATIVE = ("Buy", "Overweight", "Hold")
DISCOUNT = 0.15
EPS = 1e-9


def _passed(checklist: dict, key: str) -> bool:
    item = checklist.get(key)
    return isinstance(item, dict) and item.get("passed") is True


def flags(row: dict) -> list[str]:
    """The rules a ``results.json`` row meets, in ``RULES`` order."""
    if row.get("error") or row.get("rating") not in NOT_NEGATIVE:
        return []
    mos = row.get("margin_of_safety")
    if isinstance(mos, bool) or not isinstance(mos, (int, float)) or not math.isfinite(mos):
        return []
    checklist = row.get("checklist")
    if not isinstance(checklist, dict):
        return []
    if not (_passed(checklist, "good_business") and _passed(checklist, "good_management")):
        return []
    if mos < -EPS:
        return []
    out = ["fair_price"]
    if _passed(checklist, "still_investing"):
        out.append("fair_price_investing")
    if mos >= DISCOUNT - EPS:
        out.append("discount_15")
    return out
