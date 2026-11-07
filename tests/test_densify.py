"""scripts/densify_paths.py: transcript message chunks land between the right logged steps."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from core.llm import embeddings

_spec = importlib.util.spec_from_file_location("densify_paths", Path(__file__).resolve().parent.parent / "scripts" / "densify_paths.py")
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)


def _snap():
    steps = [{"run_id": "r1", "subtask_id": "s", "agent_id": "a", "index": i, "assumption": f"a{i}", "action": "write",
              "files_touched": ["x.py"], "ts": f"2026-01-01T00:00:0{i}+00:00", "embedding": [0.1], "live_results": None}
             for i in (1, 2, 3)]
    steps.append({**steps[0], "run_id": "r2"})
    return {"version": 1, "steps": steps, "failed_run_ids": ["r1"], "lessons": [{"id": "L"}], "extra": {"k": 1}}


def _write(tmp_path):
    d = tmp_path / "r1"
    d.mkdir()
    turns = [
        {"turn": 1, "text": "I will read the file first. Then I will plan.", "tool_calls": [{"name": "read_file"}]},
        {"turn": 2, "text": "", "tool_calls": [{"name": "log_step"}]},           # logged step 1
        {"turn": 3, "text": "Now the plan is clear and I start writing code.", "tool_calls": [{"name": "log_step"}]},  # step 2
        {"turn": 4, "text": "Tests look fine, finishing up with the final answer.", "tool_calls": [{"name": "log_step"}]},  # step 3
        {"turn": 5, "text": "All done, the change is complete and verified.", "tool_calls": [{"name": "done"}]},
    ]
    (d / "transcript.json").write_text(json.dumps({"turns": turns, "messages": []}))


def test_densify(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", embeddings.fake_embed)
    _write(tmp_path)
    snap = _snap()
    orig = copy.deepcopy(snap)
    logs: list[str] = []
    out = dp.densify(snap, tmp_path, log=logs.append)
    assert snap == orig  # input not mutated
    assert {k: v for k, v in out.items() if k != "steps"} == {k: v for k, v in orig.items() if k != "steps"}
    r1 = [s for s in out["steps"] if s["run_id"] == "r1"]
    assert [s["index"] for s in r1] == sorted(s["index"] for s in r1)
    msgs = [s for s in r1 if s.get("kind") == "message"]
    assert [s["index"] for s in msgs] == [0.5, 1.5, 2.5, 3.5]
    assert "plan is clear" in msgs[1]["assumption"] and "Tests look fine" in msgs[2]["assumption"]
    assert all(s["files_touched"] == [] and s["embedding"] and s["action"] == "message" for s in msgs)
    assert len([s for s in out["steps"] if s["run_id"] == "r2"]) == 1  # no transcript -> untouched
    assert any("r2: no transcript" in l for l in logs)


def test_idempotent_and_force(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", embeddings.fake_embed)
    _write(tmp_path)
    once = dp.densify(_snap(), tmp_path, log=lambda _: None)
    twice = dp.densify(once, tmp_path, log=lambda _: None)
    assert twice == once
    forced = dp.densify(once, tmp_path, force=True, log=lambda _: None)
    assert forced == once  # replaced, not duplicated


def test_only_failed_and_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", embeddings.fake_embed)
    _write(tmp_path)
    snap = _snap()
    snap["failed_run_ids"] = []
    assert dp.densify(snap, tmp_path, only_failed=True, log=lambda _: None) == snap
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_snap()))
    assert dp.main([str(p), "--runs-dir", str(tmp_path)]) == 0
    assert (tmp_path / "s.dense.json").exists()
    assert json.loads(p.read_text()) == _snap()
