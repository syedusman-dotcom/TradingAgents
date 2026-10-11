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
from lxml import html as lxml_html

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors import sec_edgar

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# Each section is cut to this many characters (~3,000 tokens) so the report
# informs the analyst without crowding out the numbers.
SECTION_CHARS = 12_000

# (title, start heading, end heading) in a 10-K's text, one block per line.
# A heading stands on its own line; the same words inside a sentence ("refer to
# Item 1A. Risk Factors") do not. The table of contents lists every heading too,
# so the longest start-to-end span is the section itself.
_ITEM = r"^\s*item\s*"
_SEP = r"\s*[.:\-–—]?\s*"
SECTIONS = (
    ("Risk factors (Item 1A)",
     _ITEM + r"1a" + _SEP + r"risk\s+factors\.?\s*$",
     _ITEM + r"(1b|1c|2)\b"),
    ("Management's discussion and analysis (Item 7)",
     _ITEM + r"7" + _SEP + r"management[’'`s\s]*discussion\b[^\n]*$",
     _ITEM + r"(7a|8)\b"),
)


def get_financial_history(ticker: str, as_of_date: str | None = None) -> str:
    """Every annual income, balance-sheet and cash-flow figure EDGAR has, as filed."""
    parts = [
        sec_edgar.get_income_statement(ticker, "annual", as_of_date),
        sec_edgar.get_balance_sheet(ticker, "annual", as_of_date),
        sec_edgar.get_cashflow(ticker, "annual", as_of_date),
    ]
    return ("# Long-term financial history (SEC EDGAR, annual, as filed)\n"
            "Top-line rows only, but up to about 20 years: use it for the full-cycle record of "
            "revenue, margins and cash flow.\n\n" + "\n".join(parts))


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


def _plain_text(document: bytes) -> str:
    """The document's text with one line per block element, so headings keep their own line."""
    tree = lxml_html.fromstring(document)
    for node in tree.xpath("//script|//style"):
        node.drop_tree()
    for node in tree.iter(*_BLOCKS):
        node.tail = "\n" + (node.tail or "")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in tree.text_content().splitlines())
    return "\n".join(line for line in lines if line)


def _latest_annual_filing(cik: str, as_of_date: str) -> dict | None:
    data = sec_edgar._cached_json(_SUBMISSIONS_URL.format(cik=cik), f"submissions_CIK{cik}.json")
    recent = (data.get("filings") or {}).get("recent") or {}
    rows = zip(recent.get("form", []), recent.get("filingDate", []),
               recent.get("accessionNumber", []), recent.get("primaryDocument", []), strict=False)
    filings = [{"form": f, "filed": d, "accession": a, "document": doc}
               for f, d, a, doc in rows if f == "10-K" and d <= as_of_date]
    return max(filings, key=lambda f: f["filed"]) if filings else None


def _sections(cik: str, filing: dict) -> dict[str, str]:
    """A filing's sections, cached forever: a filed document never changes."""
    path = Path(get_config()["data_cache_dir"]) / "sec_edgar" / f"10k_{cik}_{filing['accession']}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    url = _DOCUMENT_URL.format(cik=int(cik), accession=filing["accession"].replace("-", ""),
                               document=filing["document"])
    try:
        response = requests.get(url, headers={"User-Agent": sec_edgar._user_agent()}, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"SEC EDGAR filing request failed ({type(exc).__name__})") from exc
    found = section_spans(_plain_text(response.content))
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(found), encoding="utf-8"))
    return found


def get_annual_report(ticker: str, as_of_date: str | None = None) -> str:
    """Risk factors and management's discussion from the latest 10-K filed by ``as_of_date``."""
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    cik = sec_edgar.cik_for(ticker)
    if cik is None:
        raise NoMarketDataError(ticker, ticker, "not a US SEC filer")
    filing = _latest_annual_filing(cik, as_of_date)
    if filing is None:
        raise NoMarketDataError(ticker, ticker, f"no 10-K filed by {as_of_date}")
    found = _sections(cik, filing)
    lines = [f"# Annual report (10-K) for {ticker.upper()}, filed {filing['filed']}",
             "Management's own words, as filed. Cite it for risks and for why results moved; "
             f"each section is cut to its first {SECTION_CHARS:,} characters.", ""]
    if not found:
        lines.append("The filing's risk factors and discussion sections could not be located in its text.")
    for title, body in found.items():
        cut = body[:SECTION_CHARS]
        more = "" if len(body) <= SECTION_CHARS else f" [... {len(body) - SECTION_CHARS:,} more characters not shown]"
        lines += [f"## {title}", "", cut + more, ""]
    return "\n".join(lines)
