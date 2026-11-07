# H1: per-tool timeout (`mcp_tool_timeout`)

Adds `timeout_seconds: float | None = None` to `Tool`, `Tool.from_function`, `ToolManager.add_tool`,
`MCPServer.add_tool` and `@MCPServer.tool(...)`. `ToolManager.call_tool` runs the tool inside
`anyio.fail_after(timeout_seconds)` and turns the deadline into
`ToolError(f"Tool '{name}' timed out after {timeout_seconds:g}s")`.

- `base_commit`: `77001c9`; `reference.patch` applies to bare `77001c9` with `git am` / `git apply`.
- `hidden_tests/test_tool_timeout.py`: 24 tests, real clock with tiny timeouts (0.01 s) against
  tools that sleep 5 s; about 0.3 s total.

Bug patterns shared with the demo task (per-tool rate limits):

- Registration-time validation: `<= 0` raises `ValueError` from every entry point (decorator,
  `add_tool`, `ToolManager.add_tool`, `Tool.from_function`), and nothing gets registered.
- Exact messages: `:g` formatting (`0.0123456789` -> `0.0123457s`), no `Error executing tool` prefix.
- Limit boundaries: `None` means unlimited, any positive value (even `1e-9`) is valid, `0` is not.
- Only the server's own deadline counts: a tool that raises `TimeoutError` itself is a crash, not
  a timeout (same shape as "a failing call does not consume the budget").
