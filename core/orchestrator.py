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
# (spec, failed, verified, breaking, failed_outcome) -> Divergence | None
LocateFn = Callable[[str, AgentRun, list[AgentRun], Any, Any], Divergence | None]

SPEC_AMBIGUITY_MIN_CONFIDENCE = 0.6  # SubQ kind=spec_ambiguity above this → flag, no lesson charged to the agent


def default_locate(spec: str, failed: AgentRun, verified: list[AgentRun], breaking=None, failed_outcome=None) -> Divergence | None:
    """SubQ Snap root-cause decision when a key is configured, else the embedding-path heuristic."""
    from core.llm.decisions import SubQDecisions, locate_divergence_subq

    if SubQDecisions.is_configured():
        return locate_divergence_subq(spec, failed, verified, breaking=breaking, failed_outcome=failed_outcome, fallback=True)
    div = find_divergence(failed, verified)
    if div is not None:
        div.signals = ["path"]
        if breaking:
            div.breaking_case, div.majority_outcome = breaking
    return div


def run_subtask(
    task: LongTask,
    subtask: Subtask,
    store: Store,
    run_heldout: HeldoutRunner | None,
    *,
    swarm_fn: SwarmFn | None = None,
    spec_fn: SpecFn | None = None,
    reflect_fn: ReflectFn | None = None,
    locate_fn: LocateFn | None = None,
    n_fuzz: int | None = None,
    swarm_size: int | None = None,
    report: dict[str, Any] | None = None,
) -> bool:
    swarm_fn = swarm_fn or run_swarm
    spec_fn = spec_fn or load_or_author
    reflect_fn = reflect_fn or reflect
    locate_fn = locate_fn or default_locate
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
    report.update(
        verified=len(result.verified_runs), failed=len(result.failed_runs),
        failed_run_ids=list(result.failed_runs), verified_run_ids=list(result.verified_runs),
    )

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
    divergences: list[Divergence] = getattr(store, "divergences", None) or []
    for run in runs:
        if run.id not in result.failed_runs:
            continue
        bc = breaking_case(result, run.id)
        failed_out = result.outcomes.get(run.id, {}).get(bc[0].id) if bc else None
        div = locate_fn(subtask.spec, run, verified_runs, bc, failed_out)
        if div is None:
            continue
        if bc and div.breaking_case is None:
            div.breaking_case, div.majority_outcome = bc
        divergences.append(div)
        if failed_out is not None:  # the "actual" outcome for the UI card
            fo = getattr(store, "failed_outcomes", None) or {}
            fo[run.id] = failed_out
            store.failed_outcomes = fo
        probs = f" p={div.step_probabilities.get(f'S{div.step_index}', 0):.2f}" if div.step_probabilities else ""
        console.print(f"[magenta]root cause ({run.agent.id}): step {div.step_index} via {'+'.join(div.signals) or 'path'}{probs}"
                      + (f", kind={div.kind} ({div.kind_confidence:.2f})" if div.kind else ""))
        # SubQ says the spec itself is ambiguous: flag for the decomposer, don't charge the agent with a lesson.
        if div.kind == "spec_ambiguity" and (div.kind_confidence or 0) >= SPEC_AMBIGUITY_MIN_CONFIDENCE:
            report.setdefault("ambiguity_flags", []).append({"run_id": run.id, "step_index": div.step_index, "step": div.step_text})
            console.print(f"[yellow]spec ambiguity flagged at step {div.step_index}; no lesson")
            continue
        lesson = reflect_fn(subtask.spec, run, div, store)
        if div.generalizes is not None and div.generalizes < lesson.confidence:
            lesson.confidence = float(div.generalizes)  # SubQ doubts this generalizes: start the lesson lower
            store.upsert_lesson(lesson)
        new_lessons.append(lesson)
        report["lessons"].append(lesson.id)
        console.print(f"[cyan]lesson ({run.agent.id} @ step {div.step_index}): {lesson.text}")
    store.divergences = divergences  # read by the UI snapshot and /api/divergences

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
    locate_fn: LocateFn | None = None,
    n_fuzz: int | None = None,
    swarm_size: int | None = None,
    snapshot_path: str | Path | None = None,
) -> dict:
    """Run every subtask in order, stopping at the first one that does not verify.

    With `snapshot_path`, the store is written there as a UI snapshot (ui.server.write_snapshot)
    after each subtask and again at the end (also when a subtask raises). Before each write the
    cross-test verdicts so far are set on the store as plain lists, `store.failed_run_ids` and
    `store.verified_run_ids` (accumulated across subtasks), so the reasoning map colours runs.
    """
    done, stalled, per_subtask = 0, [], []
    failed_ids: list[str] = list(getattr(store, "failed_run_ids", None) or [])
    verified_ids: list[str] = list(getattr(store, "verified_run_ids", None) or [])

    def snapshot(rep: dict[str, Any] | None = None) -> None:
        if snapshot_path is None:
            return
        from ui.server import write_snapshot  # lazy: the UI (fastapi) is optional for the loop

        if rep is not None:
            failed_ids.extend(i for i in rep.get("failed_run_ids", []) if i not in failed_ids)
            verified_ids.extend(i for i in rep.get("verified_run_ids", []) if i not in verified_ids)
        store.failed_run_ids = list(failed_ids)
        store.verified_run_ids = list(verified_ids)
        write_snapshot(store, snapshot_path)

    try:
        for st in sorted(task.subtasks, key=lambda s: s.index):
            rep: dict[str, Any] = {}
            per_subtask.append(rep)
            try:
                ok = run_subtask(
                    task, st, store, run_heldout,
                    swarm_fn=swarm_fn, spec_fn=spec_fn, reflect_fn=reflect_fn, locate_fn=locate_fn, n_fuzz=n_fuzz, swarm_size=swarm_size, report=rep,
                )
            finally:
                snapshot(rep)
            if ok:
                done += 1
            else:
                stalled.append(st.id)
                break  # tightly coupled: don't build on an unverified step
    finally:
        snapshot()
    summary = {
        "subtasks_done": done,
        "stalled": stalled,
        "lessons": len(store.list_lessons()),
        "changes": len(store.list_changes()),
        "runs_failed": sum(r.get("failed", 0) for r in per_subtask),
        "per_subtask": per_subtask,
    }
    if snapshot_path is not None:
        summary["snapshot_path"] = str(snapshot_path)
    return summary


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
