"""The per-subtask loop. See docs/PROJECT_OUTLINE.md §4.

context → route → swarm → cross-test → commit → reflect → propose → gate → store
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from rich.console import Console

from core.changes.gate import HeldoutRunner, evaluate_change
from core.changes.proposer import propose_changes
from core.changes.trust import update_trust, weights_for
from core.config import settings
from core.context.builder import build_context
from core.harness.crosstest import breaking_case, cross_test
from core.harness.spec_tests import load_or_author
from core.models import Divergence, LongTask, RunManifest, Subtask
from core.paths.divergence import find_divergence
from core.reflect.reflector import reflect
from core.store.base import Store
from core.swarm.router import route
from core.swarm.runner import run_swarm

console = Console()


def run_subtask(task: LongTask, subtask: Subtask, store: Store, run_heldout: HeldoutRunner | None) -> bool:
    repo = Path(task.repo_path)
    console.rule(f"[bold]Subtask {subtask.index}: {subtask.title}")

    # 0. independent spec tests, authored before any implementation exists (cached per subtask)
    spec = load_or_author(subtask, repo)
    console.print(f"spec tests: {len(spec.cases)} cases, {len(spec.properties)} properties")

    # 1. context
    lessons_block, lesson_ids = build_context(subtask, store)
    console.print(f"lessons in play: {len(lesson_ids)}")

    # 2. route
    agents = route(subtask, store, settings.swarm_size)
    console.print("agents: " + ", ".join(a.id for a in agents))

    # 3. swarm
    runs = asyncio.run(run_swarm(subtask, agents, repo, lesson_ids, store))
    store.save_manifest(RunManifest(task_id=task.id, subtask_id=subtask.id, agent_ids=[a.id for a in agents], lessons_used=lesson_ids))

    # 4–5. cross-test
    result = cross_test(subtask, runs, weights_for(runs, store), spec_cases=spec.cases)
    console.print(f"verified: {len(result.verified_runs)}  failed: {len(result.failed_runs)}  cases: {len(result.cases)}")

    # 6. commit + advance
    agreement = min((mv.agreement for mv in result.majority.values()), default=0.0)
    if not result.verified_runs or agreement <= settings.majority_ambiguous_threshold:
        console.print("[yellow]ambiguous — no verified majority; not advancing")
        return False
    winner = max((r for r in runs if r.id in result.verified_runs), key=lambda r: store.get_trust(r.agent.id).score)
    _commit(Path(winner.sandbox_path), repo, subtask)
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
        lesson = reflect(subtask.spec, run, div, store)
        new_lessons.append(lesson)
        console.print(f"[cyan]lesson ({run.agent.id} @ step {div.step_index}): {lesson.text}")

    # 8–9. propose + gate
    changes = propose_changes(new_lessons, store) + update_trust(runs, result, store)
    for ch in changes:
        if run_heldout is None:
            store.save_change(ch)
            continue
        ch = evaluate_change(ch, store, run_heldout)
        console.print(f"[{'green' if ch.status == 'adopted' else 'red'}]{ch.status}[/] {ch.kind}: {ch.description} — {ch.reason}")
    return True


def run_task(task: LongTask, store: Store, run_heldout: HeldoutRunner | None = None) -> dict:
    done, stalled = 0, []
    for st in sorted(task.subtasks, key=lambda s: s.index):
        if run_subtask(task, st, store, run_heldout):
            done += 1
        else:
            stalled.append(st.id)
            break  # tightly coupled: don't build on an unverified step
    return {"subtasks_done": done, "stalled": stalled, "lessons": len(store.list_lessons()), "changes": len(store.list_changes())}


def _commit(src: Path, repo: Path, subtask: Subtask) -> None:
    """Copy the winning implementation's in-scope files (and its tests) into the repo."""
    for rel in subtask.files:
        s = src / rel
        if s.exists():
            d = repo / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
    tests = src / "tests"
    if tests.exists():
        shutil.copytree(tests, repo / "tests", dirs_exist_ok=True)
    # TODO(A): git commit in repo with message f"subtask {subtask.index}: {subtask.title}"
