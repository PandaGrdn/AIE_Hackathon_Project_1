"""Deterministic, network-free stand-ins for every LLM-dependent piece of the loop.

Used by the offline test and as the demo fallback (`swarm run TASK --fake`):

  - fake_spec_tests(subtask, repo, reference_dir=None) -> SpecTests
        expected values come from a reference implementation (never from a swarm run)
  - fake_swarm(reference_dir=None, ...) -> async fn with run_swarm's signature
        most runs are faithful copies of the reference, the last `n_mutants` carry a boundary bug
        made by a deterministic operator substitution, with a matching off-consensus reasoning step
  - fake_reflect(spec, failed, div, store) -> Lesson
        rule text derived from the divergence step, stored through dedup_or_insert (creates regions)
  - fake_decompose(task, reference_dir) -> LongTask
        one subtask per reference module, interface pinned with ast

Reference lookup: an explicit `reference_dir`, else `<repo>/../reference/`. A reference dir mirrors
the repo layout, i.e. it holds the subtask's files at their repo-relative paths.

Mutants "heed" lessons: if the mutant's divergent step lands in a failure region whose lesson is
active for this run (in `lesson_ids`), or an active lesson states the catalog rule for its mutation
(the text fake_reflect writes for it), it takes the consensus assumption instead and writes the
faithful implementation. That is what makes the held-out gate's with/without comparison move.

  - fake_patch_swarm(patches) / fake_patch_spec_tests / fake_heldout_swarm
        the same for a task whose reference is a patch series (demo_task/mcp/reference/NN-*.patch) and
        for the held-out gate's patch-kind slices; see the "patch series" section below
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from core.config import settings
from core.harness.crosstest import _call
from core.harness.spec_tests import SpecTests, _split_visible
from core.harness.strategies import generate_cases
from core.llm import embeddings as _emb
from core.models import AgentIdentity, AgentRun, Divergence, Lesson, LongTask, ReasoningStep, Subtask, TestCase
from core.reflect.dedup import dedup_or_insert
from core.store.base import Store

console = Console()

# ---------- mutation catalog ----------


@dataclass(frozen=True)
class Mutation:
    op: str
    replacement: str
    assumption: str  # the off-consensus reasoning that "explains" the bug
    lesson: str  # the rule that resolves it


CONSENSUS_ASSUMPTION = "boundary wording in the spec is literal: 'over N'/'under N' exclude N, 'at least N'/'up to N' include N"
BOUNDARY_ACTION = "decide the comparison operator at each threshold"

MUTATIONS: list[Mutation] = [
    Mutation(">=", ">", "assume an inclusive bound like 'at least N' excludes N itself, so compare with >",
             "'At least N' includes N: compare with >=, and always test the exact boundary value N."),
    Mutation(">", ">=", "assume an exclusive bound like 'over N' includes N itself, so compare with >=",
             "'Over N' means strictly greater than N: compare with >, and always test the exact boundary value N."),
    Mutation("<=", "<", "assume an inclusive bound like 'up to N' excludes N itself, so compare with <",
             "'Up to N' / 'N or less' includes N: compare with <=, and always test the exact boundary value N."),
    Mutation("<", "<=", "assume an exclusive bound like 'under N' includes N itself, so compare with <=",
             "'Under N' means strictly less than N: compare with <, and always test the exact boundary value N."),
]
# Condition flips for patch mutants (fake_patch_swarm), tried after the boundary catalog: code that
# wires features together has few thresholds but many `is None` / `==` branches.
CONDITION_MUTATIONS: list[Mutation] = [
    Mutation("is not", "is", "assume a missing value (None) is the configured case, so branch on `is None` where the spec checks for a value",
             "`None` means 'not set' (no policy, no limit, unknown) unless the spec says otherwise: take the configured path only on `is not None`, and test the None case explicitly."),
    Mutation("is", "is not", "assume a present value is the default case, so branch on `is not None` where the spec checks for None",
             "`None` means 'not set' (no policy, no limit, unknown) unless the spec says otherwise: take the default path on `is None`, and test the None case explicitly."),
    Mutation("==", "!=", "assume the equality in the spec selects the other branch, so test with !=",
             "Keep each equality test on the branch the spec names: write `==` where the spec says 'is'/'equals', and test both branches."),
    Mutation("!=", "==", "assume the inequality in the spec selects the other branch, so test with ==",
             "Keep each inequality test on the branch the spec names: write `!=` where the spec says 'differs'/'is not', and test both branches."),
]
_BY_ASSUMPTION = {m.assumption: m for m in MUTATIONS + CONDITION_MUTATIONS}


def _op_positions(src: str, op: str) -> list[tuple[int, int, int]]:
    """(row, col, end_col) of every OP token equal to `op`; tokenize keeps `->`/`>=` distinct from `>`.
    `is` / `is not` match the keyword (pair), `is` only when no `not` follows."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    if op in ("is", "is not"):
        out = []
        for j, t in enumerate(toks):
            if t.type != tokenize.NAME or t.string != "is":
                continue
            nxt = toks[j + 1] if j + 1 < len(toks) else None
            has_not = nxt is not None and nxt.type == tokenize.NAME and nxt.string == "not" and nxt.start[0] == t.start[0]
            if op == "is not" and has_not:
                out.append((t.start[0], t.start[1], nxt.end[1]))
            elif op == "is" and not has_not:
                out.append((t.start[0], t.start[1], t.end[1]))
        return out
    return [(t.start[0], t.start[1], t.end[1]) for t in toks if t.type == tokenize.OP and t.string == op]


def mutate_source(src: str, op: str, replacement: str, occurrence: int = 0) -> str | None:
    """Replace the `occurrence`-th OP token equal to `op`; None if there is no such token."""
    pos = _op_positions(src, op)
    if occurrence >= len(pos):
        return None
    row, col, ecol = pos[occurrence]
    lines = src.splitlines(keepends=True)
    lines[row - 1] = lines[row - 1][:col] + replacement + lines[row - 1][ecol:]
    return "".join(lines)


_BATCH_EVAL = r"""
import importlib.util, json, sys
impl_dir, cases_json = sys.argv[1:3]
sys.path.insert(0, impl_dir)
mods, out = {}, []
for file, func, args in json.loads(cases_json):
    try:
        if file not in mods:
            spec = importlib.util.spec_from_file_location("m_%d" % len(mods), f"{impl_dir}/{file}")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            mods[file] = m
        out.append(["ok", repr(getattr(mods[file], func)(*args))])
    except Exception as e:
        out.append(["exc", type(e).__name__])
print(json.dumps(out))
"""


def _batch_eval(impl_dir: Path, cases: list[tuple[str, str, list]]) -> list | None:
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _BATCH_EVAL, str(impl_dir), json.dumps(cases, default=str)],
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None


_pick_cache: dict[tuple, tuple[Mutation, str, str] | None] = {}


def _pick_mutation(ref: Path, subtask: Subtask, k: int) -> tuple[Mutation, str, str] | None:
    """The k-th mutant's preference is the catalog rotated by k, every occurrence in file order.

    The first candidate whose behaviour differs from the reference on the boundary seeds (inputs
    cross_test always includes) wins, so the mutant is catchable; else the first applicable one.
    """
    srcs = {rel: (ref / rel).read_text() for rel in subtask.files if (ref / rel).exists()}
    key = (str(ref), k, tuple(sorted(srcs.items())), tuple(s.model_dump_json() for s in subtask.interface))
    if key in _pick_cache:
        return _pick_cache[key]
    order = MUTATIONS[k % len(MUTATIONS):] + MUTATIONS[: k % len(MUTATIONS)]
    cands = [(m, rel, out) for m in order for rel, src in srcs.items()
             for i in range(len(_op_positions(src, m.op))) if (out := mutate_source(src, m.op, m.replacement, i)) is not None]
    literals = spec_literals(subtask)
    seeds = [(sig.file, c.function, c.args) for sig in subtask.interface if sig.file in srcs
             for c in generate_cases(sig, n_fuzz=1, literals=literals.get(sig.name)) if c.source == "seed"]
    picked = cands[0] if cands else None
    if cands and seeds:
        want = _batch_eval(ref, seeds)
        with tempfile.TemporaryDirectory() as td:
            for m, rel, out in cands:
                tmp = Path(td) / "impl"
                if tmp.exists():
                    shutil.rmtree(tmp)
                shutil.copytree(ref, tmp, ignore=shutil.ignore_patterns("__pycache__"))
                (tmp / rel).write_text(out)
                got = _batch_eval(tmp, seeds)
                if want is not None and got is not None and got != want:
                    picked = (m, rel, out)
                    break
    _pick_cache[key] = picked
    return picked


# ---------- reference resolution ----------


def resolve_reference(subtask: Subtask, repo: Path, reference_dir: str | Path | None = None) -> Path | None:
    cands = [Path(reference_dir)] if reference_dir else []
    cands.append(Path(repo).parent / "reference")
    for c in cands:
        if c.is_dir() and subtask.files and all((c / f).exists() for f in subtask.files):
            return c
    for c in cands:  # partial match is better than nothing
        if c.is_dir() and any((c / f).exists() for f in subtask.files):
            return c
    return None


# ---------- spec tests ----------


def spec_literals(subtask: Subtask) -> dict[str, dict[str, list]]:
    """For every `str` parameter of the pinned interface: the double-quoted lowercase words of the spec, in
    order of first appearance (e.g. "basic", "plus", "pro"). Read from the spec only, like the SubQ author's
    `literals`, so the fuzzer can reach the valid values of a closed set."""
    words = list(dict.fromkeys(re.findall(r'"([a-z][a-z0-9_]{0,30})"', subtask.spec)))
    if not words:
        return {}
    out: dict[str, dict[str, list]] = {}
    for sig in subtask.interface:
        for prm in sig.params:
            name, _, ann = prm.partition(":")
            if ann.split("=")[0].strip() == "str":
                out.setdefault(sig.name, {})[name.strip()] = list(words)
    return out


def fake_spec_tests(subtask: Subtask, repo: Path, reference_dir: str | Path | None = None, n_fuzz: int = 3) -> SpecTests:
    """Boundary + a few fuzz inputs, expected values computed by running the reference implementation."""
    ref = resolve_reference(subtask, repo, reference_dir)
    if ref is None:
        return SpecTests(subtask_id=subtask.id, cases=[], expected={}, properties=[])
    cases: list[TestCase] = []
    expected: dict[str, object] = {}
    seen: set[str] = set()
    literals = spec_literals(subtask)
    for sig in subtask.interface:
        if not (ref / sig.file).exists():
            continue
        for c in generate_cases(sig, n_fuzz=n_fuzz, seed=1, literals=literals.get(sig.name)):
            key = json.dumps([c.function, c.args], default=str, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            tc = TestCase(id=f"spec-{len(cases)}", function=c.function, args=c.args, source="spec")
            o = _call(str(ref), sig, tc)
            if o.timed_out or o.exception == "ImportOrCrash":
                continue
            cases.append(tc)
            expected[tc.id] = o.value if o.ok else {"raises": o.exception}
    return SpecTests(subtask_id=subtask.id, cases=cases, expected=expected, properties=[], visible_ids=[], literals=literals)  # fake agents never run visible tests; keep every case hidden so the verdict sees them all


# ---------- swarm ----------


def _steps_for(subtask: Subtask, boundary_assumption: str) -> list[ReasoningStep]:
    files = list(subtask.files)
    fns = ", ".join(s.name for s in subtask.interface) or "-"
    return [
        ReasoningStep(index=0, assumption=f"the pinned interface is {fns}; implement exactly that", action="read spec and existing code", files_touched=files),
        ReasoningStep(index=1, assumption=boundary_assumption, action=BOUNDARY_ACTION, files_touched=[]),
        ReasoningStep(index=2, assumption="invalid inputs named in the spec raise ValueError", action="write the implementation", files_touched=files),
        ReasoningStep(index=3, assumption="visible spec tests reflect the spec", action="run_spec_tests", files_touched=files),
        ReasoningStep(index=4, assumption="implementation is complete", action="done", files_touched=[]),
    ]


def _embed_steps(runs: list[AgentRun]) -> None:
    texts = sorted({s.as_text() for r in runs for s in r.steps})
    vecs = dict(zip(texts, _emb.embed_texts(texts)))
    for r in runs:
        for s in r.steps:
            s.embedding = vecs[s.as_text()]


def _materialize(run_dir: Path, repo: Path, ref: Path | None, subtask: Subtask) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    if Path(repo).is_dir():
        shutil.copytree(repo, run_dir, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    else:
        run_dir.mkdir(parents=True)
    if ref is None:
        return
    for rel in subtask.files:
        s = ref / rel
        if s.exists():
            d = run_dir / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            # make sure package dirs are importable
            for parent in Path(rel).parents:
                if str(parent) in ("", "."):
                    continue
                init = run_dir / parent / "__init__.py"
                if not init.exists() and (ref / parent / "__init__.py").exists():
                    init.write_text((ref / parent / "__init__.py").read_text())


def _heeds_lesson(step_text: str, lesson_ids: list[str], store: Store) -> str | None:
    """Live check, restricted to lessons active for this run: returns the lesson id the agent heeds."""
    if not lesson_ids:
        return None
    [vec] = _emb.embed_texts([step_text])
    for region, _ in store.regions_near(vec):
        if region.lesson_id in lesson_ids:
            return region.lesson_id
    return None


def fake_swarm(reference_dir: str | Path | None = None, *, n_mutants: int = 1, runs_dir: str | Path | None = None):
    """Return an async function with core.swarm.runner.run_swarm's signature."""
    base_runs = Path(runs_dir) if runs_dir else settings.runs_dir

    async def swarm(subtask: Subtask, agents: list[AgentIdentity], repo: Path, lesson_ids: list[str], store: Store) -> list[AgentRun]:
        repo = Path(repo)
        ref = resolve_reference(subtask, repo, reference_dir)
        n = len(agents)
        mutant_slots = {n - 1 - k: k for k in range(min(n_mutants, max(0, n - 1)))}  # never mutate everyone
        runs: list[AgentRun] = []
        for i, agent in enumerate(agents):
            run = AgentRun(subtask_id=subtask.id, agent=agent, sandbox_path="", lessons_used=list(lesson_ids))
            run_dir = base_runs / run.id
            _materialize(run_dir, repo, ref, subtask)
            run.sandbox_path = str(run_dir)
            assumption = CONSENSUS_ASSUMPTION
            if i in mutant_slots and ref is not None:
                picked = _pick_mutation(ref, subtask, mutant_slots[i])
                if picked is not None:
                    m, rel, mutated = picked
                    probe = ReasoningStep(index=1, assumption=m.assumption, action=BOUNDARY_ACTION).as_text()
                    heeded = _heeds_lesson(probe, lesson_ids, store) or _heeds_rule(m, lesson_ids, store)
                    if heeded is None:
                        (run_dir / rel).write_text(mutated)
                        assumption = m.assumption
                    elif heeded not in run.lessons_used:
                        run.lessons_used.append(heeded)
            if ref is None:
                run.error = "fake_swarm: no reference implementation found"
            run.steps = _steps_for(subtask, assumption)
            runs.append(run)
        _embed_steps(runs)
        for run in runs:
            store.save_steps(run, run.steps)
            store.save_run(run)
        return runs

    return swarm


# ---------- patch series (demo_task/mcp: reference/NN-*.patch on a real repo) ----------
#
# A patch-series task ships its reference as one patch per subtask (in subtask order) instead of a
# reference dir. Reference for subtask i = the repo's committed state (subtasks < i already
# committed by the orchestrator) + patch i, materialized once under runs/mcp_ref/<i>/repo. Faithful
# runs are copies of that tree; the mutant flips one comparison inside patch i's added source lines,
# the first flip that patch i's own test module catches. Spec tests = patch i's test module(s), all
# hidden, run by cross_test's pytest path.

MCP_REF_DIR = "mcp_ref"
_TREE_IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache")


def find_patch_series(task_dir: str | Path) -> list[Path]:
    """`<task dir>/reference/*.patch` in name order (01-…, 02-…), or [] when the task has no patch series."""
    ref = Path(task_dir) / "reference"
    return sorted(p for p in ref.glob("*.patch") if p.is_file()) if ref.is_dir() else []


@dataclass
class PatchFile:
    path: str  # repo-relative post-image path
    new: bool  # created by the patch
    added: set[int]  # 1-based post-image line numbers of '+' lines


def parse_patch(patch: str | Path) -> list[PatchFile]:
    """Files a unified/format-patch diff writes, with the post-image numbers of its added lines."""
    text = Path(patch).read_text() if isinstance(patch, Path) or "\n" not in str(patch) else str(patch)
    files: list[PatchFile] = []
    cur: PatchFile | None = None
    minus = ""
    in_hunk, ln = False, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, in_hunk = None, False
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m and cur is not None:
            in_hunk, ln = True, int(m.group(1))
            continue
        if in_hunk and cur is not None:
            if line.startswith("+"):
                cur.added.add(ln)
                ln += 1
            elif line.startswith(" ") or line == "":
                ln += 1
            elif line.startswith(("-", "\\")):
                pass
            else:
                in_hunk = False
            continue
        if line.startswith("--- "):
            minus = line[4:].strip()
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p == "/dev/null":  # deletion
                cur = None
                continue
            cur = PatchFile(path=p[2:] if p.startswith("b/") else p, new=minus == "/dev/null", added=set())
            files.append(cur)
    return files


def _is_test_path(rel: str) -> bool:
    return rel.startswith("tests/") and Path(rel).name.startswith("test_") and rel.endswith(".py")


def _git_apply(patch: Path, dest: Path) -> None:
    """`git apply` inside `dest`; GIT_CEILING_DIRECTORIES stops git from resolving paths against an
    enclosing checkout (runs/ lives inside this project), same care as eval/heldout_runner."""
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(dest.resolve().parent)}
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch.resolve())], cwd=dest, capture_output=True, text=True, env=env, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{patch.name} does not apply to {dest}: {r.stderr.strip()}")


def _own_git_head(repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel", "HEAD"], capture_output=True, text=True, check=False)
    lines = r.stdout.split()
    if r.returncode != 0 or len(lines) != 2 or Path(lines[0]).resolve() != repo.resolve():
        return None
    return lines[1]


def _export_tree(repo: Path, dest: Path) -> None:
    """The repo's committed state (`git archive HEAD`) when it is its own git repo, else a working-tree copy."""
    if _own_git_head(repo):
        r = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "HEAD"], capture_output=True, check=True)
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:") as tf:
            tf.extractall(dest, filter="data")
    else:
        shutil.copytree(repo, dest, ignore=_TREE_IGNORE)


def _ref_root(ref_root: str | Path | None) -> Path:
    return Path(ref_root) if ref_root else settings.runs_dir / MCP_REF_DIR


def materialize_patch_reference(repo: str | Path, patch: str | Path, index: int, ref_root: str | Path | None = None) -> Path:
    """`repo` at HEAD + `patch` under `<ref_root>/<index + 1>/repo` (ref_root defaults to runs/mcp_ref).
    Reused while (repo, HEAD, patch contents) are unchanged; a repo that is not its own git checkout is
    re-exported every call."""
    repo, patch = Path(repo).resolve(), Path(patch)
    slot = _ref_root(ref_root) / str(index + 1)
    tree, stamp_file = slot / "repo", slot / "stamp"
    head = _own_git_head(repo)
    stamp = hashlib.sha256(f"{repo}\0{head}\0".encode() + patch.read_bytes()).hexdigest()
    if head and tree.is_dir() and stamp_file.is_file() and stamp_file.read_text() == stamp:
        return tree
    if slot.exists():
        shutil.rmtree(slot)
    _export_tree(repo, tree)
    _git_apply(patch, tree)
    stamp_file.write_text(stamp)
    return tree


def patch_test_modules(patch: str | Path, tree: str | Path) -> dict[str, str]:
    """{repo-relative test path: full source} for every test module the patch writes, read from `tree`
    (a tree the patch has been applied to, so modified test files come back whole)."""
    tree = Path(tree)
    return {f.path: (tree / f.path).read_text() for f in parse_patch(Path(patch)) if _is_test_path(f.path) and (tree / f.path).is_file()}


def _series_patch(subtask: Subtask, patches: list[Path]) -> Path:
    if not 0 <= subtask.index < len(patches):
        raise ValueError(f"subtask {subtask.index} {subtask.title!r} has no patch ({len(patches)} in the series)")
    return Path(patches[subtask.index])


_patch_spec_cache: dict[str, SpecTests] = {}


def fake_patch_spec_tests(subtask: Subtask, repo: Path, patches: list[Path], ref_root: str | Path | None = None) -> SpecTests:
    """Spec tests for subtask i of a patch series: patch i's test module(s) as hidden pytest modules.
    No function-level cases (the pinned interface is mostly methods). Cached per (title, patch)."""
    patch = _series_patch(subtask, patches)
    key = hashlib.sha256(subtask.title.encode() + b"\0" + patch.read_bytes()).hexdigest()
    if key not in _patch_spec_cache:
        tree = materialize_patch_reference(repo, patch, subtask.index, ref_root)
        modules = patch_test_modules(patch, tree)
        if not modules:
            console.print(f"[yellow]fake spec: {patch.name} adds no test module; the verdict has only the repo suite")
        _patch_spec_cache[key] = SpecTests(subtask_id=subtask.id, cases=[], expected={}, properties=[], visible_ids=[], modules=modules, author_id="fake-patch")
    return _patch_spec_cache[key].model_copy(update={"subtask_id": subtask.id})


# -- mutants --

# Plausible off-consensus assumptions for the comparisons the MCP series' tests catch, keyed by
# (patch filename, fragment of the original line). Anything else gets a generic boundary gloss.
SPEC_GLOSSES: dict[tuple[str, str], str] = {
    ("01-limits-model.patch", "< self.rate_limit.per_seconds"): "a call exactly per_seconds old is still inside the window",
    ("01-limits-model.patch", "< per_seconds"): "a call exactly per_seconds old is still inside the window",
    ("01-limits-model.patch", ">= per_seconds"): "a call exactly per_seconds old is still inside the window, so pruning keeps it",
    ("01-limits-model.patch", "< 0"): "now == 0 is invalid as well, because time zero means the clock was never started",
    ("01-limits-model.patch", "> now"): "a previous call recorded at exactly `now` is in the future and must be rejected",
    ("02-tool-policies.patch", "None if tool is None else tool.limits"): "get_limits needs no lookup for a registered tool and returns None for it; only the fallback path reads `.limits`",
    ("03-enforcement.patch", 'policy.scope == "session" and scope_key is not None'): "a session-scoped policy gets its own budget only for calls that arrive without a session key",
    ("04-metadata-context.patch", "if policy is None:"): "remaining_calls has nothing to report for a tool that has a policy until it is called, so it returns None",
    ("05-session-scope.patch", 'scope_key is not None and policy.scope == "session"'): "the per-session cleanup is registered only for calls that arrive without a session key",
    ("06-status-resource.patch", "limits_resource is not None"): "the limits status resource is served only when no resource URI was configured",
    ("07-cli-limits.patch", "if policy is not None:"): "`mcp limits` prints the rate, quota and scope columns for the tools that have no policy",
}


@dataclass
class PatchMutant:
    mutation: Mutation
    file: str  # repo-relative
    line: int  # 1-based
    occurrence: int  # index into _op_positions(src, mutation.op)
    caught: bool
    original_line: str
    assumption: str
    source: str  # mutated file content


@dataclass
class _PatchTarget:
    name: str  # cache key prefix
    tree: Path  # reference tree (base + patch)
    patch: Path
    slot: Path  # where probes and the mutant choice are cached
    check: object  # Callable[[Path], tuple[bool, str]]: (tests fail on this tree, detail)


def _gloss(patch_name: str, line: str, m: Mutation) -> str:
    for (pn, frag), text in SPEC_GLOSSES.items():
        if pn == patch_name and frag in line:
            return text
    expr = line.strip().removeprefix("if ").removeprefix("elif ").removeprefix("while ").removeprefix("return ").removeprefix("assert ").rstrip(":").strip()
    if m in CONDITION_MUTATIONS:
        return f"the spec's condition `{expr}` describes the other branch"
    side = "still satisfies" if m.replacement in ("<=", ">=") else "no longer satisfies"
    return f"when both sides of `{expr}` are equal the value {side} the spec's condition"


def _hash_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(str(p).encode() + b"\0" + (p.read_bytes() if p.is_file() else b"-") + b"\0")
    return h.hexdigest()


def _link_tree(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` with hard links where possible (sandboxes are ~1800 files). Writers must
    unlink before writing (see `_write_fresh`) so a shared inode is never modified in place."""
    if dst.exists():
        shutil.rmtree(dst)

    def link(s, d):
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)

    shutil.copytree(src, dst, ignore=_TREE_IGNORE, copy_function=link)


def _write_fresh(p: Path, text: str) -> None:
    p.unlink(missing_ok=True)
    p.write_text(text)


_mutant_cache: dict[tuple[str, int, str], PatchMutant | None] = {}


def pick_patch_mutant(target: _PatchTarget, k: int = 0) -> PatchMutant | None:
    """The k-th mutant: the catalog rotated by k, every comparison inside the patch's added source lines
    in file order; the first one whose tests fail on the mutated tree wins. If none is caught, the first
    candidate is returned with caught=False (and a warning). Cached in memory and on disk (`<slot>/mutant_<k>.json`)
    per (reference sources, tests)."""
    pfiles = [f for f in parse_patch(target.patch) if f.path.endswith(".py") and not f.path.startswith("tests/") and (target.tree / f.path).is_file()]
    stamp = _hash_files([target.patch] + [target.tree / f.path for f in parse_patch(target.patch)])
    key = (target.name, k, stamp)
    if key in _mutant_cache:
        return _mutant_cache[key]
    order = MUTATIONS[k % len(MUTATIONS):] + MUTATIONS[: k % len(MUTATIONS)] + CONDITION_MUTATIONS
    srcs = {f.path: (target.tree / f.path).read_text() for f in pfiles}
    cands: list[tuple[Mutation, str, int, int]] = []
    for m in order:
        for f in pfiles:
            for occ, (row, _c, _e) in enumerate(_op_positions(srcs[f.path], m.op)):
                if row in f.added:
                    cands.append((m, f.path, row, occ))

    def build(m: Mutation, rel: str, row: int, occ: int, caught: bool) -> PatchMutant:
        line = srcs[rel].splitlines()[row - 1]
        return PatchMutant(m, rel, row, occ, caught, line.strip(), f"{_gloss(target.patch.name, line, m)}; {m.assumption}",
                           mutate_source(srcs[rel], m.op, m.replacement, occ) or srcs[rel])

    target.slot.mkdir(parents=True, exist_ok=True)
    disk = target.slot / f"mutant_{k}.json"
    picked: PatchMutant | None = None
    if disk.is_file():
        try:
            d = json.loads(disk.read_text())
            if d.get("stamp") == stamp:
                if d.get("op") is None:
                    _mutant_cache[key] = None
                    return None
                m = next(x for x in MUTATIONS + CONDITION_MUTATIONS if x.op == d["op"])
                picked = build(m, d["file"], d["line"], d["occurrence"], d["caught"])
        except (ValueError, KeyError, StopIteration, IndexError):
            picked = None
    if picked is None and cands:
        probe = target.slot / "probe"
        _link_tree(target.tree, probe)
        ref_fails, detail = target.check(probe)
        if ref_fails:
            console.print(f"[yellow]fake swarm: the reference for {target.name} fails its own tests ({detail}); its mutant cannot be told apart")
        else:
            for m, rel, row, occ in cands:
                mutated = mutate_source(srcs[rel], m.op, m.replacement, occ)
                if mutated is None:
                    continue
                _write_fresh(probe / rel, mutated)
                fails, _ = target.check(probe)
                _write_fresh(probe / rel, srcs[rel])
                if fails:
                    picked = build(m, rel, row, occ, True)
                    break
        shutil.rmtree(probe, ignore_errors=True)
        if picked is None:
            m, rel, row, occ = cands[0]
            picked = build(m, rel, row, occ, False)
            console.print(f"[yellow]fake swarm: no comparison flip in {target.patch.name} is caught by its tests; using the uncaught {rel}:{row} `{picked.original_line}`")
    elif picked is None:
        console.print(f"[yellow]fake swarm: {target.patch.name} adds no comparison to flip; every run is faithful")
    rec = {"stamp": stamp, "op": None} if picked is None else {
        "stamp": stamp, "op": picked.mutation.op, "file": picked.file, "line": picked.line, "occurrence": picked.occurrence, "caught": picked.caught,
    }
    disk.write_text(json.dumps(rec))
    _mutant_cache[key] = picked
    return picked


def _junit_fails(sandbox: Path, tests_dir: Path, base_repo: Path, name: str) -> tuple[bool, str]:
    """Run the modules in `tests_dir` inside `sandbox` with eval/heldout_runner.run_hidden_tests (base repo's
    venv, sandbox sources first on sys.path); True when at least one test fails or errors."""
    from eval.heldout_runner import HeldoutItem, run_hidden_tests

    item = HeldoutItem(subtask=Subtask(index=0, title=name, spec="", interface=[], files=[]), kind="patch", folder=tests_dir.parent / name,
                       hidden_tests_dir=tests_dir, base_repo=base_repo)
    res = run_hidden_tests(sandbox, item, timeout=300)
    return (not res.passed and (res.failures + res.errors) > 0) or res.tests == 0, f"tests={res.tests} failures={res.failures} errors={res.errors}"


def _series_target(subtask: Subtask, repo: Path, patches: list[Path], ref_root: str | Path | None) -> _PatchTarget:
    patch = _series_patch(subtask, patches)
    tree = materialize_patch_reference(repo, patch, subtask.index, ref_root)
    slot = tree.parent
    tests_dir = slot / "spec_tests"
    if tests_dir.exists():
        shutil.rmtree(tests_dir)
    tests_dir.mkdir()
    for rel, src in patch_test_modules(patch, tree).items():
        (tests_dir / Path(rel).name).write_text(src)
    name = f"s{subtask.index + 1}"
    return _PatchTarget(name=f"series-{patch.name}", tree=tree, patch=patch, slot=slot,
                        check=lambda sb: _junit_fails(sb, tests_dir, repo, name))


def _heldout_target(subtask: Subtask, repo: Path, heldout_dir: str | Path | None, ref_root: str | Path | None) -> _PatchTarget:
    from eval.heldout_runner import load_heldout, materialize_reference, run_hidden_tests

    item = next((i for i in load_heldout(heldout_dir) if i.kind == "patch" and i.name == subtask.title), None)
    if item is None:
        raise ValueError(f"fake patch swarm: no patch-kind held-out slice named {subtask.title!r}")
    mirror = repo.parent / "reference"
    tree = mirror.resolve() if mirror.is_dir() else materialize_reference(item)

    def check(sb: Path) -> tuple[bool, str]:
        res = run_hidden_tests(sb, item, timeout=300)
        return (not res.passed and (res.failures + res.errors) > 0) or res.tests == 0, f"tests={res.tests} failures={res.failures} errors={res.errors}"

    return _PatchTarget(name=f"heldout-{item.name}", tree=tree, patch=item.reference_patch, slot=_ref_root(ref_root) / f"heldout_{item.name}", check=check)


def _heeds_rule(m: Mutation, lesson_ids: list[str], store: Store) -> str | None:
    """An active lesson that states this mutation's rule (fake_reflect writes `m.lesson` for it)."""
    for lid in lesson_ids:
        lesson = store.get_lesson(lid)
        if lesson is not None and lesson.text.startswith(m.lesson):
            return lid
    return None


def fake_patch_swarm(
    patches: list[Path] | None = None,
    *,
    heldout_dir: str | Path | None = None,
    n_mutants: int = 1,
    runs_dir: str | Path | None = None,
    ref_root: str | Path | None = None,
):
    """A run_swarm-compatible async fn for patch-defined subtasks.

    With `patches` (the task's series, in subtask order): subtask i's reference is the repo's committed
    state + patches[i]. Held-out patch slices (tags contain "heldout" and "patch") are served from the
    slice's own reference.patch / hidden_tests under `heldout_dir`, with the materialized reference that
    eval/heldout_runner mirrors at `<repo>/../reference`.

    Faithful runs: a copy of the reference tree (no .git / .venv). The last `n_mutants` runs flip one
    comparison in the patch's added lines (the first flip its tests catch) and carry the matching
    off-consensus reasoning at step 1. A mutant heeds an active lesson whose region holds its divergent
    step, or whose text states the rule for its mutation, and then writes the faithful tree.
    """
    base_runs = Path(runs_dir) if runs_dir else settings.runs_dir

    async def swarm(subtask: Subtask, agents: list[AgentIdentity], repo: Path, lesson_ids: list[str], store: Store) -> list[AgentRun]:
        repo = Path(repo)
        if "heldout" in subtask.tags and "patch" in subtask.tags:
            target = _heldout_target(subtask, repo, heldout_dir, ref_root)
        elif patches:
            target = _series_target(subtask, repo, list(patches), ref_root)
        else:
            raise ValueError(f"fake_patch_swarm: no patch for subtask {subtask.title!r} (pass the task's patch series)")
        n = len(agents)
        mutant_slots = {n - 1 - k: k for k in range(min(n_mutants, max(0, n - 1)))}  # never mutate everyone
        runs: list[AgentRun] = []
        for i, agent in enumerate(agents):
            run = AgentRun(subtask_id=subtask.id, agent=agent, sandbox_path="", lessons_used=list(lesson_ids))
            run_dir = base_runs / run.id
            _link_tree(target.tree, run_dir)
            run.sandbox_path = str(run_dir)
            assumption = CONSENSUS_ASSUMPTION
            if i in mutant_slots:
                mut = pick_patch_mutant(target, mutant_slots[i])
                if mut is not None:
                    probe = ReasoningStep(index=1, assumption=mut.assumption, action=BOUNDARY_ACTION).as_text()
                    heeded = _heeds_lesson(probe, lesson_ids, store) or _heeds_rule(mut.mutation, lesson_ids, store)
                    if heeded is None:
                        _write_fresh(run_dir / mut.file, mut.source)
                        assumption = mut.assumption
                    elif heeded not in run.lessons_used:
                        run.lessons_used.append(heeded)
            run.steps = _steps_for(subtask, assumption)
            runs.append(run)
        _embed_steps(runs)
        for run in runs:
            store.save_steps(run, run.steps)
            store.save_run(run)
        return runs

    return swarm


def fake_heldout_swarm(heldout_dir: str | Path | None = None, *, runs_dir: str | Path | None = None, ref_root: str | Path | None = None):
    """The held-out gate's fake swarm: `fake_patch_swarm` for patch-kind slices, `fake_swarm` for function slices."""
    fn_swarm = fake_swarm(runs_dir=runs_dir)
    patch_swarm = fake_patch_swarm(heldout_dir=heldout_dir, runs_dir=runs_dir, ref_root=ref_root)

    async def swarm(subtask: Subtask, agents: list[AgentIdentity], repo: Path, lesson_ids: list[str], store: Store) -> list[AgentRun]:
        fn = patch_swarm if "patch" in subtask.tags else fn_swarm
        return await fn(subtask, agents, repo, lesson_ids, store)

    return swarm


# ---------- reflector ----------


def _assumption_of(step_text: str) -> str:
    m = re.match(r"assumption: (.*?) \| action:", step_text)
    return m.group(1) if m else step_text


def _mutation_for(assumption: str) -> Mutation | None:
    """The catalog mutation an assumption came from: exact text, or a patch mutant's '<gloss>; <catalog text>'."""
    if assumption in _BY_ASSUMPTION:
        return _BY_ASSUMPTION[assumption]
    return next((m for m in MUTATIONS + CONDITION_MUTATIONS if assumption.endswith("; " + m.assumption)), None)


def fake_reflect(spec: str, failed: AgentRun, div: Divergence, store: Store) -> Lesson:
    assumption = _assumption_of(div.step_text)
    mut = _mutation_for(assumption)
    if mut is not None:
        text, tags = mut.lesson, (["boundary", "comparison"] if mut in MUTATIONS else ["condition", "branch"])
    else:
        text, tags = f"Do not assume '{assumption}' without checking it against the spec; the verified majority reasoned otherwise.", ["reasoning"]
    if div.breaking_case is None:
        text += " (no breaking input recorded)"
    step = failed.steps[div.step_index] if div.step_index < len(failed.steps) else None
    div_vec = (step.embedding if step and step.embedding else None) or _emb.embed_texts([div.step_text])[0]
    [lesson_vec] = _emb.embed_texts([text])
    bc = div.breaking_case
    lesson = Lesson(
        text=text,
        tags=tags,
        embedding=lesson_vec,
        divergence_embedding=div_vec,
        confidence=0.8 if mut is not None else 0.3,
        created_from={
            "subtask_id": failed.subtask_id,
            "run_id": failed.id,
            "agent_id": failed.agent.id,
            "breaking_input": {"function": bc.function, "args": bc.args} if bc else None,
            "majority_outcome": div.majority_outcome.model_dump() if div.majority_outcome else None,
            "reflector": "fake",
        },
    )
    return dedup_or_insert(lesson, store)


# ---------- decomposer ----------


def fake_decompose(task: LongTask, reference_dir: str | Path) -> LongTask:
    """One subtask per reference module (non-__init__ .py), ordered by first mention in the task text."""
    from core.decompose.interfaces import extract_interface

    ref = Path(reference_dir)
    files = [str(p.relative_to(ref)) for p in sorted(ref.rglob("*.py")) if p.name != "__init__.py" and "__pycache__" not in p.parts]
    desc = task.description

    def order(rel: str) -> tuple[int, str]:
        pos = desc.find(rel)
        return (pos if pos >= 0 else len(desc), rel)

    subtasks: list[Subtask] = []
    prev: str | None = None
    for i, rel in enumerate(sorted(files, key=order)):
        iface = extract_interface(ref, [rel])
        if not iface:
            continue
        st = Subtask(index=len(subtasks), title=f"implement {rel}", spec=desc, interface=iface, files=[rel], depends_on=[prev] if prev else [], tags=["fake"])
        subtasks.append(st)
        prev = st.id
    task.subtasks = subtasks
    return task


# ---------- offline embeddings ----------

EMBED_TARGETS = [
    "core.llm.embeddings",
    "core.context.builder",
    "core.context.live_check",
    "core.reflect.reflector",
    "core.swarm.runner",
]


def use_fake_embeddings(setattr_fn=None) -> None:
    """Point every `embed_texts` import at `fake_embed` so nothing reaches an embedding API.

    Pass `monkeypatch.setattr` in tests so it is undone afterwards; default is a plain setattr.
    """
    import importlib

    set_ = setattr_fn or setattr
    for name in EMBED_TARGETS:
        mod = importlib.import_module(name)
        set_(mod, "embed_texts", _emb.fake_embed)
