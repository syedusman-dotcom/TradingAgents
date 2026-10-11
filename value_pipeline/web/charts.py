"""Small inline SVG charts: no scripts, no external files, colours from the page's CSS."""

from __future__ import annotations

from html import escape

_COLORS = ("var(--accent)", "var(--muted)", "var(--flag)")


def line_chart(labels: list[str], lines: dict[str, list[float]], height: int = 180) -> str:
    """One polyline per series over a shared x axis, scaled to the data's range; "" if too little data."""
    width, pad = 640, 28
    values = [v for series in lines.values() for v in series if v is not None]
    if len(labels) < 2 or not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    def x(i: int) -> float:
        return pad + i * (width - 2 * pad) / (len(labels) - 1)

    def y(v: float) -> float:
        return height - pad - (v - lo) * (height - 2 * pad) / span

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
             f'class="chart" aria-label="{escape(", ".join(lines))}">',
             f'<text x="{pad}" y="14" class="axis">{hi:,.0f}</text>',
             f'<text x="{pad}" y="{height - 6}" class="axis">{lo:,.0f}  '
             f'({escape(labels[0])} → {escape(labels[-1])})</text>']
    for k, (name, series) in enumerate(lines.items()):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series) if v is not None)
        color = _COLORS[k % len(_COLORS)]
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>')
        parts.append(f'<text x="{width - pad - 150}" y="{16 + 14 * k}" class="axis" '
                     f'style="fill:{color}">{escape(name)}</text>')
    parts.append("</svg>")
    return "".join(parts)
