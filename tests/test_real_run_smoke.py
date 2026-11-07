"""scripts/real_run.py helpers: roster filtering, subtask slicing, spec checks. No network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core.harness.spec_tests import SpecTests
from core.models import AgentIdentity, LongTask, Subtask

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("real_run", ROOT / "scripts" / "real_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["real_run"] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load()

ROSTER = [
    AgentIdentity(id="meta-code-t2", provider="meta", model="muse-wonder-1.0", temperature=0.2),
    AgentIdentity(id="meta-spark-t5", provider="meta", model="muse-spark-1.3", temperature=0.5),
    AgentIdentity(id="gemini-t2", provider="gemini", model="g", temperature=0.2),
    AgentIdentity(id="gemini-t7-alt", provider="gemini", model="g", temperature=0.7, prompt_variant="alt"),
    AgentIdentity(id="claude-t2", provider="anthropic", model="c", temperature=0.2),
    AgentIdentity(id="claude-t7-alt", provider="anthropic", model="c", temperature=0.7, prompt_variant="alt"),
]


def no_anthropic(provider: str) -> bool:
    return provider in {"gemini", "meta"}


def test_roster_drops_identities_without_a_key():
    roster, dropped = rr.build_roster("all", 6, identities=ROSTER, available=no_anthropic)
    assert [a.provider for a in roster] == ["meta", "meta", "gemini", "gemini"]
    assert dropped == ["claude-t2", "claude-t7-alt"]


def test_roster_default_agents_in_requested_order_and_capped():
    roster, _ = rr.build_roster(rr.DEFAULT_AGENTS, 3, identities=ROSTER, available=no_anthropic)
    assert [a.id for a in roster] == ["gemini-t2", "gemini-t7-alt", "meta-spark-t5"]
    roster, _ = rr.build_roster(rr.DEFAULT_AGENTS, 2, identities=ROSTER, available=no_anthropic)
    assert [a.id for a in roster] == ["gemini-t2", "gemini-t7-alt"]


def test_roster_skips_requested_ids_whose_provider_is_missing():
    roster, _ = rr.build_roster("claude-t2,gemini-t2", 3, identities=ROSTER, available=no_anthropic)
    assert [a.id for a in roster] == ["gemini-t2"]


def test_roster_with_no_keys_raises():
    with pytest.raises(RuntimeError):
        rr.build_roster("all", 3, identities=ROSTER, available=lambda p: False)


def test_roster_uses_real_swarm_identities_with_mocked_keys(monkeypatch):
    import core.llm.clients as clients

    monkeypatch.setattr(clients, "provider_available", no_anthropic)
    roster, dropped = rr.build_roster("all", 6)
    assert roster and all(a.provider in {"gemini", "meta"} for a in roster)
    assert all(d.startswith(("claude", "openai")) for d in dropped)  # providers without keys in this test


def _task(n: int) -> LongTask:
    return LongTask(title="t", description="d", repo_path="/nowhere",
                    subtasks=[Subtask(index=i, title=f"s{i}", spec="x", interface=[], files=[]) for i in range(n)])


def test_parse_indices():
    assert rr.parse_indices("0") == [0]
    assert rr.parse_indices("0,2") == [0, 2]
    assert rr.parse_indices("1-3") == [1, 2, 3]
    with pytest.raises(ValueError):
        rr.parse_indices("")


def test_slice_subtasks_keeps_requested_in_index_order():
    t = rr.slice_subtasks(_task(5), [3, 0, 3])
    assert [s.index for s in t.subtasks] == [0, 3]


def test_slice_subtasks_unknown_index_raises():
    with pytest.raises(ValueError):
        rr.slice_subtasks(_task(2), [5])


def test_slice_subtasks_on_the_mcp_decomposition():
    from core.decompose.decomposer import decompose, load_task

    task = decompose(load_task(ROOT / "demo_task" / "mcp" / "task.md"))
    rr.slice_subtasks(task, [0])
    assert len(task.subtasks) == 1 and task.subtasks[0].files == ["src/mcp/server/mcpserver/limits.py"]
    assert rr.module_of(task.subtasks[0].files[0]) == "mcp.server.mcpserver.limits"


def _spec(src: str) -> SpecTests:
    return SpecTests(subtask_id="s", cases=[], expected={}, properties=[], modules={"test_x.py": src})


def test_check_spec_accepts_enough_tests_importing_the_module():
    src = "from mcp.server.mcpserver.limits import evaluate\n" + "".join(f"def test_{i}():\n    assert True\n" for i in range(5))
    assert rr.check_spec(_spec(src), "mcp.server.mcpserver.limits") == []


def test_check_spec_flags_too_few_tests_and_missing_import():
    problems = rr.check_spec(_spec("def test_a():\n    assert True\n"), "mcp.server.mcpserver.limits")
    assert any("does not import" in p for p in problems) and any("test functions" in p for p in problems)
    assert rr.check_spec(SpecTests(subtask_id="s", cases=[], expected={}, properties=[]), "m") == ["no pytest modules"]


# --- fixes the real run needed (runner text-call recovery, sandbox interpreter + sys.path remap) ---


def test_text_tool_call_is_recovered():
    from core.swarm.runner import _text_tool_calls

    calls = _text_tool_calls("[write_file] {'path': 'src/a.py', 'content': 'x = 1\\n'}")
    assert calls == [{"name": "write_file", "arguments": {"path": "src/a.py", "content": "x = 1\n"}}]
    assert _text_tool_calls("[rm_rf] {'path': '/'}") == []  # not a tool
    assert _text_tool_calls("I will now call write_file") == []


def test_pytest_prefix_uses_the_base_repo_venv_and_remaps_editable_paths(tmp_path):
    from core.swarm.sandbox import pytest_prefix

    base, sb = tmp_path / "repo", tmp_path / "sb"
    site = base / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (base / ".venv" / "bin").mkdir()
    (base / ".venv" / "bin" / "python").write_text("")
    (site / "_editable_impl_pkg.pth").write_text(f"{base / 'src'}\n")
    (sb / "src").mkdir(parents=True)
    prefix, has_timeout = pytest_prefix(sb, base)
    assert prefix[0] == str((base / ".venv" / "bin" / "python").absolute()) and prefix[1:3] == ["-I", "-c"]
    assert repr(str((sb / "src").resolve())) in prefix[3]  # sandbox src shadows the original's editable install
    assert has_timeout is False
    prefix, _ = pytest_prefix(sb, tmp_path / "no_venv")
    assert prefix[0] == sys.executable and "pythonpath=." in prefix
