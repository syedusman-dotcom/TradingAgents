"""Peer comparison from Yahoo's industry listing, in USD.

"Cheap" means cheap against something. This lines the subject up against the largest
companies of its Yahoo industry on the same value measures the fundamentals analyst
uses, converted to dollars so a London or Tokyo peer sits in the same table.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from statistics import median

import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.value_metrics import div, fcf_yield, num, roic
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, usd_rate, yf_retry
from tradingagents.dataflows.vendors.yahoo.value_metrics import statement_years

COLUMNS = ("Ticker", "Name", "Mkt cap (USD)", "EV/EBIT", "Gross margin", "EBIT margin", "ROIC",
           "FCF yield", "Revenue CAGR 5y", "Share count 5y")
_METRICS = ("cap", "ev_ebit", "gross", "ebit_margin", "roic", "fcf", "cagr", "shares")


def _fetch(symbol: str) -> dict:
    """One company's Yahoo profile and annual statements; raises if either is missing."""
    t = yf.Ticker(symbol)
    info = yf_retry(lambda: t.info) or {}
    frames = [yf_retry(lambda attr=attr: getattr(t, attr)) for attr in ("income_stmt", "balance_sheet", "cashflow")]
    years = statement_years(*frames)
    if not info or not years:
        raise ValueError(f"no data for {symbol}")
    return {"symbol": symbol, "info": info, "years": years}


def _row(company: dict) -> dict:
    info, years = company["info"], company["years"]
    quote, reported = info.get("currency"), info.get("financialCurrency") or info.get("currency")
    cur = years[0]
    rate = usd_rate(quote)
    cap = num(info.get("marketCap"))
    cap_usd = cap * rate if cap and rate else None
    fin_rate = usd_rate(reported)
    ev_ebit = None
    ebit = num(cur.get("ebit"))
    if cap_usd and fin_rate and ebit and ebit > 0:
        net_debt = (num(cur.get("total_debt")) or 0.0) - (num(cur.get("cash")) or 0.0)
        ev_ebit = (cap_usd + net_debt * fin_rate) / (ebit * fin_rate)
    window = years[:5]
    newest, oldest = num(window[0].get("revenue")), num(window[-1].get("revenue"))
    cagr = None
    if len(window) > 1 and newest and oldest and newest > 0 and oldest > 0:
        cagr = (newest / oldest) ** (1 / (len(window) - 1)) - 1
    share_change = None
    if len(window) > 1 and num(window[0].get("shares")) and num(window[-1].get("shares")):
        share_change = window[0]["shares"] / window[-1]["shares"] - 1
    fcf_local = num(cur.get("free_cash_flow"))
    fcf = fcf_yield(fcf_local * fin_rate, cap_usd) if fcf_local is not None and fin_rate else None
    return {
        "symbol": company["symbol"],
        "name": str(info.get("shortName") or info.get("longName") or company["symbol"]).replace("|", "/"),
        "cap": cap_usd, "ev_ebit": ev_ebit,
        "gross": div(cur.get("gross_profit"), cur.get("revenue")),
        "ebit_margin": div(cur.get("ebit"), cur.get("revenue")),
        "roic": roic(cur), "fcf": fcf, "cagr": cagr, "shares": share_change,
    }


def _money(x) -> str:
    return "–" if x is None else f"${x / 1e9:,.1f}B"


def _ratio(x) -> str:
    return "–" if x is None else f"{x:.1f}×"


def _pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"


def _cells(row: dict) -> str:
    values = (_money(row["cap"]), _ratio(row["ev_ebit"]), _pct(row["gross"]), _pct(row["ebit_margin"]),
              _pct(row["roic"]), _pct(row["fcf"]), _pct(row["cagr"]), _pct(row["shares"]))
    return f"| {row['symbol']} | {row['name']} | " + " | ".join(values) + " |"


def _median_row(rows: list[dict]) -> dict:
    out = {"symbol": "Median (peers)", "name": ""}
    for key in _METRICS:
        known = [r[key] for r in rows if r[key] is not None]
        out[key] = median(known) if known else None
    return out


def _industry_peers(symbol: str, info: dict, limit: int) -> tuple[list[str], str | None]:
    """Up to ``limit`` peer symbols from Yahoo's industry listing, or the reason there are none."""
    key = info.get("industryKey")
    if not key:
        return [], f"No industry is known for {symbol} on Yahoo, so there is no peer table."
    try:
        table = yf_retry(lambda: yf.Industry(key).top_companies)
    except Exception:
        table = None
    if table is None or len(table) == 0:
        return [], f"Yahoo lists no peer companies for the {key} industry, so there is no peer table."
    if "market weight" in table.columns:
        table = table.sort_values("market weight", ascending=False)
    names = [str(s) for s in table.index if str(s).upper() != symbol.upper()]
    return names[:limit], None


def peer_comparison(symbol: str, limit: int = 6, as_of: str | None = None) -> str:
    symbol = normalize_symbol(symbol)
    day = as_of or date.today().isoformat()
    path = Path(get_config()["data_cache_dir"]) / "peers" / f"{symbol}_{day}.json"
    try:
        return json.loads(path.read_text())["report"]
    except Exception:
        pass  # no usable cache entry: build the table

    try:
        subject = _fetch(symbol)
    except ValueError:
        # No profile or statements: an ETF, a fund, an unknown ticker. Raised as the typed
        # absence (or outage) the tool layer turns into text instead of ending the run.
        raise_for_empty(symbol, symbol, "annual statements")
    names, reason = _industry_peers(symbol, subject["info"], limit)
    title = f"# Peer comparison for {symbol} ({day})"
    if reason:
        return f"{title}\n\n{reason}"
    rows, skipped = [], []
    for name in names:
        try:
            rows.append(_row(_fetch(name)))
        except Exception:
            skipped.append(name)
    if not rows:
        return f"{title}\n\nNo peer of {symbol} returned usable data ({', '.join(skipped)}), so there is no peer table."
    lines = [title, "",
             "Peers are the largest companies of the subject's Yahoo industry, by market weight. Market caps "
             "and EV in USD at today's rate; ratios from the latest annual statements.", "",
             "| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS),
             _cells(_row(subject)), *map(_cells, rows), _cells(_median_row(rows))]
    if skipped:
        lines += ["", f"Peers skipped (no data): {', '.join(skipped)}"]
    report = "\n".join(lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"report": report}))
    except OSError:
        pass  # the cache is a convenience
    return report


def get_peer_comparison(ticker: str, as_of_date: str | None = None) -> str:
    """The router's entry point: the table, withheld for a past-dated run."""
    symbol = normalize_symbol(ticker)
    if is_historical(as_of_date):
        return (f"# Peer comparison for {symbol}\n\nNot available for {as_of_date}: it combines today's "
                "quotes and market caps with statements Yahoo does not date by filing, so a past-dated run "
                "would see information from after its date.")
    return peer_comparison(symbol, as_of=as_of_date)
