"""Run every implementation on every case in an isolated subprocess. Owner: Ethan Li.

Test independence: verdict inputs are (1) spec tests and properties from `spec_tests.py`,
written before the swarm from spec + interface + base repo only, (2) fuzz cases from the
type hints. Agents' own tests are tie-breaker evidence only. See outline §5.5.

Each (run, case) executes `python -I -c <exec> <impl_dir> <file> <func> <args_json>`
with a timeout, so a hanging or crashing implementation can't take the harness down.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.harness.majority import trust_weighted_majority
from core.harness.strategies import generate_cases
from core.models import AgentRun, CrossTestResult, FunctionSig, Outcome, Subtask, TestCase

_EXEC = r"""
import importlib.util, json, sys
impl_dir, file, func, args_json = sys.argv[1:5]
sys.path.insert(0, impl_dir)
spec = importlib.util.spec_from_file_location("m", f"{impl_dir}/{file}")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
    fn = getattr(m, func)
    a = json.loads(args_json)
    v = fn(*a["args"], **a.get("kwargs", {}))
    try:
        json.dumps(v)
    except TypeError:
        v = repr(v)
    print(json.dumps({"ok": True, "value": v}))
except Exception as e:
    print(json.dumps({"ok": False, "exception": type(e).__name__}))
"""


def _call(impl_dir: str, sig: FunctionSig, case: TestCase, timeout: float = 5.0) -> Outcome:
    payload = json.dumps({"args": case.args, "kwargs": case.kwargs}, default=str)
    try:
        r = subprocess.run(
            [sys.executable, "-I", "-c", _EXEC, impl_dir, sig.file, sig.name, payload],
            capture_output=True, text=True, timeout=timeout,
        )
        line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if not line:
            return Outcome(ok=False, exception="ImportOrCrash")
        d = json.loads(line)
        return Outcome(ok=d["ok"], value=d.get("value"), exception=d.get("exception"))
    except subprocess.TimeoutExpired:
        return Outcome(ok=False, timed_out=True)


def cross_test(subtask: Subtask, runs: list[AgentRun], weights: dict[str, float], extra_cases: list[TestCase] | None = None, n_fuzz: int = 200, spec_cases: list[TestCase] | None = None) -> CrossTestResult:
    # spec_cases come from spec_tests.load_or_author(...) and are the highest-weight input.
    # TODO(Ethan Li): compare spec-case outcomes to `expected` and check properties (see spec_tests.py).
    cases: list[TestCase] = list(spec_cases or []) + list(extra_cases or [])
    sig_by_name = {s.name: s for s in subtask.interface}
    for sig in subtask.interface:
        cases += generate_cases(sig, n_fuzz=n_fuzz)

    runs = [r for r in runs if r.error is None and Path(r.sandbox_path).exists()]
    outcomes: dict[str, dict[str, Outcome]] = {r.id: {} for r in runs}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {}
        for r in runs:
            for c in cases:
                futs[(r.id, c.id)] = ex.submit(_call, r.sandbox_path, sig_by_name[c.function], c)
        for (rid, cid), f in futs.items():
            outcomes[rid][cid] = f.result()

    majority = {c.id: trust_weighted_majority(c.id, {r.id: outcomes[r.id][c.id] for r in runs}, weights) for c in cases}
    failed = sorted({r.id for r in runs for c in cases if r.id not in majority[c.id].group_run_ids})
    verified = [r.id for r in runs if r.id not in failed]
    return CrossTestResult(subtask_id=subtask.id, cases=cases, outcomes=outcomes, majority=majority, failed_runs=failed, verified_runs=verified)


def breaking_case(result: CrossTestResult, run_id: str) -> tuple[TestCase, Outcome] | None:
    """The first case where `run_id` left the majority, with the majority's outcome."""
    for c in result.cases:
        mv = result.majority[c.id]
        if run_id not in mv.group_run_ids:
            return c, mv.representative
    return None


# TODO(C): cross-test matrix — run each run's tests/ dir against every other run's sandbox
# via `python -I -m pytest <tests_dir> --rootdir <impl_dir>`; a test that passes on most
# verified impls but fails on a given impl is extra evidence for the reflector.
