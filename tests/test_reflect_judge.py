"""Reflector + SubQ judge: root-cause confirmation moves/confirms the divergence step; lesson confidence is the judge's."""

import json

from core.llm.embeddings import fake_embed
from core.models import AgentIdentity, AgentRun, Divergence, Outcome, ReasoningStep, TestCase
from core.reflect import reflector
from core.store.memory import MemoryStore


class _FakeClient:
    def chat(self, messages, tools=None):
        return {"text": json.dumps({"lesson": "'from 100' includes 100; test the boundary.", "tags": ["boundary"], "confidence": 0.5}), "tool_calls": []}


class _FakeJudge:
    def __init__(self, idx, conf, lesson_p):
        self.idx, self.conf, self.lesson_p = idx, conf, lesson_p

    def judge_root_cause(self, spec, steps, failing, failed_result, expected):
        return self.idx, {}, self.conf

    def judge_lesson(self, spec, step, failing, lesson):
        return self.lesson_p


def _run():
    steps = [ReasoningStep(index=i, assumption=a, action="write", files_touched=["pricing/tiers.py"]) for i, a in enumerate(["tiers exist", "'over 100' includes 100", "write tests"])]
    for s in steps:
        s.embedding = fake_embed([s.as_text()])[0]
    return AgentRun(id="bug", subtask_id="s", agent=AgentIdentity(id="a", provider="meta", model="x"), sandbox_path="/nowhere", steps=steps)


def _div(run, idx):
    case = TestCase(id="c_100", function="tier_for", args=[100.0], source="spec")
    return Divergence(run_id=run.id, step_index=idx, step_text=run.steps[idx].as_text(), distance=1.0, consensus_step_texts=[], breaking_case=case, majority_outcome=Outcome(ok=True, value="plus"))


def test_judge_confirms_matching_step_and_sets_confidence(monkeypatch):
    monkeypatch.setattr(reflector, "get_client", lambda identity: _FakeClient())
    monkeypatch.setattr(reflector, "embed_texts", fake_embed)
    run = _run()
    div = _div(run, 1)
    lesson = reflector.reflect("spec", run, div, MemoryStore(), judge=_FakeJudge(1, 0.95, 0.88))
    assert div.confirmed is True and "subq" in div.signals and "subq-moved" not in div.signals
    assert lesson.confidence == 0.88 and lesson.created_from["judge_lesson_p"] == 0.88


def test_judge_moves_divergence_when_confident(monkeypatch):
    monkeypatch.setattr(reflector, "get_client", lambda identity: _FakeClient())
    monkeypatch.setattr(reflector, "embed_texts", fake_embed)
    run = _run()
    div = _div(run, 2)  # embedding picked the wrong step
    reflector.reflect("spec", run, div, MemoryStore(), judge=_FakeJudge(1, 0.9, 0.7))
    assert div.step_index == 1 and "subq-moved" in div.signals and div.confirmed is True


def test_low_confidence_disagreement_keeps_step_unconfirmed(monkeypatch):
    monkeypatch.setattr(reflector, "get_client", lambda identity: _FakeClient())
    monkeypatch.setattr(reflector, "embed_texts", fake_embed)
    run = _run()
    div = _div(run, 2)
    reflector.reflect("spec", run, div, MemoryStore(), judge=_FakeJudge(1, 0.3, 0.7))
    assert div.step_index == 2 and div.confirmed is False


def test_no_judge_keeps_writer_confidence(monkeypatch):
    monkeypatch.setattr(reflector, "get_client", lambda identity: _FakeClient())
    monkeypatch.setattr(reflector, "embed_texts", fake_embed)
    run = _run()
    lesson = reflector.reflect("spec", run, _div(run, 1), MemoryStore(), judge=None)
    assert lesson.confidence == 0.5 and lesson.created_from["judge_lesson_p"] is None
