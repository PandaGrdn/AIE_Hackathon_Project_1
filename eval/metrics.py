"""Metrics for the demo and the write-up. Owner: A.

- subtasks completed
- held-out accuracy before vs after adopted changes
- repeat-mistake rate over subtasks
- changes adopted vs rolled back
- trust-weighted vs plain majority accuracy
"""

from __future__ import annotations

from core.models import ChangeStatus
from core.store.base import Store


def summarize(store: Store) -> dict:
    changes = store.list_changes()
    adopted = [c for c in changes if c.status == ChangeStatus.adopted]
    rolled = [c for c in changes if c.status == ChangeStatus.rolled_back]
    lessons = store.list_lessons()
    return {
        "changes_adopted": len(adopted),
        "changes_rolled_back": len(rolled),
        "lessons_total": len(lessons),
        "lessons_adopted": sum(1 for l in lessons if l.status == ChangeStatus.adopted),
        "heldout_accuracy_first": changes[0].heldout_before.get("accuracy") if changes else None,
        "heldout_accuracy_last": adopted[-1].heldout_after.get("accuracy") if adopted else None,
    }
