"""Held-out evaluation. Owner: A.

eval/heldout/<name>/ holds: spec.md, interface.json (list[FunctionSig]), reference/ (correct impl).
The runner builds the swarm on each held-out subtask with the given adopted lessons active,
cross-tests against the reference, and returns metrics for the gate.

For the hackathon, keep the held-out set small (3–5 subtasks) and cache swarm runs per
(subtask, sorted lesson ids) so the gate is cheap to call repeatedly.

Every LLM-dependent piece is injectable (`swarm_fn`, `spec_fn`) so the gate runs offline with
eval/fake_swarm.py. The swarm starts from `<name>/repo/` when present, else from an empty base
repo under runs/heldout_base/<name>/repo with the reference mirrored next to it (so a fake swarm
can find it at `<repo>/../reference`).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from core.changes.trust import weights_for
from core.config import settings
from core.harness.crosstest import cross_test
from core.models import AgentIdentity, AgentRun, FunctionSig, Subtask
from core.paths.divergence import find_divergence
from core.store.base import Store
from core.swarm.router import route

HELDOUT_DIR = Path(__file__).parent / "heldout"
REFERENCE_WEIGHT = 1e6  # the reference is an always-trusted voter: its outcome is the majority
DEFAULT_N_FUZZ = 50
_cache: dict[tuple[str, tuple[str, ...]], dict[str, float]] = {}  # kept for back-compat; runners use their own


def load_heldout(heldout_dir: str | Path | None = None) -> list[tuple[Subtask, Path]]:
    root = Path(heldout_dir) if heldout_dir else HELDOUT_DIR
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (d / "interface.json").exists() or not (d / "reference").is_dir():
            continue  # incomplete folder (or not a held-out subtask): skip rather than score it
        spec = (d / "spec.md").read_text() if (d / "spec.md").exists() else ""
        iface = [FunctionSig(**f) for f in json.loads((d / "interface.json").read_text())]
        files = sorted({s.file for s in iface})
        st = Subtask(id=f"heldout-{d.name}", index=0, title=d.name, spec=spec, interface=iface, files=files, tags=["heldout"])
        out.append((st, d / "reference"))
    return out


def make_heldout_runner(
    store: Store,
    *,
    swarm_fn=None,
    spec_fn=None,
    heldout_dir: str | Path | None = None,
    n_fuzz: int = DEFAULT_N_FUZZ,
    swarm_size: int | None = None,
    runs_dir: str | Path | None = None,
):
    cache: dict[tuple[str, tuple[str, ...]], dict[str, float]] = {}

    def run_heldout(lesson_ids: list[str]) -> dict[str, float]:
        key_ids = tuple(sorted(lesson_ids))
        items = load_heldout(heldout_dir)
        if not items:
            return {"accuracy": 0.0, "repeat_mistake_rate": 0.0, "false_block_rate": 0.0}
        correct, repeats, total = 0.0, 0.0, 0
        for subtask, reference in items:
            key = (str(reference), key_ids)
            if key not in cache:
                cache[key] = _run_one(
                    subtask, reference, list(key_ids), store,
                    swarm_fn=swarm_fn, spec_fn=spec_fn, n_fuzz=n_fuzz, swarm_size=swarm_size, runs_dir=runs_dir,
                )
            m = cache[key]
            correct += m["correct"]
            repeats += m["repeats"]
            total += 1
        return {"accuracy": correct / total, "repeat_mistake_rate": repeats / total, "false_block_rate": 0.0}

    run_heldout.cache = cache  # type: ignore[attr-defined]
    return run_heldout


def _base_repo(subtask: Subtask, reference: Path, runs_dir: Path) -> Path:
    own = reference.parent / "repo"
    if own.is_dir():
        return own
    base = runs_dir / "heldout_base" / subtask.title
    (base / "repo").mkdir(parents=True, exist_ok=True)
    mirror = base / "reference"
    if mirror.exists():
        shutil.rmtree(mirror)
    shutil.copytree(reference, mirror, ignore=shutil.ignore_patterns("__pycache__"))
    return base / "repo"


def _run_one(
    subtask: Subtask,
    reference: Path,
    lesson_ids: list[str],
    store: Store,
    *,
    swarm_fn=None,
    spec_fn=None,
    n_fuzz: int = DEFAULT_N_FUZZ,
    swarm_size: int | None = None,
    runs_dir: str | Path | None = None,
) -> dict[str, float]:
    """Swarm on one held-out subtask with `lesson_ids` active, cross-tested with the reference as the oracle.

    correct = share of swarm runs that match the reference on every case (1.0 when all do).
    repeats = share of failed runs whose divergence step lands inside an existing failure region.
    """
    if swarm_fn is None:
        from core.swarm.runner import run_swarm as swarm_fn
    rdir = Path(runs_dir) if runs_dir else settings.runs_dir
    repo = _base_repo(subtask, Path(reference), rdir)

    agents = route(subtask, store, swarm_size or settings.swarm_size)
    runs: list[AgentRun] = asyncio.run(swarm_fn(subtask, agents, repo, list(lesson_ids), store))
    if not runs:
        return {"correct": 0.0, "repeats": 0.0}

    ref_run = AgentRun(
        id="reference",
        subtask_id=subtask.id,
        agent=AgentIdentity(id="reference", provider="reference", model="reference"),
        sandbox_path=str(Path(reference).resolve()),
    )
    weights = weights_for(runs, store)
    weights[ref_run.id] = REFERENCE_WEIGHT
    spec_cases = spec_fn(subtask, repo).cases if spec_fn else None
    result = cross_test(subtask, runs + [ref_run], weights, n_fuzz=n_fuzz, spec_cases=spec_cases)

    verified = [r for r in runs if r.id in result.verified_runs]
    failed = [r for r in runs if r.id not in result.verified_runs]  # includes runs that errored out
    correct = len(verified) / len(runs)

    hits = 0
    for run in failed:
        if _lands_in_region(run, verified, store):
            hits += 1
    repeats = hits / len(failed) if failed else 0.0
    return {"correct": correct, "repeats": repeats}


def _lands_in_region(run: AgentRun, verified: list[AgentRun], store: Store) -> bool:
    div = find_divergence(run, verified) if verified else None
    if div is not None and div.step_index < len(run.steps):
        emb = run.steps[div.step_index].embedding
        return bool(emb is not None and store.regions_near(emb))
    # no consensus to align against: any step inside a known region counts
    return any(s.embedding is not None and store.regions_near(s.embedding) for s in run.steps)
