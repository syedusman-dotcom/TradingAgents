"""Which tracked value investors hold a stock, from their 13F filings (no network)."""

import json

import pytest

from value_pipeline import superinvestors as si
from value_pipeline.config import Paths

INFO_TABLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
    <value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
    <value>500</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>CALL</titleOfClass><cusip>037833100</cusip>
    <value>99</value><shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Call</putCall></infoTable>
  <infoTable><nameOfIssuer>BERKSHIRE HATHAWAY INC DEL</nameOfIssuer><titleOfClass>CL B NEW</titleOfClass>
    <cusip>084670702</cusip><value>300</value><shrsOrPrnAmt><sshPrnamt>2</sshPrnamt></shrsOrPrnAmt></infoTable>
</informationTable>"""


@pytest.mark.unit
def test_info_table_sums_rows_per_security_and_skips_options():
    rows = {r["cusip"]: r for r in si.parse_info_table(INFO_TABLE)}
    assert rows["037833100"]["shares"] == 15 and rows["037833100"]["value"] == 1500
    assert rows["084670702"]["issuer"] == "BERKSHIRE HATHAWAY INC DEL"
    assert len(rows) == 2


@pytest.mark.unit
def test_cusips_map_to_yahoo_tickers_and_are_cached(tmp_path):
    asked = []

    def lookup(cusips):
        asked.append(list(cusips))
        return {"037833100": "AAPL", "084670702": "BRK/B"}

    cache = tmp_path / "cusips.json"
    got = si.map_cusips(["037833100", "084670702", "999999999"], cache, lookup)
    assert got == {"037833100": "AAPL", "084670702": "BRK-B", "999999999": None}
    assert si.map_cusips(["037833100", "999999999"], cache, lookup) == {"037833100": "AAPL", "999999999": None}
    assert len(asked) == 1                         # second call served from the cache


@pytest.mark.unit
def test_refresh_writes_holdings_by_ticker(tmp_path):
    paths = Paths(tmp_path)
    investors = [{"name": "Berkshire", "cik": "0001067983"}, {"name": "Gotham", "cik": "0001510387"}]
    filings = {"0001067983": ("2026-06-30", "2026-08-14", INFO_TABLE),
               "0001510387": ("2026-06-30", "2026-08-14", INFO_TABLE.replace(b"084670702", b"191216100"))}
    data = si.refresh(paths, investors, fetch_filing=filings.__getitem__,
                      lookup=lambda c: {"037833100": "AAPL", "084670702": "BRK/B", "191216100": "KO"})
    assert sorted(h["investor"] for h in data["holdings"]["AAPL"]) == ["Berkshire", "Gotham"]
    assert data["holdings"]["KO"][0] == {"investor": "Gotham", "shares": 2, "value": 300, "period": "2026-06-30"}
    assert json.loads((tmp_path / "superinvestors.json").read_text())["holdings"]["BRK-B"][0]["investor"] == "Berkshire"
    assert si.holders(paths, "aapl") == data["holdings"]["AAPL"]
    assert si.summary(data["holdings"].get("AAPL")) == "2: Berkshire, Gotham"
    assert si.summary(None) == ""


@pytest.mark.unit
def test_refresh_keeps_going_when_one_investor_fails(tmp_path):
    def fetch(cik):
        if cik == "bad":
            raise RuntimeError("EDGAR down")
        return ("2026-06-30", "2026-08-14", INFO_TABLE)

    data = si.refresh(Paths(tmp_path), [{"name": "A", "cik": "bad"}, {"name": "B", "cik": "ok"}],
                      fetch_filing=fetch, lookup=lambda c: {"037833100": "AAPL"})
    assert [h["investor"] for h in data["holdings"]["AAPL"]] == ["B"]
    assert data["errors"] == ["A: RuntimeError: EDGAR down"]


@pytest.mark.unit
def test_screen_rows_get_the_overlap_column():
    rows = [{"ticker": "AAPL"}, {"ticker": "XYZ"}]
    si.annotate(rows, {"AAPL": [{"investor": "Berkshire"}, {"investor": "Gotham"}]})
    assert rows[0]["superinvestors"] == 2 and rows[0]["superinvestor_names"] == "Berkshire, Gotham"
    assert rows[1]["superinvestors"] == 0 and rows[1]["superinvestor_names"] == ""


class _Resp:
    def __init__(self, payload=None, content=b""):
        self._payload, self.content, self.status_code = payload, content, 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


@pytest.mark.unit
def test_latest_13f_skips_amendments_and_picks_the_holdings_table(monkeypatch):
    """A 13F-HR/A can list only the added rows; the full portfolio is the latest 13F-HR."""
    submissions = {"filings": {"recent": {
        "form": ["13F-HR/A", "13F-HR", "13F-HR"],
        "filingDate": ["2026-09-01", "2026-08-14", "2026-05-15"],
        "reportDate": ["2026-06-30", "2026-06-30", "2026-03-31"],
        "accessionNumber": ["0001-26-000003", "0001-26-000002", "0001-26-000001"]}}}
    index = {"directory": {"item": [{"name": "primary_doc.xml", "size": "900"}, {"name": "exhibit.xml", "size": "300"},
                                    {"name": "form13fInfoTable.xml", "size": "120000"}]}}
    asked = []

    def get(url, **kw):
        asked.append(url)
        if url.endswith("CIK0001067983.json"):
            return _Resp(submissions)
        if url.endswith("/index.json"):
            return _Resp(index)
        return _Resp(content=INFO_TABLE)

    monkeypatch.setattr(si.requests, "get", get)
    period, filed, xml = si.fetch_latest_13f("0001067983")
    assert (period, filed) == ("2026-06-30", "2026-08-14")
    assert any("000126000002/form13fInfoTable.xml" in u for u in asked)
    assert xml == INFO_TABLE


@pytest.mark.unit
def test_refresh_keeps_the_previous_holdings_of_an_investor_that_failed(tmp_path):
    paths = Paths(tmp_path)
    investors = [{"name": "A", "cik": "a"}, {"name": "B", "cik": "b"}]
    ok = lambda cik: ("2026-06-30", "2026-08-14", INFO_TABLE)  # noqa: E731
    lookup = lambda c: {"037833100": "AAPL", "084670702": "BRK/B"}  # noqa: E731
    si.refresh(paths, investors, fetch_filing=ok, lookup=lookup)

    def flaky(cik):
        if cik == "a":
            raise RuntimeError("EDGAR down")
        return ("2026-09-30", "2026-11-14", INFO_TABLE)

    data = si.refresh(paths, investors, fetch_filing=flaky, lookup=lookup)
    by = {h["investor"]: h["period"] for h in data["holdings"]["AAPL"]}
    assert by == {"A": "2026-06-30", "B": "2026-09-30"}          # A's older filing is kept, not dropped
    assert data["errors"] == ["A: RuntimeError: EDGAR down"]
    assert [i["name"] for i in data["investors"]] == ["A", "B"]
