"""Trust-weighted majority per test case. Owner: C."""

from __future__ import annotations

from collections import defaultdict

from core.harness.equivalence import outcome_key
from core.models import MajorityVote, Outcome


def trust_weighted_majority(case_id: str, outcomes: dict[str, Outcome], weights: dict[str, float]) -> MajorityVote:
    """outcomes: run_id -> Outcome. weights: run_id -> trust score (default 1.0)."""
    groups: dict[object, list[str]] = defaultdict(list)
    for run_id, o in outcomes.items():
        groups[outcome_key(o)].append(run_id)
    total = sum(weights.get(r, 1.0) for r in outcomes) or 1.0
    best_key, best_ids, best_w = None, [], -1.0
    for key, ids in groups.items():
        w = sum(weights.get(r, 1.0) for r in ids)
        if w > best_w:
            best_key, best_ids, best_w = key, ids, w
    rep = outcomes[best_ids[0]]
    return MajorityVote(case_id=case_id, agreement=best_w / total, group_run_ids=sorted(best_ids), representative=rep)
