# Demo long task: per-tool rate limits and quotas for the MCP Python SDK

Repo: https://github.com/modelcontextprotocol/python-sdk at commit `77001c9` (pinned). Install with `uv sync --all-extras --python 3.12`; run tests with `uv run pytest -q tests/server tests/shared -p no:cacheprovider` (~11 s, 2389 tests). Nothing like this feature exists upstream.

All new code lives under `src/mcp/server/mcpserver/` unless stated. Every subtask must keep the existing suite green. Package import root is `mcp` (e.g. `from mcp.server.mcpserver.limits import RateLimit`). The high-level server class is `MCPServer` in `src/mcp/server/mcpserver/server.py`; tools are `Tool` in `tools/base.py`, managed by `ToolManager` in `tools/tool_manager.py`; request-scoped helpers are on `Context` in `context.py`; the CLI is under `src/mcp/cli/`.

## Subtasks (ordered, each depends on the previous commit)

### S1 — Limit policy model and pure decision logic
File: `src/mcp/server/mcpserver/limits.py` (new)
- `class RateLimit(BaseModel)`: `max_calls: int` (>0), `per_seconds: float` (>0). Sliding window.
- `class Quota(BaseModel)`: `max_calls: int` (>0). Lifetime cap for the scope.
- `class LimitPolicy(BaseModel)`: `rate_limit: RateLimit | None = None`, `quota: Quota | None = None`, `scope: Literal["server", "session"] = "server"`.
- `class LimitDecision(BaseModel)`: `allowed: bool`, `retry_after: float | None` (seconds until the next call would be allowed; `None` when allowed or when a quota is exhausted for good), `remaining: int | None` (calls left in the current window/quota after this decision; the smaller of the two when both apply), `reason: Literal["rate_limit", "quota"] | None`.
- Pure function `evaluate(policy: LimitPolicy, call_times: list[float], now: float) -> LimitDecision`: `call_times` are timestamps of previous allowed calls (seconds, any monotonic clock). Rate window counts calls with `now - t < per_seconds` (strictly less: a call exactly `per_seconds` old has left the window). Quota counts all of `call_times`. If both deny, `reason="quota"`. Negative or non-finite `now`, or `call_times` containing values > `now`, raise `ValueError`. Empty policy (both None) always allows with `remaining=None`.
- `class LimitState`: in-memory tracker. `__init__(self, policy: LimitPolicy)`; `check(self, now: float) -> LimitDecision` (does not record); `record(self, now: float) -> None` (appends and prunes anything outside the rate window when a rate limit exists; quota history is never pruned, keep a counter); `reset(self) -> None`.

### S2 — Attach policies to tools
Files: `tools/base.py`, `tools/tool_manager.py`
- `Tool` gets `limits: LimitPolicy | None = None`.
- `Tool.from_function(...)` gains keyword `limits: LimitPolicy | None = None`.
- `ToolManager.add_tool(...)` gains keyword `limits: LimitPolicy | None = None` and passes it through.
- `ToolManager.get_limits(self, name: str) -> LimitPolicy | None`; `ToolManager.set_limits(self, name: str, policy: LimitPolicy | None) -> None` (raises `ToolError` when the tool is unknown).

### S3 — Enforcement on call
Files: `limits.py` (add `LimitEnforcer`), `tools/tool_manager.py`, `server.py`
- `class LimitEnforcer` in `limits.py`: `__init__(self, clock: Callable[[], float] = time.monotonic)`; `decide(self, tool_name: str, policy: LimitPolicy, scope_key: str | None, now: float | None = None) -> LimitDecision` (server-scope state keyed by tool name, session-scope keyed by `(tool_name, scope_key)`); `record(self, tool_name, policy, scope_key, now=None)`; `remaining(self, tool_name, policy, scope_key, now=None) -> int | None`; `reset(self, tool_name: str | None = None) -> None`.
- `ToolManager.__init__` gains `enforcer: LimitEnforcer | None = None` (default creates one). `ToolManager.call_tool(...)` consults the tool's policy before invoking; on denial raise `ToolError` with the exact message `f"Tool '{name}' {reason_text}"` where `reason_text` is `"rate limit exceeded; retry after {retry_after:.3f}s"` or `"quota exhausted"`. On success, record the call AFTER the tool returns successfully (a failing tool call does not consume the budget). `scope_key` for session scope is the session id from the current request context when available, else `None` (falls back to server scope).
- `MCPServer.tool(...)` decorator and `MCPServer.add_tool(...)` gain `limits: LimitPolicy | None = None`. The low-level `_handle_call_tool` path must surface the `ToolError` as an error `CallToolResult` (the SDK already does this for `ToolError`; keep that behaviour).

### S4 — Client-visible limit metadata and context helpers
Files: `server.py`, `context.py`
- `MCPServer.list_tools()` puts `{"limits": policy.model_dump(mode="json")}` under each tool's `_meta` (`meta` on the `Tool` type) when a policy exists; tools without a policy are unchanged.
- `Context.tool_limits(self, name: str) -> LimitPolicy | None` and `Context.remaining_calls(self, name: str) -> int | None` (uses the enforcer with the current session as scope key).

### S5 — Session scope
Files: `tools/tool_manager.py`, `server.py`, `limits.py`
- `scope="session"` policies track per client session; different sessions never share budget; the same session across many calls does. Session identity comes from the request context's session object (`id(session)` is acceptable). Server scope is unchanged. `LimitEnforcer.forget_scope(self, scope_key: str) -> None` drops all state for a session; `MCPServer` calls it when a session ends if a hook exists, otherwise exposes `MCPServer.forget_session_limits(self, scope_key: str) -> None`.

### S6 — Usage status resource
Files: `server.py`, `limits.py`
- `MCPServer.enable_limits_resource(self, uri: str = "limits://status") -> None` registers a resource that returns JSON: `{"tools": {name: {"policy": {...}, "remaining": int | None, "scope": "server" | "session"}}}` for every tool with a policy, computed for the calling session. `LimitEnforcer.snapshot(self, tools: dict[str, LimitPolicy], scope_key: str | None, now: float | None = None) -> dict` produces that dict.

### S7 — CLI inspection
File: `src/mcp/cli/cli.py` (plus a helper module `src/mcp/cli/limits.py` if cleaner)
- New subcommand `mcp limits FILE[:object]` that imports the server the same way `mcp run`/`mcp dev` resolve a file, and prints one line per tool: `name  rate=<max>/<per>s|-  quota=<max>|-  scope=<server|session>`. Tools without a policy print `-` for both. Exit code 0; unknown file → the CLI's existing error path.
- Pure helper `format_limits_table(tools: list[tuple[str, LimitPolicy | None]]) -> str` so the output is testable without the CLI.

## Held-out feature slices (same repo, same base commit; never used for lessons)

Each is a single subtask with its own spec, interface and reference patch.

- **H1 — per-tool timeout.** `Tool.timeout_seconds: float | None`; `MCPServer.tool(..., timeout_seconds=None)` and `add_tool(...)`; `ToolManager.call_tool` wraps the call in `anyio.fail_after(timeout_seconds)` and converts a timeout into `ToolError(f"Tool '{name}' timed out after {timeout_seconds:g}s")`. `None` means no timeout; `<= 0` raises `ValueError` at registration.
- **H2 — tool deprecation.** `Tool.deprecated: bool | str = False` (a string is the replacement hint); `MCPServer.tool(..., deprecated=False)` and `add_tool`; `list_tools` puts `{"deprecated": true, "replacement": str | None}` in `_meta`; calling a deprecated tool still works but the result's `_meta` carries `{"deprecation_warning": str}`. Pure helper `deprecation_message(name: str, deprecated: bool | str) -> str | None`.
- **H3 — resource size cap.** `MCPServer.add_resource(resource, *, max_bytes: int | None = None)` and `@resource(..., max_bytes=None)`; reading a resource whose content exceeds `max_bytes` raises `ResourceError(f"Resource '{uri}' exceeds max_bytes={max_bytes}")`. Pure helper `content_size(content: str | bytes) -> int` (UTF-8 bytes for str). `max_bytes <= 0` raises `ValueError` at registration.
