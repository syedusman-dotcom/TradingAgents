"""Which tracked value investors hold a stock, from their latest SEC 13F filings.

A second opinion for you, not an input to the agents or the action rules: a 13F
is filed up to 45 days after the quarter it describes, so it shows old moves,
and it leaves out the price paid, short positions and foreign shares.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

import requests
from lxml import etree

from value_pipeline.config import Paths

log = logging.getLogger(__name__)

_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
_FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"
_FILING_FILE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{name}"
_OPENFIGI = "https://api.openfigi.com/v3/mapping"
# OpenFIGI without a key: a few identifiers per request, about 25 requests a minute.
_FIGI_BATCH, _FIGI_PAUSE = 5, 2.5


def _sec_headers() -> dict:
    return {"User-Agent": os.getenv("SEC_EDGAR_USER_AGENT") or "value-pipeline research (contact@example.com)"}


# --- parsing and matching ------------------------------------------------------

def _text(row, tag: str) -> str:
    node = row.find(f".//{{*}}{tag}")
    return (node.text or "").strip() if node is not None else ""


def parse_info_table(xml: bytes) -> list[dict]:
    """A 13F holdings table as one row per security (cusip), shares and value summed; options skipped."""
    root = etree.fromstring(xml)
    out: dict[str, dict] = {}
    for row in root.iter("{*}infoTable"):
        if _text(row, "putCall"):
            continue
        cusip = _text(row, "cusip").upper()
        held = out.setdefault(cusip, {"cusip": cusip, "issuer": _text(row, "nameOfIssuer"), "shares": 0, "value": 0})
        held["shares"] += int(float(_text(row, "sshPrnamt") or 0))
        held["value"] += int(float(_text(row, "value") or 0))
    return list(out.values())


def _openfigi(cusips: list[str]) -> dict[str, str]:
    """Ask OpenFIGI for each CUSIP's US ticker, in small batches to stay inside the free limit."""
    found: dict[str, str] = {}
    for i in range(0, len(cusips), _FIGI_BATCH):
        batch = cusips[i:i + _FIGI_BATCH]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]
        resp = requests.post(_OPENFIGI, json=jobs, timeout=30)
        if resp.status_code == 429:
            time.sleep(60)
            resp = requests.post(_OPENFIGI, json=jobs, timeout=30)
        resp.raise_for_status()
        for cusip, result in zip(batch, resp.json(), strict=True):
            data = result.get("data") or []
            if data and data[0].get("ticker"):
                found[cusip] = data[0]["ticker"]
        time.sleep(_FIGI_PAUSE)
    return found


def map_cusips(cusips: Iterable[str], cache: Path,
               lookup: Callable[[list[str]], dict[str, str]] = _openfigi) -> dict[str, str | None]:
    """CUSIP -> Yahoo-style ticker (BRK/B -> BRK-B), or None when there is none. Cached for good:
    a CUSIP never changes its security."""
    known: dict[str, str | None] = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    wanted = list(dict.fromkeys(cusips))
    missing = [c for c in wanted if c not in known]
    if missing:
        found = lookup(missing)
        for c in missing:
            ticker = found.get(c)
            known[c] = ticker.replace("/", "-").upper() if ticker else None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known, indent=0), encoding="utf-8")
    return {c: known.get(c) for c in wanted}


# --- fetching -------------------------------------------------------------------------

def fetch_latest_13f(cik: str) -> tuple[str, str, bytes]:
    """(period of report, filing date, holdings-table XML) of an investor's latest 13F-HR."""
    cik10 = f"{int(cik):010d}"
    recent = requests.get(_SUBMISSIONS.format(cik=cik10), headers=_sec_headers(), timeout=30).json()
    recent = recent["filings"]["recent"]
    for form, filed, period, accession in zip(recent["form"], recent["filingDate"], recent["reportDate"],
                                              recent["accessionNumber"], strict=False):
        # Amendments (13F-HR/A) are skipped: one kind lists only the rows added, and would read
        # as the whole portfolio. The original filing is complete, if up to a quarter older.
        if form == "13F-HR":
            folder = accession.replace("-", "")
            index = requests.get(_FILING_INDEX.format(cik=int(cik), accession=folder),
                                 headers=_sec_headers(), timeout=30).json()
            xmls = [i for i in index["directory"]["item"]
                    if i["name"].lower().endswith(".xml") and i["name"] != "primary_doc.xml"]
            if not xmls:
                raise ValueError(f"13F {accession} has no holdings table")
            # The holdings table is by far the largest XML in the filing; exhibits are small.
            name = max(xmls, key=lambda i: int(i.get("size") or 0))["name"]
            table = requests.get(_FILING_FILE.format(cik=int(cik), accession=folder, name=name),
                                 headers=_sec_headers(), timeout=60)
            table.raise_for_status()
            return period, filed, table.content
    raise ValueError("no 13F-HR filed")


# --- the stored overlap ---------------------------------------------------------------

def _store(paths: Paths) -> Path:
    return paths.root / "superinvestors.json"


def load(paths: Paths) -> dict:
    f = _store(paths)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"holdings": {}, "investors": []}


def refresh(paths: Paths, investors: list[dict], fetch_filing=fetch_latest_13f,
            lookup: Callable[[list[str]], dict[str, str]] = _openfigi) -> dict:
    """Fetch each investor's latest 13F and store their holdings by ticker. One failure keeps that
    investor's previously stored holdings (a week-old filing beats none)."""
    previous = load(paths)
    holdings: dict[str, list[dict]] = {}
    summary, errors = [], []
    for inv in investors:
        try:
            period, filed, xml = fetch_filing(inv["cik"])
            rows = parse_info_table(xml)
            tickers = map_cusips([r["cusip"] for r in rows], paths.cache / "cusip_tickers.json", lookup)
        except Exception as exc:  # noqa: BLE001 - one investor's outage must not lose the others
            errors.append(f"{inv['name']}: {type(exc).__name__}: {exc}")
            log.warning("13F for %s unavailable: %s", inv["name"], exc)
            for ticker, held in previous.get("holdings", {}).items():
                kept = [h for h in held if h["investor"] == inv["name"]]
                if kept:
                    holdings.setdefault(ticker, []).extend(kept)
            summary.extend(i for i in previous.get("investors", []) if i["name"] == inv["name"])
            continue
        for r in rows:
            ticker = tickers.get(r["cusip"])
            if ticker:
                holdings.setdefault(ticker, []).append(
                    {"investor": inv["name"], "shares": r["shares"], "value": r["value"], "period": period})
        summary.append({"name": inv["name"], "period": period, "filed": filed, "holdings": len(rows)})
    data = {"updated": datetime.now().isoformat(timespec="seconds"), "investors": summary,
            "holdings": holdings, "errors": errors}
    paths.root.mkdir(parents=True, exist_ok=True)
    _store(paths).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def refresh_if_stale(paths: Paths, cfg: dict, force: bool = False) -> dict:
    """Refresh when the stored data is older than ``refresh_days`` (13Fs change quarterly)."""
    current = load(paths)
    updated = current.get("updated")
    fresh = updated and datetime.fromisoformat(updated) > datetime.now() - timedelta(days=cfg["refresh_days"])
    if fresh and not force:
        return current
    return refresh(paths, cfg["investors"])


def holders(paths: Paths, ticker: str) -> list[dict]:
    return load(paths)["holdings"].get(ticker.strip().upper(), [])


def summary(held: list[dict] | None) -> str:
    """'2: Berkshire, Gotham' for a list of holders; '' for none."""
    if not held:
        return ""
    names = list(dict.fromkeys(h["investor"] for h in held))
    return f"{len(names)}: {', '.join(names)}"


def annotate(rows: list[dict], holdings: dict[str, list[dict]]) -> None:
    """Add the overlap columns to screen rows in place."""
    for r in rows:
        names = list(dict.fromkeys(h["investor"] for h in holdings.get(r["ticker"], [])))
        r["superinvestors"] = len(names)
        r["superinvestor_names"] = ", ".join(names)
