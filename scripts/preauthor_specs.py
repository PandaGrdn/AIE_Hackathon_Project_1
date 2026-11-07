"""Author the hidden spec tests for every subtask of a task up front, in parallel, so the recorded run
hits the cache instead of paying a sequential Gemini call per subtask mid-run.

    python scripts/preauthor_specs.py demo_task/mcp/task.md [--workers 7] [--force]

Each subtask's SpecTests is cached under runs/spec_tests/<key>.json by core.harness.spec_tests. The
author sees only the spec, the pinned interface and the repo at its base commit; nothing here changes
that. `--force` re-authors even when a cache entry exists.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.decompose.decomposer import decompose, load_task  # noqa: E402
from core.harness.spec_tests import author, load_cached, load_or_author, save_cached, spec_node_ids  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_file")
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    task = decompose(load_task(args.task_file))
    repo = Path(task.repo_path)
    if not repo.is_dir():
        print(f"repo not found: {repo} (run demo_task/mcp/setup.sh)", file=sys.stderr)
        return 2

    def one(st):
        t0 = time.time()
        if args.force:
            spec = save_cached(st, author(st, repo)) and load_cached(st)
        else:
            spec = load_or_author(st, repo)
        n_tests = len(spec_node_ids(spec.modules))
        return st.index, st.title, len(spec.modules), n_tests, len(spec.cases), time.time() - t0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        rows = sorted(ex.map(one, task.subtasks))
    print(f"{'#':>2}  {'subtask':40}  modules  tests  cases   secs")
    weak = []
    for i, title, mods, tests, cases, secs in rows:
        print(f"{i:>2}  {title[:40]:40}  {mods:>7}  {tests:>5}  {cases:>5}  {secs:5.1f}")
        if tests < 8 and cases == 0:
            weak.append(title)
    if weak:
        print("\nWARNING: thin spec tests (fewer than 8 tests) for:", "; ".join(weak), "\nre-run with --force or inspect runs/spec_tests/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
