"""Build hypothesis strategies from pinned type hints and generate test cases. Owner: C."""

from __future__ import annotations

from hypothesis import strategies as st

from core.models import FunctionSig, TestCase

_BASIC = {
    "int": st.integers(min_value=-10_000, max_value=10_000),
    "float": st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    "str": st.text(max_size=40),
    "bool": st.booleans(),
    "Decimal": st.decimals(min_value=-1e6, max_value=1e6, places=2, allow_nan=False, allow_infinity=False),
}


def strategy_for(annotation: str) -> st.SearchStrategy:
    a = annotation.replace(" ", "")
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


def boundary_examples(annotation: str) -> list:
    a = annotation.replace(" ", "")
    if a == "int":
        return [0, 1, -1, 100, 99, 101]
    if a == "float":
        return [0.0, -0.0, 1.0, 100.0, 99.999, 100.001]
    if a == "str":
        return ["", " ", "a"]
    if a.startswith("list["):
        return [[], [boundary_examples(a[5:-1])[0]] if boundary_examples(a[5:-1]) else []]
    return []


def generate_cases(sig: FunctionSig, n_fuzz: int = 200, seed: int = 0) -> list[TestCase]:
    """Seed/boundary cases first, then `n_fuzz` random draws. Deterministic for a given seed."""
    from hypothesis import HealthCheck, Phase, given, settings as hsettings

    annots = [p.split(":", 1)[1].strip() if ":" in p else "Any" for p in sig.params]
    cases: list[TestCase] = []

    # boundary combos: vary one param at a time, others at their first boundary (or 0)
    firsts = [(boundary_examples(a) or [None])[0] for a in annots]
    for i, a in enumerate(annots):
        for ex in boundary_examples(a):
            args = list(firsts)
            args[i] = ex
            cases.append(TestCase(function=sig.name, args=args, source="seed"))

    # fuzz
    collected: list[list] = []

    @hsettings(max_examples=n_fuzz, database=None, derandomize=True, phases=[Phase.generate], suppress_health_check=list(HealthCheck))
    @given(st.tuples(*[strategy_for(a) for a in annots]))
    def _collect(t):
        collected.append(list(t))

    try:
        _collect()
    except Exception:
        pass
    cases += [TestCase(function=sig.name, args=a, source="fuzz") for a in collected]
    return cases
