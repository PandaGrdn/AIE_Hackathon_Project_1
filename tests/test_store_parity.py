"""The same behaviour on MemoryStore (reference) and MongoStore (skipped unless MONGODB_URI is set)."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.models import AgentIdentity, AgentRun, Change, ChangeKind, ChangeStatus, FailureRegion, Lesson, ReasoningStep, RunManifest, TrustScore
from core.store.memory import MemoryStore

MONGO_URI = os.environ.get("MONGODB_URI")


def _memory():
    yield MemoryStore()


def _mongo():
    from core.store.mongo import MongoStore

    db = f"swarm_test_{uuid.uuid4().hex[:8]}"
    store = MongoStore(MONGO_URI, db)
    store.ensure_indexes(dim=3)
    try:
        yield store
    finally:
        store.client.drop_database(db)


@pytest.fixture(params=["memory", pytest.param("mongo", marks=pytest.mark.skipif(not MONGO_URI, reason="MONGODB_URI unset"))])
def store(request):
    yield from (_memory() if request.param == "memory" else _mongo())


def _lesson(text, vec, status=ChangeStatus.candidate, div=None):
    return Lesson(text=text, embedding=vec, divergence_embedding=div, status=status)


def test_lesson_roundtrip(store):
    l = _lesson("boundary", [1.0, 0.0, 0.0], div=[0.0, 1.0, 0.0])
    store.upsert_lesson(l)
    got = store.get_lesson(l.id)
    assert got is not None and got.text == "boundary" and got.divergence_embedding == [0.0, 1.0, 0.0]
    assert store.get_lesson("missing") is None
    l.hit_count = 4
    store.upsert_lesson(l)
    assert store.get_lesson(l.id).hit_count == 4
    assert [x.id for x in store.list_lessons()] == [l.id]


def test_search_lessons_filters_on_status_and_ranks_by_similarity(store):
    a = _lesson("adopted close", [1.0, 0.0, 0.0], ChangeStatus.adopted)
    b = _lesson("adopted far", [0.0, 1.0, 0.0], ChangeStatus.adopted)
    c = _lesson("candidate closest", [0.99, 0.1, 0.0], ChangeStatus.candidate)
    for l in (a, b, c):
        store.upsert_lesson(l)
    hits = store.search_lessons([1.0, 0.0, 0.0], k=5, adopted_only=True)
    assert [l.id for l, _ in hits] == [a.id, b.id]
    assert hits[0][1] >= hits[1][1]
    everything = store.search_lessons([1.0, 0.0, 0.0], k=2, adopted_only=False)
    assert {l.id for l, _ in everything} == {a.id, c.id}


def test_regions_near_filters_by_radius_and_sorts_by_distance(store):
    near = FailureRegion(lesson_id="l1", centroid=[1.0, 0.0, 0.0], radius=0.5)
    nearer = FailureRegion(lesson_id="l2", centroid=[0.9, 0.0, 0.0], radius=0.5)
    far = FailureRegion(lesson_id="l3", centroid=[0.0, 1.0, 0.0], radius=0.5)
    for r in (near, nearer, far):
        store.upsert_region(r)
    hits = store.regions_near([0.95, 0.0, 0.0])
    assert [r.lesson_id for r, _ in hits] == ["l2", "l1"]
    assert all(d <= r.radius for r, d in hits)
    assert store.regions_near([0.0, 0.0, 1.0]) == []


def test_trust_roundtrip_and_default(store):
    t = store.get_trust("agent-x")
    assert t.agreements == 0 and t.score == 0.5
    t.agreements, t.history = 3, [0.5, 0.67, 0.75, 0.8]
    store.save_trust(t)
    got = store.get_trust("agent-x")
    assert got.agreements == 3 and got.history == [0.5, 0.67, 0.75, 0.8]


def test_changes_listed_decided_first_in_decision_order_then_candidates(store):
    t0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    later = Change(kind=ChangeKind.context, ref_id="l2", description="later", status=ChangeStatus.adopted, decided_at=t0 + timedelta(minutes=5))
    earlier = Change(kind=ChangeKind.context, ref_id="l1", description="earlier", status=ChangeStatus.rolled_back, decided_at=t0)
    pending = Change(kind=ChangeKind.routing, ref_id="a", description="pending")
    for c in (pending, later, earlier):
        store.save_change(c)
    assert [c.description for c in store.list_changes()] == ["earlier", "later", "pending"]


def test_runs_steps_and_manifests_are_saved(store):
    run = AgentRun(subtask_id="s", agent=AgentIdentity(id="a", provider="meta", model="m"), sandbox_path="/tmp/x")
    run.steps = [ReasoningStep(index=0, assumption="a", action="b", embedding=[0.0, 0.0, 1.0])]
    store.save_steps(run, run.steps)
    store.save_run(run)
    store.save_run(run)  # idempotent
    store.save_manifest(RunManifest(task_id="t", subtask_id="s", agent_ids=["a"], lessons_used=[]))
    runs = store.runs if isinstance(store, MemoryStore) else [AgentRun(id=d.pop("_id"), **d) for d in store.db.runs.find()]
    assert len(runs) == 1


@pytest.mark.skipif(not MONGO_URI, reason="MONGODB_URI unset")
def test_mongo_ensure_indexes_is_idempotent():
    from core.store.mongo import MongoStore

    db = f"swarm_test_{uuid.uuid4().hex[:8]}"
    store = MongoStore(MONGO_URI, db)
    try:
        store.ensure_indexes(dim=3)
        store.ensure_indexes(dim=3)
        names = {ix["name"] for ix in store.db.lessons.list_indexes()}
        assert "status_1" in names
    finally:
        store.client.drop_database(db)
