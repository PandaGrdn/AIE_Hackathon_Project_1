"""One isolated repo copy per agent. Owner: Lucas Chen.

Backends: `worktree` (git worktree, fast, default when the repo has .git) and `copy`
(plain copytree). All agent-facing paths go through `resolve()` so nothing escapes the
sandbox. Tests run with `python -I`, so `-o pythonpath=.` puts the repo root back on sys.path.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Self

from core.config import settings

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
def _pytest(root: Path) -> list[str]:
    # Sandboxes live under the project root, so pytest would otherwise adopt the project's
    # pyproject.toml as rootdir and resolve `pythonpath` against it. Pin both to the sandbox.
    r = str(root.resolve())
    return ["-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={r}", "-o", f"pythonpath={r}"]


class Sandbox:
    def __init__(self, repo: Path, run_id: str, backend: str | None = None):
        self.repo = repo
        self.run_id = run_id
        self.backend = backend or settings.sandbox_backend
        self.path = settings.sandboxes_dir / run_id

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._is_worktree():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach", str(self.path)], check=True, capture_output=True)
        else:
            shutil.copytree(self.repo, self.path, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        return self

    def __exit__(self, *exc):
        if self._is_worktree():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)], check=False, capture_output=True)
        else:
            shutil.rmtree(self.path, ignore_errors=True)

    def _is_worktree(self) -> bool:
        return self.backend == "worktree" and (self.repo / ".git").exists()

    # ---- paths -----------------------------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """Sandbox-relative path -> absolute path inside the sandbox, or ValueError."""
        if not rel or Path(rel).is_absolute():
            raise ValueError(f"path must be relative: {rel!r}")
        base = self.path.resolve()
        p = (base / rel).resolve()
        if p != base and base not in p.parents:
            raise ValueError(f"path escapes sandbox: {rel!r}")
        if ".git" in p.relative_to(base).parts:
            raise ValueError(f"path inside .git is not allowed: {rel!r}")
        return p

    def write_file(self, rel: str, content: str) -> None:
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def read_file(self, rel: str, max_bytes: int = 20_000) -> str:
        p = self.resolve(rel)
        if not p.is_file():
            raise FileNotFoundError(rel)
        data = p.read_bytes()
        text = data.decode("utf-8", errors="replace")
        if len(data) <= max_bytes:
            return text
        return text[:max_bytes] + f"\n... [truncated, {len(data)} bytes total]"

    def list_files(self, pattern: str = "**/*", max_entries: int = 200) -> list[str]:
        out: list[str] = []
        for p in self.path.glob(pattern):
            rel = p.relative_to(self.path)
            if p.is_file() and not _SKIP_DIRS.intersection(rel.parts):
                out.append(rel.as_posix())
        out.sort()
        if len(out) > max_entries:
            out = out[:max_entries] + [f"... ({len(out) - max_entries} more)"]
        return out

    # ---- tests -----------------------------------------------------------------------------

    def run_tests(self, path: str | None = None, timeout: int = 120) -> str:
        cmd = [sys.executable, *_pytest(self.path), "-x"]
        if _has_pytest_timeout():
            cmd += ["--timeout", str(timeout)]
        if path:
            self.resolve(path)
            cmd.append(path)
        try:
            r = subprocess.run(cmd, check=False, cwd=self.path, capture_output=True, text=True, timeout=timeout + 10)
            return (r.stdout + r.stderr)[-6000:]
        except subprocess.TimeoutExpired:
            return "TIMEOUT"

    def run_pytest_modules(self, modules: dict[str, str], timeout: int = 120) -> dict[str, bool]:
        """Write `{module_name: source}` as temporary test files, run them, return
        `{"<module_name>::<test_name>": passed}` per junit testcase. Files are always removed.
        A timeout (or an unparsable junit report) returns {}.
        """
        tok = uuid.uuid4().hex[:8]
        tests_dir = self.path / "tests"
        tests_dir.mkdir(exist_ok=True)
        # harness module names carry a .py suffix ("test_x.py"); the stem must not, or the junit
        # classname splits on the dot and the result keys never match the harness's node ids
        stems = {name: f"_spec_{tok}_{name.removesuffix('.py')}" for name in modules}
        files = {stems[name]: tests_dir / f"{stems[name]}.py" for name in modules}
        stem_to_mod = {stem: name for name, stem in stems.items()}
        fd, junit = tempfile.mkstemp(prefix="junit_", suffix=".xml")
        os.close(fd)
        try:
            for name, src in modules.items():
                files[stems[name]].write_text(src)
            cmd = [sys.executable, *_pytest(self.path), f"--junitxml={junit}", *(str(p.relative_to(self.path)) for p in files.values())]
            if _has_pytest_timeout():
                cmd += ["--timeout", str(timeout)]
            try:
                subprocess.run(cmd, check=False, cwd=self.path, capture_output=True, text=True, timeout=timeout + 10)
            except subprocess.TimeoutExpired:
                return {}
            return _parse_junit(Path(junit), stem_to_mod)
        finally:
            for p in files.values():
                p.unlink(missing_ok=True)
            Path(junit).unlink(missing_ok=True)

    # ---- diff ------------------------------------------------------------------------------

    def diff(self) -> str:
        if self._is_worktree():
            subprocess.run(["git", "-C", str(self.path), "add", "-A", "-N"], check=False, capture_output=True)
            r = subprocess.run(["git", "-C", str(self.path), "diff"], check=False, capture_output=True, text=True)
            return r.stdout
        rels = sorted(_walk(self.repo) | _walk(self.path))
        out: list[str] = []
        for rel in rels:
            a, b = _read_opt(self.repo / rel), _read_opt(self.path / rel)
            if a == b:
                continue
            try:
                at = a.decode("utf-8") if a is not None else ""
                bt = b.decode("utf-8") if b is not None else ""
            except UnicodeDecodeError:
                out.append(f"Binary files differ: {rel}\n")
                continue
            lines = difflib.unified_diff(at.splitlines(keepends=True), bt.splitlines(keepends=True), f"a/{rel}", f"b/{rel}")
            out.extend(line if line.endswith("\n") else line + "\n" for line in lines)
        return "".join(out)


def _walk(root: Path) -> set[str]:
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            out.add((Path(dirpath) / f).relative_to(root).as_posix())
    return out


def _read_opt(p: Path) -> bytes | None:
    return p.read_bytes() if p.is_file() else None


def _parse_junit(junit: Path, stem_to_mod: dict[str, str]) -> dict[str, bool]:
    try:
        root = ET.parse(junit).getroot()
    except (ET.ParseError, FileNotFoundError):
        return {}
    results: dict[str, bool] = {}
    for tc in root.iter("testcase"):
        parts = (tc.get("classname") or "").split(".")
        idx = next((i for i, part in enumerate(parts) if part in stem_to_mod), None)
        if idx is None:
            # collection error: classname is empty and `name` is the dotted module path
            nparts = (tc.get("name") or "").replace("/", ".").removesuffix(".py").split(".")
            stem = next((p for p in nparts if p in stem_to_mod), None)
            if stem is None:
                continue
            results[f"{stem_to_mod[stem]}::<collection error>"] = False
            continue
        key = "::".join([stem_to_mod[parts[idx]], *parts[idx + 1 :], tc.get("name", "")])
        results[key] = not any(tc.find(tag) is not None for tag in ("failure", "error", "skipped"))
    return results


def _has_pytest_timeout() -> bool:
    try:
        import pytest_timeout  # noqa: F401

        return True
    except ImportError:
        return False
