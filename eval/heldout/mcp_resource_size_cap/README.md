# H3: resource size cap (`mcp_resource_size_cap`)

Adds keyword `max_bytes: int | None = None` to `MCPServer.add_resource`, `@MCPServer.resource(...)`
(static and template URIs), `ResourceManager.add_resource` and `ResourceManager.add_template`, and
a pure helper `content_size(content: str | bytes) -> int` (UTF-8 bytes for `str`) in
`resources/base.py`. `MCPServer.read_resource` raises
`ResourceError(f"Resource '{uri}' exceeds max_bytes={max_bytes}")` when the content is larger
than the cap.

- `base_commit`: `77001c9`; `reference.patch` applies to bare `77001c9`.
- `hidden_tests/test_resource_size_cap.py`: 33 tests (helper, registration on every entry point,
  static/binary/template/JSON-converted content, wire error code, nested read from a tool).

Bug patterns shared with the demo task:

- Limit boundaries: content of exactly `max_bytes` passes and `max_bytes + 1` fails (strict `>`).
  This mirrors `now - t < per_seconds` and `remaining` in the demo.
- Units: text is measured in UTF-8 bytes, not characters (`"éé"` is 4 bytes), and JSON-converted
  content is measured after conversion.
- Exact messages: the concrete URI of a template read (not the template), plus the error type
  (`ResourceError`, not `UnexpectedResourceError`) and protocol code `INTERNAL_ERROR`.
- Registration-time validation: `<= 0` raises `ValueError` from all four entry points, and nothing
  is registered.
- Metadata only when configured: the cap never shows up in `list_resources` /
  `list_resource_templates`.
