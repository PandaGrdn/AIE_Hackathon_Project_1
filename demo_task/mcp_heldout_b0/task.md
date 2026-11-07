# Tool timeouts, deprecation and resource size caps for MCPServer

repo: ../../runs/real_repo/mcp_b

Target: the MCP Python SDK (https://github.com/modelcontextprotocol/python-sdk) at the pinned commit `77001c9`, the same clone as the rate-limits task (`demo_task/mcp/repo`, prepared by `bash demo_task/mcp/setup.sh`). Install with `uv sync --all-extras --python 3.12`; the existing suite runs with `uv run pytest -q tests/server tests/shared -p no:cacheprovider`. None of these features exist upstream.

This is Task B of the demo recording (`scripts/record_demo.py`): a second long task built from the three MCP held-out slices in `eval/heldout/` (`mcp_tool_timeout`, `mcp_tool_deprecation`, `mcp_resource_size_cap`). Each subtask's spec is the slice's `spec.md`, framed for the order below; the verdict uses the slice's verified hidden tests (`eval/heldout_specs.spec_from_hidden_tests`), not a freshly authored test set. The three reference patches, stacked in this order on `77001c9` (textual conflicts in the shared registration signatures resolved by keeping both keywords), pass all three hidden test files and the existing `tests/server tests/shared` suite.

## Goal

Give a server author three more controls on a high-level `MCPServer`: a per-tool time budget (a call that runs too long fails with a fixed tool error), a deprecation flag on tools (still callable, flagged in `tools/list`, a warning in every successful result's `_meta`), and a per-resource size cap (reading a resource whose content is larger than its cap fails with a fixed `ResourceError`). Tools and resources registered without these options behave exactly as before.

## Constraints

- Keep the existing suite green after every subtask (`tests/server`, `tests/shared`).
- No new dependencies.
- All new parameters are keywords with defaults (`timeout_seconds=None`, `deprecated=False`, `max_bytes=None`) so existing callers are unaffected; existing positional parameters keep their positions.
- Registration-time validation: an invalid value raises `ValueError` from every entry point (decorator, `add_*`, manager, `from_function`) and registers nothing.
- Error messages are exact; each subtask's spec states them.

## Subtasks (in order; each builds on the previous commit)

1. **Per-tool call timeout**: `timeout_seconds` on `Tool`, `Tool.from_function`, `ToolManager.add_tool`, `MCPServer.add_tool` and `MCPServer.tool`; `ToolManager.call_tool` runs the tool under `anyio.fail_after` and reports `Tool '<name>' timed out after <t:g>s`.
2. **Tool deprecation flags and call warnings**: `deprecated: bool | str` after `timeout_seconds` on the same four registration functions, the pure `deprecation_message` helper, `_meta` flags in `list_tools`, and `_meta["deprecation_warning"]` on successful calls.
3. **Resource size cap**: the pure `content_size` helper, keyword-only `max_bytes` on resource registration (static and template), and `MCPServer.read_resource` raising `Resource '<uri>' exceeds max_bytes=<n>`.
