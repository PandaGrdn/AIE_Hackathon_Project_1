"""The whole per-subtask loop, offline: fake swarm/spec/reflector, MemoryStore, temp held-out set."""

from __future__ import annotations

import json
import subprocess
from functools import partial
from pathlib import Path

import pytest

from core.models import ChangeStatus, FunctionSig, LongTask, Subtask

TIERS = '''\
def tier_for(amount: float) -> str:
    """'basic' up to and including 100, 'plus' up to and including 1000, else 'pro'."""
    if amount < 0:
        raise ValueError("negative amount")
    if amount <= 100:
        return "basic"
    if amount <= 1000:
        return "plus"
    return "pro"
'''

BONUS = '''\
def bulk_bonus(units: int) -> int:
    """2 bonus units when units is at least 100."""
    if units >= 100:
        return 2
    return 0
'''

LATE_FEE = '''\
def late_fee(days: int) -> int:
    """No fee up to and including 100 days late, then 25."""
    if days < 0:
        raise ValueError("negative days")
    if days <= 100:
        return 0
    return 25
'''


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _make_task(root: Path) -> LongTask:
    repo, ref = root / "task" / "repo", root / "task" / "reference"
    _write(repo / "pricing" / "__init__.py", "")
    _write(ref / "pricing" / "__init__.py", "")
    _write(ref / "pricing" / "tiers.py", TIERS)
    _write(ref / "pricing" / "bonus.py", BONUS)
    s1 = Subtask(index=0, title="tiers", spec="tier_for: basic up to 100 inclusive, plus up to 1000 inclusive, else pro; negative raises ValueError.",
                 interface=[FunctionSig(name="tier_for", file="pricing/tiers.py", params=["amount: float"], returns="str")], files=["pricing/tiers.py"])
    s2 = Subtask(index=1, title="bonus", spec="bulk_bonus: 2 when units is at least 100, else 0.",
                 interface=[FunctionSig(name="bulk_bonus", file="pricing/bonus.py", params=["units: int"], returns="int")], files=["pricing/bonus.py"], depends_on=[s1.id])
    return LongTask(title="offline pricing", description="tiers then bonus", repo_path=str(repo), subtasks=[s1, s2])


def _make_heldout(root: Path) -> Path:
    d = root / "heldout" / "late_fee"
    _write(d / "spec.md", "late_fee(days): 0 up to and including 100 days, else 25; negative raises ValueError.")
    _write(d / "interface.json", json.dumps([{"name": "late_fee", "file": "fees.py", "params": ["days: int"], "returns": "int"}]))
    _write(d / "reference" / "fees.py", LATE_FEE)
    return root / "heldout"


@pytest.fixture
def offline(monkeypatch):
    """No API keys, no network: any LLM client or embedding API call fails the test."""
    import core.decompose.decomposer
    import core.harness.spec_tests
    import core.llm.clients
    import core.reflect.reflector
    import core.swarm.runner
    from eval.fake_swarm import use_fake_embeddings

    for k in ("SUBQ_API_KEY", "META_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "MONGODB_URI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USE_MEMORY_STORE", "1")

    def no_network(*a, **k):
        raise AssertionError("network LLM client requested in offline mode")

    for mod in (core.llm.clients, core.swarm.runner, core.harness.spec_tests, core.reflect.reflector, core.decompose.decomposer):
        monkeypatch.setattr(mod, "get_client", no_network)
    use_fake_embeddings(monkeypatch.setattr)


def test_offline_loop_end_to_end(tmp_path, offline):
    from core.orchestrator import run_task
    from core.store.memory import MemoryStore
    from eval.fake_swarm import fake_reflect, fake_spec_tests, fake_swarm
    from eval.heldout_runner import make_heldout_runner
    from eval.metrics import summarize

    task = _make_task(tmp_path)
    heldout_dir = _make_heldout(tmp_path)
    runs_dir = tmp_path / "runs"
    store = MemoryStore()

    runner = make_heldout_runner(store, swarm_fn=fake_swarm(runs_dir=runs_dir), heldout_dir=heldout_dir, n_fuzz=5, swarm_size=4, runs_dir=runs_dir)
    summary = run_task(
        task, store, runner,
        swarm_fn=fake_swarm(runs_dir=runs_dir),  # reference found at <repo>/../reference
        spec_fn=partial(fake_spec_tests, n_fuzz=2),
        reflect_fn=fake_reflect,
        n_fuzz=5,
        swarm_size=4,
    )

    # every subtask verified and committed (no git repo here, so files are copied but not committed)
    assert summary["subtasks_done"] == 2 and summary["stalled"] == []
    repo = Path(task.repo_path)
    assert (repo / "pricing" / "tiers.py").read_text() == TIERS
    assert (repo / "pricing" / "bonus.py").read_text() == BONUS

    # the mutant was caught by cross-test in each subtask
    assert summary["runs_failed"] >= 1
    assert all(r["failed"] >= 1 for r in summary["per_subtask"])

    # lessons with failure regions
    lessons = store.list_lessons()
    assert lessons
    assert any(r.lesson_id == l.id for l in lessons for r in store.regions.values())
    assert any("At least N" in l.text or "Up to N" in l.text for l in lessons)

    # every change was decided by the held-out gate with scores filled
    changes = store.list_changes()
    assert changes
    decided = [c for c in changes if c.status in (ChangeStatus.adopted, ChangeStatus.rolled_back)]
    assert decided and all(c.heldout_before and c.heldout_after for c in decided)

    # the first lesson (inclusive 'up to N') fixes the held-out late_fee mutant: adopted on improvement
    adopted = [c for c in decided if c.status == ChangeStatus.adopted]
    assert adopted, [(c.kind, c.status, c.heldout_before, c.heldout_after) for c in changes]
    first = adopted[0]
    assert first.heldout_after["accuracy"] > first.heldout_before["accuracy"]
    assert first.heldout_before["repeat_mistake_rate"] == 1.0  # the held-out mutant repeats a known mistake
    assert first.heldout_after["repeat_mistake_rate"] == 0.0

    m = summarize(store)
    assert m["changes_adopted"] >= 1 and m["lessons_total"] == len(lessons)
    assert m["repeat_mistake_rate"] is not None


def test_fake_spec_tests_without_reference_is_empty(tmp_path, offline):
    from eval.fake_swarm import fake_spec_tests

    st = Subtask(index=0, title="x", spec="", interface=[FunctionSig(name="f", file="m.py", params=["x: int"])], files=["m.py"])
    spec = fake_spec_tests(st, tmp_path / "repo")
    assert spec.cases == [] and spec.properties == []


def test_mutate_source_skips_arrow_and_picks_first_op():
    from eval.fake_swarm import mutate_source

    src = "def f(x: int) -> int:\n    return 1 if x > 100 else 0\n"
    assert mutate_source(src, ">", ">=") == "def f(x: int) -> int:\n    return 1 if x >= 100 else 0\n"
    assert mutate_source(src, "<=", "<") is None


def test_commit_only_in_own_git_repo(tmp_path, monkeypatch):
    from core.orchestrator import _commit

    gitconfig = tmp_path / "gitconfig"  # isolate from the user's global git config (signing, hooks)
    gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.com\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    st = Subtask(index=3, title="tiers", spec="", interface=[], files=["pricing/tiers.py"])
    src = tmp_path / "impl"
    _write(src / "pricing" / "tiers.py", TIERS)

    # nested inside an outer repo, without its own .git: files copied, nothing committed
    outer = tmp_path / "outer"
    nested = outer / "repo"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    _commit(src, nested, st)
    assert (nested / "pricing" / "tiers.py").exists()
    log = subprocess.run(["git", "-C", str(outer), "log", "--oneline"], capture_output=True, text=True)
    assert log.returncode != 0 or log.stdout.strip() == ""

    # its own repository: committed with the subtask message
    own = tmp_path / "own"
    own.mkdir()
    subprocess.run(["git", "init", "-q", str(own)], check=True)
    _commit(src, own, st)
    log = subprocess.run(["git", "-C", str(own), "log", "--format=%s"], capture_output=True, text=True, check=True)
    assert log.stdout.strip() == "subtask 3: tiers"
