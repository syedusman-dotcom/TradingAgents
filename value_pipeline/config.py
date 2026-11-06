"""Settings (config.yaml merged over defaults) and where the pipeline keeps its data."""

from __future__ import annotations

import copy
import math
import os
import re
from datetime import date
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PACKAGE_DIR / "config.yaml"

DEFAULTS: dict = {
    "universe": {
        "indexes": [
            "sp500", "sp400", "ftse100", "ftse250", "dax", "mdax", "cac40", "cacnext20", "aex", "amx",
            "bel20", "psi", "iseq20", "ftsemib", "smi", "smim", "atx", "ibex35", "omxs30",
            "omxc25", "omxh25", "obx", "tsxcomp", "sto", "cph", "hel", "ose", "sp600",
        ],
        "exclude_sectors": ["Financials", "Real Estate", "Utilities"],
    },
    "filters": {
        "min_market_cap": 2.0e9,
        "min_fscore": 6,
        "min_altman_z": 1.8,
        "require_positive_fcf": True,
        "min_interest_coverage": 4,
        "min_roic_worst_year": 0.08,
        "max_dilution_per_year": 0.03,
        "small_markets": {"sto": 2.5e8, "cph": 2.5e8, "hel": 2.5e8, "ose": 2.5e8, "sp600": 2.5e8},
        "min_daily_value": 250000,
    },
    "screen": {"top_n": 10, "cache_days": 7, "price_days": 1, "throttle_seconds": 0.3, "normalize_years": 5, "roic_cap": 1.0},
    "review": {
        "candidates": 5,
        "analysts": ["market", "news", "fundamentals"],
        "statement_vendor": "yfinance",
        "cost_per_run": 2.0,
        "max_cost_per_review": 40.0,
        "committee": True,
        "candidate_cooldown_days": 0,
        "min_run_rate": 0.8,
        "candidate_countries": [],
        "candidate_markets": [],
    },
    "watch": {"buy_margin_of_safety": 0.30, "max_rating_age_days": 120},
    "daily": {"enabled": True, "scope": "holdings", "max_reviews": 3, "cooldown_days": 5, "big_move": 0.15,
              "exclude": []},
    "superinvestors": {
        "refresh_days": 7,
        "investors": [
            {"name": "Berkshire (Buffett)", "cik": "0001067983"},
            {"name": "Baupost (Klarman)", "cik": "0001061768"},
            {"name": "Himalaya (Li Lu)", "cik": "0001709323"},
            {"name": "Dalal Street (Pabrai)", "cik": "0001549575"},
            {"name": "Gardner Russo & Quinn (Russo)", "cik": "0000860643"},
            {"name": "Akre Capital (Akre)", "cik": "0001112520"},
        ],
    },
    "sizing": {
        "max_position_weight": 0.10,
        "min_holdings": 5,
        "overweight_size_factor": 0.5,
        "min_trade_amount": 25,
        "max_sector_weight": 0.30,
        "lane_caps": {"compounder": 0.30, "inflection": 0.20},
        "lane_size_factor": {"compounder": 0.5, "inflection": 0.5},
        "lane_buy_only": [],
    },
    # Nordnet USA Index (SEK share class): no commission and no currency fee at Nordnet, 0.20% a year,
    # cash the next bank day; it tracks MSCI USA Climate Change, not the S&P 500. A Xetra ETF such as
    # SXR8.DE costs about 1% per round trip. "" turns parking off.
    "parking": {"ticker": "0P0001K6NL.ST", "share": 0.7, "min_trade_amount": 250},
    "lanes": {"themes": [], "tickers": [], "inflection_candidates": 3, "compounder_candidates": 2,
              "dislocation_candidates": 3},
    "backtest": {"first_year": 2015, "last_year": 2025, "cohort_days": ["03-31", "06-30", "09-30", "12-31"],
                 "holds": [1, 3], "value_top_n": 30, "benchmark": "SPY"},
    "trend": {"start": "2015-07-01", "equity": 10000, "risk_per_trade": 0.01, "stop_n": 2.0, "n_days": 20,
              "max_positions": 10, "max_position_weight": 0.20, "cost_per_side": 0.0035, "market_filter": True},
    # The momentum sleeve test (value_pipeline/momentum.py): today's S&P 500 members from the day they
    # joined the index, a recent period and an earlier hold-out; the real funds are the record.
    "momentum": {"recent_start": "2015-07-01", "holdout_start": "2005-01-03", "holdout_end": "2015-06-30",
                 "equity": 10000, "positions": 10, "check_positions": 20, "cost_per_side": 0.005,
                 "market_filter": True,
                 "funds": [
                     {"fund": "MTUM", "parent": "SPY",
                      "label": "iShares MSCI USA Momentum against the S&P 500 "
                              "(US-listed; not sold to EU retail investors)"},
                     {"fund": "IS3R.DE", "parent": "EUNL.DE",
                      "label": "iShares Edge MSCI World Momentum against iShares Core MSCI World "
                              "(Xetra, at Nordnet)"},
                     {"fund": "QDVA.DE", "parent": "SXR8.DE",
                      "label": "iShares Edge MSCI USA Momentum against iShares Core S&P 500 "
                              "(Xetra, at Nordnet)"}]},
}


def parking_ticker(cfg: dict) -> str | None:
    """The index fund that holds idle money, upper-cased; None when parking is off (blank ticker)."""
    ticker = str((cfg.get("parking") or {}).get("ticker") or "").strip().upper()
    return ticker or None


_REPLACED = {"small_markets"}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        default = out.get(key)
        if key in _REPLACED and isinstance(value, dict):
            # A set of names: a deleted entry stays deleted. PyYAML reads "3.0e8" as a string.
            out[key] = {k: float(v) if isinstance(v, str) else v for k, v in value.items()}
        elif isinstance(value, dict) and isinstance(default, dict):
            out[key] = _merge(default, value)
        elif isinstance(default, (int, float)) and not isinstance(default, bool) and isinstance(value, str):
            # PyYAML reads "2.0e9" (no sign in the exponent) as a string.
            out[key] = type(default)(float(value))
        else:
            out[key] = value
    return out


# Numbers that disable a safeguard at zero: the cost cap would never block, sizes would be nothing.
_POSITIVE = {"cost_per_run", "max_cost_per_review", "max_position_weight", "max_sector_weight", "min_holdings",
             "max_reviews", "min_daily_value", "big_move", "positions", "check_positions", "equity"}


def validate_config(cfg: dict) -> None:
    """Raise ValueError when a section or value has the wrong shape for the pipeline to use."""
    for section, defaults in DEFAULTS.items():
        values = cfg.get(section)
        if not isinstance(values, dict):
            raise ValueError(f"'{section}' must be a section of settings, not {values!r}")
        for key, default in defaults.items():
            value = values.get(key)
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, (int, float)):
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                ok = isinstance(value, type(default))
            if not ok:
                raise ValueError(f"{section}.{key} must be a {type(default).__name__}, not {value!r}")
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{section}.{key} must be a finite number of zero or more, not {value!r}")
                if key in _POSITIVE and value <= 0:
                    raise ValueError(f"{section}.{key} must be more than zero, not {value!r}")
    _validate_small_markets(cfg["filters"]["small_markets"])
    _validate_cohort_days(cfg["backtest"]["cohort_days"])
    for country in cfg["review"]["candidate_countries"]:
        if not isinstance(country, str) or not country.strip():
            raise ValueError(f"review.candidate_countries entries must be country names, not {country!r}")
    _validate_candidate_markets(cfg["review"]["candidate_markets"])
    share = cfg["parking"]["share"]
    if not 0 <= share <= 1:
        raise ValueError(f"parking.share must be between 0 and 1 (a share of the idle money), not {share!r}")


def _validate_candidate_markets(markets) -> None:
    """A list of known market keys."""
    from . import market_data  # not at the top: market_data imports this module

    if not isinstance(markets, list):
        raise ValueError(f"review.candidate_markets must be a list of market keys, not {markets!r}")
    for key in markets:
        if not isinstance(key, str) or key not in market_data.MARKETS:
            raise ValueError(f"review.candidate_markets entries must be market keys "
                             f"({', '.join(market_data.MARKETS)}), not {key!r}")


def _validate_small_markets(floors: dict) -> None:
    """Each key a known market, each floor a positive finite number of dollars."""
    from . import market_data  # not at the top: market_data imports this module

    known = market_data.MARKETS
    for key, floor in floors.items():
        if key not in known:
            raise ValueError(f"filters.small_markets names an unknown market {key!r}")
        if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor) or floor <= 0:
            raise ValueError(f"filters.small_markets.{key} must be a positive number of dollars, not {floor!r}")


def _validate_cohort_days(days: list) -> None:
    """Each cohort day a real ``MM-DD`` (checked in a leap year, so 29 February is allowed)."""
    if not days:
        raise ValueError("backtest.cohort_days must name at least one MM-DD day")
    for day in days:
        ok = isinstance(day, str) and re.fullmatch(r"\d{2}-\d{2}", day) is not None
        if ok:
            try:
                date.fromisoformat(f"2000-{day}")
            except ValueError:
                ok = False
        if not ok:
            raise ValueError(f"backtest.cohort_days entries must be real MM-DD days, not {day!r}")


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, user)
    backtest = user.get("backtest") if isinstance(user, dict) else None
    if isinstance(backtest, dict) and "cohort_day" in backtest and "cohort_days" not in backtest:
        # An older config's single cohort date; the defaults' four days must not override it.
        cfg["backtest"]["cohort_days"] = [backtest["cohort_day"]]
    return cfg


class Paths:
    """Every file the pipeline reads or writes, under one data directory.

    The data directory holds personal holdings, so it is gitignored. It defaults
    to value_pipeline/data and can be moved with --data-dir or VALUE_PIPELINE_DATA.
    """

    def __init__(self, root: Path | str | None = None):
        root = root or os.environ.get("VALUE_PIPELINE_DATA") or PACKAGE_DIR / "data"
        self.root = Path(root)
        self.portfolio = self.root / "portfolio.json"
        self.trades = self.root / "trades.csv"
        self.backups = self.root / "backups"
        self.cache = self.root / "cache"
        self.screens = self.root / "screens"
        self.reports = self.root / "reports"
        self.universe = self.root / "universe.csv"

    def review_dir(self, date: str) -> Path:
        return self.reports / date
