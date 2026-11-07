"""Patch-kind held-out slices: detection, reference materialization, hidden tests in the sandbox, scoring.

Everything is built in tmp_path (a tiny git repo + a held-out folder); no network, no LLM, and the
MCP demo clone is never required.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.models import AgentRun

OPS_BASE = "def add(a: int, b: int) -> int:\n    return a + b\n"
OPS_REF = OPS_BASE + "\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n"
HIDDEN_OK = "from calc.ops import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n\n\ndef test_mul_zero():\n    assert mul(0, 9) == 0\n"
HIDDEN_BROKEN = "from calc.ops import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 13\n"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def offline(monkeypatch, tmp_path):
    import core.llm.clients

    def no_network(*a, **k):
        raise AssertionError("network LLM client requested in offline mode")

    monkeypatch.setattr(core.llm.clients, "get_client", no_network)
    monkeypatch.setenv("USE_MEMORY_STORE", "1")
    gitconfig = tmp_path / "gitconfig"  # isolate from the user's global git config (signing, hooks)
    gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.com\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _make_slice(heldout: Path, name: str, hidden: str) -> Path:
    """<heldout>/<name>/ with its own repo/ (git, at the base commit), base_commit, reference.patch, hidden_tests/."""
    d = heldout / name
    repo = d / "repo"
    _write(repo / "calc" / "__init__.py", "")
    _write(repo / "calc" / "ops.py", OPS_BASE)
    _write(repo / "tests" / "test_ops.py", "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "calc" / "ops.py").write_text(OPS_REF)
    (d / "reference.patch").write_text(_git(repo, "diff"))
    _git(repo, "checkout", "-q", "--", ".")  # back to the base
    _git(repo, "commit", "-q", "--allow-empty", "-m", "a later commit the held-out base must not see")
    (repo / "calc" / "ops.py").write_text(OPS_BASE + "\n# uncommitted noise\n")
    _write(d / "base_commit", base + "\n")
    _write(d / "spec.md", "Add mul(a, b) -> int to calc/ops.py returning the product.")
    _write(d / "hidden_tests" / "test_mul.py", hidden)
    return d


def _make_functions_slice(heldout: Path) -> None:
    d = heldout / "late_fee"
    _write(d / "spec.md", "late_fee(days): 0 up to 100, else 25.")
    _write(d / "interface.json", json.dumps([{"name": "late_fee", "file": "fees.py", "params": ["days: int"], "returns": "int"}]))
    _write(d / "reference" / "fees.py", "def late_fee(days: int) -> int:\n    return 0 if days <= 100 else 25\n")


def test_patch_slice_loads_materializes_and_scores(tmp_path, offline):
    from core.store.memory import MemoryStore
    from eval.heldout_runner import (
        _run_one,
        load_heldout,
        materialize_base,
        materialize_reference,
        run_hidden_tests,
    )

    heldout = tmp_path / "heldout"
    _make_slice(heldout, "calc_mul", HIDDEN_OK)
    _make_functions_slice(heldout)
    (heldout / "incomplete").mkdir()  # no spec/patch/tests: skipped

    items = {it.name: it for it in load_heldout(heldout)}
    assert sorted(items) == ["calc_mul", "late_fee"]
    assert items["late_fee"].kind == "functions"
    item = items["calc_mul"]
    assert item.kind == "patch"
    assert item.base_repo == heldout / "calc_mul" / "repo"
    assert item.reference_patch == heldout / "calc_mul" / "reference.patch"
    assert item.hidden_tests_dir == heldout / "calc_mul" / "hidden_tests"
    assert item.reference_dir is None and len(item.base_commit) == 40
    assert item.subtask.files == ["calc/ops.py"]  # no interface.json: files come from the patch
    st, ref = item  # back-compat tuple unpacking
    assert st is item.subtask and ref is None

    # work dir inside an enclosing checkout: git apply must not resolve paths against the outer repo
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    work = outer / "runs"

    ref_tree = materialize_reference(item, work)
    assert ref_tree == work / "heldout_ref" / "calc_mul" / item.base_commit / "repo"
    assert (ref_tree / "calc" / "ops.py").read_text() == OPS_REF
    assert not (ref_tree / ".git").exists()
    assert materialize_reference(item, work) == ref_tree  # cached

    base_tree = materialize_base(item, work)
    assert (base_tree / "calc" / "ops.py").read_text() == OPS_BASE  # the base commit, not HEAD or the working tree

    ok = run_hidden_tests(ref_tree, item)
    assert ok.passed and ok.tests == 2 and ok.failures == 0 and ok.errors == 0, ok.output
    assert not (ref_tree / "tests" / "_hidden_calc_mul").exists()  # copied tests are removed again
    bad = run_hidden_tests(base_tree, item)
    assert not bad.passed and bad.failures + bad.errors >= 1

    runs_dir = tmp_path / "runs"

    async def swarm(subtask, agents, repo, lesson_ids, store):
        assert (Path(repo) / "calc" / "ops.py").read_text() == OPS_BASE
        assert not (Path(repo) / "tests" / "_hidden_calc_mul").exists()  # hidden tests never reach the swarm
        out = []
        for i, src in enumerate([ref_tree, Path(repo)]):
            run = AgentRun(subtask_id=subtask.id, agent=agents[i % len(agents)], sandbox_path=str(runs_dir / f"sb{i}"))
            shutil.copytree(src, run.sandbox_path)
            out.append(run)
        return out

    m = _run_one(item, [], MemoryStore(), swarm_fn=swarm, swarm_size=2, runs_dir=runs_dir)
    assert m["correct"] == 0.5
    assert m["repeats"] == 0.0  # no failure regions in an empty store


def test_broken_reference_raises(tmp_path, offline):
    from core.store.memory import MemoryStore
    from eval.heldout_runner import _run_one, load_heldout

    heldout = tmp_path / "heldout"
    _make_slice(heldout, "calc_broken", HIDDEN_BROKEN)
    [item] = load_heldout(heldout)

    async def swarm(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("swarm ran on a broken slice")

    with pytest.raises(RuntimeError, match="calc_broken is broken"):
        _run_one(item, [], MemoryStore(), swarm_fn=swarm, swarm_size=2, runs_dir=tmp_path / "runs")


def test_patch_slice_falls_back_to_demo_repo(tmp_path):
    from eval.heldout_runner import DEMO_REPO, DEMO_TASK_DIR, load_heldout

    d = tmp_path / "heldout" / "mcp_x"
    _write(d / "spec.md", "x")
    _write(d / "reference.patch", "--- a/src/m.py\n+++ b/src/m.py\n@@ -0,0 +1 @@\n+x = 1\n")
    _write(d / "hidden_tests" / "test_x.py", "def test_x():\n    pass\n")
    [item] = load_heldout(tmp_path / "heldout")
    assert item.kind == "patch" and item.base_repo == DEMO_REPO  # whether or not the clone exists
    assert item.base_commit == (DEMO_TASK_DIR / "base_commit").read_text().strip()
    assert item.subtask.files == ["src/m.py"]
