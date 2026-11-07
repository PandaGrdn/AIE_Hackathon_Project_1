"""Held-out evaluation. Owner: A.

eval/heldout/<name>/ holds: spec.md, interface.json (list[FunctionSig]), reference/ (correct impl).
The runner builds the swarm on each held-out subtask with the given adopted lessons active,
cross-tests against the reference, and returns metrics for the gate.

For the hackathon, keep the held-out set small (3–5 subtasks) and cache swarm runs per
(subtask, sorted lesson ids) so the gate is cheap to call repeatedly.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.models import FunctionSig, Subtask
from core.store.base import Store

HELDOUT_DIR = Path(__file__).parent / "heldout"
_cache: dict[tuple[str, tuple[str, ...]], dict[str, float]] = {}


def load_heldout() -> list[tuple[Subtask, Path]]:
    out = []
    for d in sorted(p for p in HELDOUT_DIR.iterdir() if p.is_dir()):
        spec = (d / "spec.md").read_text() if (d / "spec.md").exists() else ""
        iface = [FunctionSig(**f) for f in json.loads((d / "interface.json").read_text())] if (d / "interface.json").exists() else []
        files = sorted({s.file for s in iface})
        out.append((Subtask(index=0, title=d.name, spec=spec, interface=iface, files=files), d / "reference"))
    return out


def make_heldout_runner(store: Store):
    def run_heldout(lesson_ids: list[str]) -> dict[str, float]:
        key_ids = tuple(sorted(lesson_ids))
        items = load_heldout()
        if not items:
            return {"accuracy": 0.0, "repeat_mistake_rate": 0.0, "false_block_rate": 0.0}
        correct, repeats, total = 0, 0, 0
        for subtask, reference in items:
            key = (subtask.id if False else subtask.title, key_ids)
            if key not in _cache:
                _cache[key] = _run_one(subtask, reference, lesson_ids, store)
            m = _cache[key]
            correct += m["correct"]
            repeats += m["repeats"]
            total += 1
        return {"accuracy": correct / total, "repeat_mistake_rate": repeats / total, "false_block_rate": 0.0}

    return run_heldout


def _run_one(subtask: Subtask, reference: Path, lesson_ids: list[str], store: Store) -> dict[str, float]:
    # TODO(A): run the swarm (core.swarm.run_swarm) with lesson_ids, cross-test against `reference`
    # as an extra always-trusted run, and count: correct = majority matches reference on all cases;
    # repeats = failed runs whose divergence lands inside an existing failure region.
    return {"correct": 0.0, "repeats": 0.0}
