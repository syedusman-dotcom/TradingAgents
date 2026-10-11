"""Settings (config.yaml merged over defaults) and where the pipeline keeps its data."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PACKAGE_DIR / "config.yaml"

DEFAULTS: dict = {
    "universe": {
        "indexes": ["sp500", "sp400"],
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
    },
    "screen": {"top_n": 10, "cache_days": 7, "throttle_seconds": 0.3, "normalize_years": 5, "roic_cap": 1.0},
    "review": {
        "candidates": 5,
        "analysts": ["market", "news", "fundamentals"],
        "statement_vendor": "yfinance",
        "cost_per_run": 2.0,
        "max_cost_per_review": 40.0,
    },
    "watch": {"buy_margin_of_safety": 0.30},
    "sizing": {
        "max_position_weight": 0.10,
        "min_holdings": 5,
        "overweight_size_factor": 0.5,
        "min_trade_amount": 25,
        "max_sector_weight": 0.30,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        default = out.get(key)
        if isinstance(value, dict) and isinstance(default, dict):
            out[key] = _merge(default, value)
        elif isinstance(default, (int, float)) and not isinstance(default, bool) and isinstance(value, str):
            # PyYAML reads "2.0e9" (no sign in the exponent) as a string.
            out[key] = type(default)(float(value))
        else:
            out[key] = value
    return out


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


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if path.exists():
        return _merge(DEFAULTS, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return copy.deepcopy(DEFAULTS)


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
