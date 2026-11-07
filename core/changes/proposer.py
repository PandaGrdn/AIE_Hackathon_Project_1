"""Turn lessons, trust deltas and failure clusters into candidate changes. Owner: Ethan Li.

Levers: context (lesson), skill (lesson promoted after repeat hits), routing (trust),
tool (generated when >= TOOL_AFTER_CLUSTER adopted lessons cluster in one failure region).

Clustering is greedy on cosine similarity of `divergence_embedding`: each adopted lesson joins the
first cluster whose seed is within `threshold`, else seeds a new one. A cluster of >= 3 proposes one
`ChangeKind.tool` whose `ref_id` is a stable hash of the member lesson ids, so the same cluster is
never proposed twice (the changelog is checked). The tool body comes from `write_tool_stub`: a
template with no LLM call; swapping in a model-written body is a one-function change.
"""

from __future__ import annotations

import hashlib

from core.llm.embeddings import cosine
from core.models import Change, ChangeKind, ChangeStatus, Lesson
from core.store.base import Store

PROMOTE_AFTER_HITS = 3
TOOL_AFTER_CLUSTER = 3
TOOL_CLUSTER_SIMILARITY = 0.85  # cosine between divergence embeddings


def propose_changes(new_lessons: list[Lesson], store: Store, cluster_threshold: float = TOOL_CLUSTER_SIMILARITY) -> list[Change]:
    changes: list[Change] = []
    for l in new_lessons:
        if l.status == ChangeStatus.candidate and l.hit_count == 1:
            changes.append(Change(kind=ChangeKind.context, ref_id=l.id, description=f"lesson: {l.text}"))
        elif l.hit_count >= PROMOTE_AFTER_HITS and l.status == ChangeStatus.adopted:
            changes.append(Change(kind=ChangeKind.skill, ref_id=l.id, description=f"promote to skill: {l.text}"))
    changes += propose_tools(store, cluster_threshold)
    return changes


def propose_tools(store: Store, threshold: float = TOOL_CLUSTER_SIMILARITY) -> list[Change]:
    """One ChangeKind.tool per cluster of >= TOOL_AFTER_CLUSTER adopted lessons not already in the changelog."""
    adopted = [l for l in store.list_lessons() if l.status == ChangeStatus.adopted and l.divergence_embedding]
    already = {c.ref_id for c in store.list_changes() if c.kind == ChangeKind.tool}
    out: list[Change] = []
    for cluster in cluster_lessons(adopted, threshold):
        if len(cluster) < TOOL_AFTER_CLUSTER:
            continue
        ref = tool_ref_id(cluster)
        if ref in already:
            continue
        names = "; ".join(f"[{l.id}] {l.text}" for l in cluster)
        out.append(Change(kind=ChangeKind.tool, ref_id=ref, description=f"tool from {len(cluster)} clustered lessons: {names}"))
    return out


def cluster_lessons(lessons: list[Lesson], threshold: float = TOOL_CLUSTER_SIMILARITY) -> list[list[Lesson]]:
    """Greedy single-pass clustering: a lesson joins the first cluster whose seed is within `threshold`."""
    clusters: list[list[Lesson]] = []
    for l in lessons:
        if not l.divergence_embedding:
            continue
        for c in clusters:
            seed = c[0].divergence_embedding
            if len(seed) == len(l.divergence_embedding) and cosine(seed, l.divergence_embedding) >= threshold:
                c.append(l)
                break
        else:
            clusters.append([l])
    return clusters


def tool_ref_id(cluster: list[Lesson]) -> str:
    """Stable, order-independent id for the tool a cluster proposes."""
    return "tool_" + hashlib.sha256("|".join(sorted(l.id for l in cluster)).encode()).hexdigest()[:12]


def write_tool_stub(cluster: list[Lesson]) -> str:
    """Candidate tool body for a cluster: a checklist tool that returns the clustered lessons for the
    agent to confirm before acting. Stand-in for a model-written tool; never calls an LLM."""
    name = "check_" + tool_ref_id(cluster).removeprefix("tool_")
    lines = [f"def {name}(assumption: str) -> list[str]:"]
    lines.append('    """Return the past-failure rules an agent must check this assumption against."""')
    lines.append("    rules = [")
    lines += [f"        {l.text!r}," for l in cluster]
    lines.append("    ]")
    lines.append("    return rules")
    return "\n".join(lines) + "\n"
