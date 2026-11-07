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
active for this run (in `lesson_ids`), it takes the consensus assumption instead and writes the
faithful implementation. That is what makes the held-out gate's with/without comparison move.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path

from core.config import settings
from core.harness.crosstest import _call
from core.harness.spec_tests import SpecTests, _split_visible
from core.harness.strategies import generate_cases
from core.llm import embeddings as _emb
from core.models import AgentIdentity, AgentRun, Divergence, Lesson, LongTask, ReasoningStep, Subtask, TestCase
from core.reflect.dedup import dedup_or_insert
from core.store.base import Store

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
_BY_ASSUMPTION = {m.assumption: m for m in MUTATIONS}


def _op_positions(src: str, op: str) -> list[tuple[int, int, int]]:
    """(row, col, end_col) of every OP token equal to `op`; tokenize keeps `->`/`>=` distinct from `>`."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
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
    seeds = [(sig.file, c.function, c.args) for sig in subtask.interface if sig.file in srcs
             for c in generate_cases(sig, n_fuzz=1) if c.source == "seed"]
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


def fake_spec_tests(subtask: Subtask, repo: Path, reference_dir: str | Path | None = None, n_fuzz: int = 3) -> SpecTests:
    """Boundary + a few fuzz inputs, expected values computed by running the reference implementation."""
    ref = resolve_reference(subtask, repo, reference_dir)
    if ref is None:
        return SpecTests(subtask_id=subtask.id, cases=[], expected={}, properties=[])
    cases: list[TestCase] = []
    expected: dict[str, object] = {}
    seen: set[str] = set()
    for sig in subtask.interface:
        if not (ref / sig.file).exists():
            continue
        for c in generate_cases(sig, n_fuzz=n_fuzz, seed=1):
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
    return SpecTests(subtask_id=subtask.id, cases=cases, expected=expected, properties=[], visible_ids=_split_visible([c.id for c in cases], 0.4))


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
                    heeded = _heeds_lesson(probe, lesson_ids, store)
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


# ---------- reflector ----------


def _assumption_of(step_text: str) -> str:
    m = re.match(r"assumption: (.*?) \| action:", step_text)
    return m.group(1) if m else step_text


def fake_reflect(spec: str, failed: AgentRun, div: Divergence, store: Store) -> Lesson:
    assumption = _assumption_of(div.step_text)
    mut = _BY_ASSUMPTION.get(assumption)
    if mut is not None:
        text, tags = mut.lesson, ["boundary", "comparison"]
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
