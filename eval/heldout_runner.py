"""Held-out evaluation. Owner: A.

Two folder formats under eval/heldout/<name>/ (detected per folder by `load_heldout`):

  functions  spec.md, interface.json (list[FunctionSig]), reference/ (correct impl mirroring the
             repo layout). Verdict: cross_test against the reference as an always-trusted voter.
  patch      spec.md, reference.patch (applies with `git apply` at the base commit),
             hidden_tests/test_*.py, optional interface.json, base_commit and repo/. The base repo
             is `<name>/repo` when present, else the demo repo demo_task/mcp/repo (whose base commit
             is demo_task/mcp/base_commit when the folder has none). Verdict: the hidden pytest
             modules run inside each run's sandbox; a run is correct iff none fail or error.

The runner builds the swarm on each held-out subtask with the given adopted lessons active,
scores it, and returns metrics for the gate. Results are cached per (folder, sorted lesson ids)
so the gate is cheap to call repeatedly.

Every LLM-dependent piece is injectable (`swarm_fn`, `spec_fn`) so the gate runs offline with
eval/fake_swarm.py. For the functions kind the swarm starts from `<name>/repo/` when present, else
from an empty base repo under runs/heldout_base/<name>/repo with the reference mirrored next to it
(so a fake swarm can find it at `<repo>/../reference`). For the patch kind it starts from an export
of the base repo at the base commit under runs/heldout_base/<name>/<commit>/repo (reference
mirrored the same way, as a symlink to the materialized reference).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from core.changes.trust import weights_for
from core.config import ROOT, settings
from core.harness.crosstest import cross_test
from core.models import AgentIdentity, AgentRun, FunctionSig, Subtask
from core.paths.divergence import find_divergence
from core.store.base import Store
from core.swarm.router import route

HELDOUT_DIR = Path(__file__).parent / "heldout"
DEMO_TASK_DIR = ROOT / "demo_task" / "mcp"
DEMO_REPO = Path(os.environ["HELDOUT_BASE_REPO"]) if os.environ.get("HELDOUT_BASE_REPO") else DEMO_TASK_DIR / "repo"  # override: gate on a clone copy
REFERENCE_WEIGHT = 1e6  # the reference is an always-trusted voter: its outcome is the majority
DEFAULT_N_FUZZ = 50
HIDDEN_TESTS_TIMEOUT_S = 900
_cache: dict[tuple[str, tuple[str, ...]], dict[str, float]] = {}  # kept for back-compat; runners use their own
_reference_checked: dict[tuple[str, str], HiddenTestResult] = {}  # (materialized ref, stamp) -> result

console = Console()

Kind = Literal["functions", "patch"]


# ---------- loading ----------


@dataclass
class HeldoutItem:
    subtask: Subtask
    kind: Kind
    folder: Path
    reference_dir: Path | None = None  # functions kind
    base_commit: str | None = None  # patch kind
    reference_patch: Path | None = None  # patch kind
    hidden_tests_dir: Path | None = None  # patch kind
    base_repo: Path | None = None  # patch: <folder>/repo or the demo repo (may not exist); functions: <folder>/repo if present

    @property
    def name(self) -> str:
        return self.folder.name

    def __iter__(self) -> Iterator:
        """Back-compat with the old `(subtask, reference_dir)` tuples: `for st, ref in load_heldout()`."""
        yield self.subtask
        yield self.reference_dir


def load_heldout(heldout_dir: str | Path | None = None) -> list[HeldoutItem]:
    """One HeldoutItem per complete folder, sorted by folder name; incomplete folders are skipped.

    functions: needs interface.json and reference/.
    patch:     needs reference.patch and hidden_tests/ with at least one test_*.py (and no reference/).
    """
    root = Path(heldout_dir) if heldout_dir else HELDOUT_DIR
    if not root.is_dir():
        return []
    out: list[HeldoutItem] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        spec = (d / "spec.md").read_text() if (d / "spec.md").exists() else ""
        iface = [FunctionSig(**f) for f in json.loads((d / "interface.json").read_text())] if (d / "interface.json").exists() else []
        own_repo = d / "repo" if (d / "repo").is_dir() else None
        if (d / "interface.json").exists() and (d / "reference").is_dir():
            files = sorted({s.file for s in iface})
            st = Subtask(id=f"heldout-{d.name}", index=0, title=d.name, spec=spec, interface=iface, files=files, tags=["heldout"])
            out.append(HeldoutItem(subtask=st, kind="functions", folder=d, reference_dir=d / "reference", base_repo=own_repo))
            continue
        patch, hidden = d / "reference.patch", d / "hidden_tests"
        if patch.is_file() and hidden.is_dir() and any(hidden.glob("test_*.py")) and spec.strip():
            base_repo = own_repo or DEMO_REPO
            base_commit = _read_commit(d / "base_commit")
            if base_commit is None and own_repo is None:
                base_commit = _read_commit(DEMO_TASK_DIR / "base_commit")
            files = sorted({s.file for s in iface}) or _patch_files(patch)
            st = Subtask(id=f"heldout-{d.name}", index=0, title=d.name, spec=spec, interface=iface, files=files, tags=["heldout", "patch"])
            out.append(HeldoutItem(
                subtask=st, kind="patch", folder=d, base_commit=base_commit, reference_patch=patch,
                hidden_tests_dir=hidden, base_repo=base_repo,
            ))
        # else: incomplete folder (or not a held-out subtask): skip rather than score it
    return out


def _read_commit(p: Path) -> str | None:
    if not p.is_file():
        return None
    return p.read_text().strip() or None


def _patch_files(patch: Path) -> list[str]:
    files = set()
    for line in patch.read_text().splitlines():
        m = re.match(r"^\+\+\+ b/(\S+)", line)
        if m:
            files.add(m.group(1))
    return sorted(files)


# ---------- runner ----------


def make_heldout_runner(
    store: Store,
    *,
    swarm_fn=None,
    spec_fn=None,
    heldout_dir: str | Path | None = None,
    n_fuzz: int = DEFAULT_N_FUZZ,
    swarm_size: int | None = None,
    runs_dir: str | Path | None = None,
    parallel: bool = True,
    max_parallel: int = 4,
):
    cache: dict[tuple[str, tuple[str, ...]], dict[str, float]] = {}
    warned: set[str] = set()

    def run_heldout(lesson_ids: list[str]) -> dict[str, float]:
        key_ids = tuple(sorted(lesson_ids))
        items = []
        for item in load_heldout(heldout_dir):
            if item.kind == "patch" and not (item.base_repo and item.base_repo.is_dir()):
                if item.name not in warned:
                    warned.add(item.name)
                    console.print(f"[yellow]held-out {item.name}: base repo {item.base_repo} not found (run demo_task/mcp/setup.sh); skipped")
                continue
            items.append(item)
        if not items:
            return {"accuracy": 0.0, "repeat_mistake_rate": 0.0, "false_block_rate": 0.0}
        # Slices are independent: run the uncached ones in parallel (each _run_one calls asyncio.run in
        # its own thread). A live sweep is then one swarm wall-time, not <slices> × swarm wall-time.
        todo = [item for item in items if (str(item.folder), key_ids) not in cache]
        if todo:
            from concurrent.futures import ThreadPoolExecutor

            def _one(item):
                return _run_one(
                    item, list(key_ids), store,
                    swarm_fn=swarm_fn, spec_fn=spec_fn, n_fuzz=n_fuzz, swarm_size=swarm_size, runs_dir=runs_dir,
                )

            if parallel and len(todo) > 1:
                with ThreadPoolExecutor(max_workers=min(len(todo), max_parallel)) as ex:
                    for item, m in zip(todo, ex.map(_one, todo)):
                        cache[(str(item.folder), key_ids)] = m
            else:
                for item in todo:
                    cache[(str(item.folder), key_ids)] = _one(item)
        correct, repeats, total = 0.0, 0.0, 0
        for item in items:
            key = (str(item.folder), key_ids)
            m = cache[key]
            correct += m["correct"]
            repeats += m["repeats"]
            total += 1
        return {"accuracy": correct / total, "repeat_mistake_rate": repeats / total, "false_block_rate": 0.0}

    run_heldout.cache = cache  # type: ignore[attr-defined]
    return run_heldout


def _run_one(
    item: HeldoutItem,
    lesson_ids: list[str],
    store: Store,
    *,
    swarm_fn=None,
    spec_fn=None,
    n_fuzz: int = DEFAULT_N_FUZZ,
    swarm_size: int | None = None,
    runs_dir: str | Path | None = None,
) -> dict[str, float]:
    """Swarm on one held-out subtask with `lesson_ids` active, scored against its oracle.

    correct = share of swarm runs judged correct (1.0 when all are).
    repeats = share of failed runs whose divergence step lands inside an existing failure region.
    """
    if swarm_fn is None:
        from core.swarm.runner import run_swarm as swarm_fn
    rdir = Path(runs_dir) if runs_dir else settings.runs_dir
    if item.kind == "patch":
        return _run_patch(item, lesson_ids, store, swarm_fn=swarm_fn, swarm_size=swarm_size, runs_dir=rdir)
    return _run_functions(item, lesson_ids, store, swarm_fn=swarm_fn, spec_fn=spec_fn, n_fuzz=n_fuzz, swarm_size=swarm_size, runs_dir=rdir)


def _score(runs: list[AgentRun], verified_ids: set[str], store: Store) -> dict[str, float]:
    verified = [r for r in runs if r.id in verified_ids]
    failed = [r for r in runs if r.id not in verified_ids]  # includes runs that errored out
    hits = sum(1 for run in failed if _lands_in_region(run, verified, store))
    return {"correct": len(verified) / len(runs), "repeats": hits / len(failed) if failed else 0.0}


# ---------- functions kind ----------


def _base_repo(subtask: Subtask, reference: Path, runs_dir: Path) -> Path:
    own = reference.parent / "repo"
    if own.is_dir():
        return own
    base = runs_dir / "heldout_base" / subtask.title
    (base / "repo").mkdir(parents=True, exist_ok=True)
    mirror = base / "reference"
    if mirror.is_symlink() or mirror.is_file():
        mirror.unlink()
    elif mirror.exists():
        shutil.rmtree(mirror)
    shutil.copytree(reference, mirror, ignore=shutil.ignore_patterns("__pycache__"))
    return base / "repo"


def _run_functions(item: HeldoutItem, lesson_ids, store, *, swarm_fn, spec_fn, n_fuzz, swarm_size, runs_dir) -> dict[str, float]:
    subtask, reference = item.subtask, Path(item.reference_dir)
    repo = _base_repo(subtask, reference, runs_dir)

    agents = route(subtask, store, swarm_size or settings.swarm_size)
    runs: list[AgentRun] = asyncio.run(swarm_fn(subtask, agents, repo, list(lesson_ids), store))
    if not runs:
        return {"correct": 0.0, "repeats": 0.0}

    ref_run = AgentRun(
        id="reference",
        subtask_id=subtask.id,
        agent=AgentIdentity(id="reference", provider="reference", model="reference"),
        sandbox_path=str(reference.resolve()),
    )
    weights = weights_for(runs, store)
    weights[ref_run.id] = REFERENCE_WEIGHT
    spec = spec_fn(subtask, repo) if spec_fn else None
    result = cross_test(subtask, runs + [ref_run], weights, n_fuzz=n_fuzz, spec=spec, repo=repo)
    return _score(runs, set(result.verified_runs), store)


# ---------- patch kind ----------


@dataclass
class HiddenTestResult:
    passed: bool
    tests: int
    failures: int
    errors: int
    returncode: int
    output: str


def _cache_key(item: HeldoutItem) -> str:
    return item.base_commit or "worktree"


def _stamp(item: HeldoutItem, with_patch: bool) -> str:
    h = hashlib.sha256()
    h.update(str(item.folder.resolve()).encode())
    h.update(b"\0" + str(item.base_repo.resolve() if item.base_repo else "").encode())
    h.update(b"\0" + (item.base_commit or "").encode())
    if with_patch and item.reference_patch:
        h.update(b"\0" + item.reference_patch.read_bytes())
    return h.hexdigest()


def _export_base(item: HeldoutItem, dest: Path) -> None:
    """The base repo at the base commit into `dest` (no .git). `git archive` when the base repo is a git
    checkout and a base commit is known (so commits on top of it, e.g. the long task's, are not seen);
    else a copy of the working tree."""
    base = item.base_repo
    if base is None or not base.is_dir():
        raise FileNotFoundError(f"held-out {item.name}: base repo {base} not found (run demo_task/mcp/setup.sh)")
    if dest.exists():
        shutil.rmtree(dest)
    if item.base_commit and (base / ".git").exists():
        r = subprocess.run(["git", "-C", str(base), "archive", "--format=tar", item.base_commit], capture_output=True, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"held-out {item.name}: git archive {item.base_commit} failed in {base}: {r.stderr.decode(errors='replace').strip()}")
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:") as tf:
            tf.extractall(dest, filter="data")
    else:
        shutil.copytree(base, dest, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"))


def _apply_patch(patch: Path, dest: Path, name: str) -> None:
    # dest may sit inside another checkout (runs/ is inside this project): stop git from discovering it,
    # or `git apply` would resolve paths against that repo's top level and silently skip them.
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(dest.resolve().parent)}
    patch = patch.resolve()
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=dest, capture_output=True, text=True, env=env, check=False)
    if r.returncode == 0:
        return
    r2 = subprocess.run(["patch", "-p1", "--batch", "--forward", "-i", str(patch)], cwd=dest, capture_output=True, text=True, check=False)
    if r2.returncode != 0:
        raise RuntimeError(
            f"held-out {name}: reference.patch does not apply at the base commit\n"
            f"git apply: {r.stderr.strip()}\npatch -p1: {(r2.stdout + r2.stderr).strip()}"
        )


def _cached_tree(item: HeldoutItem, work_dir: Path, sub: str, with_patch: bool) -> Path:
    slot = work_dir / sub / item.name / _cache_key(item)
    tree, stamp_file = slot / "repo", slot / "stamp"
    stamp = _stamp(item, with_patch)
    if tree.is_dir() and stamp_file.is_file() and stamp_file.read_text() == stamp:
        return tree
    if slot.exists():
        shutil.rmtree(slot)
    _export_base(item, tree)
    if with_patch:
        _apply_patch(item.reference_patch, tree, item.name)
    stamp_file.write_text(stamp)
    return tree


def materialize_reference(item: HeldoutItem, work_dir: str | Path | None = None) -> Path:
    """Base repo at the base commit with reference.patch applied, cached per (folder, base commit,
    patch contents) under `<work_dir>/heldout_ref/<name>/<base_commit>/repo` (work_dir defaults to
    settings.runs_dir). Returns the tree's path."""
    if item.kind != "patch":
        raise ValueError(f"materialize_reference: {item.name} is a {item.kind} slice")
    return _cached_tree(item, Path(work_dir) if work_dir else settings.runs_dir, "heldout_ref", with_patch=True)


def materialize_base(item: HeldoutItem, work_dir: str | Path | None = None) -> Path:
    """Base repo at the base commit (no patch), cached under `<work_dir>/heldout_base/<name>/<base_commit>/repo`.
    The swarm starts from a copy of this."""
    return _cached_tree(item, Path(work_dir) if work_dir else settings.runs_dir, "heldout_base", with_patch=False)


def _python_for(sandbox: Path, item: HeldoutItem) -> str:
    """The sandbox's uv venv, else the base repo's (a worktree sandbox has no .venv), else ours."""
    for cand in (sandbox / ".venv" / "bin" / "python", (item.base_repo or sandbox) / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def _source_paths(sandbox: Path, python: str, item: HeldoutItem) -> list[str]:
    """sys.path entries that make the sandbox's code shadow the interpreter's editable installs.

    A uv venv installs the project editable with absolute paths into the *original* repo
    (`_editable_impl_*.pth`), so a copied sandbox would import the original's sources. Every .pth
    entry under the base repo (or under the venv's own project dir) is remapped into the sandbox;
    then `<sandbox>/src` and the sandbox root."""
    roots = [p.resolve() for p in (item.base_repo, Path(python).parent.parent.parent) if p is not None and p.exists()]
    paths: list[Path] = []
    venv = Path(python).parent.parent
    for pth in sorted(venv.glob("lib/python*/site-packages/*.pth")):
        try:
            lines = pth.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", "import")) or not os.path.isabs(line):
                continue
            p = Path(line).resolve()
            for root in roots:
                if p.is_relative_to(root):
                    mapped = sandbox / p.relative_to(root)
                    if mapped.is_dir():
                        paths.append(mapped)
                    break
    if (sandbox / "src").is_dir():
        paths.append(sandbox / "src")
    paths.append(sandbox)
    out: list[str] = []
    for p in paths:
        s = str(p.resolve())
        if s not in out:
            out.append(s)
    return out


def _has_pytest_config(sandbox: Path) -> bool:
    if (sandbox / "pytest.ini").is_file() or (sandbox / ".pytest.ini").is_file():
        return True
    pp = sandbox / "pyproject.toml"
    if pp.is_file() and "[tool.pytest" in pp.read_text(errors="replace"):
        return True
    for name, section in (("tox.ini", "[pytest]"), ("setup.cfg", "[tool:pytest]")):
        f = sandbox / name
        if f.is_file() and section in f.read_text(errors="replace"):
            return True
    return False


_BOOT = "import sys; sys.path[:0] = {paths!r}; import pytest; raise SystemExit(pytest.main(sys.argv[1:]))"


def run_hidden_tests(sandbox: str | Path, item: HeldoutItem, *, timeout: int = HIDDEN_TESTS_TIMEOUT_S) -> HiddenTestResult:
    """Copy hidden_tests/*.py into `<sandbox>/tests/_hidden_<name>/`, run pytest on that dir only,
    remove the copy, parse the junit report.

    Equivalent to `python -I -m pytest -q <dir> -p no:cacheprovider --junitxml=<tmp>` with cwd and
    rootdir = the sandbox, except that the sandbox's sources are prepended to sys.path (see
    `_source_paths`). Passed iff the junit report exists, has at least one test, zero failures and
    zero errors, and pytest exited 0."""
    sandbox = Path(sandbox).resolve()
    if not sandbox.is_dir():
        return HiddenTestResult(False, 0, 0, 1, -1, f"sandbox {sandbox} does not exist")
    tests_dir = sandbox / "tests"
    created_tests_dir = not tests_dir.exists()
    dest = tests_dir / f"_hidden_{item.name}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in sorted(item.hidden_tests_dir.glob("*.py")):
        shutil.copy2(f, dest / f.name)
    tmp = Path(tempfile.mkdtemp(prefix="heldout_junit_"))
    junit = tmp / "junit.xml"
    python = _python_for(sandbox, item)
    args = [str(dest), "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", f"--rootdir={sandbox}"]
    if not _has_pytest_config(sandbox):
        (tmp / "pytest.ini").write_text("[pytest]\n")  # don't pick up an enclosing project's config
        args += ["-c", str(tmp / "pytest.ini")]
    cmd = [python, "-I", "-c", _BOOT.format(paths=_source_paths(sandbox, python, item)), *args]
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTEST_ADDOPTS")}
    try:
        try:
            r = subprocess.run(cmd, cwd=sandbox, capture_output=True, text=True, timeout=timeout, env=env, check=False)
            rc, output = r.returncode, (r.stdout + r.stderr)[-8000:]
        except subprocess.TimeoutExpired as e:
            rc, output = -1, f"TIMEOUT after {timeout}s\n{(e.stdout or '')[-4000:] if isinstance(e.stdout, str) else ''}"
        tests, failures, errors = _parse_junit(junit)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        if created_tests_dir:
            shutil.rmtree(tests_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    if tests is None:
        return HiddenTestResult(False, 0, 0, 1, rc, output)
    return HiddenTestResult(rc == 0 and tests > 0 and failures == 0 and errors == 0, tests, failures, errors, rc, output)


def _parse_junit(path: Path) -> tuple[int | None, int, int]:
    """(tests, failures, errors) summed over every <testsuite>; tests is None when there is no report."""
    if not path.is_file():
        return None, 0, 0
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None, 0, 0
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    tests = sum(int(s.get("tests", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    return tests, failures, errors


def check_reference(item: HeldoutItem, work_dir: str | Path | None = None) -> HiddenTestResult:
    """Materialize the reference and run the hidden tests on it; raise if it fails (the slice is broken).
    Cached per (materialized tree, stamp) for the life of the process."""
    ref = materialize_reference(item, work_dir)
    key = (str(ref), (ref.parent / "stamp").read_text())
    if key not in _reference_checked:
        res = run_hidden_tests(ref, item)
        if not res.passed:
            raise RuntimeError(
                f"held-out slice {item.name} is broken: the reference ({item.reference_patch} applied at "
                f"{item.base_commit or 'the working tree'} of {item.base_repo}) fails its hidden tests "
                f"(tests={res.tests} failures={res.failures} errors={res.errors} exit={res.returncode})\n{res.output[-3000:]}"
            )
        _reference_checked[key] = res
    return _reference_checked[key]


def _run_patch(item: HeldoutItem, lesson_ids, store, *, swarm_fn, swarm_size, runs_dir: Path) -> dict[str, float]:
    check_reference(item, runs_dir)  # before any swarm spend: a broken slice must not score anything
    ref = materialize_reference(item, runs_dir)
    repo = materialize_base(item, runs_dir)
    mirror = repo.parent / "reference"  # for the fake swarm (<repo>/../reference); never in the sandbox
    if mirror.is_symlink() and mirror.resolve() != ref.resolve():
        mirror.unlink()
    if not mirror.exists():
        mirror.symlink_to(ref.resolve(), target_is_directory=True)

    subtask = item.subtask
    agents = route(subtask, store, swarm_size or settings.swarm_size)
    runs: list[AgentRun] = asyncio.run(swarm_fn(subtask, agents, repo, list(lesson_ids), store))
    if not runs:
        return {"correct": 0.0, "repeats": 0.0}

    verified: set[str] = set()
    fractions: list[float] = []
    for run in runs:
        if run.error or not run.sandbox_path:
            fractions.append(0.0)
            continue
        res = run_hidden_tests(run.sandbox_path, item)
        if res.passed:
            verified.add(run.id)
        fractions.append(max(0.0, (res.tests - res.failures - res.errors) / res.tests) if res.tests else 0.0)
    score = _score(runs, verified, store)
    # With one or two agents per slice a binary "run verified" score is almost always 0 and the gate is
    # blind. The hidden-test pass FRACTION (mean over runs) moves when a lesson helps part of the way, so it
    # is the gate's accuracy for patch slices; the verified share is kept alongside.
    score["verified_share"] = score["correct"]
    score["correct"] = sum(fractions) / len(fractions) if fractions else 0.0
    return score


def _lands_in_region(run: AgentRun, verified: list[AgentRun], store: Store) -> bool:
    div = find_divergence(run, verified) if verified else None
    if div is not None and div.step_index < len(run.steps):
        emb = run.steps[div.step_index].embedding
        return bool(emb is not None and store.regions_near(emb))
    # no consensus to align against: any step inside a known region counts
    return any(s.embedding is not None and store.regions_near(s.embedding) for s in run.steps)
