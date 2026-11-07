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
from core.models import AgentIdentity, AgentRun, ChangeStatus, Divergence, Lesson, LongTask, RunManifest, Subtask
from core.paths.divergence import find_divergence
from core.reflect.reflector import reflect
from core.store.base import Store
from core.swarm.router import route
from core.swarm.runner import run_swarm

console = Console()

N_FUZZ = 200  # fuzz draws per interface function in cross_test; tests and the offline demo lower it

SwarmFn = Callable[[Subtask, list[AgentIdentity], Path, list[str], Store], Awaitable[list[AgentRun]]]
SpecFn = Callable[[Subtask, Path], SpecTests]
ReflectFn = Callable[..., Lesson]  # (spec, failed, div, store, result=CrossTestResult | None)
# (spec, failed, verified, breaking, failed_outcome) -> Divergence | None
LocateFn = Callable[[str, AgentRun, list[AgentRun], Any, Any], Divergence | None]

SPEC_AMBIGUITY_MIN_CONFIDENCE = 0.6  # SubQ kind=spec_ambiguity above this → flag, no lesson charged to the agent
# A SubQ root-step probability at or above this counts as confirmation of the divergence (counterfactual
# replay is the other confirmation path; it is not wired into the loop yet). Confirmed lessons create
# their failure region immediately; unconfirmed ones wait for a second hit (core/reflect/dedup.py).
SUBQ_CONFIRM_P = 0.7


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
    retries_left: int = 1,
    extra_lesson_ids: list[str] | None = None,
) -> bool:
    """One subtask: context → route → swarm → verdict → commit → reflect → propose → gate.

    When no run verifies, the failures are still reflected into lessons and the subtask is retried once
    with those provisional lessons injected (`extra_lesson_ids`); they are gated like any other lesson
    after the retry. That is the loop learning inside a subtask instead of stalling on its first all-fail.
    """
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
    n_mod_tests = sum(src.count("def test") for src in getattr(spec, "modules", {}).values())
    console.print(f"spec tests: {len(getattr(spec, 'modules', {}))} hidden module(s) / {n_mod_tests} tests, {len(spec.cases)} cases, {len(spec.properties)} properties")

    # 1. context
    lessons_block, lesson_ids = build_context(subtask, store)
    carried = [l for l in (store.get_lesson(i) for i in (extra_lesson_ids or [])) if l is not None and l.id not in lesson_ids]
    if carried:
        from core.context.builder import render_lessons

        lesson_ids = lesson_ids + [l.id for l in carried]
        lessons_block = render_lessons([l for l in (store.get_lesson(i) for i in lesson_ids) if l is not None])
    console.print(f"lessons in play: {len(lesson_ids)}" + (f" ({len(carried)} provisional from the previous attempt)" if carried else ""))

    # 2. route
    agents = route(subtask, store, swarm_size or settings.swarm_size)
    console.print("agents: " + ", ".join(a.id for a in agents))

    # 3. swarm
    # The real runner takes `spec` so agents can run the visible spec tests live; fakes may not accept it.
    import inspect

    accepts_spec = "spec" in inspect.signature(swarm_fn).parameters
    runs = asyncio.run(swarm_fn(subtask, agents, repo, lesson_ids, store, spec=spec) if accepts_spec else swarm_fn(subtask, agents, repo, lesson_ids, store))
    store.save_manifest(RunManifest(task_id=task.id, subtask_id=subtask.id, agent_ids=[a.id for a in agents], lessons_used=lesson_ids))

    # 4–5. cross-test
    # Real path: `spec` carries hidden pytest modules + expectations and `repo` the regression suite.
    # Function-level specs (pure subtasks, offline fakes) keep the case-based verdict.
    if getattr(spec, "modules", None):
        result = cross_test(subtask, runs, weights_for(runs, store), spec=spec, repo=repo, n_fuzz=n_fuzz)
    else:
        result = cross_test(subtask, runs, weights_for(runs, store), spec_cases=spec.cases, n_fuzz=n_fuzz)
    console.print(f"verified: {len(result.verified_runs)}  failed: {len(result.failed_runs)}  cases: {len(result.cases)}")
    report.update(
        verified=len(result.verified_runs), failed=len(result.failed_runs),
        failed_run_ids=list(result.failed_runs), verified_run_ids=list(result.verified_runs),
    )

    # 6. commit + advance
    agreement = min((mv.agreement for mv in result.majority.values()), default=0.0)
    # With hidden pytest modules the verdict is absolute (a verified run passed every hidden test and the
    # regression suite), so one verified run is enough to advance. The fuzz-majority agreement check only
    # applies to the case-based verdict (pure subtasks, offline fakes), where the majority IS the oracle.
    pytest_verdict = bool(getattr(spec, "modules", None))
    if not result.verified_runs or (not pytest_verdict and agreement <= settings.majority_ambiguous_threshold):
        # Learn from the all-fail before giving up: reflect every failed run into a lesson, then retry once
        # with those lessons in the agents' context. Trust still moves on this attempt.
        lessons_now = _reflect_failures(subtask, runs, result, store, locate_fn, reflect_fn, report)
        for ch in update_trust(runs, result, store):
            ch.status, ch.reason = ChangeStatus.adopted, "applied directly; kind not gated (GATE_KINDS)"
            store.save_change(ch)
        if retries_left > 0 and lessons_now:
            console.print(f"[yellow]no verified run; retrying once with {len(lessons_now)} provisional lesson(s) from these failures")
            first = {k: report.get(k) for k in ("verified", "failed", "failed_run_ids", "verified_run_ids", "lessons")}
            rep2: dict[str, Any] = {}
            ok = run_subtask(
                task, subtask, store, run_heldout,
                swarm_fn=swarm_fn, spec_fn=spec_fn, reflect_fn=reflect_fn, locate_fn=locate_fn, n_fuzz=n_fuzz, swarm_size=swarm_size,
                report=rep2, retries_left=retries_left - 1, extra_lesson_ids=[l.id for l in lessons_now] + list(extra_lesson_ids or []),
            )
            report["first_attempt"] = first
            report.update(verified=rep2.get("verified", 0), failed=rep2.get("failed", 0), committed=rep2.get("committed", False),
                          verified_run_ids=rep2.get("verified_run_ids", []), retried=True)
            report["failed_run_ids"] = list(first.get("failed_run_ids") or []) + list(rep2.get("failed_run_ids") or [])
            report["lessons"] = list(first.get("lessons") or []) + list(rep2.get("lessons") or [])
            report["changes"] = rep2.get("changes", [])
            if rep2.get("ambiguity_flags"):
                report["ambiguity_flags"] = rep2["ambiguity_flags"]
            return ok
        console.print("[yellow]ambiguous — no verified majority; not advancing")
        return False
    winner = max((r for r in runs if r.id in result.verified_runs), key=lambda r: store.get_trust(r.agent.id).score)
    _commit(Path(winner.sandbox_path), repo, subtask)
    report["committed"] = True
    console.print(f"committed implementation from {winner.agent.id}")

    # 7. reflect
    new_lessons = _reflect_failures(subtask, runs, result, store, locate_fn, reflect_fn, report)
    # provisional lessons from a failed first attempt are gated now, with this attempt's lessons
    for lid in extra_lesson_ids or []:
        l = store.get_lesson(lid)
        if l is not None and l.status == ChangeStatus.candidate and all(x.id != l.id for x in new_lessons):
            new_lessons.append(l)

    # 8–9. propose + gate
    changes = propose_changes(new_lessons, store) + update_trust(runs, result, store)
    for ch in changes:
        if run_heldout is None:
            store.save_change(ch)
            report["changes"].append(ch.id)
            continue
        if str(ch.kind) not in settings.gate_kinds:
            # Not gated (live-run cost control): applied directly and labelled so the changelog is honest.
            from datetime import datetime, timezone

            ch.status = ChangeStatus.adopted
            ch.reason = "applied directly; kind not gated (GATE_KINDS)"
            ch.decided_at = datetime.now(timezone.utc)
            store.save_change(ch)
            report["changes"].append(ch.id)
            console.print(f"[dim]applied (not gated) {ch.kind}: {ch.description[:80]}")
            continue
        ch = evaluate_change(ch, store, run_heldout)
        report["changes"].append(ch.id)
        console.print(f"[{'green' if ch.status == 'adopted' else 'red'}]{ch.status}[/] {ch.kind}: {ch.description} — {ch.reason}")
    return True


def _reflect_failures(subtask: Subtask, runs: list[AgentRun], result, store: Store, locate_fn: LocateFn, reflect_fn: ReflectFn, report: dict[str, Any]) -> list[Lesson]:
    """Locate each failed run's root-cause step (SubQ when configured), record the divergence for the UI and
    write a lesson for it. Returns the new lessons (deduplicated by the store)."""
    verified_runs = [r for r in runs if r.id in result.verified_runs]
    new_lessons: list[Lesson] = []
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
        if "subq" in div.signals and div.step_probabilities.get(f"S{div.step_index}", 0.0) >= SUBQ_CONFIRM_P:
            div.confirmed = True
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
        # pass the harness result so the reflector sees the failed run's actual outcome; fakes may not take it
        try:
            lesson = reflect_fn(subtask.spec, run, div, store, result=result)
        except TypeError:
            lesson = reflect_fn(subtask.spec, run, div, store)
        if div.generalizes is not None and div.generalizes < lesson.confidence:
            lesson.confidence = float(div.generalizes)  # SubQ doubts this generalizes: start the lesson lower
            store.upsert_lesson(lesson)
        if all(x.id != lesson.id for x in new_lessons):
            new_lessons.append(lesson)
        report.setdefault("lessons", []).append(lesson.id)
        console.print(f"[cyan]lesson ({run.agent.id} @ step {div.step_index}): {lesson.text}")
    store.divergences = divergences  # read by the UI snapshot and /api/divergences
    return new_lessons


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
    start_index: int = 0,
) -> dict:
    """Run every subtask in order, stopping at the first one that does not verify.

    `start_index` resumes a long task: subtasks below it are assumed already committed in the repo (and the
    store, e.g. loaded from the previous snapshot, already holds their lessons and trust).

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
            if st.index < start_index:
                continue
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
