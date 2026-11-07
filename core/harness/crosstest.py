"""Run every implementation against the independent tests and decide the verdict. Owner: Ethan Li.

Test independence: verdict inputs are (1) spec tests from `spec_tests.py`, written by the SubQ
verifier from the initial spec + pinned interface + base repo only, (2) the repository's own
test suite at the base commit, (3) fuzz cases from the type hints. Agents' own tests are never
used. See outline §5.5.

Two oracles, both may apply to one subtask:
  - real repo: hidden pytest modules copied into each sandbox under a temp dir and run with the
    repo's own suite (`python -I -m pytest <hidden> <repo tests> --junitxml`), then removed.
    Every pytest test becomes a synthetic TestCase (source "pytest" or "regression") whose
    outcome is "passed" or the failure message, so the rest of the verdict is uniform.
  - pure functions: every implementation on every spec/fuzz case in an isolated subprocess.

Verdict per run:
  - misses a HIDDEN spec case or hidden spec test (outcome != what the spec author expects) -> failed
  - breaks a test of the repo's existing suite                                             -> failed
  - violates a spec property on any case whose call returned normally                      -> failed
  - visible spec misses and fuzz-majority disagreements are evidence only; they go to the
    reflector as the breaking case when nothing stronger exists
  - spec author vs swarm majority disagree on a spec case/test -> AmbiguityFlag, nobody is charged
    (regression tests are exempt: breaking existing behavior is always charged)
  - no spec tests at all -> legacy fuzz-majority verdict (every run outside the majority fails)
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.harness.equivalence import equivalent
from core.harness.majority import trust_weighted_majority
from core.harness.spec_tests import SpecTests, check_property, load_cached, spec_node_ids
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

PYTEST_TIMEOUT_S = 180
PASSED = Outcome(ok=True, value="passed")

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

    spec_misses: dict[str, list[str]] = {}  # run_id -> hidden spec case/test ids the run got wrong
    visible_misses: dict[str, list[str]] = {}  # run_id -> visible spec case/test ids the run got wrong (evidence)
    regression_failures: dict[str, list[str]] = {}  # run_id -> repo-suite test ids the run broke
    property_violations: dict[str, list[list[str]]] = {}  # run_id -> [[case_id, property_name], ...]
    fuzz_disagreements: dict[str, list[str]] = {}  # run_id -> fuzz/extra case ids outside the majority (evidence)
    ambiguity_flags: list[AmbiguityFlag] = []
    expected_outcomes: dict[str, Outcome] = {}  # spec case_id -> the outcome the spec author expects
    nodeid_by_case: dict[str, str] = {}  # synthetic pytest case id -> "<file>::<test>"


VerdictResult.model_rebuild()


def expected_outcome(expected: Any) -> Outcome:
    """`{"raises": "ValueError"}` -> exception outcome; anything else is a return value."""
    if isinstance(expected, dict) and set(expected) == {"raises"}:
        return Outcome(ok=False, exception=str(expected["raises"]))
    return Outcome(ok=True, value=expected)


# --- pure-function oracle: one subprocess per (impl, case) ---


def _call(impl_dir: str, sig: FunctionSig, case: TestCase, timeout: float = 5.0) -> Outcome:
    payload = json.dumps({"args": case.args, "kwargs": case.kwargs}, default=str)
    try:
        r = subprocess.run(
            [sys.executable, "-I", "-c", _EXEC, impl_dir, sig.file, sig.name, payload],
            capture_output=True, text=True, timeout=timeout, check=False,
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


# --- real-repo oracle: hidden pytest modules + the repo's own suite, per sandbox ---


def repo_test_files(repo: Path | None) -> list[str]:
    """The repository's own test files at the base commit, repo-relative. Agents' new test files are not in it."""
    if repo is None or not (repo / "tests").exists():
        return []
    return sorted(str(p.relative_to(repo)) for p in (repo / "tests").rglob("test_*.py"))


def run_pytest(sandbox: str, modules: dict[str, str], repo_tests: list[str], timeout: float = PYTEST_TIMEOUT_S) -> dict[str, tuple[bool, str]]:
    """Copy the hidden modules into the sandbox under a temp dir, run them plus the repo suite with a junit
    report, remove them, and return {node_id: (passed, message)}. Node ids: "<module filename>::<test>" for
    spec modules, "<repo path>::<test>" for the repo suite. Tests that never ran (collection error, timeout)
    are reported as failed with the reason."""
    sb = Path(sandbox)
    tmp = sb / f"_spec_{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    written: dict[str, str] = {}  # stem on disk -> original filename
    try:
        for i, (fname, src) in enumerate(sorted(modules.items())):
            stem = f"test_spec{i}_{Path(fname).stem.removeprefix('test_')}"
            (tmp / f"{stem}.py").write_text(src)
            written[stem] = fname
        paths = ([str(tmp.relative_to(sb))] if modules else []) + [t for t in repo_tests if (sb / t).exists()]
        if not paths:
            return {}
        xml = tmp / "junit.xml"
        cmd = [
            sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
            "-o", "addopts=", "-o", "pythonpath=.", f"--junitxml={xml}", *paths,
        ]
        try:
            subprocess.run(cmd, cwd=sb, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {t: (False, "timeout: pytest run exceeded the limit") for t in _expected_node_ids(modules, repo_tests, sb)}
        results = _parse_junit(xml, tmp.name, written) if xml.exists() else {}
        for t in _expected_node_ids(modules, repo_tests, sb):
            results.setdefault(t, (False, _collection_error(results, t) or "not collected"))
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _expected_node_ids(modules: dict[str, str], repo_tests: list[str], sb: Path) -> list[str]:
    ids = list(spec_node_ids(modules))
    for t in repo_tests:
        p = sb / t
        if p.exists():
            ids += [f"{t}::{n}" for n in _test_names(p.read_text())]
    return ids


def _test_names(src: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]


def _collection_error(results: dict[str, tuple[bool, str]], node_id: str) -> str | None:
    """A module that failed to collect reports one error for the whole file; attribute it to each of its tests."""
    file = node_id.split("::", 1)[0]
    for k, (ok, msg) in results.items():
        if not ok and "::" not in k and k == file:
            return f"collection error: {msg}"
    return None


def _parse_junit(xml: Path, tmp_name: str, written: dict[str, str]) -> dict[str, tuple[bool, str]]:
    out: dict[str, tuple[bool, str]] = {}
    root = ET.parse(xml).getroot()
    for tc in root.iter("testcase"):
        classname, name = tc.get("classname", ""), tc.get("name", "")
        parts = classname.split(".") if classname else []
        if parts and parts[0] == tmp_name and len(parts) >= 2:
            file = written.get(parts[1], parts[1] + ".py")
        elif parts:
            file = "/".join(parts) + ".py"
        else:  # collection error: classname is empty and `name` is the dotted module path
            dotted = name.split(".")
            if dotted[0] == tmp_name and len(dotted) >= 2:
                file = written.get(dotted[1], dotted[1] + ".py")
            else:
                file = "/".join(dotted) + ".py"
            name = ""
        fail, err, skipped = tc.find("failure"), tc.find("error"), tc.find("skipped")
        node = f"{file}::{name}" if name else file
        if fail is not None or err is not None:
            el = fail if fail is not None else err
            text = (el.text or "").strip()
            last = text.splitlines()[-1].strip().removeprefix("E").strip() if text else ""
            if fail is not None:  # the assertion is in `message`
                kind = "AssertionError" if "assert" in text else (el.get("type") or "Failure")
                msg = f"{kind}: {el.get('message') or last or 'failed'}"
            else:  # error: the real cause is the traceback's last line, which already names the exception
                msg = last or el.get("message") or "error"
            out[node] = (False, msg[:400])
        else:
            out[node] = (True, "skipped" if skipped is not None else "passed")
    return out


def _pytest_case(node_id: str, source: str) -> TestCase:
    """A synthetic TestCase for one pytest test so the verdict and the reflector treat it like any other case."""
    cid = "pt_" + hashlib.sha256(node_id.encode()).hexdigest()[:10]
    return TestCase(id=cid, function=node_id.split("::")[-1], args=[node_id], source=source)


# --- verdict ---


def cross_test(
    subtask: Subtask,
    runs: list[AgentRun],
    weights: dict[str, float],
    extra_cases: list[TestCase] | None = None,
    n_fuzz: int = 200,
    spec_cases: list[TestCase] | None = None,
    spec: SpecTests | None = None,
    repo: Path | None = None,
) -> VerdictResult:
    """`spec` is the SubQ-authored SpecTests (preferred). `spec_cases` alone (the orchestrator's current
    call) recovers the expectations from the per-subtask cache. `repo` is the base repo; its own tests
    at `tests/` are the regression suite run in every sandbox."""
    if spec is None and spec_cases:
        spec = load_cached(subtask)
    sig_by_name = {s.name: s for s in subtask.interface}
    spec_case_list = [c for c in (spec.cases if spec else (spec_cases or [])) if c.function in sig_by_name]
    cases: list[TestCase] = spec_case_list + [c for c in (extra_cases or []) if c.function in sig_by_name]
    for sig in subtask.interface:
        cases += generate_cases(sig, n_fuzz=n_fuzz, literals=(spec.literals.get(sig.name) if spec else None))

    runs = [r for r in runs if r.error is None and Path(r.sandbox_path).exists()]
    outcomes = run_cases(subtask, {r.id: r.sandbox_path for r in runs}, cases)

    # pytest oracle: hidden spec modules + repo suite -> synthetic cases
    expected: dict[str, Outcome] = {}
    hidden: set[str] = set()
    regression_ids: set[str] = set()
    nodeid_by_case: dict[str, str] = {}
    modules = spec.modules if spec else {}
    repo_tests = repo_test_files(repo)
    if runs and (modules or repo_tests):
        with ThreadPoolExecutor(max_workers=4) as ex:
            per_run = dict(zip([r.id for r in runs], ex.map(lambda r: run_pytest(r.sandbox_path, modules, repo_tests), runs)))
        spec_test_ids = set(spec_node_ids(modules))
        all_nodes = sorted({n for res in per_run.values() for n in res} | spec_test_ids)
        visible = set(spec.visible_ids) if spec else set()
        for node in all_nodes:
            is_spec = node in spec_test_ids
            if not is_spec and "::" not in node:
                continue  # a collection-error pseudo-entry; its tests are already attributed
            tc = _pytest_case(node, "pytest" if is_spec else "regression")
            cases.append(tc)
            nodeid_by_case[tc.id] = node
            expected[tc.id] = PASSED
            if is_spec:
                if node not in visible:
                    hidden.add(tc.id)
            else:
                regression_ids.add(tc.id)
            for r in runs:
                ok, msg = per_run[r.id].get(node, (False, "not collected"))
                outcomes[r.id][tc.id] = PASSED if ok else Outcome(ok=False, exception=msg)

    majority = {c.id: trust_weighted_majority(c.id, {r.id: outcomes[r.id][c.id] for r in runs}, weights) for c in cases}

    spec_misses: dict[str, list[str]] = {r.id: [] for r in runs}
    visible_misses: dict[str, list[str]] = {r.id: [] for r in runs}
    regression: dict[str, list[str]] = {r.id: [] for r in runs}
    prop_viol: dict[str, list[list[str]]] = {r.id: [] for r in runs}
    fuzz_dis: dict[str, list[str]] = {r.id: [] for r in runs}
    flags: list[AmbiguityFlag] = []

    if spec is not None and runs:
        spec_ids = {c.id for c in spec_case_list}
        expected.update({cid: expected_outcome(e) for cid, e in spec.expected.items() if cid in spec_ids})
        hidden |= {c.id for c in spec.hidden}
        by_id = {c.id: c for c in cases}
        # two oracles: when the author and the swarm disagree, flag the case instead of charging anyone
        flagged: set[str] = set()
        for cid, exp in expected.items():
            if cid in regression_ids:
                continue
            mv = majority[cid]
            # only a real majority (> half the trust weight) can overrule the author; a tie or a lone
            # dissenter is charged as a miss
            if mv.agreement > 0.5 and not equivalent(mv.representative, exp):
                flagged.add(cid)
                c = by_id[cid]
                raw = spec.expected.get(cid, "passed")
                flags.append(AmbiguityFlag(case_id=cid, function=c.function, args=c.args, expected=raw, majority=mv.representative, agreement=mv.agreement))
        props_by_fn: dict[str, list] = defaultdict(list)
        for p in spec.properties:
            props_by_fn[p.function].append(p)
        for r in runs:
            for c in cases:
                o = outcomes[r.id][c.id]
                exp = expected.get(c.id)
                if exp is not None:
                    if c.id in regression_ids:
                        if not equivalent(o, exp):
                            regression[r.id].append(c.id)
                    elif c.id not in flagged and not equivalent(o, exp):
                        (spec_misses if c.id in hidden else visible_misses)[r.id].append(c.id)
                elif r.id not in majority[c.id].group_run_ids:
                    fuzz_dis[r.id].append(c.id)
                if o.ok and c.source in ("spec", "seed", "fuzz"):
                    for p in props_by_fn.get(c.function, []):
                        if not check_property(p, c.args, o.value):
                            prop_viol[r.id].append([c.id, p.name])
        failed = sorted(r.id for r in runs if spec_misses[r.id] or regression[r.id] or prop_viol[r.id])
    elif runs and regression_ids:
        # no spec tests but a repo suite: regressions still fail a run; fuzz majority decides the rest
        for r in runs:
            regression[r.id] = [cid for cid in regression_ids if not equivalent(outcomes[r.id][cid], PASSED)]
            fuzz_dis[r.id] = [c.id for c in cases if c.id not in regression_ids and r.id not in majority[c.id].group_run_ids]
        failed = sorted(r.id for r in runs if regression[r.id] or fuzz_dis[r.id])
    else:
        # legacy: no independent tests, so the fuzz majority is the only oracle
        failed = sorted({r.id for r in runs for c in cases if r.id not in majority[c.id].group_run_ids})
        for r in runs:
            fuzz_dis[r.id] = [c.id for c in cases if r.id not in majority[c.id].group_run_ids]

    verified = [r.id for r in runs if r.id not in failed]
    return VerdictResult(
        subtask_id=subtask.id, cases=cases, outcomes=outcomes, majority=majority, failed_runs=failed, verified_runs=verified,
        spec_misses=spec_misses, visible_misses=visible_misses, regression_failures=regression, property_violations=prop_viol,
        fuzz_disagreements=fuzz_dis, ambiguity_flags=flags, expected_outcomes=expected, nodeid_by_case=nodeid_by_case,
    )


def breaking_case(result: CrossTestResult, run_id: str) -> tuple[TestCase, Outcome] | None:
    """The strongest evidence against `run_id`: a hidden spec miss or failed hidden spec test (paired with the
    expected outcome), else a broken repo test, else a property violation, else a visible miss, else the first
    fuzz disagreement (paired with the majority)."""
    by_id = {c.id: c for c in result.cases}
    if isinstance(result, VerdictResult):
        for cid in result.spec_misses.get(run_id, []):
            return by_id[cid], result.expected_outcomes[cid]
        for cid in result.regression_failures.get(run_id, []):
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
# repo suite + properties (see spec_tests.py); fuzz majority is secondary evidence only.
