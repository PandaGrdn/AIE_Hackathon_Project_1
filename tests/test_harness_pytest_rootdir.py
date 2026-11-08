"""Regression: a sandbox nested under a project that has its own pyproject [tool.pytest] must still import its
own package. pytest resolves rootdir upward to that pyproject, so a relative `pythonpath=.` pointed at the
project root and every hidden spec test failed with ModuleNotFoundError (seen on the first real run)."""

from pathlib import Path

from core.harness.crosstest import run_pytest

MODULE = "from pricing.tiers import tier_for\n\ndef test_hundred():\n    assert tier_for(100) == 'plus'\n"
IMPL = "def tier_for(amount):\n    return 'basic' if amount < 100 else 'plus'\n"


def test_sandbox_under_project_with_pytest_config(tmp_path: Path):
    project = tmp_path / "project"
    (project / "runs" / "abc" / "pricing").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-q"\n')
    sb = project / "runs" / "abc"
    (sb / "pricing" / "__init__.py").write_text("")
    (sb / "pricing" / "tiers.py").write_text(IMPL)
    res = run_pytest(str(sb), {"test_tiers_spec.py": MODULE}, [], base_repo=project)
    assert res == {"test_tiers_spec.py::test_hundred": (True, "passed")}
