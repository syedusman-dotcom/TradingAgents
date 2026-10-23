#!/bin/bash
# Value daily check, started by launchd Monday to Friday. Never trades.
#
# Fetches prices (free), and re-runs the agents only on the few holdings and ideas whose
# triggers fired (entry price, reached value, earnings, freefall, big move). Writes
# value_pipeline/data/reports/daily_<date>.md.
#   --check  only prove uv and the repo are reachable (spends nothing)
set -euo pipefail

mode=daily
if [ "${1:-}" = "--check" ]; then
  mode=check
fi

cd "$(dirname "$0")/.."
# launchd starts with a bare PATH; find uv where Homebrew or the installer puts it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

mkdir -p value_pipeline/data/logs
log="value_pipeline/data/logs/daily_$(date +%Y-%m-%d).log"
echo "=== $(date) ($mode) ===" >> "$log"

# A scheduled run has nobody watching: say how it ended on the screen, not only in the log.
notify() { osascript -e "display notification \"$2\" with title \"$1\"" > /dev/null 2>&1 || true; }

case "$mode" in
  check)
    uv run python -m value_pipeline --help > /dev/null && echo "check OK: uv and the repo are reachable" | tee -a "$log" ;;
  daily)
    if ! uv run python -m value_pipeline daily >> "$log" 2>&1; then
      notify "Value daily check failed" "See $log"
      exit 1
    fi ;;
esac
