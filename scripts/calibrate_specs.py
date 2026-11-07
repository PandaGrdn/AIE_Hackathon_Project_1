"""Calibrate authored hidden spec tests against a known-correct reference implementation.

    python scripts/calibrate_specs.py demo_task/mcp/task.md --ref-root runs/mcp_ref

The spec author writes tests from the spec alone and sometimes gets a mock or an attribute wrong; one such
test fails every implementation, including the reference, and no run can ever verify. For each subtask i
this runs the cached authored modules against `<ref-root>/<i+1>/repo` (the materialized reference after
patches 1..i+1, as eval/fake_swarm materializes them) and REMOVES test functions that fail on the
reference, then re-saves the cache (and the tracked copy under spec_tests/). Agents never see the
reference or the tests; this only removes tests that are wrong about the spec.
"""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.decompose.decomposer import decompose, load_task  # noqa: E402
from core.harness.crosstest import run_pytest  # noqa: E402
from core.harness.spec_tests import _cache_path, load_cached, save_cached, spec_node_ids  # noqa: E402


def drop_tests(source: str, names: set[str]) -> str:
    """Remove top-level test functions named in `names` (by source line ranges)."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    cut: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            cut.append((start, node.end_lineno))
    for start, end in sorted(cut, reverse=True):
        del lines[start:end]
    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_file")
    ap.add_argument("--ref-root", default="runs/mcp_ref")
    ap.add_argument("--base-repo", default="demo_task/mcp/repo")
    ap.add_argument("--min-tests", type=int, default=6, help="warn when a subtask keeps fewer tests than this")
    args = ap.parse_args()

    task = decompose(load_task(args.task_file))
    base = Path(args.base_repo)
    for st in task.subtasks:
        spec = load_cached(st)
        if spec is None or not spec.modules:
            print(f"{st.index}: no cached spec, skip")
            continue
        ref = Path(args.ref_root) / str(st.index + 1) / "repo"
        if not ref.is_dir():
            print(f"{st.index}: no reference at {ref}, skip")
            continue
        res = run_pytest(str(ref), spec.modules, repo_tests=[], base_repo=base)
        bad_by_file: dict[str, set[str]] = {}
        for node, (ok, _msg) in res.items():
            if not ok and "::" in node:
                f, name = node.split("::", 1)
                bad_by_file.setdefault(f, set()).add(name.split("[")[0])
        before = len(spec_node_ids(spec.modules))
        if bad_by_file:
            for f, names in bad_by_file.items():
                if f in spec.modules:
                    spec.modules[f] = drop_tests(spec.modules[f], names)
            # a module that failed to collect at all (import error) has no test ids left: drop it
            spec.modules = {f: s for f, s in spec.modules.items() if spec_node_ids({f: s})}
            spec.visible_ids = [v for v in spec.visible_ids if v in set(spec_node_ids(spec.modules))]
            p = save_cached(st, spec)
            tracked = ROOT / "spec_tests" / p.name
            if tracked.parent.is_dir():
                shutil.copy2(p, tracked)
        after = len(spec_node_ids(spec.modules))
        flag = "  <-- thin" if after < args.min_tests else ""
        print(f"{st.index}: {st.title[:44]:44} tests {before:>3} -> {after:>3}  (dropped {before - after}){flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
