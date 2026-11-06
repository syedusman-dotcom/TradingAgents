"""Google News RSS news fetching: a keyless fallback when Yahoo has no news."""

import contextlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.news import _format_articles

logger = logging.getLogger(__name__)

SEARCH_URL = "https://news.google.com/rss/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def _yahoo_name(canonical: str) -> str | None:
    """The company's short name from Yahoo; None when the lookup fails or has none."""
    with contextlib.suppress(Exception):
        import yfinance as yf

        from tradingagents.dataflows.vendors.yahoo.common import yf_retry

        info = yf_retry(lambda: yf.Ticker(canonical).info) or {}
        name = (info.get("shortName") or "").strip()
        if name:
            return name
    return None


def _company_name(canonical: str) -> str:
    """The company's short name from Yahoo, else the symbol without its exchange suffix."""
    return _yahoo_name(canonical) or canonical.rsplit(".", 1)[0]


def _parse_items(body: bytes) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise VendorUnavailableError(f"Google News returned unparseable XML: {exc}") from exc
    if root.tag != "rss" or root.find("channel") is None:
        # A consent or interstitial page parses too, with no items: not "no news".
        raise VendorUnavailableError(f"Google News returned <{root.tag}>, not an RSS feed")
    items = []
    for item in root.iter("item"):
        pub_date = None
        with contextlib.suppress(TypeError, ValueError):
            pub_date = parsedate_to_datetime(item.findtext("pubDate") or "")
        items.append({
            "title": (item.findtext("title") or "").strip() or "No title",
            "summary": "",
            "publisher": (item.findtext("source") or "").strip() or "Unknown",
            "link": (item.findtext("link") or "").strip(),
            "pub_date": pub_date,
        })
    return items


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """News about ``ticker`` in the window from Google News RSS, matched by company name.

    The feed is a sample of recent coverage found by name, so each item must be
    checked against the company. A failed request or an unreadable feed raises
    ``VendorUnavailableError``.
    """
    limit = get_config()["news_article_limit"]
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    name = _company_name(canonical)
    # ``before:`` is exclusive: ask for the day after the window so its last day is searched.
    before = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    query = f'"{name}" after:{start_date} before:{before}'
    url = f"{SEARCH_URL}?q={quote(query, safe='')}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Google News request failed: {exc}") from exc
    if resp.status_code != 200:
        raise VendorUnavailableError(f"Google News returned HTTP {resp.status_code}")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    # Google's date operators are approximate, so trim to the window here too.
    in_range = [a for a in _parse_items(resp.content) if in_window(a["pub_date"], start_dt, end_dt)]
    if not in_range:
        return f"No news found for {ticker}{resolved} between {start_date} and {end_date}"
    in_range = in_range[:limit]
    for a in in_range:
        if a["pub_date"] is not None:
            a["summary"] = f"Published {a['pub_date'].strftime('%Y-%m-%d')}"
    return (
        f"## {ticker}{resolved} News, from {start_date} to {end_date}:\n\n"
        "Google News sample, matched by name: verify each item is about the company.\n\n"
        f"{_format_articles(in_range)}"
    )
