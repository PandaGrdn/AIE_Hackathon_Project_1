# Tool deprecation for MCPServer tools

Repository: the MCP Python SDK at commit `77001c9` (package import root `mcp`). Install with
`uv sync --all-extras --python 3.12`; the existing suite (`uv run pytest -q tests/server tests/shared
-p no:cacheprovider`) must stay green. Add no dependencies.

## Goal

A server author can mark a tool as deprecated, optionally naming the tool that replaces it.
Deprecated tools keep working. Clients can see the deprecation when listing tools, and every
successful call to a deprecated tool carries a warning in the result's metadata. Tools that are
not deprecated look and behave exactly as before.

## Public API

All changes are in `src/mcp/server/mcpserver/`.

Pure helper in `tools/base.py` (`from mcp.server.mcpserver.tools.base import deprecation_message`
must work; re-exporting it from the `mcp.server.mcpserver.tools` package is optional):

- `deprecation_message(name: str, deprecated: bool | str) -> str | None`
  - `deprecated is False` -> `None`
  - `deprecated is True` -> `f"Tool '{name}' is deprecated"`
  - `deprecated` is a string (the replacement hint) -> `f"Tool '{name}' is deprecated; use '{deprecated}' instead"`
    (name and hint used verbatim, e.g. `deprecation_message("get-v1", "get v2")` ->
    `Tool 'get-v1' is deprecated; use 'get v2' instead`)
  - an empty or whitespace-only string raises `ValueError` (message must contain `deprecated`)

`tools/base.py`, class `Tool`:

- New field `deprecated: bool | str`, default `False`. A string is the replacement hint.
- `Tool.from_function(fn, name=None, title=None, description=None, context_kwarg=None,
  annotations=None, icons=None, meta=None, structured_output=None, deprecated=False) -> Tool`:
  new keyword `deprecated: bool | str = False`, stored on the returned `Tool`.

`tools/tool_manager.py`:

- `ToolManager.add_tool(fn, name=None, title=None, description=None, annotations=None, icons=None,
  meta=None, structured_output=None, deprecated=False) -> Tool`: new keyword, passed through.

`server.py`, class `MCPServer`:

- `MCPServer.add_tool(fn, name=None, title=None, description=None, annotations=None, icons=None,
  meta=None, structured_output=None, deprecated=False) -> None`: new keyword, passed through.
- `MCPServer.tool(name=None, title=None, description=None, annotations=None, icons=None, meta=None,
  structured_output=None, deprecated=False)` decorator: new keyword, passed through.
- `MCPServer.list_tools() -> list[mcp_types.Tool]` and
  `MCPServer.call_tool(name, arguments, context=None) -> CallToolResult | InputRequiredResult`:
  signatures unchanged, behaviour below.

## Behaviour

Listing (`MCPServer.list_tools`, and therefore the `tools/list` response):

1. A deprecated tool's listed `_meta` (the `meta` field of `mcp_types.Tool`, alias `_meta`) contains
   `"deprecated": True` and `"replacement"`: the hint string, or `None` when `deprecated=True`.
2. These keys are merged into the tool's own `meta` (passed at registration); the other keys are
   kept, and the deprecation keys win on a clash. The registered `Tool.meta` dict and the dict
   the author passed in are never mutated.
3. A tool that is not deprecated is listed exactly as today: its `_meta` is its own `meta`
   (`None` when it has none); no `deprecated` key appears.

Calling (`MCPServer.call_tool`, and therefore `tools/call`):

4. A deprecated tool still runs and returns its normal content and structured content.
5. When the call succeeds with a `CallToolResult`, the result's `_meta` contains
   `"deprecation_warning": deprecation_message(name, tool.deprecated)` where `name` is the tool's
   registered name. Keys already present in the result's `_meta` (e.g. a tool that returns its own
   `CallToolResult(..., _meta={...})`) are kept. Do not mutate a `CallToolResult` object the tool
   returned; return a copy.
6. A non-deprecated tool's result `_meta` is unchanged (`None` from `MCPServer.call_tool` when the
   tool set none). Note that over the wire the server adds its own keys (such as
   `io.modelcontextprotocol/serverInfo`) to result `_meta`; that is existing behaviour.
7. A failing call is unchanged: a deprecated tool that raises `ToolError("bad input")` still fails
   with `Error executing tool <name>: bad input`, and the error result carries no
   `deprecation_warning`. An `InputRequiredResult` is passed through unchanged.

## Registration-time validation

- `deprecated` set to an empty or whitespace-only string raises `ValueError` at registration, from
  `Tool.from_function`, `ToolManager.add_tool`, `MCPServer.add_tool` and the `MCPServer.tool(...)`
  decorator (message must contain `deprecated`). A rejected registration registers nothing.

## Out of scope

No Python `warnings.warn` on call, no logging requirement, no change to the low-level server or
client.
