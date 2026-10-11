#!/bin/sh
# PostToolUse hook: lint (and auto-fix) the edited Python file with ruff.
# Deliberately no `ruff format` -- the repo defers formatter adoption (see pyproject.toml).
f=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty')
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
r="${CLAUDE_PROJECT_DIR:-.}/.venv/bin/ruff"
[ -x "$r" ] || r=$(command -v ruff) || exit 0
# Exit 2 sends the remaining (unfixable) diagnostics back to Claude.
out=$("$r" check --fix --quiet "$f" 2>&1) || { echo "$out" >&2; exit 2; }
