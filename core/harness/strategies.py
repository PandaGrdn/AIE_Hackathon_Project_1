"""Build hypothesis strategies from pinned type hints and generate test cases. Owner: Ethan Li.

Fuzz cases have no expected value; the swarm majority is their oracle. Known gaps and how
they are closed (docs/TASKS.md "fuzzer gaps"):
  (a) enum-like `str` params: the spec author supplies valid literals; half the draws use them
  (b) bounded `int`/`float` domains: a second, narrow strategy keeps half the draws near the
      small boundaries specs name (0..100 scores, 15-minute grace, 100/1000 tiers)
  (c) `dict[...]` boundaries are `{}` and a one-entry dict, not `None`
"""

from __future__ import annotations

from hypothesis import strategies as st

from core.models import FunctionSig, TestCase

_BASIC = {
    "int": st.one_of(st.integers(min_value=-10_000, max_value=10_000), st.integers(min_value=-5, max_value=150)),
    "float": st.one_of(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-1.0, max_value=1500.0, allow_nan=False, allow_infinity=False),
    ),
    "str": st.text(max_size=40),
    "bool": st.booleans(),
    "Decimal": st.decimals(min_value=-1e6, max_value=1e6, places=2, allow_nan=False, allow_infinity=False),
}


def strategy_for(annotation: str, literals: list | None = None) -> st.SearchStrategy:
    a = annotation.replace(" ", "")
    if literals and a == "str":
        return st.one_of(st.sampled_from(list(literals)), st.text(max_size=20))
    if a in _BASIC:
        return _BASIC[a]
    if a.startswith("list[") and a.endswith("]"):
        return st.lists(strategy_for(a[5:-1]), max_size=20)
    if a.startswith("dict[") and a.endswith("]"):
        k, v = a[5:-1].split(",", 1)
        return st.dictionaries(strategy_for(k), strategy_for(v), max_size=10)
    if "|" in a:  # e.g. int | None
        parts = [p for p in a.split("|")]
        return st.one_of(*[st.none() if p == "None" else strategy_for(p) for p in parts])
    return st.one_of(st.integers(), st.text(max_size=20), st.none())  # Any


def boundary_examples(annotation: str, literals: list | None = None) -> list:
    a = annotation.replace(" ", "")
    if a == "int":
        return [0, 1, -1, 49, 50, 51, 59, 60, 99, 100, 101]
    if a == "float":
        return [0.0, -0.0, 1.0, 100.0, 99.999, 100.001, 1000.0, 1000.01]
    if a == "str":
        if literals:
            lits = [str(x) for x in literals]
            return lits + ["", lits[0].capitalize() if lits[0] and lits[0].capitalize() != lits[0] else lits[0].upper(), f" {lits[0]}"]
        return ["", " ", "a"]
    if a.startswith("list[") and a.endswith("]"):
        inner = boundary_examples(a[5:-1])
        return [[], [inner[0]] if inner else []]
    if a.startswith("dict[") and a.endswith("]"):
        k, v = a[5:-1].split(",", 1)
        ks, vs = boundary_examples(k), boundary_examples(v)
        key = ks[-1] if ks else "k"
        val = vs[0] if vs else 0
        return [{}, {key: val}]
    if "|" in a:
        out: list = []
        for p in a.split("|"):
            out += [None] if p == "None" else boundary_examples(p)
        return out
    return []


def _parse_params(sig: FunctionSig) -> list[tuple[str, str]]:
    out = []
    for p in sig.params:
        if ":" in p:
            name, annot = p.split(":", 1)
            out.append((name.strip(), annot.strip()))
        else:
            out.append((p.strip(), "Any"))
    return out


def generate_cases(sig: FunctionSig, n_fuzz: int = 200, seed: int = 0, literals: dict[str, list] | None = None) -> list[TestCase]:
    """Seed/boundary cases first, then `n_fuzz` random draws. Deterministic for a given seed.
    `literals` maps a str param name to its valid values (from the spec author)."""
    from hypothesis import HealthCheck, Phase, given
    from hypothesis import settings as hsettings

    literals = literals or {}
    params = _parse_params(sig)
    cases: list[TestCase] = []

    # boundary combos: vary one param at a time, others at their first boundary (or 0)
    bounds = [boundary_examples(a, literals.get(n)) for n, a in params]
    firsts = [(b or [None])[0] for b in bounds]
    seen: set[str] = set()
    for i, b in enumerate(bounds):
        for ex in b:
            args = list(firsts)
            args[i] = ex
            key = repr(args)
            if key not in seen:
                seen.add(key)
                cases.append(TestCase(function=sig.name, args=args, source="seed"))

    # fuzz
    collected: list[list] = []

    @hsettings(max_examples=n_fuzz, database=None, derandomize=True, phases=[Phase.generate], suppress_health_check=list(HealthCheck))
    @given(st.tuples(*[strategy_for(a, literals.get(n)) for n, a in params]))
    def _collect(t):
        collected.append(list(t))

    try:
        _collect()
    except Exception:
        pass
    cases += [TestCase(function=sig.name, args=a, source="fuzz") for a in collected]
    return cases
