"""Hidden acceptance tests for H2: tool deprecation."""

import pytest
from mcp_types import CallToolResult, TextContent

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool, ToolManager
from mcp.server.mcpserver.tools.base import deprecation_message

pytestmark = pytest.mark.anyio


def _text(result: CallToolResult) -> str:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- pure helper ---------------------------------------------------------------------


def test_helper_not_deprecated_returns_none():
    assert deprecation_message("old", False) is None


def test_helper_deprecated_without_replacement():
    assert deprecation_message("old", True) == "Tool 'old' is deprecated"


def test_helper_deprecated_with_replacement():
    assert deprecation_message("old", "new_tool") == "Tool 'old' is deprecated; use 'new_tool' instead"


def test_helper_uses_given_name_and_hint_verbatim():
    assert deprecation_message("get-v1", "get v2") == "Tool 'get-v1' is deprecated; use 'get v2' instead"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_helper_rejects_empty_hint(bad: str):
    with pytest.raises(ValueError, match="deprecated"):
        deprecation_message("old", bad)


# --- registration --------------------------------------------------------------------


def test_default_is_not_deprecated():
    mcp = MCPServer()

    @mcp.tool()
    def plain() -> str:  # pragma: no cover
        return "ok"

    tool = mcp._tool_manager.get_tool("plain")
    assert tool is not None
    assert tool.deprecated is False


def test_decorator_and_add_tool_store_flag():
    mcp = MCPServer()

    @mcp.tool(deprecated=True)
    def flagged() -> str:  # pragma: no cover
        return "ok"

    def hinted() -> str:  # pragma: no cover
        return "ok"

    mcp.add_tool(hinted, deprecated="hinted_v2")

    flagged_tool = mcp._tool_manager.get_tool("flagged")
    hinted_tool = mcp._tool_manager.get_tool("hinted")
    assert flagged_tool is not None and flagged_tool.deprecated is True
    assert hinted_tool is not None and hinted_tool.deprecated == "hinted_v2"


def test_tool_from_function_and_manager_accept_flag():
    def f() -> str:  # pragma: no cover
        return "ok"

    assert Tool.from_function(f, deprecated="g").deprecated == "g"
    assert Tool.from_function(f).deprecated is False
    assert ToolManager().add_tool(f, deprecated=True).deprecated is True


@pytest.mark.parametrize("bad", ["", "  "])
def test_registration_rejects_empty_hint(bad: str):
    mcp = MCPServer()

    with pytest.raises(ValueError, match="deprecated"):

        @mcp.tool(deprecated=bad)
        def f() -> str:  # pragma: no cover
            return "ok"

    def g() -> str:  # pragma: no cover
        return "ok"

    with pytest.raises(ValueError, match="deprecated"):
        mcp.add_tool(g, deprecated=bad)
    with pytest.raises(ValueError, match="deprecated"):
        Tool.from_function(g, deprecated=bad)
    assert mcp._tool_manager.get_tool("f") is None
    assert mcp._tool_manager.get_tool("g") is None


# --- listing -------------------------------------------------------------------------


async def test_list_tools_flags_deprecated_tools():
    mcp = MCPServer()

    @mcp.tool(deprecated=True)
    def legacy() -> str:  # pragma: no cover
        return "ok"

    @mcp.tool(deprecated="search_v2")
    def search() -> str:  # pragma: no cover
        return "ok"

    @mcp.tool()
    def current() -> str:  # pragma: no cover
        return "ok"

    listed = {t.name: t for t in await mcp.list_tools()}
    assert listed["legacy"].meta == {"deprecated": True, "replacement": None}
    assert listed["search"].meta == {"deprecated": True, "replacement": "search_v2"}
    assert listed["current"].meta is None


async def test_list_tools_over_the_wire():
    """Over the wire the client sees the same flags (a None replacement may be omitted)."""
    mcp = MCPServer()

    @mcp.tool(deprecated=True)
    def legacy() -> str:  # pragma: no cover
        return "ok"

    @mcp.tool(deprecated="search_v2")
    def search() -> str:  # pragma: no cover
        return "ok"

    @mcp.tool()
    def current() -> str:  # pragma: no cover
        return "ok"

    async with Client(mcp) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
    legacy_meta = listed["legacy"].meta or {}
    assert legacy_meta.get("deprecated") is True
    assert legacy_meta.get("replacement") is None
    assert listed["search"].meta == {"deprecated": True, "replacement": "search_v2"}
    assert "deprecated" not in (listed["current"].meta or {})


async def test_list_tools_merges_with_existing_meta_without_mutating_it():
    mcp = MCPServer()
    own_meta = {"team": "search"}

    @mcp.tool(deprecated="find", meta=own_meta)
    def lookup() -> str:  # pragma: no cover
        return "ok"

    @mcp.tool(meta={"team": "core"})
    def keep() -> str:  # pragma: no cover
        return "ok"

    listed = {t.name: t for t in await mcp.list_tools()}
    assert listed["lookup"].meta == {"team": "search", "deprecated": True, "replacement": "find"}
    assert listed["keep"].meta == {"team": "core"}
    assert own_meta == {"team": "search"}
    tool = mcp._tool_manager.get_tool("lookup")
    assert tool is not None and tool.meta == {"team": "search"}


async def test_list_tools_deprecation_keys_win_on_clash():
    mcp = MCPServer()

    @mcp.tool(deprecated="v2", meta={"deprecated": "no", "replacement": "other", "extra": 1})
    def clash() -> str:  # pragma: no cover
        return "ok"

    (listed,) = await mcp.list_tools()
    assert listed.meta == {"deprecated": True, "replacement": "v2", "extra": 1}


async def test_list_tools_wire_format_uses_meta_key():
    mcp = MCPServer()

    @mcp.tool(deprecated=True)
    def legacy() -> str:  # pragma: no cover
        return "ok"

    (listed,) = await mcp.list_tools()
    dumped = listed.model_dump(by_alias=True, mode="json")
    assert dumped["_meta"] == {"deprecated": True, "replacement": None}


# --- calling -------------------------------------------------------------------------


async def test_deprecated_tool_still_runs_and_warns():
    mcp = MCPServer()

    @mcp.tool(deprecated="add_v2")
    def add(x: int, y: int) -> int:
        return x + y

    async with Client(mcp) as client:
        result = await client.call_tool("add", {"x": 2, "y": 3})
    assert result.is_error is False
    assert _text(result) == "5"
    assert result.meta is not None
    assert result.meta["deprecation_warning"] == "Tool 'add' is deprecated; use 'add_v2' instead"


async def test_deprecated_without_hint_warns_without_replacement():
    mcp = MCPServer()

    @mcp.tool(deprecated=True)
    def legacy() -> str:
        return "still works"

    result = await mcp.call_tool("legacy", {})
    assert isinstance(result, CallToolResult)
    assert _text(result) == "still works"
    assert result.meta == {"deprecation_warning": "Tool 'legacy' is deprecated"}


async def test_warning_uses_registered_name():
    mcp = MCPServer()

    def impl() -> str:
        return "ok"

    mcp.add_tool(impl, name="old_name", deprecated="new_name")
    result = await mcp.call_tool("old_name", {})
    assert isinstance(result, CallToolResult)
    assert result.meta == {"deprecation_warning": "Tool 'old_name' is deprecated; use 'new_name' instead"}


async def test_not_deprecated_tool_result_has_no_meta():
    mcp = MCPServer()

    @mcp.tool()
    def fresh() -> str:
        return "ok"

    direct = await mcp.call_tool("fresh", {})
    assert isinstance(direct, CallToolResult)
    assert direct.meta is None

    async with Client(mcp) as client:
        result = await client.call_tool("fresh", {})
    assert result.is_error is False
    assert "deprecation_warning" not in (result.meta or {})


async def test_warning_merges_with_result_meta_from_tool():
    mcp = MCPServer()
    returned = CallToolResult(content=[TextContent(type="text", text="raw")], _meta={"trace": "abc"})

    @mcp.tool(deprecated=True)
    def raw() -> CallToolResult:
        return returned

    result = await mcp.call_tool("raw", {})
    assert isinstance(result, CallToolResult)
    assert result.meta == {"trace": "abc", "deprecation_warning": "Tool 'raw' is deprecated"}
    assert returned.meta == {"trace": "abc"}


async def test_structured_output_is_preserved():
    mcp = MCPServer()

    @mcp.tool(deprecated="totals")
    def total(xs: list[int]) -> dict[str, int]:
        return {"sum": sum(xs)}

    result = await mcp.call_tool("total", {"xs": [1, 2, 3]})
    assert isinstance(result, CallToolResult)
    assert result.structured_content == {"sum": 6}
    assert result.meta == {"deprecation_warning": "Tool 'total' is deprecated; use 'totals' instead"}


async def test_failing_deprecated_tool_still_reports_its_error():
    mcp = MCPServer()

    @mcp.tool(deprecated=True)
    def broken() -> str:
        raise ToolError("bad input")

    with pytest.raises(ToolError, match="bad input"):
        await mcp.call_tool("broken", {})

    async with Client(mcp) as client:
        result = await client.call_tool("broken", {})
    assert result.is_error is True
    assert _text(result) == "Error executing tool broken: bad input"
    assert "deprecation_warning" not in (result.meta or {})
