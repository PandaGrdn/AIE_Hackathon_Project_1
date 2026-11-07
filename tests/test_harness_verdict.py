"""Verdict rules (outline §5.5): hidden spec cases + properties decide; fuzz majority is evidence only."""

from pathlib import Path

from core.harness.crosstest import VerdictResult, breaking_case, cross_test, run_outcome
from core.harness.spec_tests import SpecProperty, SpecTests
from core.models import AgentIdentity, AgentRun, FunctionSig, Subtask, TestCase

GOOD = """
def tier_for(amount: float) -> str:
    if amount < 0:
        raise ValueError("negative")
    if amount < 100:
        return "basic"
    if amount <= 1000:
        return "plus"
    return "pro"
"""

# treats "over 100" as inclusive: 100 -> basic
BOUNDARY_BUG = GOOD.replace("if amount < 100:", "if amount <= 100:")

# agrees with GOOD on every spec case but invents a tier for large amounts (property violation)
PROP_BUG = GOOD.replace('    return "pro"', '    return "gold" if amount > 5000 else "pro"')

SIG = FunctionSig(name="tier_for", file="pricing/tiers.py", params=["amount: float"], returns="str")


def _subtask() -> Subtask:
    return Subtask(index=0, title="Tier classification", spec="tiers", interface=[SIG], files=["pricing/tiers.py"])


def _run(tmp: Path, name: str, src: str) -> AgentRun:
    d = tmp / name / "pricing"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "tiers.py").write_text(src)
    return AgentRun(id=name, subtask_id="s", agent=AgentIdentity(id=f"agent-{name}", provider="meta", model="x"), sandbox_path=str(tmp / name))


def _spec(subtask: Subtask, expected_at_1000: str = "plus") -> SpecTests:
    cases = [
        TestCase(id="c_neg", function="tier_for", args=[-1.0], source="spec"),
        TestCase(id="c_0", function="tier_for", args=[0.0], source="spec"),
        TestCase(id="c_99", function="tier_for", args=[99.99], source="spec"),
        TestCase(id="c_100", function="tier_for", args=[100.0], source="spec"),
        TestCase(id="c_1000", function="tier_for", args=[1000.0], source="spec"),
        TestCase(id="c_1001", function="tier_for", args=[1000.01], source="spec"),
    ]
    expected = {"c_neg": {"raises": "ValueError"}, "c_0": "basic", "c_99": "basic", "c_100": "plus", "c_1000": expected_at_1000, "c_1001": "pro"}
    props = [SpecProperty(name="known_tier", function="tier_for", predicate_src="lambda args, result: result in ('basic', 'plus', 'pro')", description="")]
    # c_100 is hidden: the boundary case decides the verdict
    return SpecTests(subtask_id=subtask.id, cases=cases, expected=expected, properties=props, visible_ids=["c_0", "c_99"])


def test_hidden_spec_miss_fails_run_and_fuzz_is_evidence_only(tmp_path: Path):
    st = _subtask()
    runs = [_run(tmp_path, "good", GOOD), _run(tmp_path, "good2", GOOD), _run(tmp_path, "bug", BOUNDARY_BUG)]
    result = cross_test(st, runs, {}, n_fuzz=20, spec=_spec(st))
    assert isinstance(result, VerdictResult)
    assert result.failed_runs == ["bug"]
    assert sorted(result.verified_runs) == ["good", "good2"]
    assert result.spec_misses["bug"] == ["c_100"]
    assert result.ambiguity_flags == []
    # the breaking case is the hidden spec miss, paired with what the spec expects
    case, ref = breaking_case(result, "bug")
    assert case.id == "c_100" and ref.ok and ref.value == "plus"
    assert run_outcome(result, "bug", "c_100").value == "basic"


def test_property_violation_fails_run_even_when_spec_cases_pass(tmp_path: Path):
    st = _subtask()
    runs = [_run(tmp_path, "good", GOOD), _run(tmp_path, "good2", GOOD), _run(tmp_path, "prop", PROP_BUG)]
    result = cross_test(st, runs, {}, n_fuzz=40, spec=_spec(st))
    assert result.spec_misses["prop"] == []
    assert result.property_violations["prop"], "fuzz draws above 5000 should trip the known_tier property"
    assert result.failed_runs == ["prop"]
    case, _ = breaking_case(result, "prop")
    assert case.args[0] > 5000


def test_spec_vs_majority_disagreement_is_a_flag_not_a_failure(tmp_path: Path):
    st = _subtask()
    runs = [_run(tmp_path, "good", GOOD), _run(tmp_path, "good2", GOOD), _run(tmp_path, "good3", GOOD)]
    # the author got the upper boundary wrong: every correct impl says 1000 -> plus
    result = cross_test(st, runs, {}, n_fuzz=10, spec=_spec(st, expected_at_1000="pro"))
    assert result.failed_runs == []
    assert [f.case_id for f in result.ambiguity_flags] == ["c_1000"]
    assert result.ambiguity_flags[0].majority.value == "plus" and result.ambiguity_flags[0].expected == "pro"


def test_visible_miss_is_recorded_but_does_not_fail(tmp_path: Path):
    st = _subtask()
    spec = _spec(st)
    spec.visible_ids = ["c_100", "c_0"]  # now the boundary case is visible
    runs = [_run(tmp_path, "good", GOOD), _run(tmp_path, "good2", GOOD), _run(tmp_path, "bug", BOUNDARY_BUG)]
    result = cross_test(st, runs, {}, n_fuzz=10, spec=spec)
    assert result.visible_misses["bug"] == ["c_100"]
    assert result.spec_misses["bug"] == []
    assert "bug" not in result.failed_runs


def test_without_spec_falls_back_to_fuzz_majority(tmp_path: Path):
    st = _subtask()
    runs = [_run(tmp_path, "good", GOOD), _run(tmp_path, "good2", GOOD), _run(tmp_path, "bug", BOUNDARY_BUG)]
    result = cross_test(st, runs, {}, n_fuzz=10)
    assert result.failed_runs == ["bug"]  # the seed boundary 100.0 splits them
