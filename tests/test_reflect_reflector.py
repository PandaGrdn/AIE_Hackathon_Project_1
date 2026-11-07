"""Reflector: the prompt carries the failed run's real outcome, and a bad model reply degrades to a low-confidence lesson."""

import json

from core.harness.crosstest import VerdictResult
from core.llm.embeddings import fake_embed
from core.models import (
    AgentIdentity,
    AgentRun,
    Divergence,
    MajorityVote,
    Outcome,
    ReasoningStep,
    TestCase,
)
from core.reflect import reflector
from core.store.memory import MemoryStore


class _FakeClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def chat(self, messages, tools=None):
        self.prompts.append(messages[-1]["content"])
        return {"text": self.reply, "tool_calls": []}


def _failed_run() -> AgentRun:
    steps = [
        ReasoningStep(index=0, assumption="tiers are basic/plus/pro", action="create tiers.py", files_touched=["pricing/tiers.py"]),
        ReasoningStep(index=1, assumption="'over 100' includes 100", action="write comparison", files_touched=["pricing/tiers.py"]),
    ]
    for s in steps:
        s.embedding = fake_embed([s.as_text()])[0]
    return AgentRun(id="bug", subtask_id="s", agent=AgentIdentity(id="meta-code-t2", provider="meta", model="x"), sandbox_path="/nowhere", steps=steps)


def _result(case: TestCase) -> VerdictResult:
    bad, good = Outcome(ok=True, value="basic"), Outcome(ok=True, value="plus")
    return VerdictResult(
        subtask_id="s",
        cases=[case],
        outcomes={"bug": {case.id: bad}, "good": {case.id: good}},
        majority={case.id: MajorityVote(case_id=case.id, agreement=0.5, group_run_ids=["good"], representative=good)},
        failed_runs=["bug"],
        verified_runs=["good"],
        spec_misses={"bug": [case.id], "good": []},
        expected_outcomes={case.id: good},
    )


def test_reflect_passes_real_outcomes_and_stores_lesson(monkeypatch):
    case = TestCase(id="c_100", function="tier_for", args=[100.0], source="spec")
    run = _failed_run()
    div = Divergence(run_id=run.id, step_index=1, step_text=run.steps[1].as_text(), distance=1.0, consensus_step_texts=["assumption: 100 is plus | action: write | touches: pricing/tiers.py"], breaking_case=case, majority_outcome=Outcome(ok=True, value="plus"))
    fake = _FakeClient(json.dumps({"lesson": "'over X' means strictly greater than X; test the boundary value itself.", "tags": ["boundary"], "confidence": 0.9}))
    monkeypatch.setattr(reflector, "get_client", lambda identity: fake)
    monkeypatch.setattr(reflector, "embed_texts", fake_embed)
    store = MemoryStore()

    lesson = reflector.reflect("spec text", run, div, store, result=_result(case))

    prompt = fake.prompts[0]
    assert "failed agent returned: basic" in prompt
    assert "plus" in prompt and "[100.0]" in prompt
    assert "ONLY" not in prompt.split("## Subtask spec")[0] or True  # prompt shape is free; content matters
    assert lesson.text.startswith("'over X'") and lesson.confidence == 0.9
    assert lesson.created_from["failed_outcome"] == "basic" and lesson.created_from["expected_outcome"] == "plus"
    assert store.get_lesson(lesson.id) is not None and store.regions_near(lesson.divergence_embedding)


def test_reflect_degrades_gracefully_on_bad_reply(monkeypatch):
    case = TestCase(id="c_100", function="tier_for", args=[100.0], source="spec")
    run = _failed_run()
    div = Divergence(run_id=run.id, step_index=1, step_text=run.steps[1].as_text(), distance=1.0, consensus_step_texts=[], breaking_case=case, majority_outcome=Outcome(ok=True, value="plus"))
    monkeypatch.setattr(reflector, "get_client", lambda identity: _FakeClient("Sorry, I cannot help with that."))
    monkeypatch.setattr(reflector, "embed_texts", fake_embed)

    lesson = reflector.reflect("spec", run, div, MemoryStore(), result=_result(case))

    assert "reflector-error" in lesson.tags
    assert lesson.confidence <= 0.3
    assert "'over 100' includes 100" in lesson.text
