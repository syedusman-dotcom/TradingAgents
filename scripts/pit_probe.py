"""Probe the point-in-time statements on the EDGAR companyfacts files already cached.

Read-only and offline: for every ``CIK*.json`` under the cache folder (default
``~/.tradingagents/cache/sec_edgar``) it runs ``pit_statements.extract`` -> ``years_as_of`` on
four dates and ``shares_as_of_dated`` on the last, and prints one row per filer: the number of
fiscal years known on each date and, for the newest of them, which of revenue / EBIT / gross
profit / free cash flow / shares are missing (R E G F S; ``-`` when none is). The ticker comes
from the cached ``company_tickers.json`` when it is there. No network call is made.

    .venv/bin/python scripts/pit_probe.py [cache_dir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # this checkout, not an install

from value_pipeline import pit_statements as pit  # noqa: E402

DATES = ("2016-06-30", "2018-06-30", "2020-06-30", "2024-06-30")
FIELDS = (("revenue", "R"), ("ebit", "E"), ("gross_profit", "G"), ("free_cash_flow", "F"),
          ("shares", "S"))


def _tickers(folder: Path) -> dict[int, str]:
    path = folder / "company_tickers.json"
    if not path.exists():
        return {}
    out: dict[int, list[str]] = {}
    for entry in json.loads(path.read_text(encoding="utf-8")).values():
        out.setdefault(int(entry["cik_str"]), []).append(entry["ticker"])
    return {cik: "/".join(sorted(names)) for cik, names in out.items()}


def _cell(store: dict, as_of: str) -> str:
    years = pit.years_as_of(store, as_of)
    if not years:
        return "0"
    newest = years[0]
    missing = "".join(code for field, code in FIELDS if newest.get(field) is None)
    return f"{len(years)} {newest['fiscal_year_end'][:7]} {missing or '-'}"


def main(folder: Path) -> None:
    names = _tickers(folder)
    print("| CIK | Ticker | " + " | ".join(DATES) + " | Shares 2024-06-30 |")
    print("|---|---|" + "---|" * len(DATES) + "--:|")
    for path in sorted(folder.glob("CIK*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cik = int(path.stem.removeprefix("CIK"))
        store = pit.extract(raw)
        cells = [_cell(store, d) for d in DATES]
        dated = pit.shares_as_of_dated(store, DATES[-1])
        shares = "–" if dated is None else f"{dated[1] / 1e6:,.1f}M @ {dated[0]}"
        print(f"| {cik} | {names.get(cik, raw.get('entityName', '?'))} | "
              + " | ".join(cells) + f" | {shares} |")
    print("\nCell: fiscal years known, the newest year's end (YYYY-MM), its missing fields "
          "(R revenue, E EBIT, G gross profit, F free cash flow, S shares; - none).")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1
         else Path.home() / ".tradingagents" / "cache" / "sec_edgar")
