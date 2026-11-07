"""The offline (`--fake`) path for the MCP patch-series task: detection, per-subtask reference
materialization, spec-module extraction and a caught mutant.

Tests that need the SDK clone (demo_task/mcp/repo, created by `swarm setup demo_task/mcp`) work on a
throwaway local clone under tmp_path, so the demo repo itself is never committed to or reset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.decompose.decomposer import decompose, load_task
from core.models import Divergence
from core.store import MemoryStore
from eval.fake_swarm import (
    CONSENSUS_ASSUMPTION,
    _series_target,
    fake_patch_spec_tests,
    fake_reflect,
    find_patch_series,
    materialize_patch_reference,
    parse_patch,
    patch_test_modules,
    pick_patch_mutant,
    use_fake_embeddings,
)

ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = ROOT / "demo_task" / "mcp"
DEMO_REPO = TASK_DIR / "repo"
BASE = (TASK_DIR / "base_commit").read_text().strip()
S1_TESTS = "tests/server/mcpserver/test_limits_s1_model.py"
LIMITS = "src/mcp/server/mcpserver/limits.py"

needs_clone = pytest.mark.skipif(
    not (DEMO_REPO / ".git").exists(), reason="demo_task/mcp/repo is not cloned (run: swarm setup demo_task/mcp)"
)
needs_venv = pytest.mark.skipif(
    not (DEMO_REPO / ".venv" / "bin" / "python").exists(), reason="demo_task/mcp/repo has no uv venv (run: swarm setup demo_task/mcp)"
)


@pytest.fixture
def patches() -> list[Path]:
    return find_patch_series(TASK_DIR)


@pytest.fixture
def subtasks():
    return decompose(load_task(TASK_DIR / "task.md")).subtasks


@pytest.fixture
def clone(tmp_path) -> Path:
    """A local clone of the demo repo at the base commit, sharing the demo repo's venv via a symlink."""
    dst = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "--local", "--no-checkout", str(DEMO_REPO), str(dst)], check=True)
    subprocess.run(["git", "-C", str(dst), "checkout", "-q", "-B", "swarm", BASE], check=True)
    for k, v in (("user.name", "test"), ("user.email", "test@localhost")):
        subprocess.run(["git", "-C", str(dst), "config", k, v], check=True)
    if (DEMO_REPO / ".venv").is_dir():
        (dst / ".venv").symlink_to(DEMO_REPO / ".venv", target_is_directory=True)
    return dst


def _commit_patch(repo: Path, patch: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "apply", str(patch)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A", "src", "tests"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", patch.stem], check=True)


# ---------- no clone needed ----------


def test_patch_series_detection(patches):
    assert [p.name[:2] for p in patches] == ["01", "02", "03", "04", "05", "06", "07"]
    assert find_patch_series(ROOT / "demo_task" / "pricing") == []  # a reference dir, not a patch series
    assert find_patch_series(ROOT / "demo_task" / "nope") == []


def test_patch_count_matches_subtasks(patches, subtasks):
    assert len(patches) == len(subtasks)


def test_parse_patch_01(patches):
    files = {f.path: f for f in parse_patch(patches[0])}
    assert set(files) == {LIMITS, S1_TESTS}
    assert files[LIMITS].new and files[S1_TESTS].new
    assert files[LIMITS].added == set(range(1, 163))  # a new 162-line file: every line is added


def test_every_patch_writes_only_subtask_files_and_tests(patches, subtasks):
    """orchestrator._commit copies subtask.files plus the winner's tests/, so each patch must stay inside them."""
    for patch, st in zip(patches, subtasks):
        for f in parse_patch(patch):
            assert f.path in st.files or f.path.startswith("tests/"), (patch.name, f.path)


def test_fake_reflect_maps_patch_mutant_assumption_to_catalog_lesson():
    use_fake_embeddings()
    from eval.fake_swarm import MUTATIONS, _steps_for
    from core.models import AgentIdentity, AgentRun, Subtask

    m = MUTATIONS[0]
    st = Subtask(index=0, title="t", spec="s", interface=[], files=[])
    run = AgentRun(subtask_id=st.id, agent=AgentIdentity(id="a", provider="p", model="m"), sandbox_path="")
    run.steps = _steps_for(st, f"a call exactly per_seconds old is still inside the window; {m.assumption}")
    div = Divergence(run_id=run.id, step_index=1, step_text=run.steps[1].as_text(), distance=1.0, consensus_step_texts=[])
    lesson = fake_reflect("s", run, div, MemoryStore())
    assert lesson.text.startswith(m.lesson)


# ---------- need the clone ----------


@needs_clone
def test_reference_materialization_for_subtasks_1_and_2(clone, patches, subtasks, tmp_path):
    ref_root = tmp_path / "mcp_ref"
    t1 = materialize_patch_reference(clone, patches[0], subtasks[0].index, ref_root)
    assert t1 == ref_root / "1" / "repo"
    assert (t1 / LIMITS).is_file() and (t1 / S1_TESTS).is_file()
    assert not (t1 / ".git").exists() and not (t1 / ".venv").exists()
    assert not (clone / LIMITS).exists()  # the repo itself is untouched
    assert materialize_patch_reference(clone, patches[0], 0, ref_root) == t1  # cached while HEAD is unchanged

    # subtask 2 builds on the committed state after subtask 1
    _commit_patch(clone, patches[0])
    t2 = materialize_patch_reference(clone, patches[1], subtasks[1].index, ref_root)
    assert t2 == ref_root / "2" / "repo"
    assert (t2 / LIMITS).is_file()
    assert "def get_limits" in (t2 / "src/mcp/server/mcpserver/tools/tool_manager.py").read_text()
    assert (t2 / "tests/server/mcpserver/test_limits_s2_tool_policies.py").is_file()


@needs_clone
def test_reference_materialization_fails_loudly_out_of_order(clone, patches, tmp_path):
    with pytest.raises(RuntimeError, match="does not apply"):
        materialize_patch_reference(clone, patches[2], 2, tmp_path / "mcp_ref")  # patch 03 edits limits.py, which patch 01 creates


@needs_clone
def test_fake_spec_modules_for_patch_01(clone, patches, subtasks, tmp_path):
    spec = fake_patch_spec_tests(subtasks[0], clone, patches, ref_root=tmp_path / "mcp_ref")
    assert list(spec.modules) == [S1_TESTS]
    src = spec.modules[S1_TESTS]
    added = "".join(
        line[1:] + "\n" for line in patches[0].read_text().split(f"+++ b/{S1_TESTS}\n", 1)[1].splitlines() if line.startswith("+")
    )
    assert src == added  # a new file: the module is exactly the patch's added lines
    assert "class TestEvaluateRateLimit" in src
    assert spec.cases == [] and spec.visible_ids == [] and spec.expected == {}
    assert spec.subtask_id == subtasks[0].id
    tree = tmp_path / "mcp_ref" / "1" / "repo"
    assert patch_test_modules(patches[0], tree) == spec.modules


@needs_clone
@needs_venv
def test_s1_mutant_is_caught_by_the_s1_test_module(clone, patches, subtasks, tmp_path):
    from eval.fake_swarm import _junit_fails, _link_tree, _write_fresh

    target = _series_target(subtasks[0], clone, patches, tmp_path / "mcp_ref")
    mut = pick_patch_mutant(target, 0)
    assert mut is not None and mut.caught
    assert mut.file == LIMITS
    assert mut.line in {f.path: f for f in parse_patch(patches[0])}[LIMITS].added
    assert mut.assumption != CONSENSUS_ASSUMPTION and mut.assumption.endswith(mut.mutation.assumption)
    assert "per_seconds" in mut.assumption

    # independently: the reference passes the S1 module, the mutated tree fails it
    tests_dir = target.slot / "spec_tests"
    sb = tmp_path / "sb"
    _link_tree(target.tree, sb)
    assert _junit_fails(sb, tests_dir, clone, "s1")[0] is False
    _write_fresh(sb / mut.file, mut.source)
    assert _junit_fails(sb, tests_dir, clone, "s1")[0] is True
    assert (target.tree / mut.file).read_text() != mut.source  # the hard-linked reference was not modified


@needs_clone
def test_reset_repo_only_when_ahead(clone, patches, tmp_path):
    from core.cli import _maybe_reset_repo

    base_file = tmp_path / "base_commit"
    base_file.write_text(BASE + "\n")
    head = lambda: subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    at_base = head()
    _maybe_reset_repo(clone, base_file, None)  # at the base: nothing to do
    assert head() == at_base
    _commit_patch(clone, patches[0])
    assert head() != at_base
    _maybe_reset_repo(clone, base_file, False)  # --no-reset-repo
    assert head() != at_base
    _maybe_reset_repo(clone, base_file, None)  # default: ahead of the base -> reset
    assert head() == at_base and not (clone / LIMITS).exists()
