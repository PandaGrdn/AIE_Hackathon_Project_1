from core.harness.majority import trust_weighted_majority
from core.models import Outcome


def test_plain_majority():
    outs = {"a": Outcome(ok=True, value=2.0), "b": Outcome(ok=True, value=2.0), "c": Outcome(ok=True, value=3)}
    mv = trust_weighted_majority("case", outs, {})
    assert mv.group_run_ids == ["a", "b"]
    assert abs(mv.agreement - 2 / 3) < 1e-9


def test_trust_weight_flips_result():
    outs = {"a": Outcome(ok=True, value=2.0), "b": Outcome(ok=True, value=2.0), "c": Outcome(ok=True, value=3)}
    mv = trust_weighted_majority("case", outs, {"a": 0.1, "b": 0.1, "c": 0.9})
    assert mv.group_run_ids == ["c"]
