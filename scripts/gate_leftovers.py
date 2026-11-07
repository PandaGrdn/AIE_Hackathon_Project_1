"""Put every still-undecided lesson in a snapshot through the held-out gate, then write the snapshot back.

    python scripts/gate_leftovers.py demo/mcp_snapshot.json [--heldout-swarm-size 1] [--agents ...] [--dry-run]

Why: a lesson created in an interrupted attempt and re-hit later used to slip past the gate (fixed in
core/changes/proposer.py); this gates such leftovers after the fact with the same real swarm, same three
MCP held-out slices and the same adopt/rollback rule, so the changelog is complete.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.changes.gate import evaluate_change  # noqa: E402
from core.models import Change, ChangeKind, ChangeStatus  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("--heldout-swarm-size", type=int, default=1)
    ap.add_argument("--agents", default="gemini-t2,gemini-lite-t9-alt,claude-t2,meta-spark-t5")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import os

    os.environ.setdefault("SWARM_AGENTS", args.agents)
    from eval.heldout_runner import make_heldout_runner
    from scripts.record_demo import mcp_heldout_dir  # the symlink folder with only the three MCP slices
    from ui.server import load_snapshot, write_snapshot
    from core.config import settings

    store = load_snapshot(args.snapshot)
    decided = {c.ref_id for c in store.list_changes() if c.kind == ChangeKind.context}
    leftovers = [l for l in store.list_lessons() if l.status == ChangeStatus.candidate and l.id not in decided]
    print(f"{len(leftovers)} undecided lesson(s) not yet gated")
    for l in leftovers:
        print(" -", l.id, l.text[:100])
    if args.dry_run or not leftovers:
        return 0
    hdir = mcp_heldout_dir(settings.runs_dir / "record_demo" / "heldout_mcp")
    run_heldout = make_heldout_runner(store, heldout_dir=hdir, swarm_size=args.heldout_swarm_size)
    for l in leftovers:
        ch = evaluate_change(Change(kind=ChangeKind.context, ref_id=l.id, description=f"lesson: {l.text}"), store, run_heldout)
        print(f"{ch.status}: {l.text[:80]} — {ch.reason} {ch.heldout_before} -> {ch.heldout_after}")
        write_snapshot(store, args.snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
