"""Trust-score updates after a verified subtask. Owner: D."""

from __future__ import annotations

from core.models import Change, ChangeKind, CrossTestResult, AgentRun
from core.store.base import Store


def update_trust(runs: list[AgentRun], result: CrossTestResult, store: Store) -> list[Change]:
    """Returns routing-change candidates (one per agent whose score moved)."""
    changes = []
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
        changes.append(Change(kind=ChangeKind.routing, ref_id=run.agent.id, description=f"trust {run.agent.id}: {before:.2f} → {t.score:.2f}"))
    return changes


def weights_for(runs: list[AgentRun], store: Store) -> dict[str, float]:
    return {r.id: store.get_trust(r.agent.id).score for r in runs}
