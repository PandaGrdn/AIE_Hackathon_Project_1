"""Hidden acceptance tests for H3: resource size cap."""

import pytest
from mcp_types import INTERNAL_ERROR, InputRequiredResult, TextContent, TextResourceContents

from mcp.client import Client
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, UnexpectedResourceError
from mcp.server.mcpserver.resources import BinaryResource, ResourceManager, TextResource
from mcp.server.mcpserver.resources.base import content_size
from mcp.shared.exceptions import MCPError

pytestmark = pytest.mark.anyio


async def _read(mcp: MCPServer, uri: str) -> str | bytes:
    """Read `uri` through MCPServer.read_resource and return its single content item."""
    result = await mcp.read_resource(uri)
    assert not isinstance(result, InputRequiredResult)
    (item,) = list(result)
    return item.content


# --- pure helper ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "size"),
    [
        ("", 0),
        ("abc", 3),
        ("é", 2),
        ("漢字", 6),
        ("🎉", 4),
        ("a\nb", 3),
        (b"", 0),
        (b"abc", 3),
        (b"\x00\xff\x10", 3),
        ("é".encode(), 2),
    ],
)
def test_content_size(content: str | bytes, size: int):
    assert content_size(content) == size


# --- registration --------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1])
def test_decorator_rejects_non_positive_cap_on_static_resource(bad: int):
    mcp = MCPServer()
    with pytest.raises(ValueError, match="max_bytes"):

        @mcp.resource("data://static", max_bytes=bad)
        def static() -> str:  # pragma: no cover
            return "x"

    assert mcp._resource_manager.list_resources() == []


@pytest.mark.parametrize("bad", [0, -10])
def test_decorator_rejects_non_positive_cap_on_template(bad: int):
    mcp = MCPServer()
    with pytest.raises(ValueError, match="max_bytes"):

        @mcp.resource("data://{key}", max_bytes=bad)
        def templated(key: str) -> str:  # pragma: no cover
            return key

    assert mcp._resource_manager.list_templates() == []


@pytest.mark.parametrize("bad", [0, -1])
def test_add_resource_rejects_non_positive_cap(bad: int):
    mcp = MCPServer()
    with pytest.raises(ValueError, match="max_bytes"):
        mcp.add_resource(TextResource(uri="data://t", text="x"), max_bytes=bad)
    assert mcp._resource_manager.list_resources() == []


@pytest.mark.parametrize("bad", [0, -1])
def test_resource_manager_rejects_non_positive_cap(bad: int):
    manager = ResourceManager()
    with pytest.raises(ValueError, match="max_bytes"):
        manager.add_resource(TextResource(uri="data://t", text="x"), max_bytes=bad)

    def templated(key: str) -> str:  # pragma: no cover
        return key

    with pytest.raises(ValueError, match="max_bytes"):
        manager.add_template(templated, uri_template="data://{key}", max_bytes=bad)
    assert manager.list_resources() == []
    assert manager.list_templates() == []


def test_resource_manager_accepts_positive_cap():
    manager = ResourceManager()
    resource = TextResource(uri="data://t", text="x")
    assert manager.add_resource(resource, max_bytes=1) is resource

    def templated(key: str) -> str:  # pragma: no cover
        return key

    template = manager.add_template(templated, uri_template="data://{key}", max_bytes=1)
    assert manager.list_templates() == [template]


def test_add_resource_cap_is_keyword_only():
    mcp = MCPServer()
    with pytest.raises(TypeError):
        mcp.add_resource(TextResource(uri="data://t", text="x"), 5)  # type: ignore[misc]


async def test_cap_is_not_listed():
    mcp = MCPServer()

    @mcp.resource("data://capped", max_bytes=10)
    def capped() -> str:  # pragma: no cover
        return "x"

    @mcp.resource("data://{key}/capped", max_bytes=10)
    def capped_template(key: str) -> str:  # pragma: no cover
        return key

    (listed,) = await mcp.list_resources()
    assert listed.meta is None
    (listed_template,) = await mcp.list_resource_templates()
    assert listed_template.meta is None


# --- enforcement: static resources ---------------------------------------------------


async def test_content_at_cap_is_allowed():
    mcp = MCPServer()

    @mcp.resource("data://five", max_bytes=5)
    def five() -> str:
        return "12345"

    item = await _read(mcp, "data://five")
    assert item == "12345"


async def test_content_over_cap_raises_exact_message():
    mcp = MCPServer()

    @mcp.resource("data://six", max_bytes=5)
    def six() -> str:
        return "123456"

    with pytest.raises(ResourceError) as exc_info:
        await mcp.read_resource("data://six")
    assert not isinstance(exc_info.value, UnexpectedResourceError)
    assert str(exc_info.value) == "Resource 'data://six' exceeds max_bytes=5"


async def test_text_is_measured_in_utf8_bytes():
    mcp = MCPServer()

    @mcp.resource("data://accents", max_bytes=3)
    def accents() -> str:
        return "éé"  # 2 characters, 4 bytes

    @mcp.resource("data://accents-fit", max_bytes=4)
    def accents_fit() -> str:
        return "éé"

    with pytest.raises(ResourceError, match=r"^Resource 'data://accents' exceeds max_bytes=3$"):
        await mcp.read_resource("data://accents")
    item = await _read(mcp, "data://accents-fit")
    assert item == "éé"


async def test_binary_content_is_measured_in_bytes():
    mcp = MCPServer()
    mcp.add_resource(BinaryResource(uri="data://blob", data=b"\x00" * 8), max_bytes=8)
    mcp.add_resource(BinaryResource(uri="data://blob-big", data=b"\x00" * 9), max_bytes=8)

    item = await _read(mcp, "data://blob")
    assert item == b"\x00" * 8
    with pytest.raises(ResourceError) as exc_info:
        await mcp.read_resource("data://blob-big")
    assert str(exc_info.value) == "Resource 'data://blob-big' exceeds max_bytes=8"


async def test_add_resource_cap_on_text_resource():
    mcp = MCPServer()
    mcp.add_resource(TextResource(uri="data://note", text="hello world"), max_bytes=10)
    with pytest.raises(ResourceError) as exc_info:
        await mcp.read_resource("data://note")
    assert str(exc_info.value) == "Resource 'data://note' exceeds max_bytes=10"


async def test_no_cap_by_default():
    mcp = MCPServer()

    @mcp.resource("data://big")
    def big() -> str:
        return "x" * 100_000

    mcp.add_resource(TextResource(uri="data://big-text", text="y" * 100_000))

    item = await _read(mcp, "data://big")
    assert item == "x" * 100_000
    text_item = await _read(mcp, "data://big-text")
    assert text_item == "y" * 100_000


async def test_cap_is_per_resource():
    mcp = MCPServer()

    @mcp.resource("data://small", max_bytes=2)
    def small() -> str:
        return "abc"

    @mcp.resource("data://free")
    def free() -> str:
        return "abc"

    with pytest.raises(ResourceError):
        await mcp.read_resource("data://small")
    item = await _read(mcp, "data://free")
    assert item == "abc"


async def test_json_converted_content_is_measured_after_conversion():
    mcp = MCPServer()

    def payload() -> dict[str, int]:
        return {"alpha": 1, "beta": 2}

    mcp.resource("data://json-free")(payload)
    free_item = await _read(mcp, "data://json-free")
    assert isinstance(free_item, str)
    size = len(free_item.encode("utf-8"))

    mcp.resource("data://json-fit", max_bytes=size)(payload)
    mcp.resource("data://json-over", max_bytes=size - 1)(payload)
    fit_item = await _read(mcp, "data://json-fit")
    assert fit_item == free_item
    with pytest.raises(ResourceError) as exc_info:
        await mcp.read_resource("data://json-over")
    assert str(exc_info.value) == f"Resource 'data://json-over' exceeds max_bytes={size - 1}"


# --- enforcement: templates ----------------------------------------------------------


async def test_template_cap_uses_concrete_uri():
    mcp = MCPServer()

    @mcp.resource("docs://{name}", max_bytes=4)
    def doc(name: str) -> str:
        return name

    item = await _read(mcp, "docs://abcd")
    assert item == "abcd"
    with pytest.raises(ResourceError) as exc_info:
        await mcp.read_resource("docs://abcde")
    assert str(exc_info.value) == "Resource 'docs://abcde' exceeds max_bytes=4"


async def test_template_without_cap_is_unlimited():
    mcp = MCPServer()

    @mcp.resource("docs://{name}")
    def doc(name: str) -> str:
        return name * 1000

    item = await _read(mcp, "docs://abc")
    assert item == "abc" * 1000


# --- over the wire and nested reads --------------------------------------------------


async def test_client_sees_protocol_error_with_message():
    mcp = MCPServer()

    @mcp.resource("data://six", max_bytes=5)
    def six() -> str:
        return "123456"

    @mcp.resource("data://five", max_bytes=5)
    def five() -> str:
        return "12345"

    async with Client(mcp) as client:
        ok = await client.read_resource("data://five")
        with pytest.raises(MCPError) as exc_info:
            await client.read_resource("data://six")
    (contents,) = ok.contents
    assert isinstance(contents, TextResourceContents)
    assert contents.text == "12345"
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert exc_info.value.error.message == "Resource 'data://six' exceeds max_bytes=5"


async def test_tool_reading_oversized_resource_gets_tool_error():
    mcp = MCPServer()

    @mcp.resource("data://six", max_bytes=5)
    def six() -> str:
        return "123456"

    @mcp.tool()
    async def peek(ctx: Context) -> str:
        contents = await ctx.read_resource("data://six")
        return str(list(contents))  # pragma: no cover

    async with Client(mcp) as client:
        result = await client.call_tool("peek", {})
    assert result.is_error is True
    (block,) = result.content
    assert isinstance(block, TextContent)
    assert block.text == "Error executing tool peek: Resource 'data://six' exceeds max_bytes=5"
