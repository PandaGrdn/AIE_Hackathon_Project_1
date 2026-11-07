"""run_task(snapshot_path=...) writes a UI snapshot with cross-test verdicts, offline."""

from __future__ import annotations

import json
from functools import partial

from tests.test_offline_loop import _make_heldout, _make_task, offline  # noqa: F401  (offline is a fixture)


def test_run_task_writes_snapshot(tmp_path, offline):  # noqa: F811
    from core.orchestrator import run_task
    from core.store.memory import MemoryStore
    from eval.fake_swarm import fake_reflect, fake_spec_tests, fake_swarm
    from eval.heldout_runner import make_heldout_runner
    from ui.server import load_snapshot

    task = _make_task(tmp_path)
    heldout_dir = _make_heldout(tmp_path)
    runs_dir = tmp_path / "runs"
    store = MemoryStore()
    snap = tmp_path / "out" / "snapshot.json"

    runner = make_heldout_runner(store, swarm_fn=fake_swarm(runs_dir=runs_dir), heldout_dir=heldout_dir, n_fuzz=5, swarm_size=4, runs_dir=runs_dir)
    summary = run_task(
        task, store, runner,
        swarm_fn=fake_swarm(runs_dir=runs_dir),
        spec_fn=partial(fake_spec_tests, n_fuzz=2),
        reflect_fn=fake_reflect,
        n_fuzz=5,
        swarm_size=4,
        snapshot_path=snap,
    )

    assert summary["subtasks_done"] == 2
    assert summary["snapshot_path"] == str(snap)
    assert snap.is_file()

    # verdicts accumulated across both subtasks, as plain lists on the store
    per = summary["per_subtask"]
    assert isinstance(store.failed_run_ids, list) and isinstance(store.verified_run_ids, list)
    assert set(store.failed_run_ids) == {i for r in per for i in r["failed_run_ids"]}
    assert set(store.verified_run_ids) == {i for r in per for i in r["verified_run_ids"]}
    assert all(r["failed_run_ids"] and r["verified_run_ids"] for r in per)

    data = json.loads(snap.read_text())
    assert data["failed_run_ids"] and data["verified_run_ids"]
    assert not set(data["failed_run_ids"]) & set(data["verified_run_ids"])

    loaded = load_snapshot(snap)
    assert loaded.failed_run_ids and loaded.verified_run_ids
    assert set(store.failed_run_ids) <= loaded.failed_run_ids
    assert loaded.list_lessons() and loaded.runs
    assert set(loaded.runs) >= set(store.failed_run_ids) | set(store.verified_run_ids)


def test_run_task_without_snapshot_path_has_no_snapshot_key(tmp_path, offline):  # noqa: F811
    from core.orchestrator import run_task
    from core.store.memory import MemoryStore
    from eval.fake_swarm import fake_reflect, fake_spec_tests, fake_swarm

    task = _make_task(tmp_path)
    runs_dir = tmp_path / "runs"
    store = MemoryStore()
    summary = run_task(
        task, store, None,
        swarm_fn=fake_swarm(runs_dir=runs_dir), spec_fn=partial(fake_spec_tests, n_fuzz=2), reflect_fn=fake_reflect, n_fuzz=5, swarm_size=4,
    )
    assert "snapshot_path" not in summary
    assert not hasattr(store, "failed_run_ids")
