"""Real-repo verdict (outline §5.5): hidden pytest modules must pass and the repo's own suite must still pass."""

from pathlib import Path

from core.harness.crosstest import breaking_case, cross_test, run_outcome
from core.harness.spec_tests import SpecTests, spec_node_ids, visible_module_sources
from core.models import AgentIdentity, AgentRun, FunctionSig, Subtask

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
BOUNDARY_BUG = GOOD.replace("if amount < 100:", "if amount <= 100:")

SPEC_MODULE = """
from pricing.tiers import tier_for

def test_hundred_is_plus():
    assert tier_for(100) == "plus"

def test_zero_is_basic():
    assert tier_for(0) == "basic"
"""
BASE_TEST = "from pricing import VERSION\n\ndef test_version():\n    assert VERSION == 1\n"
SIG = FunctionSig(name="tier_for", file="pricing/tiers.py", params=["amount: float"], returns="str")


def _repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "pricing").mkdir(parents=True)
    (repo / "pricing" / "__init__.py").write_text("VERSION = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text(BASE_TEST)
    return repo


def _run(tmp: Path, repo: Path, name: str, src: str, break_repo: bool = False) -> AgentRun:
    import shutil

    sb = tmp / name
    shutil.copytree(repo, sb)
    (sb / "pricing" / "tiers.py").write_text(src)
    if break_repo:
        (sb / "pricing" / "__init__.py").write_text("VERSION = 2\n")  # regresses the repo's own test
    (sb / "tests" / "test_agent_own.py").write_text("def test_mine():\n    assert False\n")  # agents' tests must be ignored
    return AgentRun(id=name, subtask_id="s", agent=AgentIdentity(id=f"a-{name}", provider="meta", model="x"), sandbox_path=str(sb))


def _subtask() -> Subtask:
    return Subtask(index=0, title="Tier classification", spec="tiers", interface=[SIG], files=["pricing/tiers.py"])


def _spec(st: Subtask) -> SpecTests:
    return SpecTests(subtask_id=st.id, cases=[], expected={}, properties=[], modules={"test_tiers_spec.py": SPEC_MODULE}, visible_ids=["test_tiers_spec.py::test_zero_is_basic"])


def test_test_ids_and_visible_sources():
    assert spec_node_ids({"test_tiers_spec.py": SPEC_MODULE}) == ["test_tiers_spec.py::test_hundred_is_plus", "test_tiers_spec.py::test_zero_is_basic"]
    vis = visible_module_sources(_spec(_subtask()))
    assert "test_zero_is_basic" in vis["test_tiers_spec.py"] and "test_hundred_is_plus" not in vis["test_tiers_spec.py"]
    assert "from pricing.tiers import tier_for" in vis["test_tiers_spec.py"]


def test_hidden_pytest_failure_and_regression_fail_runs(tmp_path: Path):
    repo = _repo(tmp_path)
    st = _subtask()
    runs = [_run(tmp_path, repo, "good", GOOD), _run(tmp_path, repo, "good2", GOOD), _run(tmp_path, repo, "bug", BOUNDARY_BUG), _run(tmp_path, repo, "regress", GOOD, break_repo=True)]
    result = cross_test(st, runs, {}, n_fuzz=5, spec=_spec(st), repo=repo)

    assert sorted(result.verified_runs) == ["good", "good2"]
    assert result.failed_runs == ["bug", "regress"]
    # the boundary bug fails exactly the hidden spec test; the visible one passes
    [miss] = result.spec_misses["bug"]
    assert result.nodeid_by_case[miss] == "test_tiers_spec.py::test_hundred_is_plus"
    assert result.visible_misses["bug"] == []
    # the regression run passes the spec tests but breaks the repo's own suite
    assert result.spec_misses["regress"] == []
    [reg] = result.regression_failures["regress"]
    assert result.nodeid_by_case[reg] == "tests/test_base.py::test_version"
    # agents' own tests were never run
    assert not any("test_agent_own" in n for n in result.nodeid_by_case.values())
    # the reflector gets the failing test and its assertion
    case, expected = breaking_case(result, "bug")
    assert case.source == "pytest" and case.args == ["test_tiers_spec.py::test_hundred_is_plus"] and expected.value == "passed"
    out = run_outcome(result, "bug", case.id)
    assert not out.ok and "AssertionError" in out.exception


def test_uncollectable_module_fails_its_hidden_tests(tmp_path: Path):
    repo = _repo(tmp_path)
    st = _subtask()
    run = _run(tmp_path, repo, "noimpl", GOOD)
    (tmp_path / "noimpl" / "pricing" / "tiers.py").unlink()  # the spec module can't import -> collection error
    spec = _spec(st)
    result = cross_test(st, [run, _run(tmp_path, repo, "good", GOOD)], {}, n_fuzz=5, spec=spec, repo=repo)
    assert "noimpl" in result.failed_runs
    assert any("collect" in (run_outcome(result, "noimpl", c).exception or "") for c in result.spec_misses["noimpl"])
