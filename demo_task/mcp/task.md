# Per-tool rate limits and quotas for MCPServer

repo: repo

Target: the MCP Python SDK (https://github.com/modelcontextprotocol/python-sdk) at the pinned commit `77001c9`, checked out under `repo/`. Install with `uv sync --all-extras --python 3.12`; the existing suite runs with `uv run pytest -q tests/server tests/shared -p no:cacheprovider` (about 2389 tests, ~11 s). Nothing like this feature exists upstream.

## Goal

Let a server author cap how often each tool on a high-level `MCPServer` may be called. A tool can carry a sliding-window rate limit ("at most N calls per S seconds"), a lifetime quota ("at most N calls in total"), or both, and the budget can be shared by every client of the server or kept separately per client session. Calls over budget are refused with a clear tool error before the tool body runs; clients can see each tool's policy in `tools/list`, tools can ask how much budget is left, an optional resource reports usage, and a CLI command prints the configured limits.

## User-facing API being added

- A new module `mcp.server.mcpserver.limits` (file `src/mcp/server/mcpserver/limits.py`) with pydantic models `RateLimit` (`max_calls`, `per_seconds`), `Quota` (`max_calls`), `LimitPolicy` (`rate_limit`, `quota`, `scope` of `"server"` or `"session"`, default `"server"`) and `LimitDecision` (`allowed`, `retry_after`, `remaining`, `reason`); a pure function `evaluate(policy, call_times, now)` that decides whether one more call is allowed; an in-memory tracker `LimitState`; and `LimitEnforcer`, which owns the state for every tool and scope and reads time from an injectable clock (default `time.monotonic`).
- A `limits=` keyword on tool registration: `@server.tool(..., limits=LimitPolicy(...))`, `server.add_tool(fn, ..., limits=...)`, `ToolManager.add_tool(..., limits=...)` and `Tool.from_function(..., limits=...)`; the policy is stored on `Tool.limits`. `ToolManager.get_limits(name)` and `ToolManager.set_limits(name, policy)` read and change a registered tool's policy.
- In `tools/list`, every tool with a policy carries `{"limits": <policy as JSON>}` in its `_meta`.
- On `Context`: `ctx.tool_limits(name)` and `ctx.remaining_calls(name)`.
- `MCPServer.enable_limits_resource(uri="limits://status")` registers a JSON resource with the current usage of every limited tool, as seen by the calling session.
- `MCPServer.forget_session_limits(scope_key)` drops one session's budgets; session budgets are also dropped automatically when the client connection closes.
- A CLI subcommand `mcp limits FILE[:object]` that prints one line per tool, plus a pure helper `format_limits_table` in `mcp.cli.limits` that produces those lines.

## Behaviour

- Rate limits use a sliding window: a previous call at time `t` is in the window at time `now` exactly when `now - t < per_seconds`. A call exactly `per_seconds` old has left the window. A call is allowed while fewer than `max_calls` calls are in the window.
- A quota counts every recorded call in its scope, forever (until reset). A call is allowed while fewer than `max_calls` calls have been recorded.
- When both limits deny, the reason is the quota (`"quota"`), because waiting cannot help. A rate-limit denial reports `retry_after`, the seconds until the oldest blocking call leaves the window; a quota denial reports no retry time.
- Only calls that complete are counted: the budget is checked before the tool runs, and the call is recorded after the tool returns normally. A call that is refused, has invalid arguments, or raises does not consume budget.
- A refused call raises `ToolError` with exactly `Tool '<name>' rate limit exceeded; retry after <seconds with 3 decimals>s` (for example `Tool 'search' rate limit exceeded; retry after 0.500s`) or `Tool '<name>' quota exhausted`. Over the protocol the client receives a `CallToolResult` with `is_error=True` and that text, the way the SDK already reports any `ToolError`.
- Scope: `scope="server"` budgets are shared by all callers of the tool. `scope="session"` budgets are per client session: different sessions never share budget and repeated calls in one session do. The SDK builds a new `ServerSession` object for every request, so session identity must come from the connection the session wraps (its transport session id when it has one, otherwise the connection object's identity), never from the per-request `ServerSession` object. A call with no session (a direct `MCPServer.call_tool(...)` with no request, or a 2026-07-28 stateless request, which gets a fresh connection each time) falls back to the server-scope budget.
- Tools without a policy behave exactly as before, and their `tools/list` entries are unchanged.
- CLI output is one line per tool in registration order: `<name>  rate=<max_calls>/<per_seconds>s  quota=<max_calls>  scope=<server|session>`, fields separated by two spaces, with `-` for a missing rate limit or quota (a tool with no policy prints `rate=-  quota=-  scope=server`).

## Constraints

- Keep the existing suite green after every subtask (`tests/server`, `tests/shared`, and `tests/cli` for the CLI subtask).
- No new dependencies: standard library, pydantic and what the SDK already uses (anyio, typer for the CLI).
- `limits.py` must not import anything from `mcp.server.mcpserver` (tools/base.py imports it, so a back-import would create a cycle).
- All new parameters default to `None` so existing callers are unaffected; existing positional parameters keep their positions.
- Time is in seconds from a monotonic clock; all decision logic takes `now` explicitly so it can be tested without sleeping.

## Subtasks (in order; each builds on the previous commit)

1. **Limit policy model and pure decision logic**: add `limits.py` with the four pydantic models, the pure `evaluate` function with its exact window and quota boundaries, and the `LimitState` tracker.
2. **Attach policies to tools**: add `Tool.limits`, the `limits=` keyword on `Tool.from_function` and `ToolManager.add_tool`, and `ToolManager.get_limits` / `set_limits`.
3. **Enforcement on call**: add `LimitEnforcer` and the session scope-key helper, make `ToolManager.call_tool` refuse over-budget calls with the exact error messages and record successful calls, and add `limits=` to `MCPServer.tool` and `MCPServer.add_tool`.
4. **Client-visible limit metadata and context helpers**: publish each policy under `_meta["limits"]` in `MCPServer.list_tools()` and add `Context.tool_limits` and `Context.remaining_calls`.
5. **Session scope**: guarantee per-session budgets end to end, add `LimitEnforcer.forget_scope` and `MCPServer.forget_session_limits`, and drop a session's budgets when its connection closes.
6. **Usage status resource**: add `LimitEnforcer.snapshot` and `MCPServer.enable_limits_resource`, a JSON resource computed for the calling session at read time.
7. **CLI inspection**: add the pure `format_limits_table` helper and the `mcp limits FILE[:object]` subcommand that loads a server the way `mcp run` does and prints its tools' limits.
