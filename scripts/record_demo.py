"""Record the two-task demo: Task A learns lessons, Task B shows whether they transfer.

    python scripts/record_demo.py --dry-run                       # preconditions + plan, no API calls
    python scripts/record_demo.py --swarm-size 3 --heldout-swarm-size 1 --out demo/ --phase all

Phases (each writes a log and a UI snapshot; run them one at a time with --phase, or all in order):

  a        Task A (demo_task/mcp, per-tool rate limits, 7 subtasks) with the held-out gate on the three
           MCP slices (eval/heldout/mcp_*), real swarm / spec author / reflector.
           -> mcp_snapshot.json, task_a_summary.json, task_a.log
  b0       Task B (demo_task/mcp_heldout: timeout -> deprecation -> size cap) on a FRESH store (no lessons).
           Spec tests are the slices' verified hidden tests (eval.heldout_specs.spec_from_hidden_tests); no gate.
           -> task_b_baseline.json, task_b_baseline_summary.json, task_b_baseline.log
  b1       Task B again, store = the Task A snapshot (adopted lessons, failure regions, trust).
           -> task_b_with_lessons.json, task_b_with_lessons_summary.json, task_b_with_lessons.log
  compare  results.json from the three summaries (per-phase metrics + B0 vs B1 deltas), printed as a table.

Every phase that runs agents first resets the shared clone (demo_task/mcp/repo) to the base commit on
branch `swarm` (eval.heldout_specs.reset_repo: previous HEAD kept on a branch, edits stashed; the clone is
never deleted) and afterwards keeps its result on branch `record-demo/<phase>`.

With --phase all, a phase whose summary already exists is skipped (resume after a crash); --redo reruns it.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TASK_A = ROOT / "demo_task" / "mcp" / "task.md"
TASK_B = ROOT / "demo_task" / "mcp_heldout" / "task.md"
HELDOUT_ROOT = ROOT / "eval" / "heldout"
MCP_SLICES = ("mcp_resource_size_cap", "mcp_tool_deprecation", "mcp_tool_timeout")
DEFAULT_BASE = "77001c9"
DEFAULT_AGENTS = "gemini-t2,gemini-t7-alt,meta-spark-t5"  # scripts/real_run.py's tested live roster
PHASES = ("a", "b0", "b1", "compare")
AGENT_PHASES = ("a", "b0", "b1")

# phase -> (snapshot, summary, log) file names under --out
FILES = {
    "a": ("mcp_snapshot.json", "task_a_summary.json", "task_a.log"),
    "b0": ("task_b_baseline.json", "task_b_baseline_summary.json", "task_b_baseline.log"),
    "b1": ("task_b_with_lessons.json", "task_b_with_lessons_summary.json", "task_b_with_lessons.log"),
}
RESULTS = "results.json"
COMPARE_LOG = "compare.log"
LABELS = {"a": "Task A (rate limits, gated)", "b0": "Task B baseline (no lessons)", "b1": "Task B with Task A lessons"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="all", choices=[*PHASES, "all"], help="which phase to run (default all: a, b0, b1, compare)")
    ap.add_argument("--out", default="demo/", help="output directory for logs, snapshots, summaries (default demo/)")
    ap.add_argument("--swarm-size", type=int, default=3, help="agents per subtask (default 3)")
    ap.add_argument("--heldout-swarm-size", type=int, default=1, help="agents per held-out slice in Task A's gate (default 1)")
    ap.add_argument("--agents", default=DEFAULT_AGENTS,
                    help=f"comma-separated identity ids to draw every roster from (default {DEFAULT_AGENTS}); "
                         "'all' = every identity with a key, ranked by trust")
    ap.add_argument("--n-fuzz", type=int, default=10, help="fuzz draws per pinned function in the verdict (evidence only when spec tests exist; default 10)")
    ap.add_argument("--agent-timeout", type=int, default=360, help="wall-clock cap per agent, seconds (default 360)")
    ap.add_argument("--max-turns", type=int, default=40, help="model turns per agent (default 40)")
    ap.add_argument("--token-budget", type=int, default=1_000_000, help="cumulative tokens per agent (default 1,000,000)")
    ap.add_argument("--regression", default="tests/server tests/shared", help="repo-relative regression suite dirs (default 'tests/server tests/shared')")
    ap.add_argument("--base-commit", default=None, help=f"base commit to reset the clone to (default demo_task/mcp/base_commit, else {DEFAULT_BASE})")
    ap.add_argument("--lessons-snapshot", default=None, help="store loaded for b1 (default <out>/mcp_snapshot.json, Task A's snapshot)")
    ap.add_argument("--b1-no-trust", action="store_true", help="b1: drop the carried trust scores so only lessons and failure regions differ from b0")
    ap.add_argument("--redo", action="store_true", help="with --phase all: rerun phases whose summary already exists")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and check preconditions; no API calls, no repo changes")
    ap.add_argument("--check-references", action="store_true",
                    help="with --dry-run: also run each slice's hidden tests on its reference patch (local, a few seconds)")
    args = ap.parse_args(argv)
    if args.n_fuzz < 1:
        ap.error("--n-fuzz must be at least 1 (hypothesis needs one example per pinned function)")
    if args.swarm_size < 1 or args.heldout_swarm_size < 1:
        ap.error("--swarm-size and --heldout-swarm-size must be at least 1")
    return args


# ---------- logging ----------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class _Tee(io.TextIOBase):
    """Write to the real stream and (ANSI-stripped) to a log file."""

    def __init__(self, stream, fh, lock):
        self._stream, self._fh, self._lock = stream, fh, lock

    def write(self, s: str) -> int:
        with self._lock:
            self._stream.write(s)
            self._fh.write(_ANSI.sub("", s))
        return len(s)

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()
            self._fh.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self):  # type: ignore[override]
        return getattr(self._stream, "encoding", "utf-8")


@contextmanager
def tee_to(path: Path):
    """stdout and stderr also go to `path` for the duration (rich consoles resolve sys.stdout per write)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    with open(path, "w", encoding="utf-8") as fh:
        out, err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(out, fh, lock), _Tee(err, fh, lock)
        try:
            yield
        except BaseException:
            fh.write(traceback.format_exc())
            raise
        finally:
            sys.stdout, sys.stderr = out, err


# ---------- helpers ----------


def base_commit(args) -> str:
    if args.base_commit:
        return args.base_commit
    f = TASK_A.parent / "base_commit"
    return f.read_text().strip() if f.is_file() else DEFAULT_BASE


def out_paths(args, phase: str) -> tuple[Path, Path, Path]:
    out = Path(args.out)
    snap, summ, log = FILES[phase]
    return out / snap, out / summ, out / log


def lessons_snapshot(args) -> Path:
    return Path(args.lessons_snapshot) if args.lessons_snapshot else Path(args.out) / FILES["a"][0]


def mcp_heldout_dir(dest: Path) -> Path:
    """A directory holding symlinks to the three MCP held-out slices only (load_heldout reads every
    folder of its root; the function-kind slices are not part of this demo)."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in MCP_SLICES:
        link, target = dest / name, (HELDOUT_ROOT / name).resolve()
        if link.is_symlink() and link.resolve() == target:
            continue
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.exists():
            raise RuntimeError(f"{link} exists and is not a symlink; remove it")
        link.symlink_to(target, target_is_directory=True)
    for extra in dest.iterdir():
        if extra.name not in MCP_SLICES and extra.is_symlink():
            extra.unlink()
    return dest


def roster(agent_ids: str | None, swarm_size: int):
    """(identities the router may pick from, ids dropped for a missing key). With an explicit list the
    router sees exactly that list (filtered by key), so b0 and b1 route the same agents."""
    from core.llm.clients import provider_available, swarm_identities

    ids = swarm_identities()
    dropped = [a.id for a in ids if not provider_available(a.provider)]
    pool = [a for a in ids if provider_available(a.provider)]
    if agent_ids and agent_ids != "all":
        want = [x.strip() for x in agent_ids.split(",") if x.strip()]
        unknown = [w for w in want if w not in {a.id for a in ids}]
        if unknown:
            raise ValueError(f"unknown agent ids {unknown}; known: {[a.id for a in ids]}")
        by_id = {a.id: a for a in pool}
        pool = [by_id[w] for w in want if w in by_id]
    return pool, dropped


def configure_knobs(args, pool) -> None:
    """Runner budgets and the router's roster (module globals, as scripts/real_run.py does)."""
    import core.swarm.router as router
    import core.swarm.runner as runner

    runner.MAX_TURNS = args.max_turns
    runner.TOKEN_BUDGET = args.token_budget
    if args.agents and args.agents != "all":
        router.swarm_identities = lambda: list(pool)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


# ---------- metrics ----------


def store_marks(store) -> dict:
    """What the store holds before a phase, so the phase's own additions can be told apart."""
    from core.models import ChangeStatus

    return {
        "lesson_hits": {l.id: l.hit_count for l in store.list_lessons()},
        "lessons_adopted": sum(1 for l in store.list_lessons() if l.status == ChangeStatus.adopted),
        "change_ids": [c.id for c in store.list_changes()],
        "regions": len(getattr(store, "regions", {}) or {}),
        "trust_agents": sorted(getattr(store, "trust", {}) or {}),
    }


def phase_metrics(summary: dict, store, task, marks: dict, wall_s: float) -> dict:
    """Per-phase numbers for results.json. Only what this phase added counts (b1 starts from Task A's store).

    repeat-mistake rate = reflected failures that deduplicated into an existing lesson / all reflected
    failures in this phase (each reflected failure adds one hit: a new lesson or hit_count += 1)."""
    from core.models import ChangeStatus

    runs = list((getattr(store, "runs", {}) or {}).values())
    manifests = [m for m in (getattr(store, "manifests", {}) or {}).values() if m.task_id == task.id]
    per = []
    for rep in summary.get("per_subtask", []):
        sid = rep.get("subtask_id")
        n = sum(1 for r in runs if r.subtask_id == sid)
        man = next((m for m in manifests if m.subtask_id == sid), None)
        v, f = int(rep.get("verified", 0)), int(rep.get("failed", 0))
        per.append({
            "title": rep.get("title"), "subtask_id": sid, "runs": n, "verified": v, "failed": f,
            "errored": max(0, n - v - f), "first_pass_verified_share": (v / n) if n else None,
            "committed": bool(rep.get("committed")), "lessons_in_play": len(man.lessons_used) if man else 0,
            "lessons_used": list(man.lessons_used) if man else [], "agents": list(man.agent_ids) if man else [],
            "lessons_created": list(rep.get("lessons", [])), "ambiguity_flags": len(rep.get("ambiguity_flags", [])),
        })
    before = marks.get("lesson_hits", {})
    lessons = store.list_lessons()
    new_lessons = [l for l in lessons if l.id not in before]
    hits = sum(max(0, l.hit_count - before.get(l.id, 0)) for l in lessons)
    repeats = hits - len(new_lessons)
    old_changes = set(marks.get("change_ids", []))
    changes = [c for c in store.list_changes() if c.id not in old_changes]
    by_kind: dict[str, dict[str, int]] = {}
    for c in changes:
        by_kind.setdefault(str(c.kind), {}).setdefault(str(c.status), 0)
        by_kind[str(c.kind)][str(c.status)] += 1
    gated = sorted((c for c in changes if c.heldout_after and c.decided_at is not None), key=lambda c: c.decided_at)
    adopted_gated = [c for c in gated if c.status == ChangeStatus.adopted]
    total_runs = sum(p["runs"] for p in per)
    total_verified = sum(p["verified"] for p in per)
    return {
        "subtasks_total": len(task.subtasks),
        "subtasks_attempted": len(per),
        "subtasks_done": summary.get("subtasks_done", 0),
        "stalled": summary.get("stalled", []),
        "per_subtask": per,
        "runs": total_runs,
        "verified": total_verified,
        "failed": sum(p["failed"] for p in per),
        "errored": sum(p["errored"] for p in per),
        "verified_share": (total_verified / total_runs) if total_runs else None,
        "lessons_at_start_adopted": marks.get("lessons_adopted", 0),
        "lessons_created": len(new_lessons),
        "reflected_failures": hits,
        "repeat_mistakes": repeats,
        "repeat_mistake_rate": (repeats / hits) if hits else None,
        "changes": len(changes),
        "changes_adopted": sum(1 for c in changes if c.status == ChangeStatus.adopted),
        "changes_rolled_back": sum(1 for c in changes if c.status == ChangeStatus.rolled_back),
        "changes_candidate": sum(1 for c in changes if c.status == ChangeStatus.candidate),
        "changes_by_kind": by_kind,
        "heldout_accuracy_first": gated[0].heldout_before.get("accuracy") if gated else None,
        "heldout_accuracy_last": adopted_gated[-1].heldout_after.get("accuracy") if adopted_gated else None,
        "heldout_repeat_rate_first": gated[0].heldout_before.get("repeat_mistake_rate") if gated else None,
        "heldout_repeat_rate_last": adopted_gated[-1].heldout_after.get("repeat_mistake_rate") if adopted_gated else None,
        "wall_s": round(wall_s, 1),
    }


def _delta(b0, b1):
    return None if b0 is None or b1 is None else b1 - b0


def compare(phases: dict[str, dict | None]) -> dict:
    """results.json: per-phase metrics (as recorded by each phase) and the B0 vs B1 comparison."""
    m = {k: (v or {}).get("metrics") for k, v in phases.items()}
    out: dict[str, Any] = {
        "generated_at": _now(),
        "phases": {k: ({"label": LABELS[k], **v} if v else None) for k, v in m.items()},
        "missing_phases": [k for k, v in m.items() if not v],
    }
    a = m.get("a")
    if a:
        out["phases"]["a"]["changes_note"] = "adopted / rolled back by the held-out gate (routing applied directly when not in GATE_KINDS)"
    b0, b1 = m.get("b0"), m.get("b1")
    if b0 and b1:
        fp0 = {p["title"]: p["first_pass_verified_share"] for p in b0["per_subtask"]}
        fp1 = {p["title"]: p["first_pass_verified_share"] for p in b1["per_subtask"]}
        titles = [p["title"] for p in (b1["per_subtask"] if len(b1["per_subtask"]) >= len(b0["per_subtask"]) else b0["per_subtask"])]
        out["b0_vs_b1"] = {
            "subtasks_done": {"b0": b0["subtasks_done"], "b1": b1["subtasks_done"], "delta": b1["subtasks_done"] - b0["subtasks_done"]},
            "verified_share": {"b0": b0["verified_share"], "b1": b1["verified_share"], "delta": _delta(b0["verified_share"], b1["verified_share"])},
            "first_pass_verified_share": [
                {"title": t, "b0": fp0.get(t), "b1": fp1.get(t), "delta": _delta(fp0.get(t), fp1.get(t))} for t in titles
            ],
            "failed_runs": {"b0": b0["failed"], "b1": b1["failed"], "delta": b1["failed"] - b0["failed"]},
            "lessons_needed": {"b0": b0["lessons_created"], "b1": b1["lessons_created"], "delta": b1["lessons_created"] - b0["lessons_created"]},
            "repeat_mistake_rate": {"b0": b0["repeat_mistake_rate"], "b1": b1["repeat_mistake_rate"],
                                    "delta": _delta(b0["repeat_mistake_rate"], b1["repeat_mistake_rate"])},
            "lessons_in_play_b1": sum(p["lessons_in_play"] for p in b1["per_subtask"]),
            "lessons_carried_into_b1": b1["lessons_at_start_adopted"],
            "wall_s": {"b0": b0["wall_s"], "b1": b1["wall_s"]},
        }
        out["caveats"] = [
            "Task B is built from the same three slices Task A's held-out gate scores lessons on, so B1 vs B0 measures "
            "whether gate-selected lessons help a long task on those features, not transfer to unseen work.",
            "Small n: 3 subtasks x swarm-size runs per phase; a difference of one run is noise. Report counts next to shares.",
            "run_task stops at the first subtask that does not verify; compare subtasks_done and per-subtask shares only "
            "for subtasks both phases attempted.",
        ]
        b1_meta = (phases.get("b1") or {}).get("store_start") or {}
        if b1_meta.get("trust_carried"):
            out["caveats"].append("B1 also carries Task A's trust scores (vote weights, winner choice); rerun b1 with --b1-no-trust to isolate lessons.")
    return _jsonable(out)


def print_results(con, results: dict) -> None:
    from rich.table import Table

    def pct(x):
        return "-" if x is None else f"{100 * x:.0f}%"

    t = Table(title="demo recording", show_lines=False)
    for col in ("phase", "done", "verified/runs", "share", "failed", "lessons new", "repeat rate", "adopted/rolled back", "wall"):
        t.add_column(col)
    for k in AGENT_PHASES:
        m = results["phases"].get(k)
        if not m:
            t.add_row(LABELS[k], "missing", "", "", "", "", "", "", "")
            continue
        ch = f"{m['changes_adopted']}/{m['changes_rolled_back']}" if k == "a" else "-"
        t.add_row(LABELS[k], f"{m['subtasks_done']}/{m['subtasks_total']}", f"{m['verified']}/{m['runs']}", pct(m["verified_share"]),
                  str(m["failed"]), str(m["lessons_created"]), pct(m["repeat_mistake_rate"]), ch, f"{m['wall_s']:.0f}s")
    con.print(t)
    cmp_ = results.get("b0_vs_b1")
    if cmp_:
        t2 = Table(title="Task B: baseline vs with lessons (first-pass verified share)")
        for col in ("subtask", "b0", "b1", "delta"):
            t2.add_column(col)
        for row in cmp_["first_pass_verified_share"]:
            d = row["delta"]
            t2.add_row(row["title"], pct(row["b0"]), pct(row["b1"]), "-" if d is None else f"{100 * d:+.0f} pt")
        con.print(t2)
        vs = cmp_["verified_share"]
        con.print(f"verified share {pct(vs['b0'])} -> {pct(vs['b1'])}; failed runs {cmp_['failed_runs']['b0']} -> {cmp_['failed_runs']['b1']}; "
                  f"lessons needed {cmp_['lessons_needed']['b0']} -> {cmp_['lessons_needed']['b1']}; "
                  f"{cmp_['lessons_carried_into_b1']} adopted lessons carried, {cmp_['lessons_in_play_b1']} injected")
        for c in results.get("caveats", []):
            con.print(f"[dim]caveat: {c}")


# ---------- phases ----------


def run_phase(phase: str, args, con) -> dict:
    """Run one agent phase end to end; returns (and writes) its summary."""
    import core.orchestrator as orch
    from core.config import settings
    from core.decompose.decomposer import decompose, load_task
    from core.store.memory import MemoryStore
    from eval.heldout_specs import reset_repo, spec_from_hidden_tests

    snap, summ_path, log = out_paths(args, phase)
    base = base_commit(args)
    with tee_to(log):
        con.rule(f"[bold]record_demo phase {phase}: {LABELS[phase]}")
        con.print(f"command: python scripts/record_demo.py {' '.join(sys.argv[1:])}")
        con.print(f"started {_now()}; swarm size {args.swarm_size}, held-out swarm size {args.heldout_swarm_size}, "
                  f"n_fuzz {args.n_fuzz}, regression {' '.join(settings.regression_test_paths)}, gate kinds {','.join(settings.gate_kinds)}")
        pool, dropped = roster(args.agents, args.swarm_size)
        if not pool:
            raise RuntimeError(f"no swarm identity has a configured provider key (dropped: {dropped})")
        configure_knobs(args, pool)
        con.print("roster: " + ", ".join(f"{a.id} ({a.provider}/{a.model})" for a in pool)
                  + (f"; dropped (no key): {', '.join(dropped)}" if dropped else ""))

        task = decompose(load_task(TASK_A if phase == "a" else TASK_B))
        repo = Path(task.repo_path)
        reset = reset_repo(repo, base, keep_as=f"record-demo/pre-{phase}")
        con.print(f"reset {repo} to {base} on branch swarm (previous HEAD {reset['previous_head']}"
                  + (f" kept as {reset['kept_branch']}" if reset["kept_branch"] else "") + (", edits stashed" if reset["stashed"] else "") + ")")

        store_start: dict[str, Any] = {}
        if phase == "b1":
            from ui.server import load_snapshot

            src = lessons_snapshot(args)
            if not src.is_file():
                raise FileNotFoundError(f"b1 needs Task A's snapshot {src} (run --phase a first)")
            store = load_snapshot(src)
            if args.b1_no_trust:
                store.trust = {}
            store_start = {"snapshot": str(src), "trust_carried": not args.b1_no_trust}
        else:
            store = MemoryStore()
            store_start = {"snapshot": None, "trust_carried": False}
        marks = store_marks(store)
        store_start.update(lessons=len(marks["lesson_hits"]), lessons_adopted=marks["lessons_adopted"], regions=marks["regions"])
        if phase == "b1":
            con.print(f"store from {store_start['snapshot']}: {store_start['lessons_adopted']} adopted lessons "
                      f"(of {store_start['lessons']}), {store_start['regions']} failure regions, trust {'carried' if store_start['trust_carried'] else 'dropped'}")
            if not marks["lessons_adopted"]:
                con.print("[yellow]warning: no adopted lessons in the snapshot; b1 will match b0 except for trust")

        kw: dict[str, Any] = dict(swarm_size=args.swarm_size, n_fuzz=args.n_fuzz, snapshot_path=snap)
        if phase == "a":
            from eval.heldout_runner import make_heldout_runner

            hdir = mcp_heldout_dir(settings.runs_dir / "record_demo" / "heldout_mcp")
            kw["run_heldout"] = make_heldout_runner(store, heldout_dir=hdir, swarm_size=args.heldout_swarm_size)
            con.print(f"held-out gate: {', '.join(MCP_SLICES)} via {hdir}")
        else:
            kw.update(run_heldout=None, spec_fn=spec_from_hidden_tests)
            con.print("held-out gate: off; spec tests = the slices' verified hidden tests")

        t0, error, summary = time.time(), None, {"subtasks_done": 0, "stalled": [], "per_subtask": []}
        try:
            summary = orch.run_task(task, store, **kw)
        except Exception as e:  # keep what was recorded; the snapshot was already written by run_task
            error = f"{type(e).__name__}: {e}"
            con.print(f"[red]phase {phase} raised {error}")
            traceback.print_exc()
        wall = time.time() - t0
        from eval.heldout_specs import _git

        head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        _git(repo, "branch", "-f", f"record-demo/{phase}", "HEAD", check=False)
        record = {
            "phase": phase, "label": LABELS[phase], "task": task.title, "task_id": task.id,
            "started_at": datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="seconds"), "finished_at": _now(),
            "wall_s": round(wall, 1), "error": error, "reset": reset, "repo_head": head, "result_branch": f"record-demo/{phase}",
            "store_start": store_start, "snapshot": str(snap), "log": str(log),
            "args": {k: v for k, v in vars(args).items()},
            "settings": {"regression_test_paths": list(settings.regression_test_paths), "gate_kinds": list(settings.gate_kinds),
                         "agents": [a.id for a in pool]},
            **summary,
            "metrics": phase_metrics(summary, store, task, marks, wall),
        }
        summ_path.parent.mkdir(parents=True, exist_ok=True)
        summ_path.write_text(json.dumps(_jsonable(record), indent=1))
        m = record["metrics"]
        con.print(f"phase {phase}: {m['subtasks_done']}/{m['subtasks_total']} subtasks, {m['verified']}/{m['runs']} runs verified, "
                  f"{m['lessons_created']} lessons, {m['changes_adopted']} adopted / {m['changes_rolled_back']} rolled back, {wall:.0f}s")
        con.print(f"wrote {summ_path}, {snap}, {log}; repo result on branch record-demo/{phase} ({head})")
    return record


def run_compare(args, con) -> dict:
    phases = {}
    for k in AGENT_PHASES:
        p = out_paths(args, k)[1]
        phases[k] = json.loads(p.read_text()) if p.is_file() else None
    with tee_to(Path(args.out) / COMPARE_LOG):
        results = compare(phases)
        out = Path(args.out) / RESULTS
        out.write_text(json.dumps(results, indent=1))
        print_results(con, results)
        if results["missing_phases"]:
            con.print(f"[yellow]missing phases: {', '.join(results['missing_phases'])}")
        con.print(f"wrote {out}")
    return results


# ---------- dry run ----------


def dry_run(args, con) -> int:
    """Preconditions and the plan. Reads only: no API client is built, no repo is changed."""
    import json as _json

    from core.config import settings
    from core.decompose.decomposer import decompose, load_task, validate_decomposition
    from eval.heldout_runner import load_heldout
    from eval.heldout_specs import repo_state, spec_from_hidden_tests

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        con.print(f"[{'green' if ok else 'red'}]{'ok  ' if ok else 'FAIL'}[/] {name}" + (f": {detail}" if detail else ""))

    def warn(msg: str) -> None:
        con.print(f"[yellow]warn[/] {msg}")

    con.rule("[bold]record_demo dry run")
    # keys (presence only; values never printed)
    keys = {"GEMINI_API_KEY": settings.gemini_api_key, "META_API_KEY": settings.meta_api_key,
            "ANTHROPIC_API_KEY": settings.anthropic_api_key, "SUBQ_API_KEY": settings.subq_api_key}
    present = [k for k, v in keys.items() if v]
    check("Gemini key (spec author for Task A, lessons, embeddings)", bool(settings.gemini_api_key), "present" if settings.gemini_api_key else "missing GEMINI_API_KEY")
    if not settings.subq_api_key:
        warn("SUBQ_API_KEY missing: root cause falls back to the embedding-path heuristic")
    try:
        pool, dropped = roster(args.agents, args.swarm_size)
        fams = sorted({a.provider for a in pool})
        check("swarm roster", bool(pool), f"{', '.join(a.id for a in pool) or 'none'} (providers {', '.join(fams) or 'none'}; keys present: {', '.join(present) or 'none'})")
        if pool and len(pool) < args.swarm_size:
            warn(f"roster has {len(pool)} identities < swarm size {args.swarm_size}: each subtask runs {len(pool)} agents")
        if len(fams) < 2:
            warn("one provider family only: the router cannot keep two families for independence")
    except ValueError as e:
        check("swarm roster", False, str(e))
    want_reg = tuple(args.regression.replace(",", " ").split())
    if tuple(settings.regression_test_paths) != want_reg:
        warn(f"settings.regression_test_paths is {settings.regression_test_paths} (settings were loaded before this script set "
             f"REGRESSION_TEST_PATHS); a real run sets it to {want_reg} before import")

    # clone
    base = base_commit(args)
    st = repo_state(TASK_A.parent / "repo", base)
    ok = st["exists"] and st["own_git"] and st["has_base"]
    where = "at base" if st["at_base"] else (f"{st['ahead']} commits ahead of base" if st["ahead"] is not None else "not based on the base commit")
    check(f"clone {st['repo']} contains {base}", ok,
          (f"HEAD {st['head']} ({where}); every agent phase resets it to {base} and keeps the old HEAD on a branch"
           if ok else "missing or not a clone with the base commit: run bash demo_task/mcp/setup.sh"))
    if st["dirty"]:
        warn(f"clone has {len(st['dirty'])} uncommitted/untracked paths; reset would stash them: {st['dirty'][:5]}")
    venv_py = TASK_A.parent / "repo" / ".venv" / "bin" / "python"
    check("clone .venv (verdict interpreter)", venv_py.exists(), str(venv_py) if venv_py.exists() else "missing: run bash demo_task/mcp/setup.sh")

    # tasks
    try:
        ta = decompose(load_task(TASK_A))
        check("Task A loads offline", len(ta.subtasks) == 7, f"{len(ta.subtasks)} subtasks from {TASK_A.parent / 'subtasks.json'}")
    except Exception as e:
        check("Task A loads offline", False, f"{type(e).__name__}: {e}")
        ta = None
    try:
        validate_decomposition(_json.loads((TASK_B.parent / "subtasks.json").read_text()))
        tb = decompose(load_task(TASK_B))
        same_repo = ta is None or Path(tb.repo_path) == Path(ta.repo_path)
        check("Task B validates", len(tb.subtasks) == 3 and same_repo,
              f"{' -> '.join(s.title for s in tb.subtasks)}; repo {tb.repo_path}" + ("" if same_repo else " (NOT Task A's clone)"))
        for s in tb.subtasks:
            spec = spec_from_hidden_tests(s, Path(tb.repo_path))
            check(f"Task B spec tests for {s.title!r}", bool(spec.test_node_ids),
                  f"{len(spec.test_node_ids)} hidden tests from {', '.join(spec.modules)} (all hidden, {spec.author_id})")
    except Exception as e:
        check("Task B validates", False, f"{type(e).__name__}: {e}")

    # held-out slices (Task A's gate), in a throwaway dir of symlinks
    with tempfile.TemporaryDirectory(prefix="record_demo_heldout_") as td:
        items = load_heldout(mcp_heldout_dir(Path(td)))
        names = [i.name for i in items]
        bad = [i.name for i in items if i.kind != "patch" or not (i.base_repo and i.base_repo.is_dir()) or i.base_commit is None]
        detail = ", ".join(f"{i.name} ({i.kind}, base {i.base_commit})" for i in items) or f"none under {HELDOUT_ROOT}"
        check("held-out slices load", sorted(names) == sorted(MCP_SLICES) and not bad, detail + (f"; unusable: {bad}" if bad else ""))
        if args.check_references and not bad and ok:
            from eval.heldout_runner import check_reference

            for i in items:
                try:
                    r = check_reference(i)
                    check(f"reference passes hidden tests: {i.name}", r.passed, f"{r.tests} tests")
                except Exception as e:
                    check(f"reference passes hidden tests: {i.name}", False, f"{type(e).__name__}: {str(e)[:300]}")

    # outputs
    out = Path(args.out)
    existing = [str(out / f) for ph in AGENT_PHASES for f in FILES[ph] if (out / f).exists()]
    if (out / RESULTS).exists():
        existing.append(str(out / RESULTS))
    if existing:
        warn(f"will overwrite (or, with --phase all and no --redo, skip phases whose summary exists): {', '.join(existing)}")
    snap = lessons_snapshot(args)
    if args.phase == "b1" and not snap.is_file():
        check("b1 lessons snapshot", False, f"{snap} not found (run --phase a first)")

    # plan
    S, H = args.swarm_size, args.heldout_swarm_size
    phases = list(PHASES) if args.phase == "all" else [args.phase]
    con.rule("plan")
    plan = {
        "a": f"Task A, 7 subtasks x {S} agents (<= {7 * S} runs) + held-out gate: each gated lesson costs <= 3 slices x {H} runs "
             f"(with) + 3 x {H} baseline per distinct lesson set -> {out / FILES['a'][0]}, {out / FILES['a'][1]}, {out / FILES['a'][2]}",
        "b0": f"Task B, fresh store, 3 subtasks x {S} agents (<= {3 * S} runs), no gate -> {', '.join(str(out / f) for f in FILES['b0'])}",
        "b1": f"Task B, store from {snap}{' (trust dropped)' if args.b1_no_trust else ''}, 3 x {S} agents (<= {3 * S} runs) -> "
              f"{', '.join(str(out / f) for f in FILES['b1'])}",
        "compare": f"{out / RESULTS} from the three summaries (no API calls)",
    }
    for ph in phases:
        con.print(f"  {ph:8s} {plan[ph]}")
    con.print(f"  budgets: agent timeout {args.agent_timeout}s, {args.max_turns} turns, {args.token_budget:,} tokens per agent; n_fuzz {args.n_fuzz}")
    failed = [c for c in checks if not c[1]]
    con.print(("[green]preconditions ok" if not failed else f"[red]{len(failed)} precondition(s) failed")
              + f"; real run: python scripts/record_demo.py --swarm-size {S} --heldout-swarm-size {H} --out {args.out} --phase {args.phase}")
    return 0 if not failed else 1


# ---------- main ----------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # settings are read at import time: set the knobs first (as scripts/real_run.py does)
    os.environ["AGENT_TIMEOUT_S"] = str(args.agent_timeout)
    os.environ["REGRESSION_TEST_PATHS"] = args.regression
    os.environ["HELDOUT_SWARM_SIZE"] = str(args.heldout_swarm_size)
    os.environ.setdefault("USE_MEMORY_STORE", "1")

    from rich.console import Console

    con = Console()
    if args.dry_run:
        return dry_run(args, con)

    phases = list(PHASES) if args.phase == "all" else [args.phase]
    Path(args.out).mkdir(parents=True, exist_ok=True)
    rc = 0
    for ph in phases:
        if ph == "compare":
            run_compare(args, con)
            continue
        summ = out_paths(args, ph)[1]
        if args.phase == "all" and summ.is_file() and not args.redo:
            con.print(f"[dim]skipping phase {ph}: {summ} exists (--redo to rerun)")
            continue
        rec = run_phase(ph, args, con)
        if rec.get("error"):
            rc = 1
            con.print(f"[red]phase {ph} failed ({rec['error']}); later phases not run. Resume with --phase {ph}.")
            break
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
