"""Counterfactual confirmation of a divergence point (outline §5.7). Owner: Ethan Li.

Re-run the failed agent with the consensus assumption injected as a lesson block at the candidate
step, then re-check the one failing case. If it now matches what the spec author expects, the step
is a confirmed root cause (`div.confirmed = True`, "replay" added to `div.signals`). Otherwise try the
next candidates, at most `max_retries` more times. An unconfirmed divergence still produces a lesson,
but `dedup_or_insert(..., confirmed=False)` caps its confidence and withholds its failure region.

Fully injectable: `runner(lessons_block) -> AgentRun` defaults to `core.swarm.runner.run_one`, and
`checker(run, case, expected) -> bool` defaults to the harness (`run_cases` for a function case,
`run_pytest` for a pytest case). Tests pass fakes for both and never call an LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.harness.crosstest import repo_test_files, run_cases, run_pytest
from core.harness.equivalence import equivalent
from core.models import AgentRun, CrossTestResult, Divergence, Outcome, Subtask, TestCase
from core.store.base import Store

MAX_RETRIES = 2

Runner = Callable[[str], AgentRun]
Checker = Callable[[AgentRun, TestCase, Outcome | None], bool]


def replay_lessons_block(failed: AgentRun, div: Divergence, consensus_assumption: str) -> str:
    """The prompt block that stands in for "restart from step k": the agent's own path up to the candidate
    step, the assumption it made there, and the assumption the passing agents made instead."""
    before = [s for s in failed.steps if s.index < div.step_index]
    at = next((s for s in failed.steps if s.index == div.step_index), None)
    lines = ["Lessons from past failures on similar work (follow them):"]
    if before:
        lines.append("- Your previous attempt reasoned as follows and then went wrong:")
        lines += [f"    {s.index}: {s.as_text()}" for s in before]
    wrong = at.assumption if at else div.step_text
    lines.append(f"- At that point you assumed: \"{wrong}\". That assumption was wrong.")
    lines.append(f"- Agents that passed the acceptance tests assumed instead: \"{consensus_assumption}\". Build on this assumption.")
    return "\n".join(lines)


def _default_runner(failed: AgentRun, subtask: Subtask, repo: Path, store: Store | None) -> Runner:
    def run(lessons_block: str) -> AgentRun:
        from core.store.memory import MemoryStore
        from core.swarm.runner import run_one  # lazy: pulls in the LLM clients

        return run_one(subtask, failed.agent, repo, lessons_block, list(failed.lessons_used), store or MemoryStore())

    return run


def _default_checker(subtask: Subtask, repo: Path, spec_tests) -> Checker:
    def check(run: AgentRun, case: TestCase, expected: Outcome | None) -> bool:
        if case.source in ("pytest", "regression"):
            node = case.args[0] if case.args else case.function
            modules = getattr(spec_tests, "modules", None) or {}
            results = run_pytest(run.sandbox_path, modules, repo_test_files(repo))
            return bool(results.get(node, (False, ""))[0])
        outcomes = run_cases(subtask, {run.id: run.sandbox_path}, [case])
        out = outcomes.get(run.id, {}).get(case.id)
        return out is not None and expected is not None and equivalent(out, expected)

    return check


def confirm(
    failed_run: AgentRun,
    div: Divergence,
    consensus_assumption: str,
    subtask: Subtask,
    repo: Path,
    spec_tests,
    *,
    result: CrossTestResult | None = None,
    candidates: list[Divergence] | None = None,
    max_retries: int = MAX_RETRIES,
    runner: Runner | None = None,
    checker: Checker | None = None,
    store: Store | None = None,
) -> bool:
    """True when a re-run from `div` (or one of the next `max_retries` candidates) fixes the failing case.
    On success `div` is moved to the confirmed step and marked. `candidates` is the ranked list from
    `core.paths.divergence.rank_candidates`; without it only `div` itself is tried."""
    case = div.breaking_case
    if case is None:
        return False
    expected = div.majority_outcome
    if result is not None and getattr(result, "expected_outcomes", None):
        expected = result.expected_outcomes.get(case.id, expected)
    runner = runner or _default_runner(failed_run, subtask, repo, store)
    checker = checker or _default_checker(subtask, repo, spec_tests)

    tries = [div] + [c for c in (candidates or []) if c.step_index != div.step_index]
    for cand in tries[: 1 + max_retries]:
        rerun = runner(replay_lessons_block(failed_run, cand, consensus_assumption))
        if rerun.error:
            continue
        if checker(rerun, case, expected):
            if cand is not div:
                div.step_index, div.step_text, div.distance = cand.step_index, cand.step_text, cand.distance
                div.consensus_step_texts, div.signals = cand.consensus_step_texts, list(cand.signals)
            if "replay" not in div.signals:
                div.signals.append("replay")
            div.confirmed = True
            return True
    div.confirmed = False
    return False
