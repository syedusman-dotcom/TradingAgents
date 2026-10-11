# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Fork of TauricResearch/TradingAgents (LangGraph multi-agent trading pipeline). Work targets the fork (`origin`); do not open PRs or push to `upstream`.

- The agent prompts are rewritten for long-term quality value investing (see `VALUE_INVESTING.md`). When editing prompts, keep the output section labels, the five rating words, f-string variables and `get_language_instruction()`; parsers and tests depend on them.
- `value_pipeline/` is a personal monthly screen → review → action-list tool on top of the graph (see its README). Run it with `uv run python -m value_pipeline <command>`. It must never place trades. `value_pipeline/data/` holds real holdings and is gitignored; use `--data-dir` with a scratch folder when testing.

## Setup and commands

- Dev env: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"` (Python >=3.11; CI tests 3.11–3.14).
- Tests: `pytest -q`. `addopts` already excludes `-m integration`; run network tests only when asked, with `pytest -m integration`.
- Lint: `ruff check .` (line-length 100, E501 ignored, isort with `combine-as-imports`).
- **Never run `ruff format`** repo-wide. Formatting adoption is deliberately deferred (see pyproject comment); there is no black either.
- CI also runs a 3.12 job with `TZ=America/New_York`, and a clean-install smoke test: `pip install . && python -c "import tradingagents, cli.main"`. That catches runtime deps missing from `pyproject.toml`, which is where all deps live (`requirements.txt` is just `.`).
- CLI: `tradingagents` (Typer app `cli.main:app`). Headless: `tradingagents --ticker NVDA --date 2026-09-23 --analysts market,news,fundamentals --save --no-show`.

## Tests

`tests/conftest.py` blocks all network (sockets + curl_cffi), sets every provider key to `placeholder`, blanks `TRADINGAGENTS_*` env vars, and redirects results/cache/memory to tmp dirs. So:
- New tests must not touch the network unless marked `integration`, and must not assume UTC.
- No real API keys are needed to run the suite.
## Invariants enforced by tests

- Only `tradingagents/dataflows/` may import vendor libraries such as `yfinance` (`test_layering.py`).
- Every agent whose text reaches the report must call `get_language_instruction()` and be listed in `REPORT_AGENTS` (`test_i18n_coverage.py`).
- Prompts are plain text strings; an unproduced report must not render as an empty section (`test_prompt_integrity.py`).
- No look-ahead: data must never leak past the analysis date (lookahead / point-in-time / date-boundary tests).
- Error messages must not leak API keys.
- Vendor failures raise `VendorError` subclasses (`dataflows/errors.py`). A vendor list in `data_vendors`/`tool_vendors` is an exact ordered fallback chain; never silently reroute to unchosen vendors.
- The HTML report (`report_html.py`) must be self-contained: loads nothing, runs nothing.

## Config

- `tradingagents/__init__.py` loads `.env` then `.env.enterprise` on import (existing env vars win).
- `_ENV_OVERRIDES` in `tradingagents/default_config.py` is the single mapping from `TRADINGAGENTS_*` vars to config keys. To expose a new key, add a row there. Values are coerced to the default's type. Setting one also skips the matching interactive CLI prompt.

## Conventions

- Conventional Commits with scope, e.g. `fix(yahoo): read a symbol's news from Yahoo search when its quote feed is empty`. Subjects describe user-visible behaviour in plain English.
- Comments and docstrings explain *why*, in prose.
- User-facing changes get a CHANGELOG.md entry (Keep a Changelog; Added/Models/Fixed sections). The version lives in `tradingagents/__init__.py`.
