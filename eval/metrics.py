"""Metrics for the demo and the write-up. Owner: A.

- subtasks completed (from run_task's summary; not stored)
- held-out accuracy before vs after adopted changes
- repeat-mistake rate over subtasks
- changes adopted vs rolled back
- trust-weighted vs plain majority accuracy (TODO: needs both majorities recorded per case)
"""

from __future__ import annotations

from core.models import ChangeStatus
from core.store.base import Store


def summarize(store: Store) -> dict:
    changes = store.list_changes()
    decided = [c for c in changes if c.decided_at is not None]
    adopted = [c for c in decided if c.status == ChangeStatus.adopted]
    rolled = [c for c in decided if c.status == ChangeStatus.rolled_back]
    lessons = store.list_lessons()
    # held-out first→last must come from GATED changes (routing changes are applied directly, no scores)
    gated = [c for c in decided if c.heldout_after]
    first = gated[0] if gated else None
    last = next((c for c in reversed(adopted) if c.heldout_after), None)
    return {
        "changes_total": len(changes),
        "changes_adopted": len(adopted),
        "changes_rolled_back": len(rolled),
        "changes_by_kind": _by_kind(changes),
        "lessons_total": len(lessons),
        "lessons_adopted": sum(1 for l in lessons if l.status == ChangeStatus.adopted),
        "heldout_accuracy_first": first.heldout_before.get("accuracy") if first else None,
        "heldout_accuracy_last": last.heldout_after.get("accuracy") if last else None,
        "heldout_repeat_rate_first": first.heldout_before.get("repeat_mistake_rate") if first else None,
        "heldout_repeat_rate_last": last.heldout_after.get("repeat_mistake_rate") if last else None,
        "repeat_mistake_rate": repeat_mistake_rate(store),
    }


def repeat_mistake_rate(store: Store) -> float | None:
    """Share of reflected failures over subtasks that repeated an already-known mistake.

    Every reflected failure either creates a lesson (hit_count=1) or dedups into an existing one
    (hit_count += 1), so repeats / failures = sum(hit_count - 1) / sum(hit_count).
    """
    lessons = store.list_lessons()
    total = sum(l.hit_count for l in lessons)
    if not total:
        return None
    return sum(l.hit_count - 1 for l in lessons) / total


def _by_kind(changes) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for c in changes:
        out.setdefault(str(c.kind), {}).setdefault(str(c.status), 0)
        out[str(c.kind)][str(c.status)] += 1
    return out
