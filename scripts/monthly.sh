#!/bin/bash
# Monthly value review: screen the S&P 500 + 400, run the agents on holdings and the
# top candidates, write value_pipeline/data/reports/<date>/ACTIONS.md. Never trades.
#
# launchd starts this every Saturday; it only does the work on the first Saturday
# of the month (day 1-7), because launchd cannot say "first Saturday" itself.
set -euo pipefail

if [ "${1:-}" != "--now" ] && [ "${1:-}" != "--check" ] && [ "$(date +%d)" -gt 7 ]; then
  exit 0
fi

cd "$(dirname "$0")/.."
# launchd starts with a bare PATH; find uv where Homebrew or the installer puts it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

mkdir -p value_pipeline/data/logs
log="value_pipeline/data/logs/monthly_$(date +%Y-%m-%d).log"
echo "=== $(date) ===" >> "$log"
if [ "${1:-}" = "--check" ]; then
  # Proves the scheduled job can reach uv and read the repo, without spending anything.
  uv run python -m value_pipeline --help > /dev/null && echo "check OK: uv and the repo are reachable" >> "$log"
  exit 0
fi
uv run python -m value_pipeline run-all --yes >> "$log" 2>&1
