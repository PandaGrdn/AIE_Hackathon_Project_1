"""The MCP demo long task (per-tool rate limits) loads offline from its hand-authored decomposition."""

import json
from pathlib import Path

import pytest

from core.decompose import decomposer
from core.decompose.decomposer import decompose, load_task, validate_decomposition

ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = ROOT / "demo_task" / "mcp"
TASK_FILE = TASK_DIR / "task.md"
SUBTASKS_FILE = TASK_DIR / "subtasks.json"

TITLE = "Per-tool rate limits and quotas for MCPServer"
LIMITS = "src/mcp/server/mcpserver/limits.py"

ORDER = [
    "Limit policy model and pure decision logic",
    "Attach policies to tools",
    "Enforcement on call",
    "Client-visible limit metadata and context helpers",
    "Session scope",
    "Usage status resource",
    "CLI inspection",
]


@pytest.fixture
def offline(monkeypatch):
    def no_llm(*a, **k):
        raise AssertionError("decompose must not construct an LLM client when subtasks.json exists")

    monkeypatch.setattr(decomposer, "get_client", no_llm)


@pytest.fixture
def subtasks(offline):
    return decompose(load_task(TASK_FILE)).subtasks


def _sig(subtask, name):
    matches = [f for f in subtask.interface if f.name == name]
    assert len(matches) == 1, f"{subtask.title}: expected one pinned {name}, got {matches}"
    return matches[0]


def test_task_file_header():
    lines = TASK_FILE.read_text().splitlines()
    assert lines[0] == f"# {TITLE}"
    assert "repo: repo" in lines
    task = load_task(TASK_FILE)  # repo/ need not exist on disk
    assert task.title == TITLE
    assert Path(task.repo_path) == (TASK_DIR / "repo").resolve()


def test_decomposes_offline_in_contract_order(subtasks):
    assert [s.title for s in subtasks] == ORDER
    assert [s.index for s in subtasks] == list(range(7))
    ids = [s.id for s in subtasks]
    assert subtasks[0].depends_on == []
    for i, s in enumerate(subtasks):
        assert all(ids.index(d) < i for d in s.depends_on)
        assert {f.file for f in s.interface} <= set(s.files)
        assert s.files and s.spec and s.tags


def test_s1_pins_pure_evaluate(subtasks):
    s1 = subtasks[0]
    assert s1.files == [LIMITS]
    ev = _sig(s1, "evaluate")
    assert ev.file == LIMITS
    assert ev.params == ["policy: LimitPolicy", "call_times: list[float]", "now: float"]
    assert ev.returns == "LimitDecision"
    assert "pure" in s1.tags
    # The window boundary is stated exactly: a call exactly per_seconds old has left the window.
    assert "now - t < per_seconds" in s1.spec and "now - t == per_seconds" in s1.spec
    assert "ValueError" in s1.spec


def test_s7_pins_format_limits_table(subtasks):
    s7 = subtasks[6]
    f = _sig(s7, "format_limits_table")
    assert f.file in s7.files and f.file.startswith("src/mcp/cli/")
    assert f.params == ["tools: list[tuple[str, LimitPolicy | None]]"]
    assert f.returns == "str"
    assert "src/mcp/cli/cli.py" in s7.files
    assert "pure" in s7.tags


def test_specs_state_fixed_error_messages_and_formats(subtasks):
    s3, s7 = subtasks[2], subtasks[6]
    assert "f\"Tool '{name}' {reason_text}\"" in s3.spec
    assert "rate limit exceeded; retry after {retry_after:.3f}s" in s3.spec
    assert '"quota exhausted"' in s3.spec
    assert "f\"{name}  rate={rate}  quota={quota}  scope={scope}\"" in s7.spec
    assert "echo  rate=-  quota=-  scope=server" in s7.spec
    assert "search  rate=5/60s  quota=100  scope=session" in s7.spec
    s2 = subtasks[1]
    assert 'f"Unknown tool: {name}"' in s2.spec


def test_every_contract_method_is_pinned_once(subtasks):
    pinned = [(f.file, f.name) for s in subtasks for f in s.interface]
    assert len(pinned) == len(set(pinned))
    names = {name for _, name in pinned}
    for want in [
        "evaluate",
        "LimitState.check",
        "LimitState.record",
        "LimitState.reset",
        "Tool.from_function",
        "ToolManager.add_tool",
        "ToolManager.get_limits",
        "ToolManager.set_limits",
        "LimitEnforcer.decide",
        "LimitEnforcer.record",
        "LimitEnforcer.remaining",
        "LimitEnforcer.reset",
        "MCPServer.tool",
        "MCPServer.add_tool",
        "Context.tool_limits",
        "Context.remaining_calls",
        "LimitEnforcer.forget_scope",
        "MCPServer.forget_session_limits",
        "LimitEnforcer.snapshot",
        "MCPServer.enable_limits_resource",
        "format_limits_table",
    ]:
        assert want in names, want


def test_validate_decomposition_accepts_file():
    data = json.loads(SUBTASKS_FILE.read_text())
    assert data["task_title"] == TITLE
    assert len(data["subtasks"]) == 7
    validate_decomposition(data)
