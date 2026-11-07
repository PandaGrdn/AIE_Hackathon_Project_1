"""Task B (demo_task/mcp_heldout): loads offline, its spec tests are the held-out slices' hidden tests,
and scripts/record_demo.py --dry-run runs without touching the network."""

import ast
import dataclasses
import json
import socket
import sys
from pathlib import Path

import pytest

from core.decompose import decomposer
from core.decompose.decomposer import decompose, load_task, precomputed_path, validate_decomposition
from core.harness.spec_tests import spec_node_ids
from eval import heldout_specs
from eval.heldout_specs import flatten_parametrized, hidden_test_modules, slice_name, spec_from_hidden_tests

ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = ROOT / "demo_task" / "mcp_heldout"
TASK_FILE = TASK_DIR / "task.md"
CLONE = ROOT / "demo_task" / "mcp" / "repo"
TITLE = "Tool timeouts, deprecation and resource size caps for MCPServer"
ORDER = ["Per-tool call timeout", "Tool deprecation flags and call warnings", "Resource size cap"]
SLICES = ["mcp_tool_timeout", "mcp_tool_deprecation", "mcp_resource_size_cap"]
HIDDEN_COUNTS = {"mcp_tool_timeout": 24, "mcp_tool_deprecation": 24, "mcp_resource_size_cap": 33}  # pytest items

needs_clone = pytest.mark.skipif(not (CLONE / ".git").exists(), reason="demo_task/mcp/repo not cloned (bash demo_task/mcp/setup.sh)")


@pytest.fixture
def offline(monkeypatch):
    def no_llm(*a, **k):
        raise AssertionError("no LLM client may be constructed offline")

    monkeypatch.setattr(decomposer, "get_client", no_llm)


@pytest.fixture
def subtasks(offline):
    return decompose(load_task(TASK_FILE)).subtasks


def test_task_file_header_and_repo():
    lines = TASK_FILE.read_text().splitlines()
    assert lines[0] == f"# {TITLE}"
    assert "repo: ../mcp/repo" in lines
    task = load_task(TASK_FILE)
    assert Path(task.repo_path) == CLONE.resolve()  # the same clone as Task A
    assert precomputed_path(task) == TASK_DIR / "subtasks.json"  # task dir recorded, not repo.parent


def test_decomposes_offline_in_order(subtasks):
    assert [s.title for s in subtasks] == ORDER
    assert [s.index for s in subtasks] == [0, 1, 2]
    assert [slice_name(s) for s in subtasks] == SLICES
    ids = [s.id for s in subtasks]
    assert subtasks[0].depends_on == []
    for i, s in enumerate(subtasks):
        assert all(ids.index(d) < i for d in s.depends_on)
        assert {f.file for f in s.interface} <= set(s.files)
        assert s.spec and s.files and {"heldout", "patch"} <= set(s.tags)
        for f in s.interface:
            assert all(":" in p and "=" not in p for p in f.params), (f.name, f.params)


def test_files_cover_interface_and_reference_patch(subtasks):
    for s, name in zip(subtasks, SLICES):
        folder = ROOT / "eval" / "heldout" / name
        iface_files = {f["file"] for f in json.loads((folder / "interface.json").read_text())}
        patch_files = {ln[len("+++ b/"):].strip() for ln in (folder / "reference.patch").read_text().splitlines() if ln.startswith("+++ b/")}
        assert set(s.files) == iface_files | patch_files
        assert (folder / "spec.md").read_text().strip() in s.spec  # the slice spec, framed


def test_shared_registration_functions_pinned_once(subtasks):
    pinned = [(f.file, f.name) for s in subtasks for f in s.interface]
    assert len(pinned) == len(set(pinned))
    dep = subtasks[1]
    # pinned by subtask 1; subtask 2's spec states the cumulative signature instead
    assert "timeout_seconds=None, deprecated=False" in dep.spec
    assert {f.name for f in dep.interface} == {"deprecation_message", "MCPServer.list_tools", "MCPServer.call_tool"}


def test_validate_decomposition_accepts_file():
    data = json.loads((TASK_DIR / "subtasks.json").read_text())
    assert data["task_title"] == TITLE
    assert len(data["subtasks"]) == 3
    validate_decomposition(data)


def test_spec_from_hidden_tests_returns_flattened_modules(subtasks):
    for s, name in zip(subtasks, SLICES):
        spec = spec_from_hidden_tests(s, CLONE)
        assert spec.subtask_id == s.id
        assert spec.cases == [] and spec.expected == {} and spec.properties == [] and spec.visible_ids == []
        assert set(spec.modules) == {p.name for p in (ROOT / "eval" / "heldout" / name / "hidden_tests").glob("*.py")}
        ids = spec.test_node_ids
        assert len(ids) == HIDDEN_COUNTS[name]
        assert spec.hidden_test_ids == ids  # nothing visible to the agents
        for src in spec.modules.values():
            assert "mark.parametrize" not in src
            ast.parse(src)


def test_flatten_parametrized_unrolls_cases():
    src = (
        "import pytest\n\n"
        "@pytest.mark.parametrize(('a', 'b'), [(1, 2), pytest.param(3, 4)])\n"
        "@pytest.mark.parametrize('c', [5, 6])\n"
        "def test_x(a, b, c, tmp_path):\n    assert a < b\n\n"
        "async def test_y():\n    pass\n\n"
        "def test_plain():\n    pass\n"
    )
    out = flatten_parametrized(src)
    assert spec_node_ids({"t.py": out}) == ["t.py::test_x__p0", "t.py::test_x__p1", "t.py::test_x__p2", "t.py::test_x__p3", "t.py::test_y", "t.py::test_plain"]
    tree = ast.parse(out)
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_case_test_x" in names and "anyio_backend" in names
    wrapper = next(n for n in tree.body if getattr(n, "name", "") == "test_x__p3")
    assert [a.arg for a in wrapper.args.args] == ["tmp_path"]
    assert "a=3, b=4, c=6, tmp_path=tmp_path" in ast.unparse(wrapper)
    plain = "def test_plain():\n    pass\n"
    assert flatten_parametrized(plain) == plain  # nothing to do: unchanged


def test_slice_name_fallbacks(subtasks):
    s = subtasks[0].model_copy(update={"tags": []})
    assert slice_name(s) == "mcp_tool_timeout"  # by Task B title
    s = s.model_copy(update={"title": "mcp_tool_deprecation"})
    assert slice_name(s) == "mcp_tool_deprecation"  # by slice folder name
    with pytest.raises(KeyError):
        slice_name(s.model_copy(update={"title": "something else"}))


@needs_clone
def test_flattened_modules_pass_on_the_reference_through_the_harness():
    """The verdict path (crosstest.run_pytest) sees every hidden case as its own spec test, and the
    references pass all of them."""
    from core.harness.crosstest import run_pytest
    from eval.heldout_runner import load_heldout, materialize_reference

    items = {i.name: i for i in load_heldout() if i.kind == "patch"}
    for name in SLICES:
        mods = hidden_test_modules(name)
        ids = spec_node_ids(mods)
        res = run_pytest(str(materialize_reference(items[name])), mods, [], base_repo=CLONE)
        assert set(res) == set(ids), name
        assert all(ok for ok, _ in res.values()), [(k, m) for k, (ok, m) in res.items() if not ok]


# ---------- record_demo --dry-run ----------


def _load_record_demo():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import record_demo
    finally:
        sys.path.pop(0)
    return record_demo


@pytest.fixture
def no_network(monkeypatch):
    import core.config
    import core.harness.spec_tests as spec_tests
    import core.llm.clients as clients
    import core.llm.embeddings as embeddings

    def boom(*a, **k):
        raise AssertionError("dry run must not call any API")

    for mod in (clients, decomposer, spec_tests):
        monkeypatch.setattr(mod, "get_client", boom)
    monkeypatch.setattr(embeddings, "embed_texts", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    for k in ("AGENT_TIMEOUT_S", "REGRESSION_TEST_PATHS", "HELDOUT_SWARM_SIZE", "USE_MEMORY_STORE"):
        monkeypatch.delenv(k, raising=False)  # record_demo.main sets these; restored after the test
    # fake key presence (values never leave the process: no client is built)
    fake = dataclasses.replace(clients.settings, gemini_api_key="test-key", meta_api_key="test-key")
    monkeypatch.setattr(clients, "settings", fake)
    monkeypatch.setattr(core.config, "settings", fake)


def test_record_demo_dry_run_offline(no_network, tmp_path, capsys):
    record_demo = _load_record_demo()
    rc = record_demo.main(["--dry-run", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    if (CLONE / ".git").exists():
        assert rc == 0, out
        assert "preconditions ok" in out
    else:
        assert rc == 1 and "setup.sh" in out
    assert "Task B validates" in out and "held-out slices load" in out
    assert list(tmp_path.iterdir()) == []  # a dry run writes nothing


def test_record_demo_compare_from_summaries(tmp_path):
    record_demo = _load_record_demo()

    def metrics(per, lessons, rmr, adopted=0, rolled=0):
        runs = sum(p["runs"] for p in per)
        ver = sum(p["verified"] for p in per)
        return {"subtasks_total": 3, "subtasks_attempted": len(per), "subtasks_done": sum(p["committed"] for p in per), "stalled": [],
                "per_subtask": per, "runs": runs, "verified": ver, "failed": runs - ver, "errored": 0, "verified_share": ver / runs,
                "lessons_at_start_adopted": 2 if lessons == 0 else 0, "lessons_created": lessons, "reflected_failures": lessons,
                "repeat_mistakes": 0, "repeat_mistake_rate": rmr, "changes": 0, "changes_adopted": adopted, "changes_rolled_back": rolled,
                "changes_candidate": 0, "changes_by_kind": {}, "wall_s": 10.0}

    def row(title, v, n=3):
        return {"title": title, "subtask_id": title, "runs": n, "verified": v, "failed": n - v, "errored": 0,
                "first_pass_verified_share": v / n, "committed": v > 0, "lessons_in_play": 1, "lessons_used": [], "agents": [],
                "lessons_created": [], "ambiguity_flags": 0}

    b0 = metrics([row(t, 1) for t in ORDER], lessons=2, rmr=0.0)
    b1 = metrics([row(t, 3) for t in ORDER], lessons=0, rmr=None)
    a = metrics([row("s1", 2)], lessons=3, rmr=0.25, adopted=2, rolled=1)
    for ph, m in (("a", a), ("b0", b0), ("b1", b1)):
        (tmp_path / record_demo.FILES[ph][1]).write_text(json.dumps({"metrics": m, "store_start": {"trust_carried": ph == "b1"}}))
    args = record_demo.parse_args(["--out", str(tmp_path), "--phase", "compare"])
    from rich.console import Console

    res = record_demo.run_compare(args, Console(record=True))
    assert json.loads((tmp_path / record_demo.RESULTS).read_text()) == res
    c = res["b0_vs_b1"]
    assert c["verified_share"]["delta"] == pytest.approx(2 / 3)
    assert c["failed_runs"] == {"b0": 6, "b1": 0, "delta": -6}
    assert c["lessons_needed"] == {"b0": 2, "b1": 0, "delta": -2}
    assert [r["delta"] for r in c["first_pass_verified_share"]] == pytest.approx([2 / 3] * 3)
    assert res["phases"]["a"]["changes_adopted"] == 2 and res["missing_phases"] == []
    assert any("trust" in x for x in res["caveats"])


def test_record_demo_phases_offline(no_network, tmp_path, monkeypatch):
    """b0 then b1 end to end with run_task stubbed, on a throwaway git repo: reset, store hand-over, summary,
    log, result branch, compare."""
    import subprocess

    import core.orchestrator as orch
    import core.swarm.router as router
    import core.swarm.runner as runner
    from core.models import AgentIdentity, AgentRun, ChangeStatus, Lesson, RunManifest
    from core.store.memory import MemoryStore
    from ui.server import write_snapshot

    record_demo = _load_record_demo()
    for mod, name in ((router, "swarm_identities"), (runner, "MAX_TURNS"), (runner, "TOKEN_BUDGET")):
        monkeypatch.setattr(mod, name, getattr(mod, name))  # restored after the test

    repo = tmp_path / "repo"
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
    repo.mkdir()
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "leftover")
    task_dir = tmp_path / "task_b"
    task_dir.mkdir()
    (task_dir / "task.md").write_text(TASK_FILE.read_text().replace("repo: ../mcp/repo", f"repo: {repo}"))
    (task_dir / "subtasks.json").write_text((TASK_DIR / "subtasks.json").read_text())
    monkeypatch.setattr(record_demo, "TASK_B", task_dir / "task.md")

    # Task A's snapshot: one adopted lesson
    a_store = MemoryStore()
    a_store.upsert_lesson(Lesson(text="validate at registration", embedding=[1.0, 0.0], status=ChangeStatus.adopted))
    out = tmp_path / "demo"
    write_snapshot(a_store, out / "mcp_snapshot.json")

    seen = {}

    def fake_run_task(task, store, run_heldout=None, *, spec_fn=None, snapshot_path=None, **kw):
        seen.setdefault("adopted", []).append(sum(1 for l in store.list_lessons() if l.status == ChangeStatus.adopted))
        assert run_heldout is None and spec_fn is not None
        st = task.subtasks[0]
        spec = spec_fn(st, Path(task.repo_path))
        assert spec.test_node_ids
        agent = AgentIdentity(id="gemini-t2", provider="gemini", model="m")
        for _ in range(3):
            store.save_run(AgentRun(subtask_id=st.id, agent=agent, sandbox_path=""))
        store.save_manifest(RunManifest(task_id=task.id, subtask_id=st.id, agent_ids=["gemini-t2"], lessons_used=[]))
        print("fake run_task on", task.title)
        write_snapshot(store, snapshot_path)
        return {"subtasks_done": 0, "stalled": [st.id], "lessons": 0, "changes": 0, "runs_failed": 1,
                "per_subtask": [{"subtask_id": st.id, "title": st.title, "verified": 2, "failed": 1, "lessons": [], "committed": False}]}

    monkeypatch.setattr(orch, "run_task", fake_run_task)
    rc = record_demo.main(["--out", str(out), "--phase", "b0", "--base-commit", base])
    assert rc == 0
    assert git("rev-parse", "HEAD") == base and git("rev-parse", "record-demo/pre-b0") != base  # old HEAD kept
    assert git("rev-parse", "--abbrev-ref", "HEAD") == "swarm" and git("rev-parse", "record-demo/b0") == base
    rc = record_demo.main(["--out", str(out), "--phase", "b1", "--base-commit", base])
    assert rc == 0
    assert seen["adopted"] == [0, 1]  # b0 fresh store, b1 carries Task A's adopted lesson
    s1 = json.loads((out / "task_b_with_lessons_summary.json").read_text())
    m = s1["metrics"]
    assert m["per_subtask"][0]["runs"] == 3 and m["per_subtask"][0]["first_pass_verified_share"] == pytest.approx(2 / 3)
    assert m["lessons_at_start_adopted"] == 1 and m["lessons_created"] == 0 and s1["error"] is None
    assert "fake run_task on" in (out / "task_b_with_lessons.log").read_text()
    assert (out / "task_b_baseline.json").is_file() and (out / "task_b_with_lessons.json").is_file()
    assert record_demo.main(["--out", str(out), "--phase", "compare"]) == 0
    res = json.loads((out / "results.json").read_text())
    assert res["missing_phases"] == ["a"] and res["b0_vs_b1"]["verified_share"]["delta"] == 0
