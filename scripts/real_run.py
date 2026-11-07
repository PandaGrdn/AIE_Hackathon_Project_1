"""One REAL swarm run on selected subtasks of a long task: live agents, live spec author, live reflector.

    python scripts/real_run.py demo_task/mcp/task.md --subtasks 0 --swarm-size 3 \
        --snapshot demo/mcp_real_s1.json --reset-repo

What is real here (the defaults of core.orchestrator.run_task):
  - swarm:     core.swarm.runner.run_swarm (tool loop, log_step gating, live visible spec tests)
  - spec:      core.harness.spec_tests.load_or_author (Gemini author, cached under runs/spec_tests/)
  - verdict:   core.harness.crosstest.cross_test (hidden pytest modules + the repo's regression suite,
               run in every sandbox on the base repo's own .venv interpreter)
  - locate:    SubQ Snap root-cause decision when SUBQ_API_KEY is set, else the embedding-path heuristic
  - reflector: Gemini lesson text
What is off: the held-out gate (`run_heldout=None`); proposed changes are stored as candidates only.

Decomposition is offline (the task's subtasks.json). The roster is `swarm_identities()` filtered by
`provider_available()` (a missing key drops those identities instead of erroring), narrowed by
`--agents` when given.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_AGENTS = "gemini-t2,gemini-t7-alt,meta-spark-t5"
MIN_SPEC_TESTS = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", help="task markdown, e.g. demo_task/mcp/task.md (subtasks.json next to it)")
    ap.add_argument("--subtasks", default="0", help="comma-separated subtask indices to run, in order (default 0)")
    ap.add_argument("--swarm-size", type=int, default=3)
    ap.add_argument("--agents", default=DEFAULT_AGENTS,
                    help=f"comma-separated identity ids from swarm_identities() to draw the roster from (default {DEFAULT_AGENTS}); 'all' = every available identity")
    ap.add_argument("--snapshot", default=None, help="UI snapshot path written after each subtask, e.g. demo/mcp_real_s1.json")
    ap.add_argument("--reset-repo", action="store_true", help="reset the task repo to its base commit on branch `swarm` first")
    ap.add_argument("--base-commit", default=None, help="base commit for --reset-repo (default: <task dir>/base_commit, else 77001c9)")
    ap.add_argument("--isolate", action="store_true",
                    help="run on a fresh local clone of the task repo (runs/real_repo/<task>, .venv symlinked) instead of the repo itself")
    ap.add_argument("--agent-timeout", type=int, default=360, help="wall-clock cap per agent, seconds (default 360)")
    ap.add_argument("--max-turns", type=int, default=40, help="model turns per agent (default 40)")
    ap.add_argument("--token-budget", type=int, default=1_000_000,
                    help="cumulative input+output tokens per agent (every turn resends the history; default 1,000,000)")
    ap.add_argument("--n-fuzz", type=int, default=10, help="fuzz draws per interface function in the verdict (evidence only when spec tests exist)")
    ap.add_argument("--regression", default="tests/server tests/shared",
                    help="repo-relative dirs forming the regression suite (default: the MCP task's 'tests/server tests/shared')")
    return ap.parse_args(argv)


def parse_indices(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    if not out:
        raise ValueError("no subtask indices given")
    return out


def slice_subtasks(task, indices: list[int]):
    """Keep only `task.subtasks` with the given indices (in index order); unknown indices raise."""
    by_index = {s.index: s for s in task.subtasks}
    missing = [i for i in indices if i not in by_index]
    if missing:
        raise ValueError(f"unknown subtask indices {missing}; task has {sorted(by_index)}")
    task.subtasks = [by_index[i] for i in sorted(set(indices))]
    return task


def build_roster(agent_ids: str | None, swarm_size: int, identities=None, available=None) -> list:
    """`swarm_identities()` filtered by `provider_available`, narrowed to `agent_ids` (comma list, or 'all'),
    capped at `swarm_size`. Raises when nothing is left."""
    from core.llm.clients import provider_available, swarm_identities

    identities = swarm_identities() if identities is None else identities
    available = provider_available if available is None else available
    pool = [a for a in identities if available(a.provider)]
    dropped = [a.id for a in identities if not available(a.provider)]
    if agent_ids and agent_ids != "all":
        want = [x.strip() for x in agent_ids.split(",") if x.strip()]
        by_id = {a.id: a for a in pool}
        pool = [by_id[i] for i in want if i in by_id]
    if not pool:
        raise RuntimeError(f"no swarm identity has a configured provider key (dropped: {dropped})")
    return pool[:swarm_size], dropped


def reset_repo(repo: Path, base_commit: str) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", base_commit, "-B", "swarm"], check=True)


def isolated_clone(repo: Path, base_commit: str, dest: Path) -> Path:
    """A fresh `git clone --local` of `repo` at `base_commit` on branch swarm, sharing its .venv by symlink."""
    import shutil

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--local", "--no-checkout", str(repo), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", "-B", "swarm", base_commit], check=True)
    for k, v in (("user.name", "swarm"), ("user.email", "swarm@localhost")):
        subprocess.run(["git", "-C", str(dest), "config", k, v], check=True)
    if (repo / ".venv").is_dir():
        (dest / ".venv").symlink_to((repo / ".venv").resolve(), target_is_directory=True)
    return dest


def check_spec(spec, module_path: str) -> list[str]:
    """Problems with an authored spec for a real-repo subtask (empty list = fine)."""
    import ast

    problems = []
    if not spec.modules:
        return ["no pytest modules"]
    for name, src in spec.modules.items():
        try:
            ast.parse(src)
        except SyntaxError as e:
            problems.append(f"{name} does not parse: {e}")
        if module_path and module_path not in src:
            problems.append(f"{name} does not import {module_path}")
    if len(spec.test_node_ids) < MIN_SPEC_TESTS:
        problems.append(f"only {len(spec.test_node_ids)} test functions (< {MIN_SPEC_TESTS})")
    return problems


def module_of(file: str) -> str:
    """src/mcp/server/mcpserver/limits.py -> mcp.server.mcpserver.limits"""
    p = file.removeprefix("src/").removesuffix(".py")
    return p.replace("/", ".")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # settings are read at import time: set the knobs first
    os.environ["AGENT_TIMEOUT_S"] = str(args.agent_timeout)
    os.environ["REGRESSION_TEST_PATHS"] = args.regression
    os.environ.setdefault("USE_MEMORY_STORE", "1")

    from rich.console import Console

    import core.orchestrator as orch
    import core.swarm.router as router
    import core.swarm.runner as runner
    from core.decompose.decomposer import decompose, load_task, task_dir
    from core.harness.spec_tests import load_or_author
    from core.llm.decisions import SubQDecisions
    from core.store.memory import MemoryStore

    con = Console()
    t_start = time.time()
    task = decompose(load_task(args.task))
    repo = Path(task.repo_path)
    base = args.base_commit
    bc_file = task_dir(task) / "base_commit"
    if base is None:
        base = bc_file.read_text().strip() if bc_file.is_file() else "77001c9"
    if args.isolate:
        repo = isolated_clone(repo, base, ROOT / "runs" / "real_repo" / task_dir(task).name)
        task.repo_path = str(repo)
        con.print(f"isolated clone: {repo} at {base}")
    elif args.reset_repo:
        reset_repo(repo, base)
        con.print(f"reset {repo} to {base} on branch swarm")
    slice_subtasks(task, parse_indices(args.subtasks))
    roster, dropped = build_roster(args.agents, args.swarm_size)
    if dropped:
        con.print(f"[yellow]dropped (no provider key): {', '.join(dropped)}")
    con.print("roster: " + ", ".join(f"{a.id} ({a.provider}/{a.model}, t={a.temperature}, {a.prompt_variant})" for a in roster))
    con.print(f"locate: {'SubQ Snap' if SubQDecisions.is_configured() else 'embedding-path heuristic (no SUBQ_API_KEY)'}; held-out gate: OFF")

    # The router picks from swarm_identities(); hand it exactly our roster. MAX_TURNS caps every agent.
    router.swarm_identities = lambda: list(roster)
    runner.MAX_TURNS = args.max_turns
    runner.TOKEN_BUDGET = args.token_budget

    results: dict[str, Any] = {}  # subtask_id -> VerdictResult
    specs: dict[str, Any] = {}

    def spec_fn(subtask, repo_path):
        spec = load_or_author(subtask, repo_path)
        mods = sorted({module_of(s.file) for s in subtask.interface})
        problems = [p for m in mods for p in check_spec(spec, m)] if mods else check_spec(spec, "")
        con.print(f"spec: {len(spec.modules)} module(s), {len(spec.test_node_ids)} tests, "
                  f"{len(spec.hidden_test_ids)} hidden / {len(spec.test_node_ids) - len(spec.hidden_test_ids)} visible"
                  + (f" [red]problems: {problems}" if problems else ""))
        specs[subtask.id] = spec
        return spec

    real_cross_test = orch.cross_test

    def cross_test(subtask, runs, weights, **kw):
        res = real_cross_test(subtask, runs, weights, **kw)
        results[subtask.id] = res
        return res

    orch.cross_test = cross_test
    store = MemoryStore()
    try:
        summary = orch.run_task(task, store, run_heldout=None, n_fuzz=args.n_fuzz, swarm_size=args.swarm_size,
                                spec_fn=spec_fn, snapshot_path=args.snapshot)
    finally:
        orch.cross_test = real_cross_test
    wall = time.time() - t_start

    report = summarize(task, store, results, specs, runner.META, summary, wall, args)
    print_report(con, report)
    if args.snapshot:
        p = Path(args.snapshot)
        data = json.loads(p.read_text()) if p.is_file() else {}
        data["real_run"] = report
        p.write_text(json.dumps(data, indent=1, default=str))
        con.print(f"snapshot: {p} (real_run summary added; held-out gate OFF)")
    return 0 if summary["subtasks_done"] == len(task.subtasks) else 1


def summarize(task, store, results, specs, meta, summary, wall, args) -> dict[str, Any]:
    """Plain-dict report: per-run outcome, hidden-test results, root causes, lessons."""
    runs = list(getattr(store, "runs", {}).values()) if isinstance(getattr(store, "runs", None), dict) else list(getattr(store, "runs", []) or [])
    divs = {d.run_id: d for d in (getattr(store, "divergences", None) or [])}
    lessons = {l.created_from.get("run_id"): l for l in store.list_lessons()}
    out_runs = []
    for r in runs:
        res = results.get(r.subtask_id)
        spec = specs.get(r.subtask_id)
        m = meta.get(r.id, {})
        entry: dict[str, Any] = {
            "run_id": r.id, "agent_id": r.agent.id, "model": r.agent.model, "subtask_id": r.subtask_id,
            "steps": len(r.steps), "diff_lines": len(r.diff.splitlines()), "duration_s": round(r.duration_s, 1),
            "turns": m.get("turns"), "input_tokens": m.get("input_tokens"), "output_tokens": m.get("output_tokens"),
            "rejections": m.get("rejections"), "text_calls": m.get("text_calls"), "error": r.error,
        }
        if res is not None:
            entry["verdict"] = "verified" if r.id in res.verified_runs else ("failed" if r.id in res.failed_runs else "excluded (agent error)")
            node_by_case = getattr(res, "nodeid_by_case", {})
            spec_nodes = set(spec.test_node_ids) if spec else set()
            hidden_nodes = set(spec.hidden_test_ids) if spec else set()
            tests = {}
            for cid, node in node_by_case.items():
                if node in spec_nodes:
                    o = res.outcomes.get(r.id, {}).get(cid)
                    if o is not None:
                        tests[node] = {"passed": bool(o.ok), "hidden": node in hidden_nodes, "message": None if o.ok else o.exception}
            entry["spec_tests"] = tests
            entry["regression_failures"] = [node_by_case.get(c, c) for c in getattr(res, "regression_failures", {}).get(r.id, [])]
        d = divs.get(r.id)
        if d is not None:
            entry["root_cause"] = {
                "step_index": d.step_index, "step": d.step_text, "signals": d.signals, "kind": d.kind,
                "kind_confidence": d.kind_confidence, "generalizes": d.generalizes,
                "p": d.step_probabilities.get(f"S{d.step_index}") if d.step_probabilities else None,
                "breaking_case": d.breaking_case.args if d.breaking_case else None,
            }
        lz = lessons.get(r.id)
        if lz is not None:
            entry["lesson"] = {"id": lz.id, "text": lz.text, "confidence": lz.confidence}
        out_runs.append(entry)
    return {
        "command": "python scripts/real_run.py " + " ".join(sys.argv[1:]),
        "wall_s": round(wall, 1),
        "heldout_gate": "off (run_heldout=None): proposed changes stored as candidates, not gated",
        "subtasks": [s.title for s in task.subtasks],
        "summary": {k: v for k, v in summary.items() if k != "per_subtask"},
        "per_subtask": summary.get("per_subtask"),
        "runs": out_runs,
    }


def print_report(con, report: dict[str, Any]) -> None:
    con.rule("[bold]real run")
    for e in report["runs"]:
        v = e.get("verdict", "?")
        colour = "green" if v == "verified" else "red"
        tests = e.get("spec_tests", {})
        hidden = [t for t in tests.values() if t["hidden"]]
        con.print(f"[{colour}]{v}[/] {e['agent_id']} ({e['model']}): {e['steps']} steps, diff {e['diff_lines']} lines, "
                  f"{e['duration_s']}s, {e['turns']} turns, {e['input_tokens']} in / {e['output_tokens']} out tokens"
                  + (f", error={e['error']}" if e.get("error") else ""))
        con.print(f"    hidden spec tests: {sum(t['passed'] for t in hidden)}/{len(hidden)} passed; "
                  f"visible: {sum(t['passed'] for t in tests.values() if not t['hidden'])}/{len(tests) - len(hidden)}; "
                  f"regression failures: {len(e.get('regression_failures', []))}")
        for node, t in sorted(tests.items()):
            if not t["passed"]:
                con.print(f"      FAIL {'hidden' if t['hidden'] else 'visible'} {node}: {(t['message'] or '')[:160]}")
        rc = e.get("root_cause")
        if rc:
            con.print(f"    [magenta]root cause: step {rc['step_index']} via {'+'.join(rc['signals']) or 'path'}"
                      + (f" p={rc['p']:.2f}" if rc.get("p") is not None else "")
                      + (f", kind={rc['kind']} ({rc['kind_confidence']:.2f})" if rc.get("kind") else "")
                      + f": {rc['step'][:200]}")
        if e.get("lesson"):
            con.print(f"    [cyan]lesson: {e['lesson']['text']}")
    s = report["summary"]
    con.print(f"subtasks done {s.get('subtasks_done')}, stalled {s.get('stalled')}, lessons {s.get('lessons')}, "
              f"changes {s.get('changes')}; wall {report['wall_s']}s; held-out gate OFF")


if __name__ == "__main__":
    raise SystemExit(main())
