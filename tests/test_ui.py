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
    Divergence,
    Lesson,
    Outcome,
    ReasoningStep,
    TrustScore,
)
from core.models import TestCase as Case
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
    for heading in ("Changelog", "Root cause", "Trust leaderboard", "Lessons", "Reasoning map"):
        assert heading in r.text
    assert r.text.index("Changelog") < r.text.index("Root cause") < r.text.index("Trust leaderboard")
    assert "/api/divergences" in r.text


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


# ---------- root cause (divergences) ----------


@pytest.fixture
def div_store(store) -> MemoryStore:
    """Two failed runs localized: one by SubQ Snap (probabilities), one by the path signal only."""
    agent_path = AgentIdentity(id="gpt-mini", provider="openai", model="gpt-mini")
    run_path = AgentRun(id="run-path", subtask_id="st3", agent=agent_path, sandbox_path="/tmp/c")
    store.save_run(run_path)  # agent/subtask derivable from the stored run (no steps for it)
    case = Case(id="c100", function="tier", args=[100])
    store.divergences = [
        Divergence(
            run_id="run-bad", step_index=0, step_text="assumption: 'over' includes equal | action: use >=",
            distance=0.0, consensus_step_texts=["assumption: 'over' is strict"],
            breaking_case=case, majority_outcome=Outcome(ok=True, value="standard"),
            signals=["subq", "path"], confirmed=True,
            step_probabilities={"S0": 0.82, "S1": 0.18}, kind="bug", kind_confidence=0.9, generalizes=0.75,
        ),
        Divergence(
            run_id="run-path", step_index=2, step_text="assumption: round half up | action: round()",
            distance=0.41, consensus_step_texts=[], signals=["path"],
        ),
    ]
    store.failed_outcomes = {"run-bad": Outcome(ok=True, value="premium")}
    return store


def test_divergences_endpoint(div_store):
    body = TestClient(make_app(div_store)).get("/api/divergences").json()
    assert [d["run_id"] for d in body] == ["run-path", "run-bad"]  # newest first
    path, subq = body
    assert subq["agent_id"] == "gemini-pro" and subq["subtask_id"] == "st2"  # from stored steps
    assert subq["step_index"] == 0 and "includes equal" in subq["step_text"]
    assert subq["step_probabilities"] == {"S0": 0.82, "S1": 0.18}
    assert subq["kind"] == "bug" and subq["kind_confidence"] == 0.9 and subq["generalizes"] == 0.75
    assert subq["signals"] == ["subq", "path"] and subq["confirmed"] is True
    assert subq["breaking_case"]["function"] == "tier" and subq["breaking_case"]["args"] == [100]
    assert subq["majority_outcome"]["value"] == "standard"
    assert subq["failed_outcome"]["value"] == "premium"

    assert path["agent_id"] == "gpt-mini" and path["subtask_id"] == "st3"  # from the stored run
    assert path["step_probabilities"] == {} and path["kind"] is None and path["generalizes"] is None
    assert path["signals"] == ["path"] and path["confirmed"] is False
    assert path["breaking_case"] is None and path["failed_outcome"] is None


def test_divergences_empty_without_store_attribute(client):
    assert client.get("/api/divergences").json() == []


def test_divergences_accepts_plain_dicts(store):
    store.divergences = [{"run_id": "run-bad", "step_index": 0, "step_text": "t", "signals": ["subq"],
                          "failed_outcome": {"ok": False, "exception": "KeyError"}}]
    [d] = TestClient(make_app(store)).get("/api/divergences").json()
    assert d["agent_id"] == "gemini-pro" and d["failed_outcome"]["exception"] == "KeyError"
    assert d["step_probabilities"] == {} and d["confirmed"] is False


def test_snapshot_roundtrips_divergences(div_store, tmp_path):
    before = TestClient(make_app(div_store)).get("/api/divergences").json()
    snap = snapshot_store(div_store)
    assert [d["run_id"] for d in snap["divergences"]] == ["run-bad", "run-path"]  # recording order

    loaded = load_snapshot(write_snapshot(div_store, tmp_path / "snap.json"))
    assert all(isinstance(d, Divergence) for d in loaded.divergences)
    assert loaded.divergences[0].step_probabilities == {"S0": 0.82, "S1": 0.18}
    assert loaded.divergences[0].breaking_case.args == [100]
    assert TestClient(make_app(loaded)).get("/api/divergences").json() == before


def test_old_snapshot_without_divergences_loads(store, tmp_path):
    import json

    data = snapshot_store(store)
    del data["divergences"]
    path = tmp_path / "old.json"
    path.write_text(json.dumps(data))
    loaded = load_snapshot(path)
    assert loaded.divergences == []
    assert TestClient(make_app(loaded)).get("/api/divergences").json() == []


def test_concept_page_serves_terrain(client):
    r = client.get("/concept")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Reasoning terrain" in r.text and "/api/steps" in r.text


def test_colony_page_and_meta(client, monkeypatch):
    r = client.get("/colony")
    assert r.status_code == 200 and "<title>Colony</title>" in r.text
    monkeypatch.setenv("SWARM_SNAPSHOT", "demo/mcp_fake_snapshot.json")
    from ui.server import snapshot_meta
    assert snapshot_meta() == {"snapshot": "mcp_fake_snapshot.json", "simulated": True}
    monkeypatch.setenv("SWARM_SNAPSHOT", "demo/mcp_snapshot.dense.json")
    assert snapshot_meta()["simulated"] is False


def test_summary_file(tmp_path, monkeypatch, client):
    f = tmp_path / "s.json"
    f.write_text('{"per_subtask": [{"subtask_id": "a", "verified": 1, "failed": 3}]}')
    monkeypatch.setenv("SWARM_SUMMARY", str(f))
    assert client.get("/api/summary_file").json()["per_subtask"][0]["verified"] == 1
    monkeypatch.setenv("SWARM_SUMMARY", str(tmp_path / "missing.json"))
    assert client.get("/api/summary_file").json() == {}


def test_bundle_serves_named_snapshots(client):
    fake = client.get("/api/bundle?snap=fake").json()
    assert fake["meta"]["simulated"] is True
    assert {"steps", "divergences", "lessons", "changes", "trust", "regions", "summary", "runs", "summary_files"} <= set(fake)
    real = client.get("/api/bundle?snap=real").json()
    assert real["meta"]["simulated"] is False and len(real["runs"]) > 0
    assert "error" in client.get("/api/bundle?snap=nope").json()


def test_bundle_can_drop_embeddings(client):
    light = client.get("/api/bundle?snap=real&embeddings=false").json()
    assert light["steps"] and all("embedding" not in s for s in light["steps"])
    assert all("centroid" not in r for r in light["regions"])
    full = client.get("/api/bundle?snap=real").json()
    assert any(s.get("embedding") for s in full["steps"])
