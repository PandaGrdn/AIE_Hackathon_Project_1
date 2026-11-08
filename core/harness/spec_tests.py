"""Independent spec tests. Owner: Ethan Li.

Test independence rule (outline §5.5): no test used for a verdict is derived from any
implementation. A dedicated test-author identity reads ONLY the initial subtask spec, the
pinned interface and the repo at the base commit — never a swarm agent's code, reasoning or
tests — and writes:

  - cases: concrete inputs with expected values (source="spec")
  - properties: metamorphic rules that must hold for any correct implementation
    (source="property"), expressed as small Python predicates over (args, result)
  - literals: for string params with a closed set of valid values, the valid values,
    so the fuzzer can draw them (a random string never hits "book" or "dvd")
  - modules: hidden pytest files against the repo's public API (the verdict on a real repo);
    split ~40% visible / rest hidden at the test-function level, same `visible_ids`

Function-level cases and properties are the oracle for pure subtasks; pytest modules are
the oracle for anything that touches a real repo. Both may be present.

Generated once per subtask and cached under runs/spec_tests/<content-hash>.json, keyed by
(title, spec, interface) rather than the subtask's random id, so every run of that subtask,
including held-out re-runs and a fresh `swarm run`, uses the same independent tests.
The author identity must not be one of the implementers for that subtask.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from core.config import ROOT, settings
from core.llm.clients import get_client
from core.models import AgentIdentity, Subtask, TestCase

CACHE_DIR = settings.runs_dir / "spec_tests"  # written by every run (gitignored)
COMMITTED_DIR = ROOT / "spec_tests"  # tracked snapshots for the demo fallback; copy files here by hand
VISIBLE_SHARE = 0.4
AUTHOR_ATTEMPTS = 3


def author_identity() -> AgentIdentity:
    """The test author: a text-generation model (Gemini; SubQ Snap is a decision model and cannot
    write tests, see `subq_judge.py`). Its own id so trust/routing never mix it with an implementer;
    routing must not pick this identity for the same subtask."""
    return AgentIdentity(id="spec-author-gemini", provider="gemini", model=settings.gemini_model, temperature=0.0)


class SpecProperty(BaseModel):
    """A predicate over one call: `check(args, result) -> bool`, given as Python source."""

    name: str
    function: str
    predicate_src: str  # e.g. "lambda args, result: result <= args[0] * args[1]"
    description: str


class SpecTests(BaseModel):
    subtask_id: str
    cases: list[TestCase]
    expected: dict[str, object]  # case_id -> expected value, or {"raises": "ValueError"}
    properties: list[SpecProperty]
    visible_ids: list[str] = []  # ~40% of cases agents may run live; the rest + properties decide the verdict
    literals: dict[str, dict[str, list]] = {}  # function -> param -> valid literal values (str params)
    modules: dict[str, str] = {}  # pytest filename -> source; node ids are "<filename>::<test_fn>"
    author_id: str = ""

    @property
    def visible(self) -> list[TestCase]:
        return [c for c in self.cases if c.id in self.visible_ids]

    @property
    def hidden(self) -> list[TestCase]:
        return [c for c in self.cases if c.id not in self.visible_ids]

    @property
    def test_node_ids(self) -> list[str]:
        return spec_node_ids(self.modules)

    @property
    def hidden_test_ids(self) -> list[str]:
        return [t for t in self.test_node_ids if t not in self.visible_ids]


def spec_node_ids(modules: dict[str, str]) -> list[str]:
    """`<filename>::<test_fn>` for every top-level test function (and class-less test) in each module."""
    out: list[str] = []
    for fname in sorted(modules):
        try:
            tree = ast.parse(modules[fname])
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                out.append(f"{fname}::{node.name}")
    return out


def visible_module_sources(spec: SpecTests) -> dict[str, str]:
    """The modules with hidden test functions removed: what `run_spec_tests` may hand to an agent."""
    visible = set(spec.visible_ids)
    out: dict[str, str] = {}
    for fname, src in spec.modules.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        keep = [
            n for n in tree.body
            if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test") and f"{fname}::{n.name}" not in visible)
        ]
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test") for n in keep):
            out[fname] = ast.unparse(ast.Module(body=keep, type_ignores=[]))
    return out


AUTHOR_PROMPT = """You are writing acceptance tests for a subtask BEFORE anyone implements it.
You see only the spec, the pinned interface and the existing code. Do not guess at an implementation.

## Spec
{spec}

## Pinned interface
{iface}

## Existing code in scope (base commit)
{code}

Return JSON:
{{"modules": {{"test_<slug>.py": "<pytest source>"}},
  "cases": [{{"function": str, "args": [...], "expected": <value> | {{"raises": "ExceptionName"}}, "why": str}}],
  "properties": [{{"name": str, "function": str, "predicate": "lambda args, result: <bool expr>", "description": str}}],
  "literals": {{"<function>": {{"<str param name>": ["valid", "values"]}}}}}}

Rules:
- `modules`: one or two pytest files, 15 to 30 small top-level test functions named test_*, one assertion
  each; at least one test per numbered requirement in the spec, and for every boundary the spec names a
  test AT the boundary and one just past it (exact equality, zero, empty, negative, None where allowed);
  each, importing the code by its repo path (pricing/tiers.py -> `from pricing.tiers import tier_for`;
  in a src/ layout drop the `src/` prefix: src/pkg/mod.py -> `from pkg.mod import f`).
  Use only pytest and the standard library. Each test must follow from the spec literally.
- `cases`/`properties` only when the pinned interface is pure functions; otherwise return empty lists.
- 10 to 25 cases. Cover every boundary the spec names (inclusive vs exclusive, equality, zero, empty, negative).
- Expected values must follow from the spec literally. If the spec is ambiguous for a case, omit the case and
  add a property instead. Use JSON numbers for numeric results (2 decimals where the spec rounds), JSON null for None.
- 3 to 8 properties that hold for ANY correct implementation (monotonicity, bounds, idempotence, empty-input shape).
  A predicate sees one call only: `args` is the argument list, `result` the return value. Predicates are not run on raised exceptions.
- `literals`: only for string params whose valid values are a closed set named by the spec (tier names, item types);
  omit the key otherwise.
"""


def cache_key(subtask: Subtask) -> str:
    iface = [s.model_dump() for s in sorted(subtask.interface, key=lambda s: (s.file, s.name))]
    blob = json.dumps({"title": subtask.title, "spec": subtask.spec, "interface": iface}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _cache_path(subtask: Subtask) -> Path:
    return CACHE_DIR / f"{cache_key(subtask)}.json"


def load_cached(subtask: Subtask) -> SpecTests | None:
    p = _cache_path(subtask)
    if not p.exists():
        p = COMMITTED_DIR / p.name
    if not p.exists():
        return None
    st = SpecTests.model_validate_json(p.read_text())
    st.subtask_id = subtask.id  # ids are regenerated per decomposition; the tests are the same
    return st


def save_cached(subtask: Subtask, st: SpecTests) -> Path:
    p = _cache_path(subtask)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(st.model_dump_json(indent=2))
    return p


def load_or_author(subtask: Subtask, repo: Path) -> SpecTests:
    st = load_cached(subtask)
    if st is None:
        st = author(subtask, repo)
        save_cached(subtask, st)
    return st


def author(subtask: Subtask, repo: Path) -> SpecTests:
    """Ask the verifier for tests. It sees the initial spec, interface and base-commit code only."""
    iface = "\n".join(f"- {s.file}: def {s.name}({', '.join(s.params)}) -> {s.returns or 'Any'}" for s in subtask.interface)
    code = "\n\n".join(f"# {f}\n{(repo / f).read_text()}" for f in subtask.files if (repo / f).exists())[:20_000]
    identity = author_identity()
    client = get_client(identity)
    prompt = AUTHOR_PROMPT.format(spec=subtask.spec, iface=iface, code=code or "(none)")
    last_err = "no attempt"
    for _ in range(AUTHOR_ATTEMPTS):  # a model can return broken JSON or modules that don't parse: ask again
        resp = client.chat([{"role": "user", "content": prompt}])
        try:
            data = json.loads(_extract_json(resp["text"]))
        except json.JSONDecodeError as e:
            last_err = f"invalid JSON: {e}"
            continue
        st = parse_author_output(subtask, data, identity.id)
        if st.test_node_ids or st.cases:
            return st
        last_err = "no parseable test module and no cases"
    raise ValueError(f"spec author produced no usable tests for {subtask.title!r} after {AUTHOR_ATTEMPTS} attempts: {last_err}")


def parse_author_output(subtask: Subtask, data: dict, author_id: str = "") -> SpecTests:
    """Validate the model's JSON into SpecTests; drops cases for unknown functions or malformed args."""
    known = {s.name for s in subtask.interface}
    cases, expected = [], {}
    for c in data.get("cases", []):
        if not isinstance(c, dict) or c.get("function") not in known or not isinstance(c.get("args"), list) or "expected" not in c:
            continue
        tc = TestCase(function=c["function"], args=c["args"], source="spec")
        cases.append(tc)
        expected[tc.id] = c["expected"]
    props = [
        SpecProperty(name=str(p.get("name", f"p{i}")), function=p["function"], predicate_src=p["predicate"], description=str(p.get("description", "")))
        for i, p in enumerate(data.get("properties", []))
        if isinstance(p, dict) and p.get("function") in known and isinstance(p.get("predicate"), str)
    ]
    literals: dict[str, dict[str, list]] = {}
    for fn, params in (data.get("literals") or {}).items():
        if fn in known and isinstance(params, dict):
            literals[fn] = {k: list(v) for k, v in params.items() if isinstance(v, list) and v}
    modules = {str(k): v for k, v in (data.get("modules") or {}).items() if isinstance(v, str) and str(k).endswith(".py") and v.strip() and _parses(v)}
    visible = _split_visible([c.id for c in cases], VISIBLE_SHARE) + _split_visible(spec_node_ids(modules), VISIBLE_SHARE)
    return SpecTests(subtask_id=subtask.id, cases=cases, expected=expected, properties=props, visible_ids=visible, literals=literals, modules=modules, author_id=author_id)


def _split_visible(ids: list[str], share: float) -> list[str]:
    """Deterministic split so the same subtask always exposes the same visible cases/tests."""
    ranked = sorted(ids, key=lambda i: hashlib.sha256(i.encode()).hexdigest())
    n = max(1, int(len(ranked) * share)) if ranked else 0
    return ranked[:n]


_PREDICATE_BUILTINS = {
    "len": len, "abs": abs, "min": min, "max": max, "all": all, "any": any, "sum": sum, "round": round, "sorted": sorted,
    "isinstance": isinstance, "int": int, "float": float, "str": str, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "bool": bool, "range": range, "zip": zip, "enumerate": enumerate, "True": True, "False": False, "None": None,
}


def check_property(prop: SpecProperty, args: list, result: object) -> bool | None:
    """Evaluate a property predicate. Predicates are model-written: run them only on values, never on code.
    None when the predicate source itself does not evaluate (the author's fault, never the agent's);
    False when calling it raises or returns falsy."""
    try:
        fn = eval(prop.predicate_src, {"__builtins__": _PREDICATE_BUILTINS})
    except Exception:
        return None
    try:
        return bool(fn(args, result))
    except Exception:
        return False


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 else "{}"
