"""Independent spec tests. Owner: Ethan Li.

Test independence rule (outline §5.5): no test used for a verdict is derived from any
implementation. A dedicated test-author model reads ONLY the subtask spec, the pinned
interface and the repo at the base commit, and writes:

  - cases: concrete inputs with expected values (source="spec")
  - properties: metamorphic rules that must hold for any correct implementation
    (source="property"), expressed as small Python predicates over (args, result)

Generated once per subtask and cached under runs/spec_tests/<subtask_id>.json so every
run of that subtask, including held-out re-runs, uses the same independent tests.
The author identity must not be one of the implementers for that subtask.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from core.config import settings
from core.llm.clients import get_client, reflector_identity
from core.models import Subtask, TestCase

CACHE_DIR = settings.runs_dir / "spec_tests"


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

    @property
    def visible(self) -> list[TestCase]:
        return [c for c in self.cases if c.id in self.visible_ids]

    @property
    def hidden(self) -> list[TestCase]:
        return [c for c in self.cases if c.id not in self.visible_ids]


AUTHOR_PROMPT = """You are writing acceptance tests for a subtask BEFORE anyone implements it.
You see only the spec, the pinned interface and the existing code. Do not guess at an implementation.

## Spec
{spec}

## Pinned interface
{iface}

## Existing code in scope (base commit)
{code}

Return JSON:
{{"cases": [{{"function": str, "args": [...], "expected": <value> | {{"raises": "ExceptionName"}}, "why": str}}],
  "properties": [{{"name": str, "function": str, "predicate": "lambda args, result: <bool expr>", "description": str}}]}}

Rules:
- 10 to 25 cases. Cover every boundary the spec names (inclusive vs exclusive, equality, zero, empty, negative).
- Expected values must follow from the spec literally. If the spec is ambiguous for a case, omit the case and
  add a property instead.
- 3 to 8 properties that hold for ANY correct implementation (monotonicity, bounds, idempotence, empty-input shape).
"""


def _cache_path(subtask: Subtask) -> Path:
    return CACHE_DIR / f"{subtask.id}.json"


def load_or_author(subtask: Subtask, repo: Path) -> SpecTests:
    p = _cache_path(subtask)
    if p.exists():
        return SpecTests.model_validate_json(p.read_text())
    st = author(subtask, repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(st.model_dump_json(indent=2))
    return st


def author(subtask: Subtask, repo: Path) -> SpecTests:
    iface = "\n".join(f"- {s.file}: def {s.name}({', '.join(s.params)}) -> {s.returns or 'Any'}" for s in subtask.interface)
    code = "\n\n".join(f"# {f}\n{(repo / f).read_text()}" for f in subtask.files if (repo / f).exists())[:20_000]
    client = get_client(reflector_identity())  # TODO(Ethan Li): dedicated author identity, excluded from that subtask's swarm
    resp = client.chat([{"role": "user", "content": AUTHOR_PROMPT.format(spec=subtask.spec, iface=iface, code=code or "(none)")}])
    data = json.loads(_extract_json(resp["text"]))

    cases, expected = [], {}
    for c in data.get("cases", []):
        tc = TestCase(function=c["function"], args=c["args"], source="spec")
        cases.append(tc)
        expected[tc.id] = c["expected"]
    props = [SpecProperty(name=p["name"], function=p["function"], predicate_src=p["predicate"], description=p.get("description", "")) for p in data.get("properties", [])]
    visible = [c.id for c in cases[:: 5][:0]]  # placeholder, replaced below
    visible = _split_visible(cases, VISIBLE_SHARE)
    return SpecTests(subtask_id=subtask.id, cases=cases, expected=expected, properties=props, visible_ids=visible)


VISIBLE_SHARE = 0.4


def _split_visible(cases: list[TestCase], share: float) -> list[str]:
    """Deterministic split so the same subtask always exposes the same visible cases."""
    import hashlib

    ranked = sorted(cases, key=lambda c: hashlib.sha256(c.id.encode()).hexdigest())
    n = max(1, int(len(ranked) * share)) if ranked else 0
    return [c.id for c in ranked[:n]]


def check_property(prop: SpecProperty, args: list, result: object) -> bool:
    """Evaluate a property predicate. Predicates are model-written: run them only on values, never on code."""
    try:
        fn = eval(prop.predicate_src, {"__builtins__": {"len": len, "abs": abs, "min": min, "max": max, "all": all, "any": any, "sum": sum, "round": round, "sorted": sorted, "isinstance": isinstance, "int": int, "float": float, "str": str, "list": list, "dict": dict}})
        return bool(fn(args, result))
    except Exception:
        return False


# TODO(Ethan Li): in crosstest.cross_test —
#   1. cases = spec.cases + fuzz cases; outcomes as now.
#   2. for spec cases: compare each run's outcome to `expected`; a run that misses an expected value
#      is failed on that case regardless of the majority. If the majority disagrees with `expected`,
#      record an AmbiguityFlag(case_id, expected, majority) instead of failing anyone.
#   3. for properties: check_property on every (run, case) for the property's function; a violation
#      fails that run on that case even when the whole swarm agrees (shared-blind-spot catch).
#   4. agent tests (cross-test matrix) feed the reflector only.


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 else "{}"
