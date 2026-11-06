"""Nordic exchange releases and insider transactions: keyless, ahead of Yahoo for Nordic listings.

Yahoo's news for Nordic names is thin or about another company, and its insider
data for them is empty or zero-filled. The exchanges and Sweden's regulator
publish both:

- Nasdaq Nordic company news (Stockholm, Helsinki, Copenhagen), searched by
  company name; its "Managers' Transactions" category carries insider notices.
- Oslo Børs NewsWeb (Oslo), listed by issuer sign; insider notices are in the
  "MANAGERS' TRANSACTION" category.
- Finansinspektionen's insynsregistret (Stockholm insiders), a CSV export.

These are undocumented website backends, so the parsers are pinned to the fields
they serve and anything unexpected raises ``VendorUnavailableError``. A listing
outside these markets raises ``NoMarketDataError`` so the chain moves on.
"""

import csv
import html
import io
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import requests

from tradingagents.dataflows import date_window
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window, withhold_undisclosed_trades
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import google_news
from tradingagents.dataflows.vendors.yahoo.news import _format_articles

logger = logging.getLogger(__name__)

SUFFIXES = {".ST": "sto", ".HE": "hel", ".CO": "cph", ".OL": "ose"}
MARKET_NAMES = {"sto": "Stockholm", "hel": "Helsinki", "cph": "Copenhagen", "ose": "Oslo"}

NASDAQ_QUERY = "https://api.news.eu.nasdaq.com/news/query.action"
NASDAQ_VIEW = "https://view.news.eu.nasdaq.com/view"
NASDAQ_PAGE = 200
NASDAQ_MAX_PAGES = 5
NASDAQ_INSIDER_CATEGORY = "Managers' Transactions"
NEWSWEB_LIST = "https://api3.oslo.oslobors.no/v1/newsreader/list"
NEWSWEB_MESSAGE = "https://api3.oslo.oslobors.no/v1/newsreader/message"
NEWSWEB_SOURCE = "Oslo Børs NewsWeb"
NEWSWEB_INSIDER_CATEGORY = "MANAGERS' TRANSACTION"
NEWSWEB_WINDOW_DAYS = 31
FI_SEARCH = "https://marknadssok.fi.se/publiceringsklient/sv-SE/Search/Search"
FI_SOURCE = "Finansinspektionen insynsregistret"
FI_MAX_ROWS = 1000
FI_COLUMNS = (
    "Publiceringsdatum", "Emittent", "Person i ledande ställning", "Befattning", "Karaktär",
    "Instrumentnamn", "ISIN", "Transaktionsdatum", "Volym", "Volymsenhet", "Pris", "Valuta", "Status",
)
FI_NATURE = {"Förvärv": "buy", "Avyttring": "sell"}

BODY_LIMIT = 4000          # characters of a release body
NEWS_BODIES = 6            # newest releases shown with their body
INSIDER_BODIES = 5         # newest insider notices shown with their body
INSIDER_LOOKBACK_DAYS = 365
MIN_INTERVAL = 1.0         # seconds between requests: these are website backends
TIMEOUT = 20
HEADERS = google_news.HEADERS

# Words that name a legal form or a share class rather than the company, so
# "Volvo, AB ser. B" (Yahoo), "AB Volvo" and "Aktiebolaget Volvo" (FI) and
# "Volvo, AB" (Nasdaq) all reduce to "volvo", while "Volvo Car AB" does not.
_NOISE = {
    "ab", "aktiebolag", "aktiebolaget", "publ", "ser", "series", "asa", "as", "oyj", "oy", "abp",
    "a/s", "corporation", "corp", "plc", "ltd", "limited", "inc", "sa", "se", "nv", "the",
}

_last_request = 0.0


def market_of(canonical: str) -> str | None:
    """The Nordic market code of a Yahoo symbol (``sto``, ``hel``, ``cph``, ``ose``), else None."""
    upper = (canonical or "").upper()
    for suffix, market in SUFFIXES.items():
        if upper.endswith(suffix):
            return market
    return None


def _company_name(canonical: str) -> str:
    return google_news._company_name(canonical)


def _name_words(name: str) -> list[str]:
    """The words of a company name with legal forms and share-class letters dropped."""
    words = re.findall(r"[\w/&]+", name)
    return [w for w in words if w.lower() not in _NOISE and len(w) > 1]


def _name_key(name: str) -> tuple[str, ...]:
    return tuple(w.lower() for w in _name_words(name))


def _search_term(canonical: str) -> tuple[str, tuple[str, ...]]:
    """What to search the exchange for, and the key an issuer name must reduce to."""
    name = _company_name(canonical)
    words = _name_words(name)
    if not words:
        words = [canonical.rsplit(".", 1)[0]]
    return " ".join(words), tuple(w.lower() for w in words)


def _get(url: str):
    """One paced GET; a failed request or a non-200 answer raises ``VendorUnavailableError``."""
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Nordic vendor request failed: {exc}") from exc
    finally:
        _last_request = time.monotonic()
    if resp.status_code != 200:
        raise VendorUnavailableError(f"{url.split('?')[0]} returned HTTP {resp.status_code}")
    return resp


def _json(url: str):
    try:
        return _get(url).json()
    except ValueError as exc:
        raise VendorUnavailableError(f"{url.split('?')[0]} returned unreadable JSON: {exc}") from exc


def _text(raw: str) -> str:
    """Plain text from an HTML page or a message body, whitespace collapsed and capped."""
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:BODY_LIMIT]


def _body(fetch, label: str) -> str:
    """A release body, or "" when it cannot be fetched: the headline still stands."""
    try:
        return fetch()
    except Exception as exc:  # noqa: BLE001 - one missing body must not drop the list
        logger.warning("Nordic vendor: no body for %s: %s", label, exc)
        return ""


def _window(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    return datetime.strptime(start_date, "%Y-%m-%d"), datetime.strptime(end_date, "%Y-%m-%d")


# --- Nasdaq Nordic -----------------------------------------------------------------


def _nasdaq_releases(canonical: str, start_date: str, end_date: str, category: str = "") -> list[dict]:
    """The issuer's releases in the window, newest first, one per release (English preferred)."""
    term, key = _search_term(canonical)
    start_dt, end_dt = _window(start_date, end_date)
    found = []
    for page in range(NASDAQ_MAX_PAGES):
        params = {
            "type": "json", "showAttachments": "true", "showCompany": "true",
            "countResults": "true", "freeText": term, "cnscategory": category,
            "globalGroup": "exchangeNotice", "globalName": "NordicAllMarkets",
            "displayLanguage": "en", "limit": NASDAQ_PAGE, "start": page * NASDAQ_PAGE, "dir": "DESC",
        }
        data = _json(f"{NASDAQ_QUERY}?{urlencode(params)}")
        try:
            items = data["results"]["item"]
            count = int(data.get("count") or 0)
            batch = [
                {
                    "id": item["disclosureId"],
                    "headline": item["headline"],
                    "language": item.get("language") or "",
                    "category": item.get("cnsCategory") or "",
                    "url": item.get("messageUrl") or "",
                    "time": datetime.strptime(item["releaseTime"][:19], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=UTC
                    ),
                    "company": item.get("company") or "",
                }
                for item in items
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise VendorUnavailableError(f"Nasdaq news returned an unexpected shape: {exc}") from exc
        found += batch
        # Date filters are ignored server side: page back until the window's start.
        reached_start = min((r["time"] for r in batch), default=None)
        if reached_start is None or reached_start < start_dt.replace(tzinfo=UTC):
            break
        if (page + 1) * NASDAQ_PAGE >= count:
            break
    # freeText matches any release that names the company: keep the issuer's own.
    own = [r for r in found if _name_key(r["company"]) == key and in_window(r["time"], start_dt, end_dt)]
    english = {r["time"] for r in own if r["language"] == "en"}
    own = [r for r in own if r["language"] == "en" or r["time"] not in english]
    own.sort(key=lambda r: r["time"], reverse=True)
    return own


def _nasdaq_body(release: dict) -> str:
    url = release["url"] if release["url"].startswith(NASDAQ_VIEW) else f"{NASDAQ_VIEW}?id={release['id']}"
    return _text(_get(url).text)


# --- Oslo Børs NewsWeb -------------------------------------------------------------


def _newsweb_messages(canonical: str, start_date: str, end_date: str) -> list[dict]:
    """The issuer's NewsWeb messages in the window, newest first."""
    sign = canonical.rsplit(".", 1)[0].upper()
    start_dt, end_dt = _window(start_date, end_date)
    found = []
    chunk_start = start_dt
    while chunk_start <= end_dt:
        chunk_end = min(chunk_start + timedelta(days=NEWSWEB_WINDOW_DAYS - 1), end_dt)
        found += _newsweb_list(sign, chunk_start, chunk_end)
        chunk_start = chunk_end + timedelta(days=1)
    seen = set()
    own = []
    for m in sorted(found, key=lambda m: m["time"], reverse=True):
        if m["sign"] == sign and m["id"] not in seen and in_window(m["time"], start_dt, end_dt):
            seen.add(m["id"])
            own.append(m)
    return own


def _newsweb_list(sign: str, frm: datetime, to: datetime) -> list[dict]:
    params = {
        "issuer": sign, "fromDate": f"{frm:%Y-%m-%d}", "toDate": f"{to:%Y-%m-%d}",
        "category": "", "market": "", "messageTitle": "",
    }
    data = _json(f"{NEWSWEB_LIST}?{urlencode(params)}")
    try:
        messages = data["data"]["messages"]
        overflow = bool(data["data"].get("overflow"))
        parsed = [
            {
                "id": m["messageId"],
                "title": m["title"],
                "category": ", ".join(c["category_en"] for c in m.get("category") or []),
                "sign": (m.get("issuerSign") or "").upper(),
                "time": datetime.fromisoformat(m["publishedTime"].replace("Z", "+00:00")),
            }
            for m in messages
        ]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VendorUnavailableError(f"NewsWeb returned an unexpected shape: {exc}") from exc
    if overflow:
        # The list is capped; a narrower window lists the rest.
        if to <= frm:
            raise VendorUnavailableError(f"NewsWeb list overflowed for {sign} on {frm:%Y-%m-%d}")
        mid = frm + (to - frm) / 2
        mid = datetime(mid.year, mid.month, mid.day)
        return _newsweb_list(sign, frm, mid) + _newsweb_list(sign, mid + timedelta(days=1), to)
    return parsed


def _newsweb_body(message: dict) -> str:
    data = _json(f"{NEWSWEB_MESSAGE}?messageId={message['id']}")
    try:
        return _text(data["data"]["message"]["body"])
    except (KeyError, TypeError) as exc:
        raise VendorUnavailableError(f"NewsWeb message has an unexpected shape: {exc}") from exc


def _newsweb_link(message: dict) -> str:
    return f"https://newsweb.oslobors.no/message/{message['id']}"


# --- News --------------------------------------------------------------------------


def _press_sample(ticker: str, start_date: str, end_date: str) -> str:
    try:
        return google_news.get_news(ticker, start_date, end_date)
    except Exception as exc:  # noqa: BLE001 - the press sample is a supplement
        logger.warning("Google News sample unavailable for %s: %s", ticker, exc)
        return f"Press sample unavailable for {ticker} ({exc})."


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Exchange releases for a Nordic listing in the window, then a Google News press sample.

    Not a Nordic listing: ``NoMarketDataError``, so the next vendor answers. A
    failed request to the exchange: ``VendorUnavailableError``.
    """
    canonical = normalize_symbol(ticker)
    market = market_of(canonical)
    if market is None:
        raise NoMarketDataError(ticker, canonical, "not a Nordic exchange listing")
    limit = get_config()["news_article_limit"]

    if market == "ose":
        source = NEWSWEB_SOURCE
        releases = _newsweb_messages(canonical, start_date, end_date)[:limit]
        rows = [(m["title"], m["time"], m["category"], _newsweb_link(m), m) for m in releases]
        fetch = _newsweb_body
    else:
        source = f"Nasdaq {MARKET_NAMES[market]}"
        releases = _nasdaq_releases(canonical, start_date, end_date)[:limit]
        rows = [(r["headline"], r["time"], r["category"], r["url"], r) for r in releases]
        fetch = _nasdaq_body

    heading = f"## {ticker} Exchange releases, from {start_date} to {end_date} ({source}):\n\n"
    if not rows:
        section = f"No exchange releases for {ticker} between {start_date} and {end_date} ({source}).\n\n"
    else:
        articles = []
        for i, (title, when, category, link, raw) in enumerate(rows):
            summary = f"Published {when:%Y-%m-%d}" + (f" · {category}" if category else "")
            if i < NEWS_BODIES:
                body = _body(lambda raw=raw: fetch(raw), title)
                if body:
                    summary += f"\n{body}"
            articles.append({"title": title, "publisher": source, "summary": summary, "link": link})
        section = _format_articles(articles)
    press = _press_sample(ticker, start_date, end_date)
    return (
        f"{heading}{section}"
        f"## {ticker} Press coverage (Google News sample, from {start_date} to {end_date}):\n\n"
        f"{press}\n"
    )


# --- Insider transactions ----------------------------------------------------------


def _number(raw: str) -> float:
    return float((raw or "0").replace(" ", "").replace(" ", "").replace(",", "."))


def _plain(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.12g}"


def _fi_rows(term: str, frm: datetime, to: datetime) -> list[dict]:
    params = {
        "SearchFunctionType": "Insyn", "Utgivare": term,
        "Publiceringsdatum.From": f"{frm:%Y-%m-%d}", "Publiceringsdatum.To": f"{to:%Y-%m-%d}",
        "button": "export",
    }
    content = _get(f"{FI_SEARCH}?{urlencode(params)}").content
    encoding = "utf-16" if content[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-le"
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError as exc:
        raise VendorUnavailableError(f"FI export is not UTF-16: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    missing = [c for c in FI_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise VendorUnavailableError(f"FI export lacks columns {missing}")
    rows = list(reader)
    if len(rows) >= FI_MAX_ROWS:
        # The export stops at 1,000 rows: split the window and ask for each half.
        if to <= frm:
            raise VendorUnavailableError(f"FI export is full for a single day ({frm:%Y-%m-%d})")
        mid = frm + (to - frm) / 2
        mid = datetime(mid.year, mid.month, mid.day)
        return _fi_rows(term, frm, mid) + _fi_rows(term, mid + timedelta(days=1), to)
    return rows


def _fi_insiders(ticker: str, canonical: str, frm: datetime, to: datetime) -> str:
    term, key = _search_term(canonical)
    span = f"{frm:%Y-%m-%d} to {to:%Y-%m-%d}"
    rows = [r for r in _fi_rows(term, frm, to) if _name_key(r["Emittent"] or "") == key]
    if not rows:
        return (
            f"No insider transactions published for {ticker} between {frm:%Y-%m-%d} and "
            f"{to:%Y-%m-%d} ({FI_SOURCE})"
        )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([
        "published", "person", "position", "nature", "instrument", "transaction_date",
        "volume", "unit", "price", "currency", "status",
    ])
    totals = {"buy": [0, 0.0], "sell": [0, 0.0]}
    try:
        for r in rows:
            nature = FI_NATURE.get(r["Karaktär"], r["Karaktär"])
            volume = _number(r["Volym"])
            if nature in totals:
                totals[nature][0] += 1
                totals[nature][1] += volume
            writer.writerow([
                r["Publiceringsdatum"][:10], r["Person i ledande ställning"], r["Befattning"], nature,
                r["Instrumentnamn"], r["Transaktionsdatum"][:10], _plain(volume), r["Volymsenhet"],
                _plain(_number(r["Pris"])) if r["Pris"] else "", r["Valuta"], r["Status"],
            ])
    except (TypeError, ValueError) as exc:
        raise VendorUnavailableError(f"FI export has an unreadable row: {exc}") from exc
    (n_buy, v_buy), (n_sell, v_sell) = totals["buy"], totals["sell"]
    return (
        f"# Insider transactions for {ticker} ({FI_SOURCE}, {span})\n"
        f"{out.getvalue()}"
        f"Totals: {n_buy} buys {_plain(v_buy)}, {n_sell} sells {_plain(v_sell)}"
    )


def _notice_list(ticker: str, source: str, span: tuple[str, str], notices: list, fetch) -> str:
    """Insider notices as ``date · headline · link`` lines, the newest with their bodies."""
    if not notices:
        return f"No managers' transactions published for {ticker} between {span[0]} and {span[1]} ({source})"
    lines = [f"# Insider transactions for {ticker} ({source} managers' transactions, {span[0]} to {span[1]})", ""]
    for i, (title, when, link, raw) in enumerate(notices):
        lines.append(f"{when:%Y-%m-%d} · {title} · {link}")
        if i < INSIDER_BODIES:
            body = _body(lambda raw=raw: fetch(raw), title)
            if body:
                lines.append(f"    {body}")
    return "\n".join(lines)


def get_insider_transactions(ticker: str, as_of_date: str | None = None) -> str:
    """Insider transactions over the year before ``as_of_date`` from the exchange or regulator.

    Stockholm: Finansinspektionen's register as CSV with totals. Helsinki and
    Copenhagen: Nasdaq "Managers' Transactions" releases. Oslo: NewsWeb
    "MANAGERS' TRANSACTION" messages. Not a Nordic listing: ``NoMarketDataError``.
    """
    canonical = normalize_symbol(ticker)
    market = market_of(canonical)
    if market is None:
        raise NoMarketDataError(ticker, canonical, "not a Nordic exchange listing")
    withheld = withhold_undisclosed_trades(as_of_date, canonical)
    if withheld:
        return withheld
    to = datetime.strptime(as_of_date or date_window.get_current_date(), "%Y-%m-%d")
    frm = to - timedelta(days=INSIDER_LOOKBACK_DAYS)
    span = (f"{frm:%Y-%m-%d}", f"{to:%Y-%m-%d}")

    if market == "sto":
        return _fi_insiders(ticker, canonical, frm, to)
    if market == "ose":
        messages = [
            m for m in _newsweb_messages(canonical, *span)
            if NEWSWEB_INSIDER_CATEGORY in m["category"].upper().replace("’", "'")
        ]
        notices = [(m["title"], m["time"], _newsweb_link(m), m) for m in messages]
        return _notice_list(ticker, NEWSWEB_SOURCE, span, notices, _newsweb_body)
    releases = _nasdaq_releases(canonical, *span, category=NASDAQ_INSIDER_CATEGORY)
    notices = [(r["headline"], r["time"], r["url"], r) for r in releases]
    return _notice_list(ticker, f"Nasdaq {MARKET_NAMES[market]}", span, notices, _nasdaq_body)
