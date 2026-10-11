---
name: verify
description: Run the same checks CI runs (ruff lint, pytest, non-UTC timezone pass, clean-install import smoke test) before claiming a change is done or committing.
---

Run these from the repo root, using `.venv` if it exists (`source .venv/bin/activate`). Run all of them, even if one fails, then report each result with the relevant failure output.

1. Lint: `ruff check .` (do NOT run `ruff format`).
2. Tests: `pytest -q`
3. Non-UTC timezone pass (CI runs one job this way): `TZ=America/New_York pytest -q`
4. Clean-install smoke test, which catches runtime deps missing from pyproject.toml:
   ```
   tmp=$(mktemp -d) && python -m venv "$tmp/venv" && "$tmp/venv/bin/pip" install -q . && (cd "$tmp" && "$tmp/venv/bin/python" -c "import tradingagents, cli.main") ; rm -rf "$tmp"
   ```

Integration tests (`-m integration`) hit the network. Run them only if the user asks.

For a failure, show the failing test or lint rule and the file:line. Don't claim success unless all four passed.
