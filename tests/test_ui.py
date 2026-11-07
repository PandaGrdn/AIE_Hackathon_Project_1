"""UI server: endpoints return what is in the store; snapshot round-trips."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core.models import (
    AgentIdentity,
    AgentRun,
    Change,
    ChangeKind,
    ChangeStatus,
    Lesson,
    ReasoningStep,
    TrustScore,
)
from core.store.memory import MemoryStore
from ui.server import load_snapshot, make_app, snapshot_store, write_snapshot

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    agent_ok = AgentIdentity(id="muse-code", provider="meta", model="muse-code")
    agent_bad = AgentIdentity(id="gemini-pro", provider="gemini", model="gemini-2.5-pro")
    run_ok = AgentRun(id="run-ok", subtask_id="st2", agent=agent_ok, sandbox_path="/tmp/a")
    run_bad = AgentRun(id="run-bad", subtask_id="st2", agent=agent_bad, sandbox_path="/tmp/b")

    s.upsert_lesson(Lesson(
        id="L1",
        text="'over 100' is strict: 100 itself belongs to the lower tier",
        hit_count=3,
        confidence=0.8,
        status=ChangeStatus.adopted,
        created_from={"subtask_id": "st2", "run_id": "run-bad", "breaking_input": 100},
    ))
    s.save_change(Change(
        id="C1",
        kind=ChangeKind.context,
        ref_id="L1",
        description="inject lesson L1 for boundary subtasks",
        status=ChangeStatus.adopted,
        heldout_before={"accuracy": 0.6, "repeat_mistake_rate": 0.4},
        heldout_after={"accuracy": 0.85, "repeat_mistake_rate": 0.1},
        decided_at=T0,
        reason="repeat-mistake rate fell, accuracy rose",
    ))
    s.save_trust(TrustScore(agent_id="muse-code", agreements=4, disagreements=1, history=[0.5, 0.67, 0.75, 0.71]))
    s.save_steps(run_ok, [ReasoningStep(index=0, assumption="'over' is strict", action="use >", embedding=[1.0, 0.0, 0.0])])
    s.save_steps(run_bad, [ReasoningStep(index=0, assumption="'over' includes equal", action="use >=", embedding=[0.0, 1.0, 0.0])])
    return s


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(make_app(store))


def test_index_serves_html_with_sections(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for heading in ("Changelog", "Trust leaderboard", "Lessons", "Reasoning map"):
        assert heading in r.text


def test_changes(client):
    body = client.get("/api/changes").json()
    assert len(body) == 1
    c = body[0]
    assert c["id"] == "C1" and c["status"] == "adopted" and c["kind"] == "context"
    assert c["heldout_before"] == {"accuracy": 0.6, "repeat_mistake_rate": 0.4}
    assert c["heldout_after"] == {"accuracy": 0.85, "repeat_mistake_rate": 0.1}
    assert c["reason"]


def test_changes_newest_first_with_undecided_mixed_in(store, client):
    store.save_change(Change(id="C2", kind=ChangeKind.routing, ref_id="gemini-pro", description="d",
                             status=ChangeStatus.rolled_back, decided_at=T0 + timedelta(minutes=5)))
    store.save_change(Change(id="C3", kind=ChangeKind.skill, ref_id="L1", description="d"))  # undecided
    ids = [c["id"] for c in client.get("/api/changes").json()]
    assert ids == ["C3", "C2", "C1"]


def test_lessons(client):
    body = client.get("/api/lessons").json()
    assert len(body) == 1
    assert body[0]["id"] == "L1"
    assert body[0]["hit_count"] == 3
    assert body[0]["status"] == "adopted"
    assert body[0]["confidence"] == 0.8


def test_trust(client):
    body = client.get("/api/trust").json()
    assert body == [{
        "agent_id": "muse-code",
        "score": pytest.approx(5 / 7),
        "agreements": 4,
        "disagreements": 1,
        "history": [0.5, 0.67, 0.75, 0.71],
    }]


def test_steps_with_failed_flag(client):
    body = {s["run_id"]: s for s in client.get("/api/steps").json()}
    assert set(body) == {"run-ok", "run-bad"}
    bad, ok = body["run-bad"], body["run-ok"]
    assert bad["agent_id"] == "gemini-pro" and bad["step_index"] == 0
    assert bad["embedding"] == [0.0, 1.0, 0.0]
    assert "includes equal" in bad["text"]
    assert bad["failed"] is True  # a lesson was reflected from this run
    assert ok["failed"] is None  # no verdict stored for this run


def test_summary(client):
    body = client.get("/api/summary").json()
    assert "error" not in body
    assert body["changes_adopted"] == 1
    assert body["lessons_total"] == 1


def test_summary_survives_broken_metrics(monkeypatch, client):
    import eval.metrics

    monkeypatch.delattr(eval.metrics, "summarize")
    r = client.get("/api/summary")
    assert r.status_code == 200
    assert "error" in r.json()


def test_snapshot_roundtrip(store, tmp_path, monkeypatch):
    store.verified_run_ids = {"run-ok"}
    path = write_snapshot(store, tmp_path / "snap.json")
    snap = snapshot_store(store)
    assert set(snap) >= {"lessons", "changes", "trust", "steps"}
    assert snap["failed_run_ids"] == ["run-bad"]

    loaded = load_snapshot(path)
    assert isinstance(loaded, MemoryStore)
    assert loaded.get_lesson("L1").text == store.get_lesson("L1").text
    assert loaded.changes["C1"].heldout_after == {"accuracy": 0.85, "repeat_mistake_rate": 0.1}
    assert loaded.trust["muse-code"].history == [0.5, 0.67, 0.75, 0.71]
    assert len(loaded.steps) == 2

    # the app picks the snapshot up from SWARM_SNAPSHOT when no store is injected
    monkeypatch.setenv("SWARM_SNAPSHOT", str(path))
    c = TestClient(make_app())
    steps = {s["run_id"]: s["failed"] for s in c.get("/api/steps").json()}
    assert steps == {"run-bad": True, "run-ok": False}
    assert [x["id"] for x in c.get("/api/changes").json()] == ["C1"]
