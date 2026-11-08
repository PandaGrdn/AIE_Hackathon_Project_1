"""Properties: an unevaluable predicate is None, and a property every run violates is flagged, not charged.
Seen live: the author wrote `lambda args, result: summarize(list(reversed(args[0]))) == result`, which raises
NameError on every call, and all six real agents 'failed' subtask 3 on it."""

from pathlib import Path

from core.harness.crosstest import cross_test
from core.harness.spec_tests import SpecProperty, SpecTests, check_property
from core.models import AgentIdentity, AgentRun, FunctionSig, Subtask, TestCase

GOOD = "def tier_for(amount: float) -> str:\n    if amount < 0:\n        raise ValueError\n    return 'basic' if amount < 100 else ('plus' if amount <= 1000 else 'pro')\n"
SIG = FunctionSig(name="tier_for", file="pricing/tiers.py", params=["amount: float"], returns="str")


def _run(tmp: Path, name: str) -> AgentRun:
    d = tmp / name / "pricing"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "tiers.py").write_text(GOOD)
    return AgentRun(id=name, subtask_id="s", agent=AgentIdentity(id=name, provider="meta", model="x"), sandbox_path=str(tmp / name))


def test_unevaluable_predicate_is_none_not_false():
    assert check_property(SpecProperty(name="bad_src", function="f", predicate_src="lambda args, result: (", description=""), [1], 1) is None
    assert check_property(SpecProperty(name="raises", function="f", predicate_src="lambda args, result: other_fn(args) == result", description=""), [1], 1) is False
    assert check_property(SpecProperty(name="ok", function="f", predicate_src="lambda args, result: result == 1", description=""), [1], 1) is True


def test_property_violated_by_every_run_is_flagged_not_charged(tmp_path: Path):
    st = Subtask(index=0, title="t", spec="tiers", interface=[SIG], files=["pricing/tiers.py"])
    bad = SpecProperty(name="order_independence", function="tier_for", predicate_src="lambda args, result: summarize(args) == result", description="")
    spec = SpecTests(subtask_id=st.id, cases=[TestCase(id="c1", function="tier_for", args=[5.0], source="spec")], expected={"c1": "basic"}, properties=[bad], visible_ids=[])
    runs = [_run(tmp_path, "a"), _run(tmp_path, "b"), _run(tmp_path, "c")]
    res = cross_test(st, runs, {}, n_fuzz=5, spec=spec, judge=None)
    assert res.failed_runs == [] and sorted(res.verified_runs) == ["a", "b", "c"]
    assert [f.function for f in res.ambiguity_flags] == ["property:order_independence"]
    assert all(res.property_violations[r.id] == [] for r in runs)


RAISER = "def tier_for(amount: float) -> str:\n    if amount < 0:\n        raise ValueError\n    if amount > 5000:\n        raise ValueError('too big')\n    return 'basic' if amount < 100 else ('plus' if amount <= 1000 else 'pro')\n"


def test_property_never_satisfied_is_broken_even_when_some_runs_raise(tmp_path: Path):
    """Seen live: on inputs where one agent raises, the broken property is not evaluated for it, so the
    violation is not unanimous per case and the other agents were charged."""
    st = Subtask(index=0, title="t", spec="tiers", interface=[SIG], files=["pricing/tiers.py"])
    bad = SpecProperty(name="order_independence", function="tier_for", predicate_src="lambda args, result: summarize(args) == result", description="")
    spec = SpecTests(subtask_id=st.id, cases=[TestCase(id="c1", function="tier_for", args=[5.0], source="spec"), TestCase(id="c2", function="tier_for", args=[9000.0], source="spec")], expected={"c1": "basic", "c2": "pro"}, properties=[bad], visible_ids=[])
    runs = [_run(tmp_path, "a"), _run(tmp_path, "b")]
    d = tmp_path / "c" / "pricing"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "tiers.py").write_text(RAISER)
    runs.append(AgentRun(id="c", subtask_id="s", agent=AgentIdentity(id="c", provider="meta", model="x"), sandbox_path=str(tmp_path / "c")))
    res = cross_test(st, runs, {}, n_fuzz=20, spec=spec, judge=None)
    assert all(res.property_violations[r.id] == [] for r in runs)
    assert any(f.function == "property:order_independence" for f in res.ambiguity_flags)
    assert res.failed_runs == ["c"]  # c still fails: it misses the hidden spec case 9000 -> pro
