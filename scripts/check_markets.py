"""Fetch every market's Wikipedia pages and print how many tickers each yields.

Network script, not a test: ``uv run python scripts/check_markets.py``. Exits 1 when any
market gives fewer than 10 tickers, so a wrong page or a changed table shows up here.
"""

from __future__ import annotations

import sys

from value_pipeline import market_data


def main() -> int:
    failed = False
    for market in market_data.MARKETS.values():
        try:
            count = len(market_data.fetch_market(market))
        except Exception as exc:  # noqa: BLE001 - report and carry on to the next market
            print(f"{market.key} {market.country} 0 WARNING fetch failed: {exc}")
            failed = True
            continue
        warn = count < market_data.MIN_TICKERS
        failed = failed or warn
        print(f"{market.key} {market.country} {count}" + (" static" if market.tickers else "") + (" WARNING" if warn else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
