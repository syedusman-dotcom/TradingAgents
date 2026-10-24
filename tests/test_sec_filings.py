"""Quarterly report (10-Q MD&A) and earnings release (8-K exhibit 99.1) from EDGAR, offline."""

import json

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import sec_filings

CIK = "0000021344"

SUBMISSIONS = {"filings": {"recent": {
    "form": ["8-K", "10-Q", "8-K", "10-Q", "10-K", "8-K"],
    "filingDate": ["2026-07-25", "2026-07-30", "2026-07-02", "2026-04-28", "2026-02-20", "2026-04-22"],
    "accessionNumber": ["0000021344-26-000040", "0000021344-26-000041", "0000021344-26-000030",
                        "0000021344-26-000020", "0000021344-26-000010", "0000021344-26-000019"],
    "primaryDocument": ["e2.htm", "q2.htm", "e-other.htm", "q1.htm", "k.htm", "e1.htm"],
    "reportDate": ["2026-07-24", "2026-06-30", "2026-07-01", "2026-03-31", "2025-12-31", "2026-04-22"],
    "items": ["2.02,9.01", "", "5.02", "", "", "2.02,9.01"],
}}}

MDNA_10Q = """
PART I. FINANCIAL INFORMATION
Item 1. Financial Statements 3
Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations 25
Item 3. Quantitative and Qualitative Disclosures About Market Risk 40
Item 4. Controls and Procedures 41
Item 1. Financial Statements
Balance sheet numbers.
Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations
Overview
We make drinks. """ + "Overview filler. " * 30 + """
Results of Operations
""" + "Net revenue rose 6% on pricing. " * 30 + """
Liquidity and Capital Resources
""" + "Cash from operations was strong. " * 30 + """
Critical Accounting Estimates
Estimates boilerplate.
Item 3. Quantitative and Qualitative Disclosures About Market Risk
Rates.
PART II. OTHER INFORMATION
Item 2. Unregistered Sales of Equity Securities and Use of Proceeds
Buybacks table.
"""


def _html(text: str) -> bytes:
    return ("<html><body>" + "".join(f"<p>{line}</p>" for line in text.splitlines())
            + "</body></html>").encode()


@pytest.fixture
def edgar(monkeypatch, tmp_path):
    """Fake EDGAR: submissions JSON, filing index JSON and documents keyed by URL tail."""
    monkeypatch.setattr(sec_filings, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(sec_filings.sec_edgar, "cik_for", lambda t: CIK)
    monkeypatch.setattr(sec_filings.sec_edgar, "_cached_json", lambda url, name: SUBMISSIONS)
    docs: dict[str, bytes] = {}
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        for tail, body in docs.items():
            if url.endswith(tail):
                return body
        raise AssertionError(f"unexpected fetch {url}")

    def fetch_document(url):
        assert not url.endswith("index.json"), "the index JSON goes through sec_edgar._fetch_json"
        return fetch(url)

    monkeypatch.setattr(sec_filings, "_fetch", fetch_document)
    monkeypatch.setattr(sec_filings.sec_edgar, "_fetch_json", lambda url: json.loads(fetch(url)))
    return docs, fetched


@pytest.mark.unit
def test_latest_10q_picks_the_newest_on_or_before_as_of(edgar):
    docs, fetched = edgar
    docs["000002134426000020/q1.htm"] = _html(MDNA_10Q)
    docs["000002134426000041/q2.htm"] = _html(MDNA_10Q.replace("6%", "9%"))
    out = sec_filings.latest_10q_mdna("ko", "2026-07-29")         # Q2 10-Q filed the next day
    assert out.splitlines()[0] == (
        "# Quarterly report (10-Q) for KO, filed 2026-04-28, period ending 2026-03-31")
    assert "rose 6%" in out and not any("q2.htm" in u for u in fetched)
    later = sec_filings.latest_10q_mdna("KO", "2026-07-30")
    assert "filed 2026-07-30, period ending 2026-06-30" in later and "rose 9%" in later
    with pytest.raises(NoMarketDataError):
        sec_filings.latest_10q_mdna("KO", "2026-04-27")


@pytest.mark.unit
def test_mdna_skips_the_table_of_contents_entry():
    body = sec_filings.mdna_span(sec_filings._plain_text(_html(MDNA_10Q)))
    assert "Balance sheet numbers" not in body and "Item 3" not in body
    assert "Rates." not in body and "Buybacks table" not in body
    assert "Net revenue rose 6%" in body


@pytest.mark.unit
def test_mdna_keeps_results_and_liquidity(edgar):
    docs, _ = edgar
    docs["000002134426000041/q2.htm"] = _html(MDNA_10Q)
    out = sec_filings.latest_10q_mdna("KO", "2026-08-01")
    assert "Net revenue rose 6%" in out and "Cash from operations was strong" in out
    assert "Overview filler" not in out and "Estimates boilerplate" not in out
    # Without the two sub-headings the whole item is kept, cut with the usual note.
    whole = MDNA_10Q.replace("\nResults of Operations\n", "\n").replace(
        "\nLiquidity and Capital Resources\n", "\n").replace("Overview filler. ", "x" * 500)
    body = sec_filings.mdna_span(sec_filings._plain_text(_html(whole)))
    assert "Overview" in body and "Estimates boilerplate" in body
    docs["000002134426000041/q2.htm"] = _html(whole)
    for path in sec_filings._cache_dir().glob("*"):
        path.unlink()
    out = sec_filings.latest_10q_mdna("KO", "2026-08-01")
    assert "more characters not shown]" in out


RELEASE = ["Acme reports second-quarter results.",
           "Revenue was $1.2 billion. " * 20] + ["Segment detail paragraph. " * 40] * 12 + [
           "Full-year outlook: we now expect revenue growth of 8% to 10%.",
           "Backlog reached a record $4.1 billion; book-to-bill was 1.2.",
           "Remaining performance obligations (RPO) grew 30%.",
           "Unrelated closing paragraph about the conference call."]


@pytest.mark.unit
def test_earnings_release_requires_item_2_02(edgar):
    docs, fetched = edgar
    docs["000002134426000019/index.json"] = (
        b'{"directory": {"item": [{"name": "e1.htm"}, {"name": "ex99-1.htm"}, {"name": "r1.xml"}]}}')
    docs["000002134426000019/ex99-1.htm"] = _html("Acme reports first-quarter results.")
    out = sec_filings.latest_earnings_release("KO", "2026-07-20")   # a 5.02 8-K was filed 07-02
    assert out.splitlines()[0] == "# Earnings release (8-K exhibit 99.1) for KO, filed 2026-04-22"
    assert "first-quarter results" in out
    assert not any("000002134426000030" in u for u in fetched)
    with pytest.raises(NoMarketDataError):
        sec_filings.latest_earnings_release("KO", "2026-04-21")


@pytest.mark.unit
def test_outlook_lines_are_kept_beyond_the_cut(edgar):
    docs, _ = edgar
    docs["000002134426000040/index.json"] = (
        b'{"directory": {"item": [{"name": "e2.htm"}, {"name": "EX991.HTM"}]}}')
    docs["000002134426000040/EX991.HTM"] = _html("\n".join(RELEASE))
    out = sec_filings.latest_earnings_release("KO", "2026-07-25")
    assert out.splitlines()[0] == "# Earnings release (8-K exhibit 99.1) for KO, filed 2026-07-25"
    assert sec_filings.RELEASE_CHARS == 10_000
    head, outlook = out.split("## Outlook and demand lines")
    assert "Full-year outlook" not in head and "more characters not shown]" in head
    for line in RELEASE[-4:-1]:
        assert line in outlook
    assert "conference call" not in outlook and "Segment detail" not in outlook


@pytest.mark.unit
def test_non_filer_sentence_for_both_tools(monkeypatch):
    from tradingagents.agents import tools

    monkeypatch.setattr(sec_filings.sec_edgar, "cik_for", lambda t: None)
    assert sec_filings.latest_10q_mdna("000270.KS", "2026-10-23") == sec_filings.NOT_A_US_FILER
    assert sec_filings.latest_earnings_release("000270.KS") == sec_filings.NOT_A_US_FILER
    state = {"ticker": "000270.KS", "trade_date": "2026-10-23"}
    assert tools.get_quarterly_report.func(**state) == sec_filings.NOT_A_US_FILER
    assert tools.get_earnings_release.func(**state) == sec_filings.NOT_A_US_FILER


@pytest.mark.unit
def test_tools_are_offered_to_both_analysts(monkeypatch):
    from tradingagents.agents.analysts import fundamentals_analyst, news_analyst
    from tradingagents.agents.tools import get_earnings_release, get_quarterly_report

    names = [t.name for t in fundamentals_analyst.TOOLS]
    assert names[names.index("get_annual_report") + 1: names.index("get_annual_report") + 3] == [
        "get_quarterly_report", "get_earnings_release"]
    assert get_quarterly_report in news_analyst.TOOLS and get_earnings_release in news_analyst.TOOLS

    prompts = {}

    def capture(key):
        def take_turn(prompt, llm, tools, messages):
            prompts[key] = prompt.format(messages=[])
            return None, ""
        return take_turn

    state = {"trade_date": "2026-10-23", "company_of_interest": "KO", "messages": []}
    monkeypatch.setattr(fundamentals_analyst, "take_turn", capture("f"))
    monkeypatch.setattr(news_analyst, "take_turn", capture("n"))
    monkeypatch.setattr(fundamentals_analyst, "get_instrument_context_from_state", lambda s: "")
    monkeypatch.setattr(news_analyst, "get_instrument_context_from_state", lambda s: "")
    fundamentals_analyst.create_fundamentals_analyst(None)(state)
    news_analyst.create_news_analyst(None)(state)
    assert ("Call `get_quarterly_report` for the latest quarter's drivers and management's guidance;"
            " quote it with the filing date.") in prompts["f"]
    assert ("`get_earnings_release` and `get_quarterly_report` are verified sources: a quoted"
            " sentence with its filing date counts as verified demand evidence and as evidence for"
            " the cause of a fall.") in prompts["n"]


@pytest.mark.unit
def test_mdna_accepts_operations_review_and_a_combined_liquidity_heading():
    text = MDNA_10Q.replace("\nResults of Operations\n", "\nOPERATIONS REVIEW\n").replace(
        "\nLiquidity and Capital Resources\n", "\nLIQUIDITY, CAPITAL RESOURCES AND FINANCIAL POSITION\n")
    body = sec_filings.mdna_span(sec_filings._plain_text(_html(text)))
    assert body.startswith("OPERATIONS REVIEW") and "Overview filler" not in body
    assert "Cash from operations was strong" in body and "Estimates boilerplate" not in body


@pytest.mark.unit
def test_an_earnings_exhibit_named_without_99_is_found_by_its_type(edgar):
    docs, _ = edgar
    docs["000002134426000019/index.json"] = (
        b'{"directory": {"item": [{"name": "0000021344-26-000019-index-headers.html"},'
        b' {"name": "0000021344-26-000019-index.html"}, {"name": "e1.htm"},'
        b' {"name": "q1cfocommentary.htm"}, {"name": "q1pr.htm"}, {"name": "R1.htm"}]}}')
    docs["000002134426000019/0000021344-26-000019-index.html"] = b"""<html><body>
        <table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
        <tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/21344/000002134426000019/e1.htm">
        e1.htm</a></td><td>8-K</td></tr>
        <tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/21344/000002134426000019/q1pr.htm">
        q1pr.htm</a></td><td>EX-99.1</td></tr>
        <tr><td>3</td><td>CFO COMMENTARY</td><td><a href="/Archives/edgar/data/21344/000002134426000019/q1cfocommentary.htm">
        q1cfocommentary.htm</a></td><td>EX-99.2</td></tr></table></body></html>"""
    docs["000002134426000019/q1pr.htm"] = _html("Acme first-quarter results.")
    out = sec_filings.latest_earnings_release("KO", "2026-07-20")
    assert "Acme first-quarter results." in out


def _release(edgar, paragraphs):
    docs, _ = edgar
    docs["000002134426000040/index.json"] = b'{"directory": {"item": [{"name": "ex99-1.htm"}]}}'
    docs["000002134426000040/ex99-1.htm"] = _html("\n".join(paragraphs))
    return sec_filings.latest_earnings_release("KO", "2026-07-25")


@pytest.mark.unit
def test_an_outlook_paragraph_inside_the_cut_is_not_repeated(edgar):
    shown = "We expect demand to stay firm in the second half."
    out = _release(edgar, [shown] + RELEASE[1:])
    head, outlook = out.split("## Outlook and demand lines")
    assert shown in head and shown not in outlook
    assert "Backlog reached a record $4.1 billion" in outlook


@pytest.mark.unit
def test_safe_harbor_paragraphs_are_left_out_of_the_outlook(edgar):
    boilerplate = [
        "This release contains forward-looking statements; words such as expect identify them.",
        "These statements are made under the safe harbor; actual demand may differ.",
        "Statements under the private securities litigation reform act of 1995 about our outlook.",
    ]
    out = _release(edgar, RELEASE[:-1] + boilerplate)
    outlook = out.split("## Outlook and demand lines")[1]
    assert "Backlog reached a record $4.1 billion" in outlook
    for paragraph in boilerplate:
        assert paragraph not in outlook


@pytest.mark.unit
def test_the_outlook_section_is_capped(edgar):
    assert sec_filings.OUTLOOK_CHARS == 4_000
    many = [f"Guidance item {i}: we expect bookings to grow. " + "Detail. " * 60 for i in range(20)]
    out = _release(edgar, RELEASE[:-4] + many)
    outlook = out.split("## Outlook and demand lines")[1]
    assert "Guidance item 0:" in outlook and "Guidance item 19:" not in outlook
    assert outlook.rstrip().endswith("more characters not shown]")
    assert len(outlook) < sec_filings.OUTLOOK_CHARS + 200


BANK_10Q = """
Item 1. Financial Statements 3
Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations 4
Item 3. Quantitative and Qualitative Disclosures About Market Risk 90
Management's discussion and analysis
""" + "Net interest income rose. " * 40 + """
PART II. OTHER INFORMATION
Item 2. Unregistered Sales of Equity Securities and Use of Proceeds
""" + "We repurchased shares under the buyback program. " * 10 + """
Item 6. Exhibits
"""


@pytest.mark.unit
def test_part_ii_item_2_is_never_taken_for_the_mdna(edgar):
    assert sec_filings.mdna_span(sec_filings._plain_text(_html(BANK_10Q))) == ""
    docs, _ = edgar
    docs["000002134426000041/q2.htm"] = _html(BANK_10Q)
    out = sec_filings.latest_10q_mdna("JPM", "2026-08-01")
    assert "could not be located" in out and "buyback" not in out


@pytest.mark.unit
def test_an_empty_extraction_is_cached_and_a_new_parser_version_rereads(edgar, monkeypatch):
    docs, fetched = edgar
    assert sec_filings._PARSER_VERSION == 3
    docs["000002134426000041/q2.htm"] = _html(BANK_10Q)
    assert "could not be located" in sec_filings.latest_10q_mdna("KO", "2026-08-01")
    [cached] = sec_filings._cache_dir().glob("10q_*")
    assert cached.name.endswith("_v3.txt") and cached.read_text() == ""
    reads = len(fetched)
    assert "could not be located" in sec_filings.latest_10q_mdna("KO", "2026-08-01")
    assert len(fetched) == reads   # the empty result is served from the cache
    monkeypatch.setattr(sec_filings, "_PARSER_VERSION", 4)
    docs["000002134426000041/q2.htm"] = _html(MDNA_10Q)
    assert "Net revenue rose 6%" in sec_filings.latest_10q_mdna("KO", "2026-08-01")


FOREIGN = {"filings": {"recent": {
    "form": ["6-K", "20-F", "6-K"],
    "filingDate": ["2026-07-17", "2026-04-15", "2026-04-17"],
    "accessionNumber": ["0001046179-26-000050", "0001046179-26-000030", "0001046179-26-000031"],
    "primaryDocument": ["k6.htm", "f20.htm", "k6b.htm"],
    "reportDate": ["", "2025-12-31", ""],
    "items": ["", "", ""],
}}}


@pytest.mark.unit
def test_a_20f_filer_gets_a_sentence_from_both_tools_not_an_error(edgar, monkeypatch):
    from tradingagents.agents import tools

    _, fetched = edgar
    monkeypatch.setattr(sec_filings.sec_edgar, "_cached_json", lambda url, name: FOREIGN)
    assert sec_filings.latest_10q_mdna("TSM", "2026-10-23") == sec_filings.NO_QUARTERLY_FILINGS
    assert sec_filings.latest_earnings_release("TSM", "2026-10-23") == (
        sec_filings.NO_QUARTERLY_FILINGS)
    assert sec_filings.NO_QUARTERLY_FILINGS.startswith(
        "This company files 20-F/6-K with the SEC, not 10-Q or 8-K earnings releases")
    assert fetched == []
    state = {"ticker": "TSM", "trade_date": "2026-10-23"}
    assert tools.get_quarterly_report.func(**state) == sec_filings.NO_QUARTERLY_FILINGS
    assert tools.get_earnings_release.func(**state) == sec_filings.NO_QUARTERLY_FILINGS


@pytest.mark.unit
def test_exhibit_99_1_is_taken_before_99_2(edgar):
    docs, _ = edgar
    docs["000002134426000040/index.json"] = (
        b'{"directory": {"item": [{"name": "e2.htm"}, {"name": "ex99-2.htm"},'
        b' {"name": "ex99-1.htm"}]}}')
    docs["000002134426000040/ex99-2.htm"] = _html("CFO commentary slides.")
    docs["000002134426000040/ex99-1.htm"] = _html("Acme second-quarter results.")
    out = sec_filings.latest_earnings_release("KO", "2026-07-25")
    assert "Acme second-quarter results." in out and "CFO commentary" not in out


@pytest.mark.unit
def test_an_empty_exhibit_is_the_vendor_unavailable_not_a_crash(edgar):
    from tradingagents.dataflows.errors import VendorUnavailableError

    docs, _ = edgar
    docs["000002134426000040/index.json"] = b'{"directory": {"item": [{"name": "ex99-1.htm"}]}}'
    docs["000002134426000040/ex99-1.htm"] = b""
    with pytest.raises(VendorUnavailableError):
        sec_filings.latest_earnings_release("KO", "2026-07-25")
