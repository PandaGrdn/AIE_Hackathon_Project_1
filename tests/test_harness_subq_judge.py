"""SubQ judge: request shapes and answer parsing, with the HTTP call stubbed."""

from core.harness.subq_judge import SubQJudge, _fmt


class _StubJudge(SubQJudge):
    def __init__(self, answers: dict):
        super().__init__(api_key="x", base_url="https://example.invalid/v1", model="m")
        self.answers = answers
        self.calls: list[tuple[object, dict]] = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        return self.answers


def test_ambiguity_state_has_spec_and_both_answers_but_no_code():
    j = _StubJudge({"author_right": {"type": "noul", "noul": 0.93}})
    p = j.judge_ambiguity("spec text", "tier_for", [100.0], "plus", "basic")
    assert p == 0.93
    state, questions = j.calls[0]
    assert state == {"spec": "spec text", "call": "tier_for(100.0)", "answer_A": "plus", "answer_B": "basic"}
    assert questions["author_right"]["type"] == "noul"


def test_root_cause_choice_parsing():
    j = _StubJudge({"root_cause": {"type": "choice", "choice": "step_2", "probabilities": {"step_0": 0.0, "step_1": 0.1, "step_2": 0.9, "none": 0.0}, "confidence": 0.9}})
    idx, probs, conf = j.judge_root_cause("spec", ["0: a", "1: b", "2: c"], "test_x", "basic", "plus")
    assert idx == 2 and probs["step_2"] == 0.9 and conf == 0.9
    _, questions = j.calls[0]
    assert set(questions["root_cause"]["criteria"]) == {"step_0", "step_1", "step_2", "none"}


def test_root_cause_none():
    j = _StubJudge({"root_cause": {"type": "choice", "choice": "none", "probabilities": {}, "confidence": 0.4}})
    assert j.judge_root_cause("spec", ["0: a"], "t", "x", "y")[0] is None


def test_fmt_raises_and_values():
    assert _fmt({"raises": "ValueError"}) == "raises ValueError"
    assert _fmt("plus") == "plus" and _fmt(1.5) == "1.5"


def test_unavailable_without_key():
    assert not SubQJudge(api_key="", base_url="https://example.invalid/v1", model="m").available
