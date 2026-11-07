from core.harness.equivalence import equivalent
from core.models import Outcome


def test_float_tolerance():
    assert equivalent(Outcome(ok=True, value=0.1 + 0.2), Outcome(ok=True, value=0.3))


def test_sets_unordered():
    assert equivalent(Outcome(ok=True, value={1, 2}), Outcome(ok=True, value={2, 1}))


def test_lists_ordered():
    assert not equivalent(Outcome(ok=True, value=[1, 2]), Outcome(ok=True, value=[2, 1]))


def test_same_exception_type_agrees():
    assert equivalent(Outcome(ok=False, exception="ValueError"), Outcome(ok=False, exception="ValueError"))
    assert not equivalent(Outcome(ok=False, exception="ValueError"), Outcome(ok=False, exception="TypeError"))


def test_timeout_never_agrees():
    assert not equivalent(Outcome(ok=False, timed_out=True), Outcome(ok=False, timed_out=True))
