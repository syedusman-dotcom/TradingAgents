"""Render the action list as Markdown and as one self-contained, phone-friendly HTML page."""

from __future__ import annotations

import html

from markdown_it import MarkdownIt

from value_pipeline.actions import Action


def _money(x: float | None) -> str:
    return "–" if x is None else f"${x:,.2f}"


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.1%}"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def actions_markdown(date: str, actions: list[Action], summary: dict, *, reports: dict[str, str],
                     comparison: dict | None, cost: float, failures: list[str], dry_run: bool,
                     knives: list[str] | None = None) -> str:
    held = [a for a in actions if a.held]
    new = [a for a in actions if not a.held and a.action == "BUY"]
    skipped = [a for a in actions if not a.held and a.action != "BUY"]
    lines = [f"# Value review {date}" + (" (DRY RUN: fake ratings, no agent calls)" if dry_run else ""), ""]
    lines.append("These are suggestions. Nothing was traded: place orders at your broker, then record "
                 "each fill with `buy` / `sell`. Place sells and trims before buys; buy amounts assume their proceeds.")
    lines.append("")

    lines += ["## Portfolio", ""]
    lines.append(f"- Value: **{_money(summary['total_value'])}** (cash {_money(summary['cash'])})")
    if comparison:
        lines.append(f"- Return since {comparison['since']}: **{_pct(comparison['portfolio_return'])}** "
                     f"vs SPY {_pct(comparison['spy_return'])} (same cash flows; {_money(comparison['invested'])} invested)")
    lines.append(f"- Target weight per stock: {summary['target_weight']:.0%}; cash left after these actions: "
                 f"{_money(summary['cash_after'])}")
    lines.append(f"- Cost of this review: ~{_money(cost)}")
    lines.append("")

    lines += ["## Holdings", ""]
    if held:
        lines += ["| Ticker | Rating | Action | Reason | Report |", "|---|---|---|---|---|"]
        for a in held:
            what = a.action
            if a.action in ("TRIM", "SELL") and a.shares:
                what += f" {a.shares:g} sh (~{_money(a.amount)})"
            elif a.action == "ADD" and a.amount:
                what += f" {_money(a.amount)} ({a.shares:g} sh)"
            reason = a.reason + "".join(f" **⚑ {f}**" for f in a.flags)
            lines.append(f"| {a.ticker} | {a.rating} | {_cell(what)} | {_cell(reason)} | {_link(reports, a.ticker)} |")
    else:
        lines.append("No holdings yet.")
    lines.append("")

    lines += ["## New buys", ""]
    if new:
        lines += ["| Ticker | Rating | Amount | Shares | Margin of safety | F-Score | Trend | Report |",
                  "|---|---|---|---|---|---|---|---|"]
        for a in new:
            d = a.decision
            shares = "–" if a.shares is None else f"{a.shares:g}"
            lines.append(f"| {a.ticker} | {a.rating} | {_money(a.amount)} | {shares} | {_pct(d.margin_of_safety)} | "
                         f"{d.fscore if d.fscore is not None else '–'} | {d.trend or '–'} | {_link(reports, a.ticker)} |")
    else:
        lines.append("No new buys this month.")
    lines.append("")

    if skipped:
        lines += ["## Candidates not bought", ""]
        lines += [f"- {a.ticker}: {a.reason} ({_link(reports, a.ticker)})" for a in skipped]
        lines.append("")
    if knives:
        lines += ["## Watch: falling knives", "", "Ranked well in the screen but in FREEFALL: " + ", ".join(knives), ""]
    if failures:
        lines += ["## Failed runs", ""] + [f"- {f}" for f in failures] + [""]
    return "\n".join(lines)


def _link(reports: dict[str, str], ticker: str) -> str:
    return f"[report]({reports[ticker]})" if ticker in reports else "–"


_CSS = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e3e1da; --accent:#0b6e4f; --flag:#b4530c; }
@media (prefers-color-scheme: dark) { :root { --bg:#161614; --fg:#ecebe6; --muted:#a3a29b; --line:#33322e; --accent:#5fc29b; --flag:#f0a35e; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.5rem; margin: 0 0 12px; }
h2 { font-size: 1.1rem; margin: 28px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
p, li { color: var(--fg); }
a { color: var(--accent); }
strong { font-weight: 650; }
.table { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
td:first-child { font-weight: 650; white-space: nowrap; }
td strong { color: var(--flag); }
"""


def actions_html(markdown: str, title: str) -> str:
    """The Markdown as a standalone page: inline CSS, no scripts, nothing loaded."""
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
    body = body.replace("<table>", '<div class="table"><table>').replace("</table>", "</table></div>")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )
