"""The per-subtask loop. See docs/PROJECT_OUTLINE.md §4.

context → route → swarm → cross-test → commit → reflect → propose → gate → store

Every LLM-dependent step is injectable so the loop runs offline (see eval/fake_swarm.py):
  - swarm_fn:   same signature as core.swarm.runner.run_swarm
  - spec_fn:    same signature as core.harness.spec_tests.load_or_author
  - reflect_fn: same signature as core.reflect.reflector.reflect
Defaults are the real implementations.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.console import Console

from core.changes.gate import HeldoutRunner, evaluate_change
from core.changes.proposer import propose_changes
from core.changes.trust import update_trust, weights_for
from core.config import settings
from core.context.builder import build_context
from core.harness.crosstest import breaking_case, cross_test
from core.harness.spec_tests import SpecTests, load_or_author
from core.models import AgentIdentity, AgentRun, Divergence, Lesson, LongTask, RunManifest, Subtask
from core.paths.divergence import find_divergence
from core.reflect.reflector import reflect
from core.store.base import Store
from core.swarm.router import route
from core.swarm.runner import run_swarm

console = Console()

N_FUZZ = 200  # fuzz draws per interface function in cross_test; tests and the offline demo lower it

SwarmFn = Callable[[Subtask, list[AgentIdentity], Path, list[str], Store], Awaitable[list[AgentRun]]]
SpecFn = Callable[[Subtask, Path], SpecTests]
ReflectFn = Callable[[str, AgentRun, Divergence, Store], Lesson]


def run_subtask(
    task: LongTask,
    subtask: Subtask,
    store: Store,
    run_heldout: HeldoutRunner | None,
    *,
    swarm_fn: SwarmFn | None = None,
    spec_fn: SpecFn | None = None,
    reflect_fn: ReflectFn | None = None,
    n_fuzz: int | None = None,
    swarm_size: int | None = None,
    report: dict[str, Any] | None = None,
) -> bool:
    swarm_fn = swarm_fn or run_swarm
    spec_fn = spec_fn or load_or_author
    reflect_fn = reflect_fn or reflect
    n_fuzz = N_FUZZ if n_fuzz is None else n_fuzz
    report = report if report is not None else {}
    report.update(subtask_id=subtask.id, title=subtask.title, verified=0, failed=0, lessons=[], changes=[], committed=False)

    repo = Path(task.repo_path)
    console.rule(f"[bold]Subtask {subtask.index}: {subtask.title}")

    # 0. independent spec tests, authored before any implementation exists (cached per subtask)
    spec = spec_fn(subtask, repo)
    console.print(f"spec tests: {len(spec.cases)} cases, {len(spec.properties)} properties")

    # 1. context
    lessons_block, lesson_ids = build_context(subtask, store)
    console.print(f"lessons in play: {len(lesson_ids)}")

    # 2. route
    agents = route(subtask, store, swarm_size or settings.swarm_size)
    console.print("agents: " + ", ".join(a.id for a in agents))

    # 3. swarm
    runs = asyncio.run(swarm_fn(subtask, agents, repo, lesson_ids, store))
    store.save_manifest(RunManifest(task_id=task.id, subtask_id=subtask.id, agent_ids=[a.id for a in agents], lessons_used=lesson_ids))

    # 4–5. cross-test
    result = cross_test(subtask, runs, weights_for(runs, store), spec_cases=spec.cases, n_fuzz=n_fuzz)
    console.print(f"verified: {len(result.verified_runs)}  failed: {len(result.failed_runs)}  cases: {len(result.cases)}")
    report.update(verified=len(result.verified_runs), failed=len(result.failed_runs), failed_run_ids=list(result.failed_runs))

    # 6. commit + advance
    agreement = min((mv.agreement for mv in result.majority.values()), default=0.0)
    if not result.verified_runs or agreement <= settings.majority_ambiguous_threshold:
        console.print("[yellow]ambiguous — no verified majority; not advancing")
        return False
    winner = max((r for r in runs if r.id in result.verified_runs), key=lambda r: store.get_trust(r.agent.id).score)
    _commit(Path(winner.sandbox_path), repo, subtask)
    report["committed"] = True
    console.print(f"committed implementation from {winner.agent.id}")

    # 7. reflect
    verified_runs = [r for r in runs if r.id in result.verified_runs]
    new_lessons = []
    for run in runs:
        if run.id not in result.failed_runs:
            continue
        div = find_divergence(run, verified_runs)
        if div is None:
            continue
        bc = breaking_case(result, run.id)
        if bc:
            div.breaking_case, div.majority_outcome = bc
        lesson = reflect_fn(subtask.spec, run, div, store)
        new_lessons.append(lesson)
        report["lessons"].append(lesson.id)
        console.print(f"[cyan]lesson ({run.agent.id} @ step {div.step_index}): {lesson.text}")

    # 8–9. propose + gate
    changes = propose_changes(new_lessons, store) + update_trust(runs, result, store)
    for ch in changes:
        if run_heldout is None:
            store.save_change(ch)
            report["changes"].append(ch.id)
            continue
        ch = evaluate_change(ch, store, run_heldout)
        report["changes"].append(ch.id)
        console.print(f"[{'green' if ch.status == 'adopted' else 'red'}]{ch.status}[/] {ch.kind}: {ch.description} — {ch.reason}")
    return True


def run_task(
    task: LongTask,
    store: Store,
    run_heldout: HeldoutRunner | None = None,
    *,
    swarm_fn: SwarmFn | None = None,
    spec_fn: SpecFn | None = None,
    reflect_fn: ReflectFn | None = None,
    n_fuzz: int | None = None,
    swarm_size: int | None = None,
) -> dict:
    done, stalled, per_subtask = 0, [], []
    for st in sorted(task.subtasks, key=lambda s: s.index):
        rep: dict[str, Any] = {}
        per_subtask.append(rep)
        ok = run_subtask(
            task, st, store, run_heldout,
            swarm_fn=swarm_fn, spec_fn=spec_fn, reflect_fn=reflect_fn, n_fuzz=n_fuzz, swarm_size=swarm_size, report=rep,
        )
        if ok:
            done += 1
        else:
            stalled.append(st.id)
            break  # tightly coupled: don't build on an unverified step
    return {
        "subtasks_done": done,
        "stalled": stalled,
        "lessons": len(store.list_lessons()),
        "changes": len(store.list_changes()),
        "runs_failed": sum(r.get("failed", 0) for r in per_subtask),
        "per_subtask": per_subtask,
    }


def _commit(src: Path, repo: Path, subtask: Subtask) -> None:
    """Copy the winning implementation's in-scope files (and its tests) into the repo.

    Runs `git add` + `git commit` only when `repo` is the top level of its own git repository;
    a repo nested inside another checkout without its own .git (e.g. demo_task/repo) is never committed.
    """
    touched: list[str] = []
    for rel in subtask.files:
        s = src / rel
        if s.exists():
            d = repo / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            touched.append(rel)
    tests = src / "tests"
    if tests.exists() and tests.resolve() != (repo / "tests").resolve():
        shutil.copytree(tests, repo / "tests", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        touched.append("tests")
    if touched and _is_own_git_repo(repo):
        subprocess.run(["git", "-C", str(repo), "add", "--", *touched], capture_output=True)
        r = subprocess.run(["git", "-C", str(repo), "commit", "-m", f"subtask {subtask.index}: {subtask.title}"], capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            console.print(f"[yellow]git commit failed: {(r.stderr or r.stdout).strip()[:200]}")


def _is_own_git_repo(repo: Path) -> bool:
    if not (repo / ".git").exists():
        return False
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == repo.resolve()
