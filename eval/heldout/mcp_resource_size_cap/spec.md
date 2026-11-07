# Resource size cap for MCPServer resources

Repository: the MCP Python SDK at commit `77001c9` (package import root `mcp`). Install with
`uv sync --all-extras --python 3.12`; the existing suite (`uv run pytest -q tests/server tests/shared
-p no:cacheprovider`) must stay green. Add no dependencies.

## Goal

A server author can cap how large a resource's content may be. Reading a resource whose content
is larger than its cap fails with a fixed `ResourceError` instead of returning the content.
Resources registered without a cap behave exactly as before.

## Public API

All changes are in `src/mcp/server/mcpserver/`.

Pure helper in `resources/base.py` (`from mcp.server.mcpserver.resources.base import content_size`
must work; re-exporting it from the `mcp.server.mcpserver.resources` package is optional):

- `content_size(content: str | bytes) -> int`: the size in bytes. For `str` it is the length of the
  UTF-8 encoding (`"abc"` -> 3, `"é"` -> 2, `"漢字"` -> 6, `"🎉"` -> 4, `""` -> 0); for `bytes` it
  is `len(content)`.

`server.py`, class `MCPServer`:

- `MCPServer.add_resource(resource: Resource, *, max_bytes: int | None = None) -> None`:
  `max_bytes` is keyword-only (`add_resource(r, 5)` is a `TypeError`).
- `MCPServer.resource(uri, *, name=None, title=None, description=None, mime_type=None, icons=None,
  annotations=None, meta=None, security=None, max_bytes=None)` decorator: new keyword
  `max_bytes: int | None = None`. It applies to static resources (registered through
  `MCPServer.add_resource`) and to template resources (URIs with `{...}` variables, registered
  through `ResourceManager.add_template`).
- `MCPServer.read_resource(uri, context=None)`: signature unchanged; enforces the cap (below).

`resources/resource_manager.py`, class `ResourceManager`:

- `ResourceManager.add_resource(resource: Resource, *, max_bytes: int | None = None) -> Resource`
- `ResourceManager.add_template(fn, uri_template, name=None, title=None, description=None,
  mime_type=None, icons=None, annotations=None, meta=None, security=DEFAULT_RESOURCE_SECURITY,
  max_bytes=None) -> ResourceTemplate`

Both accept and remember the cap for what they register. How the cap is stored (on the manager,
on `ResourceTemplate`, ...) is up to you.

## Behaviour

1. `max_bytes=None` (the default) means no cap.
2. After `MCPServer.read_resource` has obtained the content (`Resource.read()`, including the JSON
   conversion `FunctionResource` applies to non-str/bytes return values), it measures it with
   `content_size`. If the size is strictly greater than `max_bytes`, it raises
   `ResourceError(f"Resource '{uri}' exceeds max_bytes={max_bytes}")`, where `uri` is the URI being
   read (for a template, the concrete URI, e.g. `docs://abcde`, not the template). Content of
   exactly `max_bytes` bytes is returned normally.
3. The error is a plain `ResourceError`, not `UnexpectedResourceError`.
4. Over the protocol, the existing `MCPServer._handle_read_resource` turns it into an `MCPError`
   with code `INTERNAL_ERROR` (-32603) and that exact message; keep that.
5. A tool that reads the resource through `Context.read_resource` gets the `ResourceError`, so the
   tool call fails with `Error executing tool <tool>: Resource '<uri>' exceeds max_bytes=<n>`
   (existing `Tool.run` wrapping).
6. Text is measured in UTF-8 bytes, not characters: `"éé"` is 4 bytes, so it fails a cap of 3 and
   passes a cap of 4. Binary content is measured by its length.
7. Caps are per resource / per template; a capped resource never affects another one.
8. The cap is not client-visible: `list_resources` / `list_resource_templates` output is unchanged.
9. Registering a second static resource with an already-used URI keeps today's behaviour (the
   first registration, and its cap, wins).

## Registration-time validation

- `max_bytes <= 0` raises `ValueError` at registration, from `MCPServer.add_resource`, the
  `MCPServer.resource(...)` decorator (static and template URIs), `ResourceManager.add_resource`
  and `ResourceManager.add_template`. The message must contain `max_bytes`, e.g.
  `max_bytes must be greater than 0, got 0`. A rejected registration registers nothing.

## Out of scope

No streaming or truncation, no global default cap, no change to the low-level server or client.
