"""Fuzzer gaps from docs/TASKS.md: enum-like str params, bounded int domains, dict boundaries."""

from core.harness.spec_tests import SpecTests
from core.harness.strategies import boundary_examples, generate_cases
from core.models import FunctionSig, TestCase


def test_str_param_with_literals_gets_valid_values():
    sig = FunctionSig(name="late_fine", file="fines.py", params=["days_late: int", "item_type: str"], returns="float")
    cases = generate_cases(sig, n_fuzz=60, literals={"item_type": ["book", "dvd", "laptop"]})
    seeds = [c for c in cases if c.source == "seed"]
    assert {c.args[1] for c in seeds} >= {"book", "dvd", "laptop", ""}
    fuzz = [c for c in cases if c.source == "fuzz"]
    assert sum(c.args[1] in ("book", "dvd", "laptop") for c in fuzz) >= len(fuzz) // 4


def test_bounded_int_domain_gets_in_range_draws():
    sig = FunctionSig(name="letter_grade", file="grades.py", params=["score: int"], returns="str")
    cases = generate_cases(sig, n_fuzz=100)
    in_range = [c for c in cases if c.source == "fuzz" and 0 <= c.args[0] <= 100]
    assert len(in_range) >= 15
    seeds = {c.args[0] for c in cases if c.source == "seed"}
    assert {0, 1, -1, 49, 50, 51, 99, 100, 101} <= seeds


def test_dict_boundaries_are_dicts_not_none():
    ex = boundary_examples("dict[str, float]")
    assert ex[0] == {} and isinstance(ex[1], dict) and len(ex[1]) == 1
    ex2 = boundary_examples("int | None")
    assert None in ex2 and 0 in ex2


def test_spec_tests_literals_round_trip():
    st = SpecTests(subtask_id="s", cases=[TestCase(function="f", args=[], source="spec")], expected={}, properties=[], literals={"late_fine": {"item_type": ["book"]}})
    assert SpecTests.model_validate_json(st.model_dump_json()).literals == {"late_fine": {"item_type": ["book"]}}
