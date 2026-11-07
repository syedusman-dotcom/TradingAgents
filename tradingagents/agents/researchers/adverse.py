"""The bear's own evidence in the independent arm: insider transactions and recent news.

Fetched through the same vendor routing the analysts' tools use, so a run reads the same data
either way. Each section is cut to a fixed length and, when its fetch fails, says so instead of
reading as an empty finding.
"""

from __future__ import annotations

from datetime import date as _date, timedelta

from tradingagents.dataflows.router import route_to_vendor

SECTION_LIMIT = 6_000
NEWS_DAYS = 90


def _routed_news(ticker: str, start: str, end: str) -> str:
    return route_to_vendor("get_news", ticker, start, end)


def _routed_insiders(ticker: str, date: str) -> str:
    return route_to_vendor("get_insider_transactions", ticker, date or None)


def _section(fetch, *args) -> str:
    """The fetch's text cut to ``SECTION_LIMIT``, or ``not retrieved: <reason>`` on failure."""
    try:
        text = str(fetch(*args) or "").strip()
    except Exception as exc:  # noqa: BLE001 — a missing section must not stop the bear
        return f"not retrieved: {str(exc) or type(exc).__name__}"
    return text[:SECTION_LIMIT] if text else "(the vendor returned nothing)"


def adverse_pack(ticker: str, date: str, fetch_news=None, fetch_insiders=None) -> str:
    """The **Adverse evidence pack** for ``ticker`` as of ``date`` (yyyy-mm-dd)."""
    fetch_news = fetch_news or _routed_news
    fetch_insiders = fetch_insiders or _routed_insiders
    insiders = _section(fetch_insiders, ticker, date)

    def news_window():
        start = (_date.fromisoformat(date) - timedelta(days=NEWS_DAYS)).isoformat()
        return fetch_news(ticker, start, date)

    news = _section(news_window)
    return (
        "**Adverse evidence pack**\n\n"
        f"## Insider transactions\n{insiders}\n\n"
        f"## News, last {NEWS_DAYS} days\n{news}"
    )
