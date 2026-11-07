"""Trust-score updates after a verified subtask. Owner: D."""

from __future__ import annotations

from core.models import Change, ChangeKind, CrossTestResult, AgentRun
from core.store.base import Store


def update_trust(runs: list[AgentRun], result: CrossTestResult, store: Store) -> list[Change]:
    """Apply trust updates and return ONE routing-change candidate per subtask summarizing them.

    One change per agent per subtask drowned the changelog (6 agents × N subtasks of near-identical
    rolled-back rows); the gate evaluates routing as a whole, so a batched change is what it decides on.
    """
    deltas: list[str] = []
    for run in runs:
        t = store.get_trust(run.agent.id)
        before = t.score
        if run.id in result.verified_runs:
            t.agreements += 1
        elif run.id in result.failed_runs:
            t.disagreements += 1
        else:
            continue
        t.history.append(t.score)
        store.save_trust(t)
        deltas.append(f"{run.agent.id} {before:.2f}→{t.score:.2f}")
    if not deltas:
        return []
    return [Change(kind=ChangeKind.routing, ref_id=result.subtask_id, description="trust: " + ", ".join(deltas))]


def weights_for(runs: list[AgentRun], store: Store) -> dict[str, float]:
    return {r.id: store.get_trust(r.agent.id).score for r in runs}
