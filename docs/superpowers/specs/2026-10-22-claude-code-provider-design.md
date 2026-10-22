# Claude Code provider for the agents — design

**Date:** 2026-10-22
**Status:** approved in conversation; spec for review

## Goal

Run the TradingAgents review on the user's Claude subscription instead of the
Anthropic API. The API bill is the problem; the review itself is fine. Every
agent call becomes a headless Claude Code session (`claude -p`) that uses the
CLI's own login. Nothing downstream of the model changes: the graph, the
memory log, the report tree, `value_pipeline`'s parsers, action rules, watch,
history and website all keep working as they are.

### What the user said

- The API is too expensive; the subscription has usage to spare.
- The agents are prompts, so they can run as Claude Code agents.
- Reviews must work both interactively (from inside a Claude Code session) and
  headlessly (the website's Run page and the Saturday launchd job).
- On the subscription the models cost nothing extra, so the Claude Code path
  uses **Fable 5.1 for the deep tier** (research manager, portfolio manager)
  and **Opus 5.5 for the quick tier** (analysts, researchers, trader, risk
  debaters, reflection). The API path keeps its own pair (Opus / Sonnet).
- Later upstream merges from TauricResearch/TradingAgents must stay possible.

### Non-goals

- Rebuilding the workflow as native `.claude/agents` with a model as the
  orchestrator (approach B in the discussion). The sequencing stays in code.
- Changing any prompt, schema, report file or action rule.
- The screen and watch commands, which use no LLM.
- The tooling still never places a trade.

## Verified facts (probed on 2026-10-22, Claude Code 2.1.305)

These were tested from inside a Claude Code session with throwaway files; the
implementation plan does not need to re-discover them.

| Fact | Result |
|---|---|
| A headless `claude -p` call from inside a running Claude Code session | Works once the `CLAUDECODE`, `CLAUDE_CODE_*` variables are removed from the child's environment. |
| `--bare` | Drops the login ("Not logged in · Please run /login"). Do not use it. |
| `--setting-sources ""` | Keeps the login and loads no user or project settings or hooks. Use this for isolation. |
| `--tools ""` | Disables every built-in tool; MCP tools from `--mcp-config` stay available. |
| `--mcp-config '<inline json>' --strict-mcp-config --allowedTools mcp__ta__<name> --permission-mode dontAsk` | The CLI starts the stdio server, lists `mcp__ta__<name>`, calls it, and returns the finished text. `env` in the server entry reaches the server process. |
| `--json-schema '<schema>'` | The result JSON carries `structured_output` (a parsed object) as well as `result` (the JSON text). |
| `--model claude-fable-5-1`, `--model opus` | Both accepted; `modelUsage` names the model used. |
| `--system-prompt`, prompt on stdin | Both work; stdin avoids argument-length limits. |
| Result JSON fields used | `is_error`, `subtype`, `result`, `structured_output`, `num_turns`, `duration_ms`, `total_cost_usd`, `usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`, `modelUsage`. |
| `ANTHROPIC_API_KEY` in the child environment | Must be removed, or the CLI bills the key instead of the subscription. |
| The `mcp` Python package | 2.x on PyPI (2.3.0 today). `FastMCP` was renamed; the server class is `from mcp.server.mcpserver import MCPServer`. |

Not yet verified (the plan includes a step for each): the exact text of the
usage-limit error in `-p` mode, and whether a `CLAUDE.md` is read when
`--setting-sources` is empty (the child runs in an empty temporary directory
either way).

## Architecture

One new provider, `claude_code`, registered in the existing LLM client
factory. It returns a LangChain chat model whose three operations map to CLI
runs:

| Graph needs | Today (API) | With `claude_code` |
|---|---|---|
| `invoke(messages)` free text | one API call | one `claude -p` run; `result` becomes the `AIMessage` content |
| `bind_tools(tools)` then `invoke` (the three data analysts, up to 20 tool rounds) | LangGraph `ToolNode` loop, one API call per round | one `claude -p` run with the repo's data tools exposed over a local MCP server; the CLI runs the tool loop; the returned `AIMessage` has no `tool_calls`, so the graph's loop ends after one round and the content is the report |
| `with_structured_output(schema)` (research manager, trader, portfolio manager) | API structured output | `--json-schema` with the Pydantic schema; `structured_output` validated into the schema; any failure raises so the existing free-text fallback runs |

Everything else in the graph is unchanged. The end-to-end test's
`ScriptedModel` already proves this is the full interface the graph needs.

### Components

**New files**

- `tradingagents/llm_clients/claude_code_client.py`
  - `ChatClaudeCode(BaseChatModel)`: the chat model. Fields: `model`,
    `timeout_seconds` (default 900), `max_turns` (default 30 when tools are
    bound, 2 otherwise), `wait_minutes` (usage-limit wait, default 360),
    `effort` (optional), `runner` (the function that runs the CLI; tests
    inject a fake). `set_context(company, trade_date, tool_vendors)` stores
    what the MCP server needs. `bind_tools(tools)` returns a copy with the
    tool list set. `with_structured_output(schema)` returns a runnable that
    runs with `--json-schema` and returns an instance of `schema`.
  - `ClaudeCodeClient(BaseLLMClient)`: `get_llm()` builds the model; it
    checks `claude auth status` once per client and raises a clear error
    ("Claude Code is not logged in: run `claude login`") if the CLI is
    missing or logged out.
  - `UsageLimitReached(RuntimeError)` and `ClaudeCodeError(RuntimeError)`.
- `tradingagents/mcp_server.py`: a stdio MCP server, started by the CLI per
  analyst call as `<sys.executable> -m tradingagents.mcp_server`. It reads
  from its environment `TA_COMPANY`, `TA_TRADE_DATE`, `TA_TOOLS` (comma
  separated tool names) and `TA_TOOL_VENDORS` (JSON, the graph's
  `tool_vendors`), applies the vendors to the dataflows config, and registers
  one MCP tool per named LangChain tool from `tradingagents.agents.tools`
  with the same name, the tool's description, and its non-injected
  arguments (LangChain's `tool_call_schema`, which already excludes
  `InjectedState` parameters). Each call fills the injected parameters from
  the environment and runs `tool.invoke(args)`, exactly as `ToolNode` does.
  A tool error returns its message as the tool result, as the graph does
  today.
- `tradingagents/llm_clients/usage.py`: `UsageTally(BaseCallbackHandler)`,
  summing `usage_metadata` tokens and `response_metadata["total_cost_usd"]`
  per run. (The CLI's `cli/stats_handler.py` does the same for the terminal
  UI; it stays as is.)
- `.claude/skills/review/SKILL.md`: the interactive `/review TICKER` skill
  (see below).
- `tests/test_claude_code_client.py`, `tests/test_mcp_server.py`, and one
  network test in `tests/test_claude_code_integration.py` marked
  `integration`.

**Edits to existing files, kept to registrations**

- `tradingagents/llm_clients/factory.py`: one branch in `create_llm_client`
  for `"claude_code"`; `build_llm_kwargs` passes `effort`, `timeout`,
  `wait_minutes` for it; `create_tier_client` picks the model from
  `claude_code_<tier>_model` when the tier's provider is `claude_code`.
- `tradingagents/default_config.py`: keys `claude_code_deep_model`
  (`claude-fable-5-1`), `claude_code_quick_model` (`claude-opus-5-5`),
  `claude_code_wait_minutes` (360), `claude_code_timeout_seconds` (900), each
  with its `TRADINGAGENTS_*` environment override.
- `tradingagents/llm_clients/validators.py`: `claude_code` accepts any model
  string (aliases like `opus` and `fable` are valid CLI inputs).
- `tradingagents/llm_clients/api_key_env.py`: `claude_code` needs no key.
- `tradingagents/graph/trading_graph.py`: `propagate` calls `set_context`
  on each tier model that has it (duck-typed; the API models are untouched).
  `review.py` passes a `UsageTally` through the existing `callbacks` extra.
- `cli/prompts.py` and `cli/selections.py`: a "Claude Code (your Claude
  subscription)" entry in the provider list.
- `value_pipeline/review.py`, `render.py`, `web/views.py`: measured usage in
  results and the ACTIONS report (see Cost below).
- `pyproject.toml`: `mcp>=2,<3` as a dependency.
- `.env.example`, `README.md`, `value_pipeline/README.md`,
  `VALUE_INVESTING.md`: the new provider and its settings.

## One agent call, end to end

1. The agent code calls `llm.invoke(...)` as today.
2. `ChatClaudeCode` turns the messages into a system prompt (all `system`
   messages joined) and a user prompt. A single human message is sent as is.
   A longer history (system, human, AI, human) is written out as a transcript
   with `Human:` and `Assistant:` labels, ending with the last human turn;
   the three analysts never reach this path because the CLI runs their tool
   loop, and the wrap-up path of `turn.py` is unreachable for the same
   reason.
3. It runs, with the prompt on stdin:

   ```
   claude -p --output-format json --model <model> --system-prompt <system>
          --tools "" --no-session-persistence --setting-sources ""
          --permission-mode dontAsk --max-turns <n> [--effort <level>]
          [--json-schema <schema>]
          [--mcp-config <inline json> --strict-mcp-config
           --allowedTools mcp__ta__<tool> ...]
   ```

   in a fresh empty temporary directory, with an environment copied from the
   process minus every `CLAUDECODE*` / `CLAUDE_CODE_*` variable and minus
   `ANTHROPIC_API_KEY`. The inline MCP config is
   `{"mcpServers": {"ta": {"command": sys.executable, "args": ["-m",
   "tradingagents.mcp_server"], "env": {TA_COMPANY, TA_TRADE_DATE, TA_TOOLS,
   TA_TOOL_VENDORS}}}}`.
4. It parses the result JSON into an `AIMessage` whose `usage_metadata` has
   `input_tokens` (input plus cache creation plus cache read),
   `output_tokens` and `total_tokens`, and whose `response_metadata` has
   `total_cost_usd`, `model`, `num_turns` and `duration_ms`. For structured
   calls it validates `structured_output` into the schema.
5. It logs one INFO line per completed call: tier, model, seconds, tokens.
   The website's job log and the Saturday log show these lines, and the
   `/review` skill tails them.

### Errors

| Condition | Behaviour |
|---|---|
| CLI missing or not logged in | `ClaudeCodeError` at `get_llm()`, before any review work starts; the message says to run `claude login`. |
| Result `is_error` with a usage-limit message (matched case-insensitively on "usage limit", "rate limit", "limit reached", "resets"; the plan pins the real text) | `UsageLimitReached`. The model itself waits and retries the same call: 10 minutes between attempts, logging "usage limit reached, retrying at HH:MM" each time, up to `wait_minutes`; then it raises. Waiting inside the call keeps the graph's progress for that ticker. `review.py`'s existing retry-once-then-REVIEW handles the final failure. |
| Other `is_error`, non-zero exit, invalid JSON | `ClaudeCodeError` with the result text or the last lines of stderr. |
| Timeout | the child is killed; `ClaudeCodeError("timed out after N s")`. |
| `max_turns` reached on a tool call | the CLI returns what it has; the content is the report, as the wrap-up turn does today. |
| Structured output missing or invalid | raises; `invoke_structured_or_freetext` falls back to a free-text call as today. |

### Concurrency

The three analysts run on threads, so up to three CLI processes and three
MCP server processes run at once. Each MCP server imports `tradingagents`
(a few seconds). That overhead is small against calls that take a minute,
and the throttle in `value_pipeline` stays per ticker.

## Models and settings

```
TRADINGAGENTS_LLM_PROVIDER=claude_code
TRADINGAGENTS_CLAUDE_CODE_DEEP_MODEL=claude-fable-5-1     # default
TRADINGAGENTS_CLAUDE_CODE_QUICK_MODEL=claude-opus-5-5     # default
TRADINGAGENTS_CLAUDE_CODE_WAIT_MINUTES=360                # 0 = fail at once
TRADINGAGENTS_CLAUDE_CODE_TIMEOUT_SECONDS=900
```

`TRADINGAGENTS_DEEP_THINK_LLM` / `QUICK_THINK_LLM` keep their API pair
(Opus / Sonnet), so switching the provider back is one line and never
changes the other pair. `anthropic_effort`, when set, is passed as
`--effort`. `temperature`, `max_tokens` and `llm_max_retries` do not apply
to the CLI and are ignored for this provider, which the README says.

## Cost and usage

With the subscription there is no dollar cost per run, but the plan has a
rolling usage limit, and the CLI reports what each call would have cost at
API prices. The review records both:

- `UsageTally` is attached per review and read after each ticker;
  `RunResult` and `results.json` gain `tokens_in`, `tokens_out` and
  `nominal_cost_usd`.
- The ACTIONS report keeps its "Cost of this review:" line so the website's
  parser keeps working, and the text becomes, for this provider,
  `Cost of this review: ~$12.40 nominal (N tokens in, M out; billed to your
  Claude plan, not the API)`. For the API provider the line is unchanged.
- `cost_per_run` and `max_cost_per_review` keep their meaning as the
  pre-run estimate and cap; the docs say the cap is a usage budget, not a
  bill, under `claude_code`.

## The `/review` skill

`.claude/skills/review/SKILL.md`, user-invoked (`disable-model-invocation:
true`), argument: one or more tickers. It:

1. Refuses if the review lock is held (the same check as the Run page).
2. Runs `.venv/bin/python -m value_pipeline review --tickers <tickers>` in the
   background from the repo root, logging to
   `value_pipeline/data/logs/review_<date>.log`.
3. Tails the log and relays the per-call lines (which agent finished, on
   which model, in how long) and any usage-limit waits.
4. When the run ends, reads `complete_report.md` for each ticker and gives
   the rating, price target, margin of safety and the planned action, with
   the report path, so the user can question the result in the same
   session.

What the user watches is agents completing, not the model thinking; the
spec says so in the skill's description.

## Upstream merges

The fork already merges from `upstream/main` (two merge commits in history).
On 2026-10-22 upstream is 11 commits ahead (release 0.7.0: a ChatGPT-plan
provider that signs in with its own credential, a memory scoring fix, a CLI
update check). Only `pyproject.toml`, `default_config.py` and the end-to-end
test are touched on both sides. **The implementation plan starts by merging
`upstream/main`**, so the new provider is built on the 0.7.0 factory and the
merge is done while the conflict surface is three files.

This change is designed to keep later merges cheap too:

- All substantive code is in new files that upstream does not have.
- Edits to shared files are one-line registrations in `factory.py`,
  `validators.py`, `api_key_env.py`, `default_config.py`, the CLI provider
  list, and one duck-typed hook in `trading_graph.propagate`. A conflict in
  any of them is a one-line re-add.
- Nothing in the agents' prompts, schemas or graph setup changes.

If upstream later changes the chat-model interface the graph relies on
(`invoke`, `bind_tools`, `with_structured_output`), the end-to-end test's
scripted model and the new unit tests both fail, which is the signal to
adapt the client.

## Testing

- Unit (`tests/test_claude_code_client.py`), with a fake runner injected:
  prompt and system-prompt assembly from each message shape; the flag list
  for plain, tool and structured calls; the MCP config JSON; the child
  environment has no `ANTHROPIC_API_KEY` and no `CLAUDECODE*` variable;
  usage metadata and nominal cost on the message; structured output
  validated, and invalid output raising; usage-limit wait with a fake clock
  and `wait_minutes=0` failing at once; timeout; not-logged-in at
  `get_llm()`.
- Unit (`tests/test_mcp_server.py`): the registered tools match `TA_TOOLS`
  with injected parameters removed from their schemas; a call fills
  `company_of_interest` and `trade_date` from the environment; a tool error
  comes back as text.
- Existing suites: provider registry, tier providers and the end-to-end
  graph test gain `claude_code` cases where they enumerate providers.
- `value_pipeline` tests: results carry measured usage; the ACTIONS cost
  line renders both forms; the web cost parser reads the new line.
- Integration (`-m integration`, network, uses the subscription): one plain
  call, one structured call, one MCP tool call through the real CLI.
- The verify skill's four checks (ruff, pytest, the New York timezone pass,
  the clean-install smoke test) must pass; the smoke test catches the new
  `mcp` dependency.

## Rollout

1. Merge; `.env` gets `TRADINGAGENTS_LLM_PROVIDER=claude_code`.
2. Restart the website (it runs reviews in-process and reads `.env` at
   start).
3. Run `/review HRB` once interactively and compare its report with the
   2026-10-15 API run for the same ticker.
4. Leave the Saturday job as it is; it picks up `.env`. The launchd job's
   environment must be able to find `claude` (`~/.local/bin`) — the plan
   adds the path to `scripts/monthly.sh`.
