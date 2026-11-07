"""CLI: `swarm setup <task dir>`, `swarm run <task.md>`, `swarm eval`, `swarm changelog`.

Two demo tasks, one folder each under demo_task/ (task.md, subtasks.json, repo/, reference/):

    demo_task/pricing/   tiered-pricing module on a near-empty package; offline fallback, no setup
    demo_task/mcp/       per-tool rate limits and quotas on the MCP Python SDK at a pinned commit;
                         `swarm setup demo_task/mcp` first (runs its setup.sh: clone + uv sync)

    swarm run demo_task/pricing/task.md --fake       # offline demo, no API keys, no network
    swarm setup demo_task/mcp && swarm run demo_task/mcp/task.md

`--fake` swaps every LLM-dependent piece for eval/fake_swarm.py: the decomposer (one subtask per
reference module, interface pinned with ast), the spec-test author (expected values from the
reference), the swarm (reference copies plus a boundary-bug mutant) and the reflector; embeddings
use the deterministic fake_embed. `--reference-dir` defaults to `<task dir>/reference`; it must mirror
the repo layout (e.g. demo_task/pricing/reference/pricing/tiers.py). The held-out gate runs on
eval/heldout/ (or `--heldout-dir`) with the same fakes (patch-kind slices via fake_patch_swarm).

A task whose `<task dir>/reference/` holds `*.patch` files (demo_task/mcp) is a patch series instead:
`--fake` keeps the precomputed subtasks.json, takes subtask i's reference as the repo's committed state
plus patch i, and its spec tests from patch i's test module (see demo_task/mcp/README.md). Its repo is
committed into, so it is reset to `base_commit` first when it has moved (`--reset-repo/--no-reset-repo`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import print
from rich.table import Table

# The installed package is `core` only (pyproject: packages.find include = ["core*"]), so the
# `swarm` console script cannot import the sibling `eval` package unless run from the repo root.
_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "eval" / "__init__.py").is_file() and str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

app = typer.Typer(no_args_is_help=True)

FAKE_N_FUZZ = 20
# Every patch-series run executes the SDK's tests/server + tests/shared (~15 s); 4 runs fit one batch of
# cross_test's 4 pytest workers. SWARM_SIZE in the environment overrides it.
PATCH_SWARM_SIZE = 4
PATCH_N_FUZZ = 1  # patch-series subtasks pin methods; the verdict is the pytest modules + repo suite, fuzz adds only boundary seeds (hypothesis needs >= 1)


@app.command()
def run(
    task_file: str,
    heldout: bool = typer.Option(True, help="Gate changes on the held-out set"),
    fake: bool = typer.Option(False, "--fake", help="Offline: deterministic fake decomposer/spec/swarm/reflector, no API calls"),
    reference_dir: str = typer.Option(None, "--reference-dir", help="With --fake: reference implementation mirroring the repo layout (default: <task dir>/reference)"),
    heldout_dir: str = typer.Option(None, "--heldout-dir", help="Held-out folder (default: eval/heldout)"),
    snapshot: str = typer.Option(None, "--snapshot", help="Write the UI snapshot JSON here after every subtask (serve with SWARM_SNAPSHOT=<path> uvicorn ui.server:app)"),
    reset_repo: Optional[bool] = typer.Option(
        None, "--reset-repo/--no-reset-repo",
        help="With --fake on a patch-series task: reset the repo to its base_commit first (default: when it has commits or edits on top of it)",
    ),
):
    """Run a long task end to end."""
    from core.decompose.decomposer import decompose, load_task
    from core.orchestrator import run_task
    from core.store import get_store
    from eval.heldout_runner import make_heldout_runner
    from eval.metrics import summarize

    store = get_store()
    if not fake:
        task = decompose(load_task(task_file))
        print(f"[bold]{task.title}[/] → {len(task.subtasks)} subtasks")
        runner = make_heldout_runner(store, heldout_dir=heldout_dir) if heldout else None
        print(run_task(task, store, runner, snapshot_path=snapshot))
        return

    import os
    from functools import partial

    from core.config import settings

    from eval.fake_swarm import (
        fake_decompose, fake_heldout_swarm, fake_patch_spec_tests, fake_patch_swarm, fake_reflect, fake_spec_tests, fake_swarm,
        find_patch_series, use_fake_embeddings,
    )

    use_fake_embeddings()
    task_dir = Path(task_file).resolve().parent
    runner = (
        make_heldout_runner(store, swarm_fn=fake_heldout_swarm(heldout_dir), spec_fn=fake_spec_tests, heldout_dir=heldout_dir, n_fuzz=FAKE_N_FUZZ)
        if heldout else None
    )
    patches = [] if reference_dir else find_patch_series(task_dir)
    if patches and heldout:
        # every held-out sweep on the MCP slices runs real pytest per run: keep it to the live-run size
        runner = make_heldout_runner(
            store, swarm_fn=fake_heldout_swarm(heldout_dir), spec_fn=fake_spec_tests, heldout_dir=heldout_dir, n_fuzz=FAKE_N_FUZZ,
            swarm_size=getattr(settings, "heldout_swarm_size", None),
        )
    if patches:
        # patch-series reference (demo_task/mcp/reference/NN-*.patch): the precomputed subtasks.json, one patch per subtask
        task = decompose(load_task(task_file))
        repo = Path(task.repo_path)
        if len(task.subtasks) != len(patches):
            raise typer.BadParameter(f"{len(task.subtasks)} subtasks but {len(patches)} patches in {task_dir / 'reference'}")
        if not (repo / ".git").exists():
            raise typer.BadParameter(f"{repo} is not set up (run: swarm setup {task_dir})")
        _maybe_reset_repo(repo, task_dir / "base_commit", reset_repo)
        _default_regression_paths(repo)
        print(f"[bold]{task.title}[/] → {len(task.subtasks)} subtasks [dim](fake, patch series: {len(patches)} patches in {task_dir / 'reference'})[/]")
        summary = run_task(
            task, store, runner,
            swarm_fn=fake_patch_swarm(patches), spec_fn=partial(fake_patch_spec_tests, patches=patches), reflect_fn=fake_reflect,
            n_fuzz=PATCH_N_FUZZ, swarm_size=PATCH_SWARM_SIZE if "SWARM_SIZE" not in os.environ else None, snapshot_path=snapshot,
        )
    else:
        ref = Path(reference_dir) if reference_dir else task_dir / "reference"
        if not ref.is_dir():
            raise typer.BadParameter(f"reference dir not found: {ref} (pass --reference-dir)")
        task = fake_decompose(load_task(task_file), ref)
        print(f"[bold]{task.title}[/] → {len(task.subtasks)} subtasks [dim](fake, reference: {ref})[/]")
        summary = run_task(
            task, store, runner,
            swarm_fn=fake_swarm(ref), spec_fn=partial(fake_spec_tests, reference_dir=ref), reflect_fn=fake_reflect, n_fuzz=FAKE_N_FUZZ,
            snapshot_path=snapshot,
        )
    summary.pop("per_subtask", None)
    print(summary)
    print(summarize(store))
    _print_changes(store)


def _default_regression_paths(repo: Path) -> None:
    """The MCP SDK's full tests/ tree has suites that only run under its own CI (≈46 s); the regression suite
    for the verdict is tests/server + tests/shared (setup.sh's baseline, ≈15 s) unless REGRESSION_TEST_PATHS is set."""
    import os

    from core.config import settings

    if "REGRESSION_TEST_PATHS" in os.environ or not hasattr(settings, "regression_test_paths"):
        return
    paths = tuple(d for d in ("tests/server", "tests/shared") if (repo / d).is_dir())
    if paths:
        object.__setattr__(settings, "regression_test_paths", paths)  # Settings is frozen; this is the env default it would have read


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _maybe_reset_repo(repo: Path, base_commit_file: Path, reset: bool | None) -> None:
    """Same as `bash <task dir>/setup.sh --reset`: `git checkout -f -B swarm <base>` + `git clean -fdq`.
    With reset=None, only when HEAD has commits on top of the base or tracked files are modified."""
    if reset is False or not base_commit_file.is_file():
        return
    base = base_commit_file.read_text().strip()
    if reset is None:
        ahead = _git(repo, "rev-list", "--count", f"{base}..HEAD").stdout.strip()
        dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        at_base = _git(repo, "rev-parse", "HEAD").stdout.strip() == _git(repo, "rev-parse", f"{base}^{{commit}}").stdout.strip()
        if ahead == "0" and at_base and not dirty:
            return
    r = _git(repo, "checkout", "-q", "-f", "-B", "swarm", base)
    if r.returncode != 0:
        raise typer.BadParameter(f"cannot reset {repo} to {base}: {r.stderr.strip()}")
    _git(repo, "clean", "-fdq")
    print(f"[dim]reset {repo} to {base} (branch swarm)[/]")


@app.command()
def setup(task_dir: str):
    """Prepare a task folder by running its setup.sh (e.g. `swarm setup demo_task/mcp`)."""
    d = Path(task_dir)
    if d.is_file():  # accept the task file too
        d = d.parent
    script = d / "setup.sh"
    if not script.is_file():
        raise typer.BadParameter(f"no setup.sh in {d}")
    rc = subprocess.run(["bash", str(script.resolve())], cwd=d.resolve()).returncode
    if rc:
        raise typer.Exit(rc)


@app.command()
def eval(
    fake: bool = typer.Option(False, "--fake", help="Offline: fake swarm on the held-out set"),
    heldout_dir: str = typer.Option(None, "--heldout-dir", help="Held-out folder (default: eval/heldout)"),
):
    """Run the held-out set with currently adopted lessons and print metrics."""
    from core.store import get_store
    from eval.heldout_runner import make_heldout_runner

    store = get_store()
    adopted = [l.id for l in store.list_lessons() if l.status == "adopted"]
    if fake:
        from eval.fake_swarm import fake_heldout_swarm, fake_spec_tests, use_fake_embeddings

        use_fake_embeddings()
        print(make_heldout_runner(store, swarm_fn=fake_heldout_swarm(heldout_dir), spec_fn=fake_spec_tests, heldout_dir=heldout_dir, n_fuzz=FAKE_N_FUZZ)(adopted))
    else:
        print(make_heldout_runner(store, heldout_dir=heldout_dir)(adopted))


@app.command()
def changelog():
    """Print every versioned change and its held-out decision."""
    from core.store import get_store

    _print_changes(get_store())


def _print_changes(store) -> None:
    t = Table("status", "kind", "description", "before", "after", "reason")
    for c in store.list_changes():
        t.add_row(c.status, c.kind, c.description[:60], str(c.heldout_before), str(c.heldout_after), c.reason)
    print(t)


if __name__ == "__main__":
    app()
