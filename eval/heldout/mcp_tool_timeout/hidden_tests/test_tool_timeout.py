"""Hidden acceptance tests for H1: per-tool timeout.

Real time is used, but every timeout is tiny (<= 0.05 s) and every slow tool sleeps far
longer than its limit, so the module runs in well under a second.
"""

import anyio
import pytest
from mcp_types import CallToolResult, TextContent

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool, ToolManager

pytestmark = pytest.mark.anyio

SLOW = 5.0  # a slow tool sleeps this long; the timeout must cut it short


def _text(result: CallToolResult) -> str:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- registration --------------------------------------------------------------------


def test_default_is_no_timeout():
    mcp = MCPServer()

    @mcp.tool()
    def plain() -> str:  # pragma: no cover
        return "ok"

    tool = mcp._tool_manager.get_tool("plain")
    assert tool is not None
    assert tool.timeout_seconds is None


def test_decorator_stores_timeout():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=2.5)
    def bounded() -> str:  # pragma: no cover
        return "ok"

    tool = mcp._tool_manager.get_tool("bounded")
    assert tool is not None
    assert tool.timeout_seconds == 2.5


def test_add_tool_stores_timeout():
    mcp = MCPServer()

    def bounded() -> str:  # pragma: no cover
        return "ok"

    mcp.add_tool(bounded, timeout_seconds=3)
    tool = mcp._tool_manager.get_tool("bounded")
    assert tool is not None
    assert tool.timeout_seconds == 3


def test_tool_from_function_and_manager_accept_timeout():
    def f() -> str:  # pragma: no cover
        return "ok"

    assert Tool.from_function(f, timeout_seconds=0.5).timeout_seconds == 0.5
    assert Tool.from_function(f).timeout_seconds is None
    manager = ToolManager()
    assert manager.add_tool(f, timeout_seconds=1.5).timeout_seconds == 1.5


@pytest.mark.parametrize("bad", [0, 0.0, -1, -0.001])
def test_decorator_rejects_non_positive_timeout(bad: float):
    mcp = MCPServer()
    with pytest.raises(ValueError, match="timeout_seconds"):

        @mcp.tool(timeout_seconds=bad)
        def f() -> str:  # pragma: no cover
            return "ok"

    assert mcp._tool_manager.get_tool("f") is None


@pytest.mark.parametrize("bad", [0, -5])
def test_add_tool_rejects_non_positive_timeout(bad: float):
    mcp = MCPServer()

    def f() -> str:  # pragma: no cover
        return "ok"

    with pytest.raises(ValueError, match="timeout_seconds"):
        mcp.add_tool(f, timeout_seconds=bad)
    assert mcp._tool_manager.get_tool("f") is None


@pytest.mark.parametrize("bad", [0, -0.5])
def test_from_function_and_manager_reject_non_positive_timeout(bad: float):
    def f() -> str:  # pragma: no cover
        return "ok"

    with pytest.raises(ValueError, match="timeout_seconds"):
        Tool.from_function(f, timeout_seconds=bad)
    manager = ToolManager()
    with pytest.raises(ValueError, match="timeout_seconds"):
        manager.add_tool(f, timeout_seconds=bad)
    assert manager.get_tool("f") is None


def test_tiny_positive_timeout_is_accepted():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=1e-9)
    def f() -> str:  # pragma: no cover
        return "ok"

    assert mcp._tool_manager.get_tool("f") is not None


# --- enforcement ---------------------------------------------------------------------


async def test_slow_tool_times_out_with_exact_message():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=0.01)
    async def slow() -> str:
        await anyio.sleep(SLOW)
        return "too late"  # pragma: no cover

    with anyio.fail_after(2):
        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool("slow", {})
    assert str(exc_info.value) == "Tool 'slow' timed out after 0.01s"


async def test_timeout_message_uses_general_format():
    """The timeout is formatted with :g, not str() or a fixed number of decimals."""
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=0.0123456789)
    async def slow() -> str:
        await anyio.sleep(SLOW)
        return "too late"  # pragma: no cover

    with anyio.fail_after(2):
        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool("slow", {})
    assert str(exc_info.value) == "Tool 'slow' timed out after 0.0123457s"


async def test_timeout_message_uses_registered_name():
    mcp = MCPServer()

    async def impl() -> str:
        await anyio.sleep(SLOW)
        return "too late"  # pragma: no cover

    mcp.add_tool(impl, name="renamed", timeout_seconds=0.02)
    with anyio.fail_after(2):
        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool("renamed", {})
    assert str(exc_info.value) == "Tool 'renamed' timed out after 0.02s"


async def test_timeout_reaches_client_as_error_result():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=0.01)
    async def slow() -> str:
        await anyio.sleep(SLOW)
        return "too late"  # pragma: no cover

    with anyio.fail_after(2):
        async with Client(mcp) as client:
            result = await client.call_tool("slow", {})
    assert result.is_error is True
    assert _text(result) == "Tool 'slow' timed out after 0.01s"


async def test_fast_tool_under_timeout_succeeds():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=1.0)
    async def fast(x: int) -> str:
        await anyio.sleep(0)
        return f"got {x}"

    @mcp.tool(timeout_seconds=1.0)
    def fast_sync(x: int) -> str:
        return f"sync {x}"

    async with Client(mcp) as client:
        result = await client.call_tool("fast", {"x": 7})
        sync_result = await client.call_tool("fast_sync", {"x": 8})
    assert result.is_error is False
    assert _text(result) == "got 7"
    assert sync_result.is_error is False
    assert _text(sync_result) == "sync 8"


async def test_no_timeout_lets_slow_tool_finish():
    mcp = MCPServer()

    @mcp.tool()
    async def leisurely() -> str:
        await anyio.sleep(0.05)
        return "done"

    with anyio.fail_after(2):
        result = await mcp.call_tool("leisurely", {})
    assert isinstance(result, CallToolResult)
    assert _text(result) == "done"


async def test_timeout_is_per_tool():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=0.01)
    async def strict() -> str:
        await anyio.sleep(SLOW)
        return "too late"  # pragma: no cover

    @mcp.tool()
    async def relaxed() -> str:
        await anyio.sleep(0.05)
        return "relaxed done"

    async with Client(mcp) as client:
        with anyio.fail_after(2):
            strict_result = await client.call_tool("strict", {})
            relaxed_result = await client.call_tool("relaxed", {})
    assert strict_result.is_error is True
    assert _text(strict_result) == "Tool 'strict' timed out after 0.01s"
    assert relaxed_result.is_error is False
    assert _text(relaxed_result) == "relaxed done"


async def test_tool_manager_call_tool_enforces_timeout():
    """The timeout lives in ToolManager.call_tool, so it applies without convert_result too."""
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=0.01)
    async def slow() -> str:
        await anyio.sleep(SLOW)
        return "too late"  # pragma: no cover

    from mcp.server.mcpserver import Context

    with anyio.fail_after(2):
        with pytest.raises(ToolError, match=r"^Tool 'slow' timed out after 0\.01s$"):
            await mcp._tool_manager.call_tool("slow", {}, Context(mcp_server=mcp))


async def test_tool_error_inside_timeout_is_not_rewritten():
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=1.0)
    async def fails() -> str:
        raise ToolError("nope")

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("fails", {})
    assert "timed out" not in str(exc_info.value)
    assert str(exc_info.value) == "Error executing tool fails: nope"


async def test_tool_raising_timeout_error_is_not_reported_as_timeout():
    """Only the server's own deadline produces the timeout message."""
    mcp = MCPServer()

    @mcp.tool(timeout_seconds=1.0)
    async def own_timeout() -> str:
        raise TimeoutError("upstream took too long")

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("own_timeout", {})
    assert "timed out after" not in str(exc_info.value)


async def test_unknown_tool_still_reports_unknown():
    mcp = MCPServer()
    with pytest.raises(ToolError, match="Unknown tool: missing"):
        await mcp.call_tool("missing", {})
