"""CLI: `swarm run <task.md>`, `swarm eval`, `swarm changelog`."""

from __future__ import annotations

import typer
from rich import print
from rich.table import Table

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(task_file: str, heldout: bool = typer.Option(True, help="Gate changes on the held-out set")):
    """Run a long task end to end."""
    from core.decompose.decomposer import decompose, load_task
    from core.orchestrator import run_task
    from core.store import get_store
    from eval.heldout_runner import make_heldout_runner

    store = get_store()
    task = decompose(load_task(task_file))
    print(f"[bold]{task.title}[/] → {len(task.subtasks)} subtasks")
    runner = make_heldout_runner(store) if heldout else None
    summary = run_task(task, store, runner)
    print(summary)


@app.command()
def eval():
    """Run the held-out set with currently adopted lessons and print metrics."""
    from core.store import get_store
    from eval.heldout_runner import make_heldout_runner

    store = get_store()
    adopted = [l.id for l in store.list_lessons() if l.status == "adopted"]
    print(make_heldout_runner(store)(adopted))


@app.command()
def changelog():
    """Print every versioned change and its held-out decision."""
    from core.store import get_store

    t = Table("status", "kind", "description", "before", "after", "reason")
    for c in get_store().list_changes():
        t.add_row(c.status, c.kind, c.description[:60], str(c.heldout_before), str(c.heldout_after), c.reason)
    print(t)


if __name__ == "__main__":
    app()
