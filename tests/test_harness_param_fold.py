"""Parametrized and anyio tests must count as their bare spec test, not as regression tests."""

from pathlib import Path

from core.harness.crosstest import run_pytest

MODULE = '''
import pytest
import anyio

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.mark.parametrize("x", [1, 2, 3])
def test_param_ok(x):
    assert x > 0

@pytest.mark.parametrize("x", [1, 0])
def test_param_one_fails(x):
    assert x > 0

@pytest.mark.anyio
async def test_async_ok():
    await anyio.sleep(0)
    assert True
'''


def test_suffixed_ids_fold_into_bare_spec_ids(tmp_path: Path):
    sandbox = tmp_path / "sb"
    (sandbox / "tests").mkdir(parents=True)
    (sandbox / "pkg").mkdir()
    (sandbox / "pkg" / "__init__.py").write_text("")
    results = run_pytest(str(sandbox), {"test_spec.py": MODULE}, repo_tests=[])
    keys = set(results)
    assert "test_spec.py::test_param_ok" in keys and results["test_spec.py::test_param_ok"][0] is True
    assert "test_spec.py::test_param_one_fails" in keys and results["test_spec.py::test_param_one_fails"][0] is False
    assert "test_spec.py::test_async_ok" in keys and results["test_spec.py::test_async_ok"][0] is True
    # no suffixed leftovers that would be misfiled as regression tests
    assert not [k for k in keys if "[" in k]
