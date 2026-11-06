"""Google News RSS vendor: the keyless fallback for the news tool. No network."""

import pytest
import requests

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import google_news

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>feed</title>
<item><title>Betsson wins licence</title><link>https://example.com/a</link>
<pubDate>Tue, 03 Nov 2026 09:30:00 GMT</pubDate>
<description>&lt;a href="https://example.com/a"&gt;Betsson wins licence&lt;/a&gt;</description>
<source url="https://example.com">Example Wire</source></item>
<item><title>Betsson Q3 results</title><link>https://example.com/b</link>
<pubDate>Thu, 05 Nov 2026 07:00:00 GMT</pubDate>
<source url="https://example.org">Other Post</source></item>
<item><title>Betsson old story</title><link>https://example.com/c</link>
<pubDate>Mon, 01 Jun 2026 07:00:00 GMT</pubDate>
<source url="https://example.org">Other Post</source></item>
</channel></rss>"""


class _Resp:
    def __init__(self, status=200, text=RSS):
        self.status_code = status
        self.content = text.encode()


@pytest.fixture(autouse=True)
def _name(monkeypatch):
    monkeypatch.setattr(google_news, "_company_name", lambda canonical: "Betsson AB")


def _serve(monkeypatch, resp):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


def test_in_window_articles_only(monkeypatch):
    seen = _serve(monkeypatch, _Resp())
    out = google_news.get_news("BETS-B.ST", "2026-10-30", "2026-11-06")
    assert out.startswith("## BETS-B.ST News, from 2026-10-30 to 2026-11-06:")
    assert "Google News sample, matched by name: verify each item is about the company." in out
    assert "### Betsson wins licence (source: Example Wire)" in out
    assert "### Betsson Q3 results (source: Other Post)" in out
    assert "2026-11-03" in out and "2026-11-05" in out
    assert "old story" not in out
    assert "Betsson%20AB" in seen["url"] or "Betsson+AB" in seen["url"]
    assert "after%3A2026-10-30" in seen["url"] and "before%3A2026-11-06" in seen["url"]
    assert seen["kwargs"]["timeout"] == 20
    assert "User-Agent" in seen["kwargs"]["headers"]


def test_http_error_is_unavailable(monkeypatch):
    _serve(monkeypatch, _Resp(status=503, text=""))
    with pytest.raises(VendorUnavailableError):
        google_news.get_news("BETS-B.ST", "2026-10-30", "2026-11-06")


def test_network_error_is_unavailable(monkeypatch):
    def boom(url, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(VendorUnavailableError):
        google_news.get_news("BETS-B.ST", "2026-10-30", "2026-11-06")


def test_malformed_xml_is_unavailable(monkeypatch):
    _serve(monkeypatch, _Resp(text="<rss><channel><item>"))
    with pytest.raises(VendorUnavailableError):
        google_news.get_news("BETS-B.ST", "2026-10-30", "2026-11-06")


def test_no_items_in_window(monkeypatch):
    _serve(monkeypatch, _Resp())
    out = google_news.get_news("BETS-B.ST", "2026-08-01", "2026-08-31")
    assert out == "No news found for BETS-B.ST between 2026-08-01 and 2026-08-31"


def test_router_falls_back_to_google(monkeypatch):
    def down(*a, **k):
        raise VendorUnavailableError("yahoo down")

    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "nordic", down)
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", down)
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "google_news", lambda *a, **k: "from google")
    assert router.route_to_vendor("get_news", "BETS-B.ST", "2026-10-30", "2026-11-06") == "from google"
