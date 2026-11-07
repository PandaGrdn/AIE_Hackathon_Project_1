# Per-tool timeout for MCPServer tools

Repository: the MCP Python SDK at commit `77001c9` (package import root `mcp`). Install with
`uv sync --all-extras --python 3.12`; the existing suite (`uv run pytest -q tests/server tests/shared
-p no:cacheprovider`) must stay green. Add no dependencies.

## Goal

A server author can give any tool a time budget. A call that runs longer than the budget is
cancelled and reported to the caller as an ordinary tool error with a fixed message. Tools
registered without a budget behave exactly as before.

## Public API

All changes are in `src/mcp/server/mcpserver/`.

`tools/base.py`, class `Tool` (the pydantic model holding a registered tool):

- New field `timeout_seconds: float | None`, default `None`.
- `Tool.from_function(fn, name=None, title=None, description=None, context_kwarg=None,
  annotations=None, icons=None, meta=None, structured_output=None, timeout_seconds=None) -> Tool`:
  new keyword `timeout_seconds: float | None = None`, stored on the returned `Tool`.

`tools/tool_manager.py`, class `ToolManager`:

- `ToolManager.add_tool(fn, name=None, title=None, description=None, annotations=None, icons=None,
  meta=None, structured_output=None, timeout_seconds=None) -> Tool`: new keyword
  `timeout_seconds: float | None = None`, passed through to `Tool.from_function`.
- `ToolManager.call_tool(name, arguments, context, convert_result=False) -> Any`: signature
  unchanged; enforces the timeout (below).

`server.py`, class `MCPServer`:

- `MCPServer.add_tool(fn, name=None, title=None, description=None, annotations=None, icons=None,
  meta=None, structured_output=None, timeout_seconds=None) -> None`: new keyword
  `timeout_seconds: float | None = None`, passed through to `ToolManager.add_tool`.
- `MCPServer.tool(name=None, title=None, description=None, annotations=None, icons=None, meta=None,
  structured_output=None, timeout_seconds=None)` decorator: new keyword
  `timeout_seconds: float | None = None`, passed through to `MCPServer.add_tool`.

## Behaviour

1. `timeout_seconds=None` (the default) means no timeout: the call runs for as long as the tool
   takes, exactly as today.
2. With a positive `timeout_seconds`, `ToolManager.call_tool` runs the tool (the whole of
   `Tool.run`: argument validation, resolvers and the body) inside `anyio.fail_after(timeout_seconds)`.
3. When the deadline passes, the call raises
   `ToolError(f"Tool '{name}' timed out after {timeout_seconds:g}s")`, where `name` is the tool's
   registered name and `timeout_seconds` is the tool's configured value formatted with `:g`.
   Examples: `0.01` -> `Tool 'slow' timed out after 0.01s`; `2.0` -> `... after 2s`;
   `0.0123456789` -> `... after 0.0123457s`. The message has no prefix (it is not wrapped in
   `Error executing tool ...`).
4. Because `MCPServer.call_tool` delegates to `ToolManager.call_tool`, the same `ToolError` is
   raised there, and a client calling over the protocol receives a `CallToolResult` with
   `is_error=True` whose single text content is exactly that message (the existing
   `MCPServer._handle_call_tool` already turns a `ToolError` into such a result; keep that).
5. A call that finishes within its budget returns its normal result. Every other failure keeps
   today's behaviour and text: a tool that raises `ToolError("nope")` still fails with
   `Error executing tool <name>: nope`; an unknown tool still fails with `Unknown tool: <name>`.
6. Only the server's own deadline produces the timeout message. A tool body that itself raises
   `TimeoutError` is a crash like any other exception (today's `UnexpectedToolError` path); it must
   not be reported as `timed out after`.
7. Each tool has its own budget; one tool's timeout never affects another tool.
8. Sync tools run on a worker thread (existing `FuncMetadata.call_fn`); the timeout cannot
   interrupt a running thread, so it is only guaranteed for async tools. Do not change how sync
   tools are run.

## Registration-time validation

- `timeout_seconds <= 0` (zero or negative, int or float) raises `ValueError` at registration,
  from `Tool.from_function`, `ToolManager.add_tool`, `MCPServer.add_tool` and the
  `MCPServer.tool(...)` decorator. The message must contain `timeout_seconds`, e.g.
  `timeout_seconds must be greater than 0, got 0`.
- A rejected registration registers nothing (`ToolManager.get_tool(name)` stays `None`).
- Any positive value is accepted, however small (e.g. `1e-9`). Ints are accepted (`3` is stored as
  `3` / `3.0`).

## Out of scope

No client-visible metadata for the timeout (`list_tools` output is unchanged), no global default
timeout, no change to the low-level server.
