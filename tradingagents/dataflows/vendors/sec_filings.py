"""Long financial history and annual-report text from SEC EDGAR, as filed by a date.

Two gaps the value analyst filled from memory: a record longer than Yahoo's four
or five years, and what management says in the 10-K (risk factors, its own
discussion of results). EDGAR dates every filing, so both are served exactly as
they stood on the analysis date.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import requests
from lxml import etree as lxml_etree, html as lxml_html

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors import sec_edgar

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

NOT_A_US_FILER = (
    "Not a US SEC filer: use get_fundamentals, get_income_statement, get_balance_sheet and "
    "get_cashflow (Yahoo) and the annual report on the company's investor site."
)
# For an SEC filer with no 10-Q or 8-K at all: a foreign private issuer (TSM, ASML) files
# 20-F and 6-K instead, so there is nothing to retry.
NO_QUARTERLY_FILINGS = (
    "This company files 20-F/6-K with the SEC, not 10-Q or 8-K earnings releases: no quarterly "
    "report or earnings release exists; use the statements and the annual report on the "
    "company's site."
)

# Each section is cut to this many characters (~3,000 tokens) so the report
# informs the analyst without crowding out the numbers.
SECTION_CHARS = 12_000
# The Business section (Item 1) is cut shorter: its product and innovation paragraphs follow it whole.
BUSINESS_CHARS = 6_000

# (title, start heading, end heading) in a 10-K's text, one block per line.
# A heading stands on its own line; the same words inside a sentence ("refer to
# Item 1A. Risk Factors") do not. The table of contents lists every heading too,
# so the longest start-to-end span is the section itself.
_ITEM = r"^\s*item\s*"
_SEP = r"\s*[.:\-–—]?\s*"
SECTIONS = (
    ("Business (Item 1)",
     _ITEM + r"1" + _SEP + r"business\.?\s*$",
     _ITEM + r"1a\b"),
    ("Risk factors (Item 1A)",
     _ITEM + r"1a" + _SEP + r"risk\s+factors\.?\s*$",
     _ITEM + r"(1b|1c|2)\b"),
    ("Management's discussion and analysis (Item 7)",
     _ITEM + r"7" + _SEP + r"management[’'`s\s]*discussion\b[^\n]*$",
     _ITEM + r"(7a|8)\b"),
)


def get_financial_history(ticker: str, as_of_date: str | None = None) -> str:
    """Every annual income, balance-sheet and cash-flow figure EDGAR has, as filed."""
    if sec_edgar.cik_for(ticker) is None:
        return NOT_A_US_FILER
    parts = [
        sec_edgar.get_income_statement(ticker, "annual", as_of_date),
        sec_edgar.get_balance_sheet(ticker, "annual", as_of_date),
        sec_edgar.get_cashflow(ticker, "annual", as_of_date),
    ]
    return ("# Long-term financial history (SEC EDGAR, annual, as filed)\n"
            "Top-line rows only, but up to about 20 years: use it for the full-cycle record of "
            "revenue, margins and cash flow.\n\n" + "\n".join(parts))


def annual_revenue(ticker: str, as_of_date: str | None = None) -> dict[str, float]:
    """Fiscal year (the year its period ends) -> revenue in dollars, from 10-K/20-F/40-F facts filed
    on or before the date, tagged as in the income statement; {} for a non-filer or no dollar figures."""
    cik = sec_edgar.cik_for(ticker)
    if cik is None:
        return {}
    facts = sec_edgar._cached_json(sec_edgar._FACTS_URL.format(cik=cik), f"CIK{cik}.json")
    us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    tags = dict(sec_edgar._STATEMENTS["income_statement"])["Revenue"]
    values, unit = sec_edgar._as_of(us_gaap, tags, as_of_date or datetime.now().strftime("%Y-%m-%d"),
                                    (sec_edgar._SPANS["annual"],), sec_edgar._ANNUAL_FORMS)
    if unit != "USD":
        return {}
    out: dict[str, float] = {}
    for (end, _), value in sorted(values.items(), reverse=True):
        out.setdefault(end[:4], value)    # a changed fiscal year: the later period of that year
    return out


def section_spans(text: str) -> dict[str, str]:
    """The 10-K sections in ``SECTIONS``, each the longest span between its headings."""
    found = {}
    for title, start, end in SECTIONS:
        best = ""
        for m in re.finditer(start, text, re.I | re.M):
            stop = re.search(end, text[m.end():], re.I | re.M)
            body = text[m.end(): m.end() + stop.start()] if stop else ""
            if len(body) > len(best):
                best = body
        if len(best.strip()) > 500:   # a table-of-contents entry is a line or two
            found[title] = re.sub(r"\n{2,}", "\n", best.strip())
    return found


_BLOCKS = ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table")


def _parse(document: bytes):
    """An HTML tree; an empty or unparseable document is EDGAR failing to serve it."""
    try:
        return lxml_html.fromstring(document)
    except (lxml_etree.ParserError, ValueError) as exc:
        raise VendorUnavailableError(f"SEC EDGAR returned an unreadable document ({exc})") from exc


def _plain_text(document: bytes) -> str:
    """The document's text with one line per block element, so headings keep their own line."""
    tree = _parse(document)
    for node in tree.xpath("//script|//style"):
        node.drop_tree()
    for node in tree.iter(*_BLOCKS):
        node.tail = "\n" + (node.tail or "")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in tree.text_content().splitlines())
    return "\n".join(line for line in lines if line)


def _filings(cik: str, as_of_date: str, form: str) -> list[dict]:
    """The company's filings of ``form`` filed on or before ``as_of_date``, newest first."""
    data = sec_edgar._cached_json(_SUBMISSIONS_URL.format(cik=cik), f"submissions_CIK{cik}.json")
    recent = (data.get("filings") or {}).get("recent") or {}
    count = len(recent.get("form", []))
    def column(name):
        return list(recent.get(name) or []) + [""] * count
    rows = zip(recent.get("form", []), column("filingDate"), column("accessionNumber"),
               column("primaryDocument"), column("reportDate"), column("items"), strict=False)
    filings = [{"form": f, "filed": d, "accession": a, "document": doc, "period": period,
                "items": [i.strip() for i in (items or "").split(",") if i.strip()]}
               for f, d, a, doc, period, items in rows if f == form and d and d <= as_of_date]
    return sorted(filings, key=lambda f: f["filed"], reverse=True)


def _latest_annual_filing(cik: str, as_of_date: str) -> dict | None:
    filings = _filings(cik, as_of_date, "10-K")
    return filings[0] if filings else None


def _fetch(url: str) -> bytes:
    """One EDGAR archive document; a failed request is the vendor being unavailable."""
    try:
        response = requests.get(url, headers={"User-Agent": sec_edgar._user_agent()}, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"SEC EDGAR filing request failed ({type(exc).__name__})") from exc
    return response.content


def _cache_dir() -> Path:
    return Path(get_config()["data_cache_dir"]) / "sec_edgar"


def _filing_url(cik: str, filing: dict, document: str) -> str:
    return _DOCUMENT_URL.format(cik=int(cik), accession=filing["accession"].replace("-", ""),
                                document=document)


def _sections(cik: str, filing: dict) -> dict[str, str]:
    """A filing's sections, cached forever: a filed document never changes."""
    # _v2: Item 1 (Business) added; filings cached before it are read again.
    path = _cache_dir() / f"10k_{cik}_{filing['accession']}_v2.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    found = section_spans(_plain_text(_fetch(_filing_url(cik, filing, filing["document"]))))
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(found), encoding="utf-8"))
    return found


def get_annual_report(ticker: str, as_of_date: str | None = None) -> str:
    """Business, risk factors and management's discussion from the latest 10-K filed by ``as_of_date``."""
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    cik = sec_edgar.cik_for(ticker)
    if cik is None:
        return NOT_A_US_FILER
    filing = _latest_annual_filing(cik, as_of_date)
    if filing is None:
        raise NoMarketDataError(ticker, ticker, f"no 10-K filed by {as_of_date}")
    found = _sections(cik, filing)
    lines = [f"# Annual report (10-K) for {ticker.upper()}, filed {filing['filed']}",
             "Management's own words, as filed. Cite it for what the company is building, for risks "
             f"and for why results moved; the Business section is cut to its first {BUSINESS_CHARS:,} "
             f"characters and followed by its product and innovation paragraphs, kept whole; each "
             f"other section is cut to its first {SECTION_CHARS:,} characters.", ""]
    if not found:
        lines.append("The filing's business, risk factors and discussion sections could not be located in its text.")
    for title, *_ in SECTIONS:
        if title not in found:
            continue
        body = found[title]
        if title == SECTIONS[0][0]:
            innovation = _innovation_lines(body)
            lines += [f"## {title}", "", _cut(body, BUSINESS_CHARS), "",
                      "## Products and innovation lines", "",
                      _cut("\n".join(innovation), OUTLOOK_CHARS) if innovation else "None found.", ""]
        else:
            lines += [f"## {title}", "", _cut(body, SECTION_CHARS), ""]
    return "\n".join(lines)


# Paragraphs of the Business section that say what the company is building, kept whole.
INNOVATION_WORDS = ("new product", "launch", "innovation", "innovative", "research and development",
                    "pipeline", "patent", "roadmap", "introduced", "next generation", "next-generation")
_INNOVATION = re.compile(r"\b(" + "|".join(re.escape(w) for w in INNOVATION_WORDS) + ")", re.I)


def _innovation_lines(business: str) -> list[str]:
    """The Business section's paragraphs naming a product, launch, pipeline or R&D, in order."""
    found = []
    for paragraph in business.split("\n"):
        if _INNOVATION.search(paragraph) and paragraph not in found:
            found.append(paragraph)
    return found


def _cut(body: str, limit: int) -> str:
    more = "" if len(body) <= limit else f" [... {len(body) - limit:,} more characters not shown]"
    return body[:limit] + more


# Part of every extracted-text cache key: raise it when extraction changes, so filings are re-read.
_PARSER_VERSION = 3


def _cached_text(name: str, build) -> str:
    """Text extracted from a filing, cached forever (a filed document never changes), an empty
    result too; a new ``_PARSER_VERSION`` reads every filing again."""
    path = _cache_dir() / f"{name}_v{_PARSER_VERSION}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    text = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    return text


# A 10-Q's Item 2 (MD&A) heading on its own line. The table of contents lists it too, followed
# closely by the next item's heading or a page number; Part II has an unrelated Item 2 later.
_MDNA_START = re.compile(_ITEM + r"2\b[^\n]*$", re.I | re.M)
# The MD&A heading names it (on its line or the next two); Part II's Item 2 is never it.
_MDNA_TITLE = re.compile(r"management|discussion", re.I)
_NOT_MDNA = re.compile(r"unregistered|use\s+of\s+proceeds", re.I)
_MDNA_END = re.compile(_ITEM + r"[34]\b", re.I | re.M)
_TOC_FOLLOWS = re.compile(_ITEM + r"\d|^\s*(page\s*)?\d{1,3}\s*$", re.I | re.M)
_PAGE_NUMBER_END = re.compile(r"\s\d{1,3}\s*$")
# Sub-headings within the MD&A, each a short line of its own.
_RESULTS = re.compile(r"^[ \t]*(results\s+of\s+operations|operations\s+review)\b[^\n]{0,60}$",
                      re.I | re.M)
_LIQUIDITY = re.compile(r"^[ \t]*liquidity[\s,]+(and\s+)?capital\s+resources\b[^\n]{0,60}$",
                        re.I | re.M)
_AFTER_LIQUIDITY = re.compile(
    r"^[ \t]*(critical\s+accounting|recent(ly)?\s+(issued\s+|adopted\s+)?accounting|"
    r"off[\s-]balance\s+sheet|contractual\s+obligations|non-gaap|"
    r"(cautionary\s+)?(note|statement)s?\s+(regarding|about|on)\s+forward|forward[\s-]looking)"
    r"[^\n]{0,80}$", re.I | re.M)


def mdna_span(text: str) -> str:
    """Item 2 of a 10-Q, trimmed to Results of Operations through Liquidity and Capital Resources
    when both sub-headings are there, else the whole item; empty when no heading is found."""
    for m in _MDNA_START.finditer(text):
        heading = "\n".join([m.group(0)] + text[m.end(): m.end() + 1000].split("\n")[1:3])
        if _NOT_MDNA.search(m.group(0)) or not _MDNA_TITLE.search(heading):
            continue
        if _PAGE_NUMBER_END.search(m.group(0)) or _TOC_FOLLOWS.search(text, m.end(), m.end() + 200):
            continue
        stop = _MDNA_END.search(text, m.end())
        item = text[m.end(): stop.start() if stop else len(text)]
        results, liquidity = _RESULTS.search(item), _LIQUIDITY.search(item)
        if results and liquidity and results.start() < liquidity.start():
            after = _AFTER_LIQUIDITY.search(item, liquidity.end())
            item = item[results.start(): after.start() if after else len(item)]
        return re.sub(r"\n{2,}", "\n", item.strip())
    return ""


def latest_10q_mdna(ticker: str, as_of_date: str | None = None) -> str:
    """Management's discussion (Item 2) from the latest 10-Q filed by ``as_of_date``."""
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    cik = sec_edgar.cik_for(ticker)
    if cik is None:
        return NOT_A_US_FILER
    filings = _filings(cik, as_of_date, "10-Q")
    if not filings and not _filings(cik, "9999-12-31", "10-Q"):
        return NO_QUARTERLY_FILINGS
    if not filings:
        raise NoMarketDataError(ticker, ticker, f"no 10-Q filed by {as_of_date}")
    filing = filings[0]
    body = _cached_text(f"10q_{cik}_{filing['accession']}", lambda: mdna_span(
        _plain_text(_fetch(_filing_url(cik, filing, filing["document"])))))
    lines = [f"# Quarterly report (10-Q) for {ticker.upper()}, filed {filing['filed']}, "
             f"period ending {filing['period'] or 'unknown'}",
             "Management's discussion of the latest quarter (Item 2), as filed: results of "
             "operations and liquidity where marked, cut to its first "
             f"{SECTION_CHARS:,} characters. Quote it with the filing date.", ""]
    lines.append(_cut(body, SECTION_CHARS) if body else
                 "The filing's management's discussion (Item 2) could not be located in its text.")
    return "\n".join(lines)


# An earnings release is cut to this many characters (~2,500 tokens); paragraphs that speak to
# outlook or demand are kept whole after the cut, wherever they fall.
RELEASE_CHARS = 10_000
OUTLOOK_WORDS = ("outlook", "guidance", "expect", "backlog", "bookings", "book-to-bill",
                 "remaining performance obligations", "RPO", "demand")
_OUTLOOK = re.compile(r"\b(" + "|".join(re.escape(w) for w in OUTLOOK_WORDS) + ")", re.I)
# Legal boilerplate that names the outlook words without giving any guidance.
_SAFE_HARBOR = re.compile(r"forward[\s-]looking|safe\s+harbor|"
                          r"private\s+securities\s+litigation\s+reform\s+act", re.I)
# The outlook and demand paragraphs past the cut are capped at this many characters.
OUTLOOK_CHARS = 4_000


def _outlook_beyond_cut(text: str) -> list[str]:
    """Outlook and demand paragraphs that end past ``RELEASE_CHARS``, without safe-harbor text."""
    found, end = [], -1
    for paragraph in text.split("\n"):
        end += len(paragraph) + 1
        if (end > RELEASE_CHARS and _OUTLOOK.search(paragraph)
                and not _SAFE_HARBOR.search(paragraph) and paragraph not in found):
            found.append(paragraph)
    return found


_EXHIBIT_99 = re.compile(r"ex.?99.*\.(htm|html|txt)$", re.I)
# Exhibit 99.1 itself (the press release), ahead of 99.2 and later (slides, CFO commentary).
_EXHIBIT_99_1 = re.compile(r"(99[._-]?1|991|99d1)(?!\d)", re.I)


def _exhibit_by_type(cik: str, filing: dict, names: list[str]) -> str | None:
    """The EX-99 document named in the filing's index page, for a filer whose exhibit file name
    does not say 99 ("q2fy27pr.htm", "...earningsreleaseex-9.htm")."""
    index = next((n for n in names if re.search(r"-index\.html?$", n, re.I)), None)
    if index is None:
        return None
    tree = _parse(_fetch(_filing_url(cik, filing, index)))
    for row in tree.iter("tr"):
        cells = [cell.text_content().strip().upper() for cell in row.iter("td")]
        links = row.xpath(".//a/@href")
        if links and any(cell.startswith("EX-99") for cell in cells):
            return links[0].rsplit("/", 1)[-1]
    return None


def _release_text(cik: str, filing: dict) -> str:
    index = sec_edgar._fetch_json(_filing_url(cik, filing, "index.json"))
    names = [item.get("name", "") for item in (index.get("directory") or {}).get("item") or []]
    exhibits = [n for n in names if _EXHIBIT_99.search(n)]
    exhibit = (next((n for n in exhibits if _EXHIBIT_99_1.search(n)), None)
               or next(iter(exhibits), None)
               or _exhibit_by_type(cik, filing, names))
    if exhibit is None:
        return ""
    document = _fetch(_filing_url(cik, filing, exhibit))
    if exhibit.lower().endswith(".txt"):
        text = document.decode("utf-8", errors="replace")
        lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)
    return _plain_text(document)


def latest_earnings_release(ticker: str, as_of_date: str | None = None) -> str:
    """Exhibit 99.1 of the latest 8-K reporting results (Item 2.02) filed by ``as_of_date``."""
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    cik = sec_edgar.cik_for(ticker)
    if cik is None:
        return NOT_A_US_FILER
    filings = [f for f in _filings(cik, as_of_date, "8-K") if "2.02" in f["items"]]
    if not filings and not _filings(cik, "9999-12-31", "8-K"):
        return NO_QUARTERLY_FILINGS
    if not filings:
        raise NoMarketDataError(ticker, ticker, f"no earnings 8-K (Item 2.02) filed by {as_of_date}")
    filing = filings[0]
    text = _cached_text(f"8k_{cik}_{filing['accession']}", lambda: _release_text(cik, filing))
    lines = [f"# Earnings release (8-K exhibit 99.1) for {ticker.upper()}, filed {filing['filed']}",
             "The company's own results announcement, as filed; cut to its first "
             f"{RELEASE_CHARS:,} characters; outlook and demand paragraphs past the cut follow, "
             "kept whole. Quote it with the filing date.", ""]
    if not text:
        lines.append("The filing's exhibit 99 press release could not be located.")
        return "\n".join(lines)
    lines += [_cut(text, RELEASE_CHARS), "", "## Outlook and demand lines", ""]
    outlook = _outlook_beyond_cut(text)
    lines.append(_cut("\n".join(outlook), OUTLOOK_CHARS) if outlook else "None found.")
    return "\n".join(lines)
