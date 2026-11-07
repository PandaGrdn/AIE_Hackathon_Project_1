"""Held-out gate: run held-out subtasks with vs. without a change; adopt or roll back. Owner: D.

Held-out subtasks live in eval/heldout/ and were never used to produce a lesson.
Metric: majority accuracy against reference implementations, plus repeat-mistake rate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from core.models import Change, ChangeKind, ChangeStatus
from core.store.base import Store

HeldoutRunner = Callable[[list[str]], dict[str, float]]
"""Runs the held-out set with the given adopted lesson ids active; returns metrics
{"accuracy": float, "repeat_mistake_rate": float, "false_block_rate": float}."""


def evaluate_change(change: Change, store: Store, run_heldout: HeldoutRunner) -> Change:
    adopted = [l.id for l in store.list_lessons() if l.status == ChangeStatus.adopted]
    if change.kind in (ChangeKind.context, ChangeKind.skill):
        with_ids = sorted(set(adopted) | {change.ref_id})
    else:
        with_ids = adopted  # routing/tool changes are applied by the runner, not via lesson ids
    before = run_heldout(adopted)
    after = run_heldout(with_ids)
    change.heldout_before, change.heldout_after = before, after
    change.decided_at = datetime.now(timezone.utc)

    improved = after.get("accuracy", 0) >= before.get("accuracy", 0) and after.get("repeat_mistake_rate", 1) <= before.get("repeat_mistake_rate", 1)
    no_regression = after.get("false_block_rate", 0) <= before.get("false_block_rate", 0)
    strictly_better = after.get("accuracy", 0) > before.get("accuracy", 0) or after.get("repeat_mistake_rate", 1) < before.get("repeat_mistake_rate", 1)

    if improved and no_regression and strictly_better:
        change.status = ChangeStatus.adopted
        change.reason = "held-out improved without regression"
    else:
        change.status = ChangeStatus.rolled_back
        change.reason = "no held-out improvement" if no_regression else "held-out regression"

    if change.kind in (ChangeKind.context, ChangeKind.skill):
        lesson = store.get_lesson(change.ref_id)
        if lesson:
            lesson.status = change.status
            lesson.version += 1
            store.upsert_lesson(lesson)
    store.save_change(change)
    return change
