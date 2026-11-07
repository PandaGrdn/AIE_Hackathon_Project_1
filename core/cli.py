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
eval/heldout/ (or `--heldout-dir`) with the same fakes.
"""

from __future__ import annotations

import sys
from pathlib import Path

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


@app.command()
def run(
    task_file: str,
    heldout: bool = typer.Option(True, help="Gate changes on the held-out set"),
    fake: bool = typer.Option(False, "--fake", help="Offline: deterministic fake decomposer/spec/swarm/reflector, no API calls"),
    reference_dir: str = typer.Option(None, "--reference-dir", help="With --fake: reference implementation mirroring the repo layout (default: <task dir>/reference)"),
    heldout_dir: str = typer.Option(None, "--heldout-dir", help="Held-out folder (default: eval/heldout)"),
    snapshot: str = typer.Option(None, "--snapshot", help="Write the UI snapshot JSON here after every subtask (serve with SWARM_SNAPSHOT=<path> uvicorn ui.server:app)"),
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

    from functools import partial

    from eval.fake_swarm import fake_decompose, fake_reflect, fake_spec_tests, fake_swarm, use_fake_embeddings

    use_fake_embeddings()
    ref = Path(reference_dir) if reference_dir else Path(task_file).resolve().parent / "reference"
    if not ref.is_dir():
        raise typer.BadParameter(f"reference dir not found: {ref} (pass --reference-dir)")
    task = fake_decompose(load_task(task_file), ref)
    print(f"[bold]{task.title}[/] → {len(task.subtasks)} subtasks [dim](fake, reference: {ref})[/]")
    runner = make_heldout_runner(store, swarm_fn=fake_swarm(), heldout_dir=heldout_dir, n_fuzz=FAKE_N_FUZZ) if heldout else None
    summary = run_task(
        task, store, runner,
        swarm_fn=fake_swarm(ref), spec_fn=partial(fake_spec_tests, reference_dir=ref), reflect_fn=fake_reflect, n_fuzz=FAKE_N_FUZZ,
        snapshot_path=snapshot,
    )
    summary.pop("per_subtask", None)
    print(summary)
    print(summarize(store))
    _print_changes(store)


@app.command()
def setup(task_dir: str):
    """Prepare a task folder by running its setup.sh (e.g. `swarm setup demo_task/mcp`)."""
    import subprocess

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
        from eval.fake_swarm import fake_swarm, use_fake_embeddings

        use_fake_embeddings()
        print(make_heldout_runner(store, swarm_fn=fake_swarm(), heldout_dir=heldout_dir, n_fuzz=FAKE_N_FUZZ)(adopted))
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
