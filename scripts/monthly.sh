#!/bin/bash
# Value pipeline, started by launchd every Saturday. Never trades.
#
# Every Saturday: the full review. Screen the S&P 500 + 400, run the agents on
# holdings and the top candidates (plus lane hints and themes), and write
# value_pipeline/data/reports/<date>/ACTIONS.md. Weekly since 2026-10-24; the
# weekday daily check (scripts/daily.sh) covers the days between.
#   --watch  run the free weekly watch instead (prices only, no agents)
#   --now    kept for old habits: the same as no flag
#   --check  only prove uv and the repo are reachable (spends nothing)
set -euo pipefail

mode=review
if [ "${1:-}" = "--check" ]; then
  mode=check
elif [ "${1:-}" = "--watch" ]; then
  mode=watch
fi

cd "$(dirname "$0")/.."
# launchd starts with a bare PATH; find uv where Homebrew or the installer puts it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

mkdir -p value_pipeline/data/logs
log="value_pipeline/data/logs/monthly_$(date +%Y-%m-%d).log"
echo "=== $(date) ($mode) ===" >> "$log"

# A scheduled run has nobody watching: say how it ended on the screen, not only in the log.
notify() { osascript -e "display notification \"$2\" with title \"$1\"" > /dev/null 2>&1 || true; }

case "$mode" in
  check)
    uv run python -m value_pipeline --help > /dev/null && echo "check OK: uv and the repo are reachable" >> "$log" ;;
  watch)
    if ! uv run python -m value_pipeline watch >> "$log" 2>&1; then
      notify "Value watch failed" "See $log"
      exit 1
    fi ;;
  review)
    if uv run python -m value_pipeline run-all --yes >> "$log" 2>&1; then
      # The free watch after the review keeps the weekly "New ideas" list and its sidecar current.
      uv run python -m value_pipeline watch --no-notify >> "$log" 2>&1 || true
      notify "Value review done" "Open the website or value_pipeline/data/reports for ACTIONS.md"
    else
      notify "Value review failed" "Nothing to act on. See $log"
      exit 1
    fi ;;
esac
