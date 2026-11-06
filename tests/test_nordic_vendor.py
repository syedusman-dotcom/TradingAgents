"""Nordic exchange releases and insider transactions vendor. No network."""

import json

import pytest
import requests

from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import google_news, nordic

NASDAQ = {
    "count": 3,
    "results": {"item": [
        {
            "disclosureId": 1003, "headline": "Betsson AB Interim report Q3 2026", "language": "en",
            "cnsCategory": "Interim report (Q1 and Q3)",
            "messageUrl": "https://view.news.eu.nasdaq.com/view?id=b3&lang=en&src=listed",
            "releaseTime": "2026-11-05 07:30:00 +0000", "market": "Main Market, Stockholm",
            "company": "Betsson AB", "attachment": [{"attachmentUrl": "https://example.com/q3.pdf"}],
        },
        {
            "disclosureId": 1002, "headline": "Betsson AB Invitation to Q3 presentation", "language": "en",
            "cnsCategory": "Investor News",
            "messageUrl": "https://view.news.eu.nasdaq.com/view?id=b2&lang=en&src=listed",
            "releaseTime": "2026-10-20 08:00:00", "market": "Main Market, Stockholm",
            "company": "Betsson AB", "attachment": [],
        },
        {
            "disclosureId": 1001, "headline": "Betsson AB old notice", "language": "en",
            "cnsCategory": "Investor News",
            "messageUrl": "https://view.news.eu.nasdaq.com/view?id=b1&lang=en&src=listed",
            "releaseTime": "2026-06-01 08:00:00", "market": "Main Market, Stockholm",
            "company": "Betsson AB", "attachment": [],
        },
        {
            "disclosureId": 1004, "headline": "Avanza monthly savings report mentioning Betsson",
            "language": "en", "cnsCategory": "Investor News",
            "messageUrl": "https://view.news.eu.nasdaq.com/view?id=b4&lang=en&src=listed",
            "releaseTime": "2026-11-01 05:00:00", "market": "Main Market, Stockholm",
            "company": "Avanza Bank AB", "attachment": [],
        },
    ]},
}

BODY_HTML = (
    "<html><head><style>p{color:red}</style><script>var x=1;</script></head>"
    "<body><h1>Interim report</h1><p>Revenue  grew\n 12 per cent &amp; margin held.</p></body></html>"
)


class _Resp:
    def __init__(self, status=200, text="", content=None):
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode()

    def json(self):
        return json.loads(self.text)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(nordic, "MIN_INTERVAL", 0)
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-11-07")
    monkeypatch.setattr(nordic, "_company_name", lambda canonical: "Betsson AB ser. B")
    monkeypatch.setattr(google_news, "get_news", lambda *a, **k: "GOOGLE SAMPLE")


def _serve(monkeypatch, routes):
    """Answer requests.get by the first route key found in the URL; record every URL."""
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        assert kwargs["timeout"] == 20
        assert "User-Agent" in kwargs["headers"]
        for key, resp in routes.items():
            if key in url:
                return resp(url) if callable(resp) else resp
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


def test_market_of():
    assert nordic.market_of("BETS-B.ST") == "sto"
    assert nordic.market_of("NOKIA.HE") == "hel"
    assert nordic.market_of("NOVO-B.CO") == "cph"
    assert nordic.market_of("EQNR.OL") == "ose"
    assert nordic.market_of("AAPL") is None


def test_non_nordic_news_and_insiders_pass_on():
    with pytest.raises(NoMarketDataError):
        nordic.get_news("AAPL", "2026-10-01", "2026-11-06")
    with pytest.raises(NoMarketDataError):
        nordic.get_insider_transactions("AAPL")


def test_nasdaq_releases_in_window(monkeypatch):
    seen = _serve(monkeypatch, {
        "api.news.eu.nasdaq.com": _Resp(text=json.dumps(NASDAQ)),
        "view?id=b3": _Resp(text=BODY_HTML),
        "view?id=b2": _Resp(status=404),
    })
    out = nordic.get_news("BETS-B.ST", "2026-10-15", "2026-11-06")
    assert out.startswith(
        "## BETS-B.ST Exchange releases, from 2026-10-15 to 2026-11-06 (Nasdaq Stockholm):"
    )
    assert "### Betsson AB Interim report Q3 2026 (source: Nasdaq Stockholm)" in out
    assert "Published 2026-11-05 · Interim report (Q1 and Q3)" in out
    assert "### Betsson AB Invitation to Q3 presentation (source: Nasdaq Stockholm)" in out
    assert "Published 2026-10-20 · Investor News" in out
    assert "Interim report Revenue grew 12 per cent & margin held." in out
    assert "color:red" not in out and "var x" not in out
    assert "Link: https://view.news.eu.nasdaq.com/view?id=b3&lang=en&src=listed" in out
    assert "old notice" not in out
    assert "Avanza" not in out
    assert out.index("Interim report Q3") < out.index("Invitation to Q3")
    assert out.rstrip().endswith("GOOGLE SAMPLE")
    assert "freeText=Betsson" in seen[0] and "globalName=NordicAllMarkets" in seen[0]


def test_nasdaq_no_releases_still_appends_google(monkeypatch):
    _serve(monkeypatch, {"api.news.eu.nasdaq.com": _Resp(text=json.dumps(NASDAQ))})
    out = nordic.get_news("BETS-B.ST", "2026-08-01", "2026-08-31")
    assert "No exchange releases for BETS-B.ST between 2026-08-01 and 2026-08-31" in out
    assert "GOOGLE SAMPLE" in out


def test_google_failure_is_one_line(monkeypatch):
    def boom(*a, **k):
        raise VendorUnavailableError("down")

    monkeypatch.setattr(google_news, "get_news", boom)
    _serve(monkeypatch, {"api.news.eu.nasdaq.com": _Resp(text=json.dumps(NASDAQ))})
    out = nordic.get_news("BETS-B.ST", "2026-08-01", "2026-08-31")
    assert "Press sample unavailable" in out


def test_nasdaq_http_error_is_unavailable(monkeypatch):
    _serve(monkeypatch, {"api.news.eu.nasdaq.com": _Resp(status=503)})
    with pytest.raises(VendorUnavailableError):
        nordic.get_news("BETS-B.ST", "2026-10-15", "2026-11-06")


def test_nasdaq_unexpected_shape_is_unavailable(monkeypatch):
    _serve(monkeypatch, {"api.news.eu.nasdaq.com": _Resp(text=json.dumps({"oops": 1}))})
    with pytest.raises(VendorUnavailableError):
        nordic.get_news("BETS-B.ST", "2026-10-15", "2026-11-06")


def _oslo_list(url):
    messages = [
        {"messageId": 11, "title": "Equinor ASA: Share buy-back",
         "category": [{"category_en": "ACQUISITION OR DISPOSAL OF AN ISSUER'S OWN SHARES"}],
         "issuerId": 1309, "issuerSign": "EQNR", "issuerName": "Equinor ASA",
         "publishedTime": "2026-11-03T07:00:05.164Z", "numbAttachments": 1},
        {"messageId": 12, "title": "Other ASA: Results",
         "category": [{"category_en": "HALF YEAR FINANCIAL REPORT"}],
         "issuerId": 99, "issuerSign": "OTHR", "issuerName": "Other ASA",
         "publishedTime": "2026-11-02T07:00:00.000Z", "numbAttachments": 0},
        {"messageId": 10, "title": "Equinor ASA: Primary insider purchase",
         "category": [{"category_en": "MANAGERS’ TRANSACTION"}],
         "issuerId": 1309, "issuerSign": "EQNR", "issuerName": "Equinor ASA",
         "publishedTime": "2026-09-15T16:00:00.000Z", "numbAttachments": 0},
    ]
    # Each window serves only the messages published in it.
    frm = url.split("fromDate=")[1][:10]
    to = url.split("toDate=")[1][:10]
    picked = [m for m in messages if frm <= m["publishedTime"][:10] <= to]
    return _Resp(text=json.dumps({"data": {"messages": picked, "overflow": False}}))


def _oslo_body(url):
    mid = url.split("messageId=")[1]
    return _Resp(text=json.dumps({"data": {"message": {"body": f"Body of message {mid}."}}}))


def test_newsweb_splits_window_and_keeps_only_the_issuer(monkeypatch):
    seen = _serve(monkeypatch, {"newsreader/list": _oslo_list, "newsreader/message": _oslo_body})
    out = nordic.get_news("EQNR.OL", "2026-09-07", "2026-11-05")
    lists = [u for u in seen if "newsreader/list" in u]
    assert len(lists) == 2
    assert "fromDate=2026-09-07&toDate=2026-10-07" in lists[0]
    assert "fromDate=2026-10-08&toDate=2026-11-05" in lists[1]
    assert all("issuer=EQNR" in u for u in lists)
    assert out.startswith(
        "## EQNR.OL Exchange releases, from 2026-09-07 to 2026-11-05 (Oslo Børs NewsWeb):"
    )
    assert "### Equinor ASA: Share buy-back (source: Oslo Børs NewsWeb)" in out
    assert "Published 2026-11-03 · ACQUISITION OR DISPOSAL OF AN ISSUER'S OWN SHARES" in out
    assert "### Equinor ASA: Primary insider purchase (source: Oslo Børs NewsWeb)" in out
    assert "Other ASA" not in out
    assert "Body of message 11." in out and "Body of message 10." in out
    assert [u for u in seen if "newsreader/message" in u] == [
        f"{nordic.NEWSWEB_MESSAGE}?messageId=11", f"{nordic.NEWSWEB_MESSAGE}?messageId=10",
    ]
    assert out.index("Share buy-back") < out.index("Primary insider purchase")


def test_newsweb_insiders_are_managers_transactions(monkeypatch):
    _serve(monkeypatch, {"newsreader/list": _oslo_list, "newsreader/message": _oslo_body})
    out = nordic.get_insider_transactions("EQNR.OL", "2026-11-07")
    assert out.startswith("# Insider transactions for EQNR.OL (Oslo Børs NewsWeb managers' transactions,")
    assert "2026-09-15 · Equinor ASA: Primary insider purchase · " in out
    assert "Body of message 10." in out
    assert "Share buy-back" not in out


FI_HEADER = (
    "Publiceringsdatum;Emittent;LEI-kod;Anmälningsskyldig;Person i ledande ställning;Befattning;"
    "Närstående;Korrigering;Beskrivning av korrigering;Är förstagångsrapportering;"
    "Är kopplad till aktieprogram;Karaktär;Instrumenttyp;Instrumentnamn;ISIN;Transaktionsdatum;"
    "Volym;Volymsenhet;Pris;Valuta;Handelsplats;Status;"
)
FI_ROWS = [
    "2026-10-27 14:18:37;Betsson AB;LEI1;;Anna Andersson;VD;Nej;;;Ja;;Förvärv;Aktie;"
    "Betsson AB ser. B;SE0000000001;2026-10-27 00:00:00;1000,0;Antal;309,8227;SEK;NASDAQ STOCKHOLM AB;Aktuell;",
    "2026-09-01 09:00:00;Betsson AB (publ);LEI1;;Bo Berg;CFO;Nej;;;Ja;;Avyttring;Aktie;"
    "Betsson AB ser. B;SE0000000001;2026-08-30 00:00:00;250,0;Antal;101,25;SEK;NASDAQ STOCKHOLM AB;Aktuell;",
    "2026-09-02 09:00:00;Betsson Car AB;LEI2;;Cia Ek;VD;Nej;;;Ja;;Förvärv;Aktie;"
    "Betsson Car AB;SE0000000002;2026-09-01 00:00:00;5,0;Antal;10;SEK;NASDAQ STOCKHOLM AB;Aktuell;",
]


def _fi(rows):
    return _Resp(content="\r\n".join([FI_HEADER, *rows]).encode("utf-16-le"))


def test_fi_csv_buys_and_sells(monkeypatch):
    seen = _serve(monkeypatch, {"marknadssok.fi.se": _fi(FI_ROWS)})
    out = nordic.get_insider_transactions("BETS-B.ST", "2026-11-07")
    assert out.startswith(
        "# Insider transactions for BETS-B.ST (Finansinspektionen insynsregistret, "
        "2025-11-07 to 2026-11-07)"
    )
    lines = out.splitlines()
    assert lines[1] == (
        "published,person,position,nature,instrument,transaction_date,volume,unit,price,currency,status"
    )
    assert lines[2] == (
        "2026-10-27,Anna Andersson,VD,buy,Betsson AB ser. B,2026-10-27,1000,Antal,309.8227,SEK,Aktuell"
    )
    assert lines[3] == (
        "2026-09-01,Bo Berg,CFO,sell,Betsson AB ser. B,2026-08-30,250,Antal,101.25,SEK,Aktuell"
    )
    assert "Betsson Car" not in out
    assert lines[-1] == "Totals: 1 buys 1000, 1 sells 250"
    url = seen[0]
    assert "Utgivare=Betsson" in url
    assert "Publiceringsdatum.From=2025-11-07" in url and "Publiceringsdatum.To=2026-11-07" in url


def test_fi_with_bom_and_other_nature(monkeypatch):
    rows = [FI_ROWS[0].replace("Förvärv", "Tilldelning")]
    body = "﻿" + "\r\n".join([FI_HEADER, *rows])
    _serve(monkeypatch, {"marknadssok.fi.se": _Resp(content=body.encode("utf-16"))})
    out = nordic.get_insider_transactions("BETS-B.ST", "2026-11-07")
    assert ",Tilldelning," in out
    assert out.splitlines()[-1] == "Totals: 0 buys 0, 0 sells 0"


def test_fi_empty_export(monkeypatch):
    _serve(monkeypatch, {"marknadssok.fi.se": _fi([])})
    out = nordic.get_insider_transactions("BETS-B.ST", "2026-11-07")
    assert out == (
        "No insider transactions published for BETS-B.ST between 2025-11-07 and 2026-11-07 "
        "(Finansinspektionen insynsregistret)"
    )


def test_fi_unexpected_columns_are_unavailable(monkeypatch):
    _serve(monkeypatch, {"marknadssok.fi.se": _Resp(content="<html>error</html>".encode("utf-16-le"))})
    with pytest.raises(VendorUnavailableError):
        nordic.get_insider_transactions("BETS-B.ST", "2026-11-07")


def test_fi_full_page_splits_the_window(monkeypatch):
    calls = []

    def page(url):
        calls.append(url)
        return _fi(FI_ROWS[:1] * (1000 if len(calls) == 1 else 1))

    _serve(monkeypatch, {"marknadssok.fi.se": page})
    out = nordic.get_insider_transactions("BETS-B.ST", "2026-11-07")
    assert len(calls) == 3
    assert out.splitlines()[-1] == "Totals: 2 buys 2000, 0 sells 0"


def test_historical_run_is_withheld(monkeypatch):
    seen = _serve(monkeypatch, {})
    out = nordic.get_insider_transactions("BETS-B.ST", "2020-01-02")
    assert "withheld" in out and seen == []


def test_helsinki_insiders_are_nasdaq_managers_transactions(monkeypatch):
    monkeypatch.setattr(nordic, "_company_name", lambda canonical: "Nokia Corporation")
    feed = {"count": 2, "results": {"item": [
        {"disclosureId": 2, "headline": "Nokia Corporation - Managers' transactions (Heard)",
         "language": "en", "cnsCategory": "Managers' Transactions",
         "messageUrl": "https://view.news.eu.nasdaq.com/view?id=n2&lang=en",
         "releaseTime": "2026-10-15 14:00:00 +0000", "market": "Main Market, Helsinki",
         "company": "Nokia", "attachment": []},
        {"disclosureId": 1, "headline": "Nokia Oyj - Johdon liiketoimet (Heard)",
         "language": "fi", "cnsCategory": "Managers' Transactions",
         "messageUrl": "https://view.news.eu.nasdaq.com/view?id=n1&lang=fi",
         "releaseTime": "2026-10-15 14:00:00 +0000", "market": "Main Market, Helsinki",
         "company": "Nokia", "attachment": []},
    ]}}
    seen = _serve(monkeypatch, {
        "api.news.eu.nasdaq.com": _Resp(text=json.dumps(feed)),
        "view?id=n2": _Resp(text="<p>Heard bought 10,000 shares.</p>"),
    })
    out = nordic.get_insider_transactions("NOKIA.HE", "2026-11-07")
    assert out.startswith(
        "# Insider transactions for NOKIA.HE (Nasdaq Helsinki managers' transactions, "
        "2025-11-07 to 2026-11-07)"
    )
    assert (
        "2026-10-15 · Nokia Corporation - Managers' transactions (Heard) · "
        "https://view.news.eu.nasdaq.com/view?id=n2&lang=en"
    ) in out
    assert "Heard bought 10,000 shares." in out
    assert "Johdon liiketoimet" not in out    # the Finnish twin of an English release
    assert "cnscategory=Managers%27+Transactions" in seen[0] or "Managers%27%20Transactions" in seen[0]


def test_router_tries_nordic_first(monkeypatch):
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "nordic", lambda *a, **k: "from nordic")
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", lambda *a, **k: "from yahoo")
    with run_config({}):
        assert router.route_to_vendor("get_news", "BETS-B.ST", "2026-10-30", "2026-11-06") == "from nordic"


def test_router_falls_through_to_yahoo_for_us(monkeypatch):
    seen = _serve(monkeypatch, {})
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", lambda *a, **k: "from yahoo")
    monkeypatch.setitem(
        router.VENDOR_METHODS["get_insider_transactions"], "yfinance", lambda *a, **k: "yahoo insiders"
    )
    with run_config({}):
        assert router.route_to_vendor("get_news", "AAPL", "2026-10-30", "2026-11-06") == "from yahoo"
        assert router.route_to_vendor("get_insider_transactions", "AAPL") == "yahoo insiders"
    assert seen == []
