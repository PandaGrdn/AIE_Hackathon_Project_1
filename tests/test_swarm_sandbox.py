"""Sandbox: path safety, file tools, pytest runners and diff for both backends."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.swarm.sandbox as sandbox_mod
from core.swarm.sandbox import Sandbox

TIERS = "def tier(amount: float) -> str:\n    return 'gold' if amount >= 100 else 'basic'\n"
TEST_X = "from pricing.tiers import tier\n\ndef test_tier():\n    assert tier(150) == 'gold'\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    (r / "pricing").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "pricing" / "__init__.py").write_text("")
    (r / "pricing" / "tiers.py").write_text(TIERS)
    (r / "tests" / "test_x.py").write_text(TEST_X)
    (r / "README.md").write_text("# demo\n")
    (r / ".git").mkdir()
    (r / ".git" / "config").write_text("[core]\n")
    (r / "pricing" / "__pycache__").mkdir()
    (r / "pricing" / "__pycache__" / "tiers.pyc").write_bytes(b"\x00")
    monkeypatch.setattr(sandbox_mod, "settings", SimpleNamespace(sandboxes_dir=tmp_path / "sb", sandbox_backend="copy"))
    return r


def test_resolve_rejects_escapes(repo: Path):
    with Sandbox(repo, "r1") as sb:
        for bad in ("../x", "/etc/passwd", ".git/config", "pricing/../../x"):
            with pytest.raises(ValueError):
                sb.resolve(bad)
        assert sb.resolve("pricing/tiers.py") == (sb.path / "pricing" / "tiers.py").resolve()


def test_write_read_roundtrip_and_truncation(repo: Path):
    with Sandbox(repo, "r2") as sb:
        sb.write_file("pricing/new/mod.py", "x = 1\n")
        assert sb.read_file("pricing/new/mod.py") == "x = 1\n"
        sb.write_file("big.txt", "a" * 100)
        out = sb.read_file("big.txt", max_bytes=10)
        assert out.startswith("a" * 10) and out.endswith("... [truncated, 100 bytes total]")
        with pytest.raises(FileNotFoundError):
            sb.read_file("nope.py")


def test_list_files_excludes_and_caps(repo: Path):
    with Sandbox(repo, "r3") as sb:
        (sb.path / ".git").mkdir()
        (sb.path / ".git" / "HEAD").write_text("ref")
        (sb.path / "pricing" / "__pycache__").mkdir()
        (sb.path / "pricing" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        files = sb.list_files()
        assert files == ["README.md", "pricing/__init__.py", "pricing/tiers.py", "tests/test_x.py"]
        assert sb.list_files("pricing/*.py") == ["pricing/__init__.py", "pricing/tiers.py"]
        capped = sb.list_files(max_entries=2)
        assert capped == ["README.md", "pricing/__init__.py", "... (2 more)"]


def test_run_tests_copy_mode_uses_pythonpath(repo: Path):
    with Sandbox(repo, "r4") as sb:
        out = sb.run_tests()
        assert "1 passed" in out
        assert "passed" in sb.run_tests("tests/test_x.py")
        with pytest.raises(ValueError):
            sb.run_tests("../tests")
        # Without `-o pythonpath=.`, `python -I` cannot import the package from cwd.
        r = subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider"], check=False, cwd=sb.path, capture_output=True, text=True)
        assert r.returncode != 0 and "ModuleNotFoundError" in r.stdout + r.stderr


def test_run_pytest_modules(repo: Path):
    mods = {
        "spec_a": "from pricing.tiers import tier\n\ndef test_gold():\n    assert tier(100) == 'gold'\n\ndef test_wrong():\n    assert tier(1) == 'gold'\n",
        "spec_b": "import pytest\n\ndef test_basic():\n    assert 1\n\n@pytest.mark.skip\ndef test_skipped():\n    pass\n",
    }
    with Sandbox(repo, "r5") as sb:
        res = sb.run_pytest_modules(mods)
        assert res == {"spec_a::test_gold": True, "spec_a::test_wrong": False, "spec_b::test_basic": True, "spec_b::test_skipped": False}
        assert not list((sb.path / "tests").glob("_spec_*"))
        assert sb.list_files() == ["README.md", "pricing/__init__.py", "pricing/tiers.py", "tests/test_x.py"]


def test_diff_copy_mode(repo: Path):
    with Sandbox(repo, "r6") as sb:
        sb.write_file("pricing/quote.py", "def quote(): return 0\n")
        sb.write_file("pricing/tiers.py", TIERS.replace("gold", "plat"))
        sb.write_file("img.bin", "ok")
        (sb.path / "img.bin").write_bytes(b"\xff\xfe\x00binary")
        d = sb.diff()
        assert "--- a/pricing/quote.py" in d and "+++ b/pricing/quote.py" in d and "+def quote(): return 0" in d
        assert "--- a/pricing/tiers.py" in d and "-    return 'gold'" in d and "+    return 'plat'" in d
        assert "Binary files differ: img.bin" in d
        assert "README.md" not in d


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


def test_diff_worktree_mode(repo: Path, tmp_path: Path):
    import shutil

    shutil.rmtree(repo / ".git")
    shutil.rmtree(repo / "pricing" / "__pycache__")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    with Sandbox(repo, "r7", backend="worktree") as sb:
        assert sb.path.exists()
        sb.write_file("pricing/quote.py", "def quote(): return 0\n")
        sb.write_file("README.md", "# demo\nmore\n")
        d = sb.diff()
        assert "b/pricing/quote.py" in d and "+def quote(): return 0" in d
        assert "b/README.md" in d and "+more" in d
        with pytest.raises(ValueError):
            sb.resolve(".git")
    assert not sb.path.exists()
