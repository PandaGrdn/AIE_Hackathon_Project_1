# Reference solution: per-tool rate limits and quotas

Seven `git format-patch` patches, one per subtask, that apply in order on top of
MCP Python SDK commit `77001c9`. Each patch leaves the whole existing suite
green and adds its own test module under
`tests/server/mcpserver/test_limits_sN_<slug>.py`.

```
./apply.sh /path/to/python-sdk            # git am each patch (fallback: git apply --3way), then run the suite
./apply.sh /path/to/python-sdk --no-test  # apply only
```

The spec is `../subtasks.json` (each subtask's `spec` and `interface`). Where it
differs from `../CONTRACT.md`, these patches follow `subtasks.json`. The
differences are listed at the end.

## Test counts

`uv run pytest -q tests/server tests/shared -p no:cacheprovider` (Python 3.12):

| state | passed | new |
|---|---|---|
| 77001c9 (base) | 2389 | |
| + 01-limits-model | 2446 | +57 |
| + 02-tool-policies | 2457 | +11 |
| + 03-enforcement | 2501 | +44 |
| + 04-metadata-context | 2515 | +14 |
| + 05-session-scope | 2529 | +14 |
| + 06-status-resource | 2545 | +16 |
| + 07-cli-limits | **2567** | +22 |

`tests/cli` also stays green (2597 passed for `tests/server tests/shared tests/cli`
on the final state). `ruff check src tests` and `ruff format --check` pass after
every patch, and `pyright src/mcp` plus the seven new test modules report
0 errors.

## Patches

**01-limits-model.patch (S1).** Adds the new `src/mcp/server/mcpserver/limits.py`,
which imports only the standard library and pydantic. It defines the pydantic
models `RateLimit` (`max_calls > 0`, `per_seconds > 0` and finite), `Quota`,
`LimitPolicy` and `LimitDecision`. It also adds the pure `evaluate(policy,
call_times, now)` function. Validation runs first, for every policy:
- `now` must be finite and `>= 0`.
- No call time may be later than `now`, and no call time may be non-finite.

The rate window is strict: `now - t < per_seconds`, so a call exactly
`per_seconds` old has left the window. The quota counts every call. When both
limits deny, the quota wins. `remaining` counts the calls left after this one,
and is 0 on a denial. `retry_after` is measured from the in-window timestamp at
index `in_window - max_calls`. `LimitState` keeps rate-window timestamps only,
pruned on `record`. The quota is a counter that is never pruned. If time goes
backwards, `LimitState` raises `ValueError`. Tests are in
`tests/server/mcpserver/test_limits_s1_model.py`.

**02-tool-policies.patch (S2).** Changes `tools/base.py` and
`tools/tool_manager.py`:
- `Tool.limits: LimitPolicy | None`.
- `Tool.from_function(..., *, limits=None)` and `ToolManager.add_tool(..., *, limits=None)`.
- `ToolManager.get_limits(name)` returns `None` for an unknown tool.
- `ToolManager.set_limits(name, policy)` raises `ToolError("Unknown tool: {name}")` for an unknown tool.

A duplicate `add_tool` still returns the existing tool, and its `limits`
argument is ignored. There is no enforcement yet. Tests are in
`test_limits_s2_tool_policies.py`.

**03-enforcement.patch (S3).** Changes `limits.py`, `tools/tool_manager.py` and
`server.py`.
- `LimitEnforcer(clock=time.monotonic)` provides `decide`, `record`,
  `remaining` and `reset`.
  - State is keyed by `tool_name` for server scope and by
    `(tool_name, scope_key)` for session scope. A session policy with
    `scope_key=None` falls back to the server key.
  - A changed policy for a key starts from an empty history.
  - `remaining()` is the number of calls you could make right now, which is
    `decision.remaining + 1`, or 0 on a denial.
- A module-level `session_scope_key(context)` in `tools/tool_manager.py`
  returns:
  - `None` when there is no request context, or when the connection speaks a
    2026-07-28 protocol version (stateless; a fresh `Connection` per request);
  - otherwise `connection.session_id` when it is a `str`;
  - otherwise `f"connection:{id(connection)}"`.

  The connection is read from the per-request `ServerSession`'s `_connection`.
  The function never uses `id(session)`.
- `ToolManager(..., *, enforcer=None)` stores the enforcer on the public
  attribute `self.enforcer`, which is read at call time.
- `call_tool` decides before running the tool. A denied call raises
  `ToolError` with exactly `Tool '<name>' rate limit exceeded; retry after
  <x.xxx>s` or `Tool '<name>' quota exhausted`.
  - The call is recorded, at completion time from the enforcer's clock, only
    after `tool.run` returns normally.
  - Raised errors and `InputRequiredResult` rounds use no budget.
- `remove_tool` resets that tool's budget.
- `MCPServer.tool(..., *, limits=None)` and `MCPServer.add_tool(..., *, limits=None)`
  pass the policy through. The existing `_handle_call_tool` turns the
  `ToolError` into an `is_error=True` result.

Tests are in `test_limits_s3_enforcement.py`. They use a fake clock and
in-process `Client(server)`.

**04-metadata-context.patch (S4).** Changes `server.py` and `context.py`.
`MCPServer.list_tools()` publishes `_meta = {**tool.meta, "limits":
policy.model_dump(mode="json")}` for every tool that has a policy, including an
empty `LimitPolicy()`. It never mutates the tool's own `meta`. `"limits"`
overrides a user key of the same name. A tool without a policy keeps exactly
`tool.meta`, including `None`. `Context.tool_limits(name)` and
`Context.remaining_calls(name)` use the same scope key a call from that context
would use. Inside a tool body, the current call is not counted yet. Tests are in
`test_limits_s4_metadata.py`.

**05-session-scope.patch (S5).** Changes `limits.py`, `tools/tool_manager.py`
and `server.py`. `LimitEnforcer.forget_scope(scope_key)` drops every
`(tool, scope_key)` state. The first session-scoped call that has a non-`None`
key registers a callback on `connection.exit_stack`. The runner unwinds that
stack when the client connection closes, and the callback then calls
`self.enforcer.forget_scope(key)`. A marker in `connection.state` keeps this to
at most one registration per connection. `MCPServer.forget_session_limits(scope_key)`
is the manual way to drop a session's budgets. Tests are in
`test_limits_s5_session_scope.py`. They cover isolation between two
`Client(server, mode="legacy")` connections and budget sharing within one
session. They also check that server scope stays shared, that sessionless calls
use server scope, and that the disconnect cleanup runs.

**06-status-resource.patch (S6).** Changes `limits.py` and `server.py`.
`LimitEnforcer.snapshot(tools, scope_key, now=None)` returns
`{"tools": {name: {"policy", "remaining", "scope"}}}` and records nothing.
`MCPServer.enable_limits_resource(uri="limits://status")` registers a static
`FunctionResource` named `limits_status` with mime type `application/json`.
`MCPServer.read_resource(uri, context)` intercepts that URI and builds the JSON
when the resource is read, for `session_scope_key(context)`. A static resource
cannot receive a Context, which is why the read happens there. A direct read
without a context uses scope key `None`. Tests are in
`test_limits_s6_status_resource.py`.

**07-cli-limits.patch (S7).** Adds `src/mcp/cli/limits.py` with the pure
`format_limits_table(tools)`. It prints one line per tool:
`name  rate=<max>/<per>s|-  quota=<max>|-  scope=<scope>`.
- Fields are separated by exactly two spaces, with no padding and no trailing
  newline. An empty list gives `""`.
- A whole-number `per_seconds` prints without `.0`.
- A tool with no policy prints `scope=server`.

`src/mcp/cli/cli.py` gains the `mcp limits FILE[:object]` command. It calls
`_parse_file_path` and then `_import_server`, as `mcp run` does, and prints
the table with `typer.echo`. It prints nothing for a server with no tools, and
it never runs the server. Tests are in `test_limits_s7_cli_limits.py`, using
`typer.testing.CliRunner` against temporary server files.

## Interpretations and deviations (named, not silent)

Deltas from CONTRACT.md, all taken from `subtasks.json`:
1. **Session identity.** `ServerSession` is rebuilt for every request
   (`runner.py` `_make_context`), so `id(session)` would give each call its own
   budget. The key comes from the wrapped `Connection` instead. On the
   stateless 2026-07-28 protocol, `session_scope_key` returns `None`. That
   protocol is what the default `Client(server)` speaks, so on it session
   policies fall back to server scope.
2. **`session_scope_key` lives in `tools/tool_manager.py`.** `limits.py` imports
   nothing from `mcp.server.mcpserver`.
3. **Session end.** A hook does exist (`Connection.exit_stack`), so it is used.
   `MCPServer.forget_session_limits` is still exposed as well.
4. **S6.** The limits URI is handled inside `MCPServer.read_resource`. A static
   resource cannot take a Context.
5. **Enforcer injection.** `MCPServer.__init__` is unchanged. Tests swap
   `server._tool_manager.enforcer = LimitEnforcer(clock=fake)`. There is no new
   `MCPServer` keyword.
6. **Recording.** A changed policy resets that key's history. `remove_tool`
   resets the tool's budget. A call is recorded at its completion time.
   `InputRequiredResult` rounds use no budget.

Choices where the spec leaves room:
- **Private access.** `ServerSession._connection` is read with
  `getattr(..., "_connection", None)` plus an `isinstance(..., Connection)`
  check, so a session object without it gives `None`. `Context` reaches the
  tool manager through `self.mcp_server._tool_manager`, with
  `# pyright: ignore[reportPrivateUsage]`, as the spec says.
- **Hook registration.** The marker in `connection.state` is keyed by the
  `ToolManager`'s `id()`, so two servers on one connection would each register
  once. The callback reads `self.enforcer` when the connection closes, so a
  swapped enforcer is the one that gets cleaned up. The hook is registered
  before the limit decision, so a denied first call also registers it, which is
  harmless.
- **Concurrency.** In-flight calls do not reserve budget. Because recording
  happens after success, N concurrent calls can all pass the check. The spec
  accepts this as a known limitation.
- **`LimitState` extras.** It also has read-only `total_calls` and
  `window_calls` properties, which the tests use. `evaluate` additionally
  rejects non-finite call times, as the spec asks.
- **`enable_limits_resource`.** Calling it again with the same URI returns
  early, so there is no duplicate-resource warning. If a different resource was
  already registered at that URI, the ResourceManager keeps it and the URI is
  not intercepted.
- **S2 test omitted.** The "limits do not leak into `list_tools`" check for S2
  is not a test. S4 deliberately changes that output, and every patch must
  leave the suite green.
- **S7 test location.** The CLI tests live in `tests/server/mcpserver/` as
  instructed, not in `tests/cli/`. They set `propagate = False` on the `cli`
  logger. Otherwise pytest's live-logging root handler (`log_cli = true`)
  conflicts with `CliRunner`'s stream swap on the error paths ("I/O operation
  on closed file"). Those tests therefore assert the exit code (1) and an empty
  stdout, not the log text.
- **`apply.sh` uses `uv run python -m pytest`.** A `cp -R` copy of a checkout
  keeps a `.venv` whose console scripts have shebangs pointing at the original
  checkout. `uv sync` repairs the editable `.pth`, but not those scripts.
