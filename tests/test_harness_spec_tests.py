from core.harness.spec_tests import SpecProperty, check_property


def test_property_predicate_runs_on_values():
    p = SpecProperty(name="total_le_subtotal", function="quote", predicate_src="lambda args, result: result['total'] <= result['subtotal']", description="")
    assert check_property(p, [10.0, 2], {"subtotal": 20.0, "total": 19.0})
    assert not check_property(p, [10.0, 2], {"subtotal": 20.0, "total": 21.0})


def test_property_predicate_is_sandboxed():
    p = SpecProperty(name="bad", function="f", predicate_src="lambda a, r: __import__('os')", description="")
    assert check_property(p, [], None) is False
