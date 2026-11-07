"""Turn lessons, trust deltas and failure clusters into candidate changes. Owner: D.

Levers: context (lesson), skill (lesson promoted after repeat hits), routing (trust),
tool (generated when ≥3 adopted lessons cluster in one region).
"""

from __future__ import annotations

from core.models import Change, ChangeKind, ChangeStatus, Lesson
from core.store.base import Store

PROMOTE_AFTER_HITS = 3
TOOL_AFTER_CLUSTER = 3


def propose_changes(new_lessons: list[Lesson], store: Store) -> list[Change]:
    changes: list[Change] = []
    for l in new_lessons:
        if l.status == ChangeStatus.candidate and l.hit_count == 1:
            changes.append(Change(kind=ChangeKind.context, ref_id=l.id, description=f"lesson: {l.text}"))
        elif l.hit_count >= PROMOTE_AFTER_HITS and l.status == ChangeStatus.adopted:
            changes.append(Change(kind=ChangeKind.skill, ref_id=l.id, description=f"promote to skill: {l.text}"))
    # TODO(D): cluster adopted lessons by divergence_embedding; if a cluster has ≥ TOOL_AFTER_CLUSTER members,
    # ask the reflector model to write a small tool (e.g. boundary-input generator) and propose ChangeKind.tool.
    return changes
