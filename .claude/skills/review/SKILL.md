---
name: review
description: Run the value_pipeline agent review for one or more tickers from inside Claude Code, relay each agent as it finishes, then summarise the rating, price target, margin of safety and planned action. Nothing is traded.
disable-model-invocation: true
---

Run the agent review for the tickers in `$ARGUMENTS` (comma or space separated, upper-cased).
The agents run on the Claude plan through headless sessions when `.env` sets
`TRADINGAGENTS_LLM_PROVIDER=claude_code`; you watch agents complete, not the model think.

1. From the repo root, refuse if a review is already running:
   `.venv/bin/python -c "from value_pipeline import lock; from value_pipeline.config import Paths; import pathlib; print(lock.held(Paths(pathlib.Path('value_pipeline/data')), 'review'))"`
   prints `True` → say the scheduled run or the website holds the lock and stop.
2. Start it in the background, logging to `value_pipeline/data/logs/review_<today>.log`:
   `.venv/bin/python -m value_pipeline review --tickers <TICKERS> --yes >> value_pipeline/data/logs/review_<today>.log 2>&1`
3. While it runs, read the log every minute or two and relay the new lines that matter:
   `[n/N] TICKER...`, `INFO claude-...: 41s, 23,000 tokens in, 900 out` (one per finished agent call),
   `WARNING usage limit reached ... retrying in 10 minutes`, and any `failed` line.
   Do not paste the whole log.
4. When the process exits, read `value_pipeline/data/reports/<latest folder>/ACTIONS.md` and, for each
   ticker, `<folder>/<TICKER>/complete_report.md`. Report per ticker: rating, price target, margin of
   safety, the planned action and its reason, and the report path. Then stop and let the user ask.
5. If the run failed, show the last 20 log lines and the likely cause (not logged in → `claude login`;
   usage limit → when it resets; a vendor error → which tool).
