"""Run every implementation on every case in an isolated subprocess and decide the verdict. Owner: Ethan Li.

Test independence: verdict inputs are (1) spec tests and properties from `spec_tests.py`,
written by the SubQ verifier from spec + interface + base repo only, (2) fuzz cases from the
type hints. Agents' own tests are never used. See outline §5.5.

Verdict per run:
  - misses a HIDDEN spec case (outcome != the spec author's expected value)  -> failed
  - violates a spec property on any case whose call returned normally        -> failed
  - visible spec misses and fuzz-majority disagreements are evidence only; they go to the
    reflector as the breaking case when nothing stronger exists
  - spec author vs swarm majority disagree on a spec case -> AmbiguityFlag, nobody is charged
  - no spec tests at all -> legacy fuzz-majority verdict (every run outside the majority fails)

Each (run, case) executes `python -I -c <exec> <impl_dir> <file> <func> <args_json>`
with a timeout, so a hanging or crashing implementation can't take the harness down.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.harness.equivalence import equivalent
from core.harness.majority import trust_weighted_majority
from core.harness.spec_tests import SpecTests, check_property, load_cached
from core.harness.strategies import generate_cases
from core.models import (  # noqa: F401 — MajorityVote resolves CrossTestResult's forward ref
    AgentRun,
    CrossTestResult,
    FunctionSig,
    MajorityVote,
    Outcome,
    Subtask,
    TestCase,
)

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


class AmbiguityFlag(BaseModel):
    """The spec author and the swarm majority disagree on a spec case. Sent to the reflector and
    the decomposer; never charged to an agent."""

    case_id: str
    function: str
    args: list[Any]
    expected: Any
    majority: Outcome
    agreement: float


class VerdictResult(CrossTestResult):
    """A CrossTestResult (so the orchestrator and held-out runner are unchanged) plus the verdict evidence."""

    spec_misses: dict[str, list[str]] = {}  # run_id -> hidden spec case ids the run got wrong
    visible_misses: dict[str, list[str]] = {}  # run_id -> visible spec case ids the run got wrong (evidence)
    property_violations: dict[str, list[list[str]]] = {}  # run_id -> [[case_id, property_name], ...]
    fuzz_disagreements: dict[str, list[str]] = {}  # run_id -> fuzz/extra case ids outside the majority (evidence)
    ambiguity_flags: list[AmbiguityFlag] = []
    expected_outcomes: dict[str, Outcome] = {}  # spec case_id -> the outcome the spec author expects


VerdictResult.model_rebuild()


def expected_outcome(expected: Any) -> Outcome:
    """`{"raises": "ValueError"}` -> exception outcome; anything else is a return value."""
    if isinstance(expected, dict) and set(expected) == {"raises"}:
        return Outcome(ok=False, exception=str(expected["raises"]))
    return Outcome(ok=True, value=expected)


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


def run_cases(subtask: Subtask, impl_dirs: dict[str, str], cases: list[TestCase]) -> dict[str, dict[str, Outcome]]:
    """outcomes[run_id][case_id] for every (impl, case), in parallel subprocesses."""
    sig_by_name = {s.name: s for s in subtask.interface}
    outcomes: dict[str, dict[str, Outcome]] = {rid: {} for rid in impl_dirs}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {(rid, c.id): ex.submit(_call, path, sig_by_name[c.function], c) for rid, path in impl_dirs.items() for c in cases}
        for (rid, cid), f in futs.items():
            outcomes[rid][cid] = f.result()
    return outcomes


def cross_test(
    subtask: Subtask,
    runs: list[AgentRun],
    weights: dict[str, float],
    extra_cases: list[TestCase] | None = None,
    n_fuzz: int = 200,
    spec_cases: list[TestCase] | None = None,
    spec: SpecTests | None = None,
) -> VerdictResult:
    """`spec` is the SubQ-authored SpecTests (preferred). `spec_cases` alone (the orchestrator's current
    call) recovers the expectations from the per-subtask cache."""
    if spec is None and spec_cases:
        spec = load_cached(subtask)
    sig_by_name = {s.name: s for s in subtask.interface}
    spec_case_list = [c for c in (spec.cases if spec else (spec_cases or [])) if c.function in sig_by_name]
    cases: list[TestCase] = spec_case_list + [c for c in (extra_cases or []) if c.function in sig_by_name]
    for sig in subtask.interface:
        cases += generate_cases(sig, n_fuzz=n_fuzz, literals=(spec.literals.get(sig.name) if spec else None))

    runs = [r for r in runs if r.error is None and Path(r.sandbox_path).exists()]
    outcomes = run_cases(subtask, {r.id: r.sandbox_path for r in runs}, cases)
    majority = {c.id: trust_weighted_majority(c.id, {r.id: outcomes[r.id][c.id] for r in runs}, weights) for c in cases}

    spec_misses: dict[str, list[str]] = {r.id: [] for r in runs}
    visible_misses: dict[str, list[str]] = {r.id: [] for r in runs}
    prop_viol: dict[str, list[list[str]]] = {r.id: [] for r in runs}
    fuzz_dis: dict[str, list[str]] = {r.id: [] for r in runs}
    flags: list[AmbiguityFlag] = []
    expected: dict[str, Outcome] = {}

    if spec is not None and runs:
        expected = {cid: expected_outcome(e) for cid, e in spec.expected.items() if cid in {c.id for c in spec_case_list}}
        hidden = {c.id for c in spec.hidden}
        # two oracles: when the author and the swarm disagree, flag the case instead of charging anyone
        flagged: set[str] = set()
        for c in spec_case_list:
            exp = expected.get(c.id)
            if exp is None:
                continue
            mv = majority[c.id]
            if not equivalent(mv.representative, exp):
                flagged.add(c.id)
                flags.append(AmbiguityFlag(case_id=c.id, function=c.function, args=c.args, expected=spec.expected[c.id], majority=mv.representative, agreement=mv.agreement))
        props_by_fn: dict[str, list] = defaultdict(list)
        for p in spec.properties:
            props_by_fn[p.function].append(p)
        for r in runs:
            for c in cases:
                o = outcomes[r.id][c.id]
                exp = expected.get(c.id)
                if exp is not None:
                    if c.id not in flagged and not equivalent(o, exp):
                        (spec_misses if c.id in hidden else visible_misses)[r.id].append(c.id)
                elif r.id not in majority[c.id].group_run_ids:
                    fuzz_dis[r.id].append(c.id)
                if o.ok:
                    for p in props_by_fn.get(c.function, []):
                        if not check_property(p, c.args, o.value):
                            prop_viol[r.id].append([c.id, p.name])
        failed = sorted(r.id for r in runs if spec_misses[r.id] or prop_viol[r.id])
    else:
        # legacy: no independent tests, so the fuzz majority is the only oracle
        failed = sorted({r.id for r in runs for c in cases if r.id not in majority[c.id].group_run_ids})
        for r in runs:
            fuzz_dis[r.id] = [c.id for c in cases if r.id not in majority[c.id].group_run_ids]

    verified = [r.id for r in runs if r.id not in failed]
    return VerdictResult(
        subtask_id=subtask.id, cases=cases, outcomes=outcomes, majority=majority, failed_runs=failed, verified_runs=verified,
        spec_misses=spec_misses, visible_misses=visible_misses, property_violations=prop_viol, fuzz_disagreements=fuzz_dis,
        ambiguity_flags=flags, expected_outcomes=expected,
    )


def breaking_case(result: CrossTestResult, run_id: str) -> tuple[TestCase, Outcome] | None:
    """The strongest evidence against `run_id`: a hidden spec miss (paired with the expected outcome),
    else a property violation, else a visible miss, else the first fuzz disagreement (paired with the majority)."""
    by_id = {c.id: c for c in result.cases}
    if isinstance(result, VerdictResult):
        for cid in result.spec_misses.get(run_id, []):
            return by_id[cid], result.expected_outcomes[cid]
        for cid, _prop in result.property_violations.get(run_id, []):
            return by_id[cid], result.majority[cid].representative
        for cid in result.visible_misses.get(run_id, []):
            return by_id[cid], result.expected_outcomes[cid]
        for cid in result.fuzz_disagreements.get(run_id, []):
            return by_id[cid], result.majority[cid].representative
        return None
    for c in result.cases:
        mv = result.majority[c.id]
        if run_id not in mv.group_run_ids:
            return c, mv.representative
    return None


def run_outcome(result: CrossTestResult, run_id: str, case_id: str) -> Outcome | None:
    """What `run_id` actually returned on `case_id` (the reflector needs it next to the expected value)."""
    return result.outcomes.get(run_id, {}).get(case_id)


# Agents' own tests are deliberately NOT run against other implementations: they are derived
# from the agent's code and would share its misunderstanding. Verdict = hidden spec tests +
# properties (see spec_tests.py); fuzz majority is secondary evidence only.
