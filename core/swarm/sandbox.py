"""One isolated repo copy per agent. Owner: B.

Backends: `worktree` (git worktree, fast, default) and `docker` (full isolation).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.config import settings


class Sandbox:
    def __init__(self, repo: Path, run_id: str, backend: str | None = None):
        self.repo = repo
        self.run_id = run_id
        self.backend = backend or settings.sandbox_backend
        self.path = settings.sandboxes_dir / run_id

    def __enter__(self) -> "Sandbox":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.backend == "worktree" and (self.repo / ".git").exists():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach", str(self.path)], check=True, capture_output=True)
        else:
            shutil.copytree(self.repo, self.path, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        return self

    def __exit__(self, *exc):
        if self.backend == "worktree" and (self.repo / ".git").exists():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)], capture_output=True)
        else:
            shutil.rmtree(self.path, ignore_errors=True)

    def write_file(self, rel: str, content: str) -> None:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def run_tests(self, timeout: int = 120) -> str:
        cmd = ["python", "-I", "-m", "pytest", "-q", "-x", "--timeout", str(timeout)] if _has_pytest_timeout() else ["python", "-I", "-m", "pytest", "-q", "-x"]
        try:
            r = subprocess.run(cmd, cwd=self.path, capture_output=True, text=True, timeout=timeout + 10)
            return (r.stdout + r.stderr)[-6000:]
        except subprocess.TimeoutExpired:
            return "TIMEOUT"

    def diff(self) -> str:
        if (self.path / ".git").exists() or self.backend == "worktree":
            r = subprocess.run(["git", "-C", str(self.path), "diff"], capture_output=True, text=True)
            return r.stdout
        return ""  # TODO(B): diff against original for copy backend


def _has_pytest_timeout() -> bool:
    try:
        import pytest_timeout  # noqa: F401

        return True
    except ImportError:
        return False
