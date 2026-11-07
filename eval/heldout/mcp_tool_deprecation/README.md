# H2: tool deprecation (`mcp_tool_deprecation`)

Adds `deprecated: bool | str = False` (a string is the replacement hint) to `Tool`,
`Tool.from_function`, `ToolManager.add_tool`, `MCPServer.add_tool` and `@MCPServer.tool(...)`, and
a pure helper `deprecation_message(name, deprecated) -> str | None` in `tools/base.py`.
`MCPServer.list_tools` merges `{"deprecated": True, "replacement": str | None}` into a deprecated
tool's `_meta`. `MCPServer.call_tool` adds `{"deprecation_warning": ...}` to the result's `_meta`.

- `base_commit`: `77001c9`; `reference.patch` applies to bare `77001c9`.
- `hidden_tests/test_tool_deprecation.py`: 24 tests (pure helper, registration, listing, calling,
  wire format).

Bug patterns shared with the demo task:

- Metadata only when configured: non-deprecated tools keep `_meta` exactly as before (`None`, no
  `deprecated: False` key). This is the same rule as S4's `{"limits": ...}`.
- Merge without mutation: the author's `meta` dict and the result object the tool returns stay
  untouched; the deprecation keys win on a clash.
- Exact messages: `Tool 'old' is deprecated` vs `Tool 'old' is deprecated; use 'new' instead`,
  using the registered name (not the function name).
- Registration-time validation: an empty or whitespace hint raises `ValueError` (a falsy string
  must not quietly mean "not deprecated").
- Error results carry no warning, and failures keep their existing text.
