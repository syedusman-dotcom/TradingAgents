"""Screen the S&P 500 + 400 for cheap, good, financially healthy businesses (Magic Formula ranked)."""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from value_pipeline import market_data, metrics, superinvestors
from value_pipeline.config import Paths

log = logging.getLogger(__name__)


# The Magic Formula ranks on these: multi-year averages, so a cyclical peak does
# not pass as a bargain, and ROIC capped so asset-light outliers (ROIC in the
# thousands of percent) cannot buy the top of the list on one ratio.
EY_KEY, ROIC_KEY = "earnings_yield_avg", "roic_ranked"


def evaluate(data: dict, normalize_years: int = 5, roic_cap: float = 1.0,
             rate: float = metrics.DISCOUNT_RATE) -> dict:
    """Every screen metric for one ticker's fetched data (see market_data.fundamentals)."""
    years = data["years"]
    if len(years) < 2:
        raise market_data.DataUnavailable("fewer than two annual statements")
    cur, prev = years[0], years[1]
    mcap = data.get("market_cap")
    ev = data.get("enterprise_value") or metrics.enterprise_value(mcap, cur.get("total_debt"), cur.get("cash"))
    fs = metrics.piotroski(cur, prev)
    net_debt = (metrics.num(cur.get("total_debt")) or 0.0) - (metrics.num(cur.get("cash")) or 0.0)
    shares = metrics.share_count(mcap, data.get("price"), cur.get("shares"), data.get("shares"))
    value = metrics.formula_value(years, data.get("price"), shares, net_debt, rate) or {}
    tr = metrics.trend(data.get("closes") or [])
    norm = metrics.normalized(years, normalize_years)
    row = {
        "ticker": data["ticker"],
        "fiscal_year_end": cur["fiscal_year_end"],
        "price": data.get("price"),
        "market_cap": mcap,
        "shares": shares,
        "enterprise_value": ev,
        "earnings_yield": metrics.earnings_yield(cur.get("ebit"), ev),
        "roic": metrics.roic(cur),
        "earnings_yield_avg": metrics.earnings_yield(norm["ebit_avg"], ev),
        "roic_avg": norm["roic_avg"],
        "roic_min": norm["roic_min"],
        "roic_ranked": None if norm["roic_avg"] is None else min(norm["roic_avg"], roic_cap),
        "dilution_per_year": norm["dilution_per_year"],
        "fcf_yield": metrics.fcf_yield(cur.get("free_cash_flow"), mcap),
        "free_cash_flow": cur.get("free_cash_flow"),
        "fscore": fs["score"],
        "fscore_missing": fs["missing"],
        "altman_z": metrics.altman_z(cur, mcap),
        "debt_to_equity": metrics.debt_to_equity(cur),
        "interest_coverage": metrics.interest_coverage(cur),
        "value_base": value.get("base"),
        "value_low": value.get("low"),
        "value_high": value.get("high"),
        "mos_formula": value.get("margin_of_safety"),
        "implied_growth": value.get("implied_growth"),
        "discount_rate": rate,
        "trend": tr.get("trend"),
        "vs_sma200": tr.get("vs_sma200"),
        "momentum_12_1": tr.get("momentum_12_1"),
    }
    row.update({f"f_{k}": v for k, v in fs["points"].items()})
    return row


def failed_filters(row: dict, filters: dict) -> list[str]:
    """The hard filters a row fails; a missing value fails its filter."""
    def below(key, floor):
        v = row.get(key)
        return v is None or v <= floor

    reasons = []
    if below("market_cap", filters["min_market_cap"]):
        reasons.append("market cap")
    if row.get("fscore") is None or row["fscore"] < filters["min_fscore"]:
        reasons.append("F-Score")
    if below("altman_z", filters["min_altman_z"]):
        reasons.append("Altman Z")
    if filters.get("require_positive_fcf") and below("free_cash_flow", 0):
        reasons.append("FCF")
    if below("interest_coverage", filters["min_interest_coverage"]):
        reasons.append("interest coverage")
    if row.get(EY_KEY) is None or row.get(ROIC_KEY) is None:
        reasons.append("no EBIT/EV or ROIC")
    floor = filters.get("min_roic_worst_year")
    if floor is not None and row.get("roic_min") is not None and row["roic_min"] < floor:
        reasons.append("ROIC unstable")
    cap = filters.get("max_dilution_per_year")
    if cap is not None and row.get("dilution_per_year") is not None and row["dilution_per_year"] > cap:
        reasons.append("dilution")
    return reasons


def rank(rows: list[dict], filters: dict, owned: set[str], top_n: int) -> dict:
    """Split evaluated rows into the ranked list, the top picks and the falling knives."""
    passing = [r for r in rows if not r["fail_reasons"]]
    ranked = metrics.magic_formula_rank(passing, EY_KEY, ROIC_KEY)
    for i, r in enumerate(ranked, 1):
        r["mf_position"] = i
    not_owned = [r for r in ranked if r["ticker"] not in owned]
    knives = [r for r in not_owned if r.get("trend") == "FREEFALL"]
    top = [r for r in not_owned if r.get("trend") != "FREEFALL"][:top_n]
    return {"ranked": ranked, "top": top, "knives": knives}


def latest_screen(paths: Paths) -> Path | None:
    files = sorted(paths.screens.glob("screen_????-??-??.csv"))
    return files[-1] if files else None


def top_candidates(paths: Paths, owned: set[str], n: int) -> list[dict]:
    """The best-ranked, not-owned, not-FREEFALL tickers from the latest screen."""
    path = latest_screen(paths)
    if path is None:
        return []
    df = pd.read_csv(path)
    df = df[df["mf_position"].notna()].sort_values("mf_position")
    df = df[~df["ticker"].isin(owned) & (df["trend"] != "FREEFALL")]
    return df.head(n).to_dict("records")


def run(paths: Paths, cfg: dict, owned: set[str], today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    uni = market_data.universe(paths, cfg["universe"]["indexes"])
    excluded_mask = uni["sector"].isin(cfg["universe"]["exclude_sectors"])
    excluded, candidates = uni[excluded_mask], uni[~excluded_mask]

    rate = metrics.current_discount_rate()
    log.info("valuing at a %.1f%% discount rate (10-year Treasury %s)", rate["rate"] * 100,
             "unavailable" if rate["treasury_10y"] is None else f"{rate['treasury_10y']:.2%}")
    rows, skipped = [], []
    total = len(candidates)
    for i, rec in enumerate(candidates.itertuples(index=False), 1):
        if i % 50 == 0 or i == total:
            log.info("screened %d/%d", i, total)
        try:
            data = market_data.fundamentals(rec.ticker, paths, cfg["screen"]["cache_days"])
            if not data.get("from_cache"):
                market_data.throttle(cfg["screen"]["throttle_seconds"])
            row = evaluate(data, cfg["screen"]["normalize_years"], cfg["screen"]["roic_cap"], rate["rate"])
        except market_data.DataUnavailable as exc:
            log.info("skip %s: %s", rec.ticker, exc)
            skipped.append({"ticker": rec.ticker, "reason": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the screen
            log.info("skip %s: %s", rec.ticker, exc)
            skipped.append({"ticker": rec.ticker, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        row.update(name=rec.name, sector=rec.sector, index=rec.index)
        row["fail_reasons"] = failed_filters(row, cfg["filters"])
        rows.append(row)

    try:
        overlap = superinvestors.refresh_if_stale(paths, cfg["superinvestors"])["holdings"]
    except Exception as exc:  # noqa: BLE001 - a second opinion; the screen stands without it
        log.warning("superinvestor holdings unavailable: %s", exc)
        overlap = superinvestors.load(paths)["holdings"]
    superinvestors.annotate(rows, overlap)
    result = rank(rows, cfg["filters"], owned, cfg["screen"]["top_n"])
    positions = {r["ticker"]: r["mf_position"] for r in result["ranked"]}
    for r in rows:
        r["mf_position"] = positions.get(r["ticker"])
        r["passes_filters"] = not r["fail_reasons"]
        r["fail_reasons"] = "; ".join(r["fail_reasons"])

    paths.screens.mkdir(parents=True, exist_ok=True)
    out = paths.screens / f"screen_{today}.csv"
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("mf_position", na_position="last")
    df.to_csv(out.with_suffix(".csv.tmp"), index=False)
    out.with_suffix(".csv.tmp").replace(out)          # whole or absent: the website reads it any time
    _write_rows(paths.screens / f"screen_{today}_skipped.csv", skipped, ["ticker", "reason"])
    _write_rows(paths.screens / f"screen_{today}_excluded.csv",
                excluded[["ticker", "name", "sector"]].to_dict("records"), ["ticker", "name", "sector"])
    return {**result, "path": out, "screened": len(rows), "skipped": skipped, "excluded": excluded}


def _write_rows(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
