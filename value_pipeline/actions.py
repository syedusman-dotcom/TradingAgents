"""Turn ratings into a low-churn action list with position sizes. Pure functions, no I/O.

The list is advice for you to act on at your broker; nothing here trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUY_RATINGS = {"Buy", "Overweight"}


@dataclass
class Decision:
    """What one review run concluded about one ticker."""

    ticker: str
    rating: str                       # Buy / Overweight / Hold / Underweight / Sell / REVIEW
    price: float | None
    held_shares: float = 0.0
    consecutive_underweight: int = 0
    trend: str | None = None
    intrinsic_value: float | None = None
    margin_of_safety: float | None = None   # fraction, 0.3 = 30%
    fscore: int | None = None
    formula_value: float | None = None      # the screen's base value (3% growth)
    sector: str | None = None

    @property
    def held(self) -> bool:
        return self.held_shares > 0


@dataclass
class Action:
    ticker: str
    held: bool
    rating: str
    action: str                       # KEEP / ADD / TRIM / SELL / BUY / SKIP
    reason: str
    amount: float | None = None       # USD to buy (positive) or estimated proceeds of a sale
    shares: float | None = None
    consecutive_underweight: int = 0
    flags: list[str] = field(default_factory=list)
    decision: Decision | None = None


# The agents' value may differ from the formula's for stated reasons; past this
# gap, check their reasons before acting on it.
VALUE_GAP = 0.30


def value_gap_flag(d: Decision) -> str | None:
    if d.intrinsic_value is None or not d.formula_value or d.formula_value <= 0:
        return None
    gap = d.intrinsic_value / d.formula_value - 1
    if abs(gap) <= VALUE_GAP:
        return None
    return f"agents' value {d.intrinsic_value:,.2f} is {gap:+.0%} vs formula {d.formula_value:,.2f}: check their reasons"


def base_action(d: Decision) -> Action:
    """The rating rule for one ticker, before sizing."""
    a = Action(ticker=d.ticker, held=d.held, rating=d.rating, action="KEEP", reason="", decision=d)
    gap = value_gap_flag(d)
    if gap:
        a.flags.append(gap)
    if d.held:
        a.consecutive_underweight = 0
        if d.rating == "Sell":
            a.action, a.reason = "SELL", "Rated Sell: exit the position"
        elif d.rating == "Underweight":
            a.consecutive_underweight = d.consecutive_underweight + 1
            if a.consecutive_underweight >= 2:
                a.action, a.reason = "SELL", "Underweight twice in a row: exit"
            else:
                a.action, a.reason = "TRIM", "Underweight (first time): trim half; sell if it repeats"
        elif d.rating in BUY_RATINGS:
            a.reason = f"Rated {d.rating}: thesis intact"
        elif d.rating == "Hold":
            a.reason = "Rated Hold: keep"
        else:
            a.consecutive_underweight = d.consecutive_underweight
            a.reason = "No readable rating: keep, and read the report"
        if d.price is not None and d.intrinsic_value is not None and d.price > d.intrinsic_value:
            a.flags.append("REACHED VALUE: consider selling")
    else:
        if d.trend == "FREEFALL":
            a.action, a.reason = "SKIP", f"Rated {d.rating} but in FREEFALL: wait for BASING"
        elif d.rating in BUY_RATINGS:
            a.action, a.reason = "BUY", f"Rated {d.rating}"
        else:
            a.action, a.reason = "SKIP", f"Rated {d.rating}"
    return a


def target_weight(n_positions: int, sizing: dict) -> float:
    """Equal weight across the positions you'll hold, at least min_holdings, capped at the max."""
    return min(sizing["max_position_weight"], 1.0 / max(sizing["min_holdings"], n_positions, 1))


def plan(decisions: list[Decision], cash: float, sizing: dict) -> tuple[list[Action], dict]:
    """Apply the rules, then size buys and adds against cash.

    Cash for buys includes the estimated proceeds of the sells and trims in the
    same list, so place those first. Buys never exceed that cash.
    """
    actions = [base_action(d) for d in decisions]
    held_value = sum(d.held_shares * (d.price or 0.0) for d in decisions if d.held)
    total_value = cash + held_value

    for a in actions:
        d = a.decision
        if a.action in ("SELL", "TRIM") and d.price is not None:
            a.shares = d.held_shares if a.action == "SELL" else round(d.held_shares / 2, 4)
            a.amount = round(a.shares * d.price, 2)
    available = cash + sum(a.amount or 0.0 for a in actions if a.action in ("SELL", "TRIM"))

    # Money per sector after the sells and trims on this list, for the sector cap.
    sector_value: dict[str, float] = {}
    for a in actions:
        d = a.decision
        if d.held and d.sector and d.price is not None:
            sector_value[d.sector] = sector_value.get(d.sector, 0.0) + d.held_shares * d.price - (
                a.amount or 0.0 if a.action in ("SELL", "TRIM") else 0.0)
    sector_cap = sizing.get("max_sector_weight", 1.0) * total_value

    n_positions = sum(1 for a in actions if a.held and a.action != "SELL") + \
        sum(1 for a in actions if a.action == "BUY")
    weight = target_weight(n_positions, sizing)
    target_value = weight * total_value

    def size_factor(rating: str) -> float:
        return 1.0 if rating == "Buy" else sizing["overweight_size_factor"]

    # New Buys first, then adds to Buy-rated holdings, then the half-size Overweights;
    # within a group, the widest margin of safety first.
    wants = []
    for a in actions:
        d = a.decision
        if a.action == "BUY":
            wants.append((0 if d.rating == "Buy" else 2, a, target_value * size_factor(d.rating)))
        elif a.held and a.action == "KEEP" and d.rating in BUY_RATINGS:
            if d.trend == "FREEFALL":
                a.reason += "; no adding in FREEFALL"
                continue
            gap = target_value * size_factor(d.rating) - d.held_shares * (d.price or 0.0)
            if gap > 0:
                wants.append((1 if d.rating == "Buy" else 3, a, gap))
    wants.sort(key=lambda w: (w[0], -(w[1].decision.margin_of_safety or -1.0)))

    for _, a, wanted in wants:
        d = a.decision
        room = sector_cap - sector_value.get(d.sector, 0.0) if d.sector else float("inf")
        amount = round(min(wanted, available, max(room, 0.0)), 2)
        capped = room < min(wanted, available)
        if d.price is None:
            a.reason += "; no current price, size it yourself"
            if a.action == "BUY":
                a.amount = amount
                available -= amount
            continue
        limit = f"{d.sector} at the {sizing.get('max_sector_weight', 1.0):.0%} sector cap" if capped \
            else f"not enough cash (have {available:,.2f})"
        if amount < sizing["min_trade_amount"]:
            if a.action == "BUY":
                a.action = "SKIP"
                a.reason += f"; {limit}"
            elif capped:
                a.reason += f"; no add, {limit}"
            continue
        a.amount = amount
        a.shares = round(amount / d.price, 4)
        available -= amount
        if d.sector:
            sector_value[d.sector] = sector_value.get(d.sector, 0.0) + amount
        if capped:
            a.reason += f"; reduced, {limit}"
        if a.action == "KEEP":
            a.action = "ADD"
            a.reason += f"; below target {weight:.0%}"
        elif d.rating == "Overweight":
            a.reason += " (half size)"

    summary = {
        "total_value": total_value,
        "cash": cash,
        "target_weight": weight,
        "cash_after": available,
    }
    return actions, summary
