"""Trust-score updates after a verified subtask, and the leaderboard the UI shows. Owner: Ethan Li."""

from __future__ import annotations

from core.models import AgentRun, Change, ChangeKind, CrossTestResult
from core.store.base import Store

HISTORY_POINTS = 10


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


def leaderboard(store: Store, agent_ids: list[str]) -> list[dict]:
    """Agents by Laplace-smoothed score (desc, then id), each with agreement/disagreement counts and the
    last HISTORY_POINTS score points. Plain dicts so `ui/server.py` can return them as JSON directly.
    An agent with no trust record yet appears at the prior (0.5)."""
    rows = []
    for aid in agent_ids:
        t = store.get_trust(aid)
        rows.append({
            "agent_id": aid,
            "score": float(t.score),
            "agreements": t.agreements,
            "disagreements": t.disagreements,
            "history": [float(x) for x in t.history[-HISTORY_POINTS:]],
        })
    rows.sort(key=lambda r: (-r["score"], r["agent_id"]))
    return rows
