"""The day's trade suggestions as one short file, pushed to a private repo.

A Claude routine reads ``latest.md`` from that repo every day and messages the
user when there is a buy. The file carries only the actions (ticker, shares,
amount, price), never the portfolio's value or cash.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from value_pipeline.config import Paths

log = logging.getLogger(__name__)

TRADES = ("BUY", "ADD", "SELL", "TRIM")


def latest_review(paths: Paths, today: str) -> Path | None:
    """Today's newest review folder holding an actions.json (a second review the same day is ``<date>-2``)."""
    folders = [p for p in paths.reports.glob(f"{today}*")
               if p.is_dir() and not p.name.endswith("-dry-run") and (p / "actions.json").exists()]
    return max(folders, key=lambda p: (p / "actions.json").stat().st_mtime) if folders else None


def _price(x: float | None, currency: str | None) -> str:
    if x is None:
        return ""
    return f"${x:,.2f}" if not currency or currency == "USD" else f"{x:,.2f} {currency}"


def line(a: dict) -> str:
    """One action as one line: ``- Dunelm (DNLM.L): BUY 112 sh ($1,017), rated Buy, price 900.00 GBp``."""
    who = f"{a['name']} ({a['ticker']})" if a.get("name") else a["ticker"]
    what = a["action"]
    if a.get("shares"):
        what += f" {a['shares']:g} sh"
    if a.get("amount"):
        what += f" (${a['amount']:,.0f})"
    bits = [f"- {who}: {what}", f"rated {a['rating']}"]
    if a.get("lane") and a["lane"] != "value":
        bits.append(f"lane {a['lane']}")
    if a.get("price") is not None:
        bits.append(f"price {_price(a['price'], a.get('currency'))}")
    if a.get("entry_price") is not None:
        bits.append(f"buy below {_price(a['entry_price'], a.get('currency'))}")
    if a.get("flags"):
        bits.append("flags: " + "; ".join(a["flags"]))
    return ", ".join(bits)


def summary(paths: Paths, today: str) -> str:
    """Today's buys, sells and trims as Markdown; says so when no review ran."""
    folder = latest_review(paths, today)
    lines = [f"# Value pipeline: {today}", ""]
    if folder is None:
        return "\n".join(lines + [f"No review ran on {today}.", ""])
    rows = json.loads((folder / "actions.json").read_text(encoding="utf-8"))
    written = datetime.fromtimestamp((folder / "actions.json").stat().st_mtime).strftime("%H:%M")
    buys = [line(a) for a in rows if a["action"] in ("BUY", "ADD")]
    sells = [line(a) for a in rows if a["action"] in ("SELL", "TRIM")]
    others = [f"{a['ticker']} {a['rating']}" for a in rows if a["action"] not in TRADES]
    lines += [f"Review {folder.name}, written {written} local time. Nothing is traded automatically.", "",
              "## Buys", "", *(buys or ["None."]), "",
              "## Sells and trims", "", *(sells or ["None."]), ""]
    if others:
        lines += ["## Reviewed, no trade", "", ", ".join(others), ""]
    return "\n".join(lines)


def publish(paths: Paths, today: str, repo_dir: Path | None = None) -> Path:
    """Write ``latest.md`` into the status clone and push it; a failed push is logged, not raised."""
    repo = repo_dir or paths.root / "status"
    repo.mkdir(parents=True, exist_ok=True)
    out = repo / "latest.md"
    out.write_text(summary(paths, today), encoding="utf-8")
    if not (repo / ".git").exists():
        log.warning("%s is not a git clone: wrote %s but pushed nothing", repo, out)
        return out

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)

    git("add", "latest.md")
    if git("diff", "--cached", "--quiet").returncode == 0:
        log.info("status unchanged; nothing to push")
        return out
    git("commit", "-q", "-m", f"status {today}")
    pushed = git("push", "-q", "origin", "HEAD")
    if pushed.returncode:
        log.warning("status push failed: %s", pushed.stderr.strip())
    return out
