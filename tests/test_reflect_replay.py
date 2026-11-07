"""Counterfactual replay (outline §5.7): re-run the failed agent with the consensus assumption injected
and check whether the failing case now passes. No LLM: the runner and checker are injected."""

from pathlib import Path

from core.models import AgentIdentity, AgentRun, Divergence, FunctionSig, Outcome, ReasoningStep, Subtask, TestCase
from core.reflect import replay
from core.reflect.replay import confirm, replay_lessons_block


def _subtask() -> Subtask:
    return Subtask(
        index=0, title="tiers", spec="tier_for(amount)",
        interface=[FunctionSig(name="tier_for", file="pricing/tiers.py", params=["amount: float"], returns="str")],
        files=["pricing/tiers.py"],
    )


def _failed(n=4) -> AgentRun:
    steps = [ReasoningStep(index=i, assumption=f"assume {i}", action=f"act {i}", files_touched=["pricing/tiers.py"]) for i in range(n)]
    return AgentRun(id="f", subtask_id="s", agent=AgentIdentity(id="a", provider="meta", model="m"), sandbox_path="/tmp/f", steps=steps)


def _div(idx: int, case: TestCase, expected: Outcome) -> Divergence:
    return Divergence(run_id="f", step_index=idx, step_text=f"assumption: assume {idx} | action: act {idx} | touches: -", distance=1.0,
                      consensus_step_texts=[], breaking_case=case, majority_outcome=expected, signals=["path"])


CASE = TestCase(id="c1", function="tier_for", args=[100.0], source="spec")
EXPECTED = Outcome(ok=True, value="silver")


def _rerun(sandbox="/tmp/replay") -> AgentRun:
    return AgentRun(id="r", subtask_id="s", agent=AgentIdentity(id="a", provider="meta", model="m"), sandbox_path=sandbox)


def test_confirm_true_when_rerun_fixes_case_and_block_carries_consensus_assumption():
    seen: list[str] = []

    def runner(lessons_block: str) -> AgentRun:
        seen.append(lessons_block)
        return _rerun()

    div = _div(2, CASE, EXPECTED)
    ok = confirm(_failed(), div, "'over $100' is strictly greater", _subtask(), Path("/tmp/repo"), None,
                 runner=runner, checker=lambda run, case, expected: True)
    assert ok is True and div.confirmed is True
    assert len(seen) == 1
    assert "'over $100' is strictly greater" in seen[0]
    assert "assume 2" in seen[0]  # the agent's own wrong assumption at the candidate step is named
    assert "replay" in div.signals


def test_confirm_retries_next_candidates_and_moves_div_to_the_confirmed_step():
    calls: list[str] = []

    def runner(lessons_block: str) -> AgentRun:
        calls.append(lessons_block)
        return _rerun()

    verdicts = iter([False, False, True])
    div = _div(1, CASE, EXPECTED)
    cands = [_div(1, CASE, EXPECTED), _div(3, CASE, EXPECTED), _div(0, CASE, EXPECTED), _div(2, CASE, EXPECTED)]
    ok = confirm(_failed(), div, "consensus", _subtask(), Path("/tmp/repo"), None, candidates=cands,
                 runner=runner, checker=lambda run, case, expected: next(verdicts))
    assert ok is True and div.confirmed is True
    assert div.step_index == 0 and "assume 0" in div.step_text
    assert len(calls) == 3


def test_confirm_gives_up_after_two_retries():
    n = 0

    def runner(lessons_block: str) -> AgentRun:
        nonlocal n
        n += 1
        return _rerun()

    div = _div(0, CASE, EXPECTED)
    cands = [_div(i, CASE, EXPECTED) for i in range(6)]
    ok = confirm(_failed(6), div, "consensus", _subtask(), Path("/tmp/repo"), None, candidates=cands,
                 runner=runner, checker=lambda run, case, expected: False)
    assert ok is False and div.confirmed is False
    assert div.step_index == 0  # unchanged when nothing confirms
    assert n == 3
    assert "replay" not in div.signals


def test_confirm_without_breaking_case_is_false_and_does_not_run():
    div = Divergence(run_id="f", step_index=0, step_text="x", distance=0.0, consensus_step_texts=[])
    ok = confirm(_failed(), div, "c", _subtask(), Path("/tmp/repo"), None,
                 runner=lambda b: (_ for _ in ()).throw(AssertionError("must not run")), checker=lambda *a: True)
    assert ok is False and div.confirmed is False


def test_errored_rerun_counts_as_a_failed_attempt():
    def runner(lessons_block: str) -> AgentRun:
        r = _rerun()
        r.error = "RuntimeError: boom"
        return r

    div = _div(0, CASE, EXPECTED)
    ok = confirm(_failed(), div, "c", _subtask(), Path("/tmp/repo"), None, runner=runner,
                 checker=lambda *a: (_ for _ in ()).throw(AssertionError("checker must not see an errored run")))
    assert ok is False


def test_default_checker_function_case_uses_run_cases(monkeypatch):
    monkeypatch.setattr(replay, "run_cases", lambda subtask, dirs, cases: {rid: {cases[0].id: Outcome(ok=True, value="silver")} for rid in dirs})
    div = _div(0, CASE, EXPECTED)
    ok = confirm(_failed(), div, "c", _subtask(), Path("/tmp/repo"), None, runner=lambda b: _rerun())
    assert ok is True


def test_default_checker_pytest_case_uses_run_pytest(monkeypatch):
    case = TestCase(id="pt_1", function="test_boundary", args=["test_spec.py::test_boundary"], source="pytest")
    seen = {}

    def fake_run_pytest(sandbox, modules, repo_tests, timeout=0):
        seen["modules"] = modules
        return {"test_spec.py::test_boundary": (True, "passed"), "test_spec.py::test_other": (False, "AssertionError")}

    monkeypatch.setattr(replay, "run_pytest", fake_run_pytest)
    monkeypatch.setattr(replay, "repo_test_files", lambda repo: [])

    class Spec:
        modules = {"test_spec.py": "def test_boundary(): pass"}

    div = _div(0, case, Outcome(ok=True, value="passed"))
    ok = confirm(_failed(), div, "c", _subtask(), Path("/tmp/repo"), Spec(), runner=lambda b: _rerun())
    assert ok is True and seen["modules"] == Spec.modules


def test_lessons_block_mentions_prior_steps_and_replacement():
    block = replay_lessons_block(_failed(), _div(2, CASE, EXPECTED), "the right idea")
    assert "assume 0" in block and "assume 1" in block
    assert "assume 2" in block and "the right idea" in block
