import json

import httpx
import numpy as np
import pytest

from core.llm.decisions import (
    KIND_OPTIONS,
    SubQDecisions,
    SubQError,
    build_localization_questions,
    build_localization_state,
    locate_divergence_subq,
)
from core.models import AgentIdentity, AgentRun, Outcome, ReasoningStep
from core.models import TestCase as Case

SPEC = (
    "pricing/tiers.py - tier_for(amount: float) -> str: returns \"basic\" for amounts under 100, "
    "\"plus\" for amounts from 100 up to and including 1000, and \"pro\" for amounts over 1000. "
    "Amounts are USD. Negative amounts raise ValueError."
)

FAILED_STEPS = [
    ("The function lives in pricing/tiers.py and takes a float amount.", "read the spec and the package layout", []),
    ("Negative amounts must raise ValueError before any tier logic.", "add the negative-amount guard", ["pricing/tiers.py"]),
    ("Plus covers amounts over 100 up to 1000, so exactly 100 is still basic.", "write `if amount <= 100: return 'basic'`", ["pricing/tiers.py"]),
    ("Amounts over 1000 are pro, 1000 itself is plus.", "write `elif amount <= 1000: return 'plus'` and `else: return 'pro'`", ["pricing/tiers.py"]),
    ("The implementation matches the spec.", "run a quick smoke check on 50, 500, 5000", ["pricing/tiers.py"]),
]

VERIFIED_STEPS = [
    ("The function lives in pricing/tiers.py and takes a float amount.", "read the spec", []),
    ("Negative amounts raise ValueError.", "add the guard", ["pricing/tiers.py"]),
    ("Basic is strictly under 100; 100 itself is plus (lower edge inclusive).", "write `if amount < 100: return 'basic'`", ["pricing/tiers.py"]),
    ("Plus runs up to and including 1000; above that is pro.", "write `elif amount <= 1000: return 'plus'`", ["pricing/tiers.py"]),
    ("Done; boundaries 100 and 1000 checked.", "smoke check 99.99, 100, 1000, 1000.01", ["pricing/tiers.py"]),
]


def _run(rid: str, rows, embeddings=None) -> AgentRun:
    agent = AgentIdentity(id=f"agent-{rid}", provider="meta", model="x")
    steps = [
        ReasoningStep(index=i, assumption=a, action=act, files_touched=f, embedding=None if embeddings is None else embeddings[i])
        for i, (a, act, f) in enumerate(rows)
    ]
    return AgentRun(id=rid, subtask_id="tiers", agent=agent, sandbox_path="/tmp", steps=steps)


def _pricing_case():
    failed = _run("f1", FAILED_STEPS)
    verified = [_run(f"v{i}", VERIFIED_STEPS) for i in range(3)]
    case = Case(id="c100", function="tier_for", args=[100.0], source="seed")
    breaking = (case, Outcome(ok=True, value="plus"))
    return failed, verified, breaking, Outcome(ok=True, value="basic")


def _answers(choice="S2", kind="spec_ambiguity", gen=0.98):
    probs = {f"S{i}": 0.005 for i in range(5)}
    probs["none"] = 0.0
    if choice in probs:
        probs[choice] = 0.98
    return {
        "root_step": {"type": "choice", "choice": choice, "probabilities": probs, "confidence": probs.get(choice, 0.9)},
        "kind": {"type": "choice", "choice": kind, "probabilities": {"bug": 0.2, "spec_ambiguity": 0.79, "acceptable_variation": 0.01}, "confidence": 0.79},
        "generalizes": {"type": "noul", "noul": gen},
    }


def _client(handler, **kw) -> SubQDecisions:
    return SubQDecisions(api_key="test-key", url="https://subq.test/v1/systemone", transport=httpx.MockTransport(handler), backoff=0.0, **kw)


def _ok(answers):
    return httpx.Response(200, json={"model": "subq-snap-1.1", "answers": answers, "usage": {"input_tokens": 900, "output_tokens": 0}})


# ---------- builders ----------


def test_state_format_and_labels():
    failed, verified, breaking, fo = _pricing_case()
    state, labels = build_localization_state(SPEC, failed, verified, breaking, fo, hidden_test_output="FAILED test_tier_boundaries")
    assert labels == ["S0", "S1", "S2", "S3", "S4"]
    for header in ("SUBTASK SPEC", "FAILED AGENT REASONING PATH", "CONSENSUS at each phase", "FAILING EVIDENCE"):
        assert header in state
    assert state.index("SUBTASK SPEC") < state.index("FAILED AGENT") < state.index("CONSENSUS") < state.index("FAILING EVIDENCE")
    assert "[S2] assumption: Plus covers amounts over 100 up to 1000" in state
    # no embeddings anywhere -> consensus grouped by step index, deduped across the 3 identical runs
    assert "step 2 (aligns with S2" in state
    assert state.count("Basic is strictly under 100") == 1
    assert "tier_for(100.0)" in state
    assert 'expected / majority outcome: returned "plus"' in state
    assert 'failed agent\'s outcome: returned "basic"' in state
    assert "FAILED test_tier_boundaries" in state
    # deterministic
    assert build_localization_state(SPEC, failed, verified, breaking, fo, hidden_test_output="FAILED test_tier_boundaries")[0] == state


def test_state_uses_embedding_phase_bins_when_available():
    rng = np.random.default_rng(0)
    embs = [rng.standard_normal(4).tolist() for _ in range(5)]
    failed = _run("f1", FAILED_STEPS, embs)
    verified = [_run(f"v{i}", VERIFIED_STEPS, embs) for i in range(3)]
    state, _ = build_localization_state(SPEC, failed, verified, None, None)
    assert "phase " in state and "aligns with S2" in state
    assert "breaking case: none recorded" in state


def test_state_trims_consensus_before_spec():
    failed, verified, breaking, fo = _pricing_case()
    full, _ = build_localization_state(SPEC, failed, verified, breaking, fo)
    capped, _ = build_localization_state(SPEC, failed, verified, breaking, fo, max_chars=len(full) - 50)
    assert len(capped) <= len(full) - 50
    assert SPEC in capped  # spec intact, consensus trimmed
    long_spec = "x" * 10_000
    s, _ = build_localization_state(long_spec, failed, verified, breaking, fo)
    assert "x" * 4_100 not in s and "[truncated]" in s


def test_questions_shape():
    q = build_localization_questions(["S0", "S1", "S2"])
    assert set(q) == {"root_step", "kind", "generalizes"}
    assert q["root_step"]["type"] == "choice" and list(q["root_step"]["criteria"]) == ["S0", "S1", "S2", "none"]
    assert q["kind"]["type"] == "choice" and set(q["kind"]["criteria"]) == {"bug", "spec_ambiguity", "acceptable_variation"}
    assert q["generalizes"]["type"] == "noul" and set(q["generalizes"]["criteria"]) == {"true", "false"}


# ---------- client + localization over MockTransport ----------


def test_request_body_and_divergence_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return _ok(_answers())

    failed, verified, breaking, fo = _pricing_case()
    div = locate_divergence_subq(SPEC, failed, verified, breaking, fo, client=_client(handler))
    body = seen["body"]
    assert seen["auth"] == "Bearer test-key"
    assert body["model"] == "subq-snap-1.1"
    assert isinstance(body["state"], str)
    assert "[S2] assumption: Plus covers amounts over 100 up to 1000" in body["state"]
    assert "FAILING EVIDENCE" in body["state"] and "tier_for(100.0)" in body["state"]
    qs = body["questions"]
    assert list(qs["root_step"]["criteria"]) == ["S0", "S1", "S2", "S3", "S4", "none"]
    assert qs["kind"]["criteria"] == KIND_OPTIONS
    assert set(qs["generalizes"]["criteria"]) == {"true", "false"}

    assert div is not None
    assert div.run_id == "f1" and div.step_index == 2
    assert div.step_text.startswith("assumption: Plus covers amounts over 100")
    assert div.signals == ["subq"] and div.distance == 0.0
    assert any("Basic is strictly under 100" in t for t in div.consensus_step_texts)
    assert div.step_probabilities["S2"] == pytest.approx(0.98) and "none" not in div.step_probabilities
    assert div.kind == "spec_ambiguity" and div.kind_confidence == pytest.approx(0.79)
    assert div.generalizes == pytest.approx(0.98)
    assert div.breaking_case.id == "c100" and div.majority_outcome.value == "plus"


def test_subq_and_path_agree_adds_path_signal():
    rng = np.random.default_rng(1)
    base = [rng.standard_normal(8).tolist() for _ in range(5)]
    verified = [_run(f"v{i}", VERIFIED_STEPS, [(np.asarray(v) + rng.normal(0, 0.01, 8)).tolist() for v in base]) for i in range(4)]
    bad = [list(v) for v in base]
    bad[2] = (np.asarray(base[2]) + 5.0).tolist()
    failed = _run("f1", FAILED_STEPS, bad)
    div = locate_divergence_subq(SPEC, failed, verified, client=_client(lambda r: _ok(_answers())))
    assert div.step_index == 2 and div.signals == ["subq", "path"] and div.distance > 0


def test_none_falls_back_to_find_divergence():
    rng = np.random.default_rng(2)
    base = [rng.standard_normal(8).tolist() for _ in range(5)]
    verified = [_run(f"v{i}", VERIFIED_STEPS, [(np.asarray(v) + rng.normal(0, 0.01, 8)).tolist() for v in base]) for i in range(4)]
    bad = [list(v) for v in base]
    bad[3] = (np.asarray(base[3]) + 5.0).tolist()
    failed = _run("f1", FAILED_STEPS, bad)
    div = locate_divergence_subq(SPEC, failed, verified, client=_client(lambda r: _ok(_answers(choice="none"))))
    assert div.step_index == 3 and div.signals == ["path"]
    assert div.kind == "spec_ambiguity"  # SubQ's other answers are still attached
    assert locate_divergence_subq(SPEC, failed, verified, client=_client(lambda r: _ok(_answers(choice="none"))), fallback=False) is None


def test_403_raises_subq_error():
    def handler(req):
        return httpx.Response(403, json={"detail": {"error_type": "forbidden", "message": "key lacks access"}})

    with pytest.raises(SubQError) as ei:
        _client(handler).decide("s", build_localization_questions(["S0"]))
    assert ei.value.status == 403 and ei.value.error_type == "forbidden" and "lacks access" in ei.value.message

    failed, verified, breaking, fo = _pricing_case()
    with pytest.raises(SubQError):
        locate_divergence_subq(SPEC, failed, verified, breaking, fo, client=_client(handler), fallback=False)
    # with fallback, find_divergence finds nothing (no embeddings) -> None, no exception
    assert locate_divergence_subq(SPEC, failed, verified, breaking, fo, client=_client(handler)) is None


def test_429_then_200_retries_once():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"detail": {"error_type": "rate_limited", "message": "slow down"}})
        return _ok(_answers())

    answers = _client(handler).decide("s", build_localization_questions(["S0"]))
    assert len(calls) == 2 and answers["root_step"]["choice"] == "S2"

    calls.clear()

    def always_500(req):
        calls.append(1)
        return httpx.Response(503, json={"detail": {"error_type": "unavailable", "message": "down"}})

    with pytest.raises(SubQError) as ei:
        _client(always_500).decide("s", {})
    assert len(calls) == 2 and ei.value.status == 503


def test_no_key_falls_back_without_network():
    def handler(req):  # pragma: no cover - must not be called
        raise AssertionError("network used without a key")

    rng = np.random.default_rng(3)
    base = [rng.standard_normal(8).tolist() for _ in range(5)]
    verified = [_run(f"v{i}", VERIFIED_STEPS, [(np.asarray(v) + rng.normal(0, 0.01, 8)).tolist() for v in base]) for i in range(4)]
    bad = [list(v) for v in base]
    bad[1] = (np.asarray(base[1]) + 5.0).tolist()
    failed = _run("f1", FAILED_STEPS, bad)
    client = SubQDecisions(api_key="", transport=httpx.MockTransport(handler))
    assert not client.is_configured()
    case = Case(function="tier_for", args=[100.0])
    div = locate_divergence_subq(SPEC, failed, verified, (case, Outcome(ok=True, value="plus")), client=client)
    assert div.step_index == 1 and div.signals == ["path"] and div.breaking_case is case
    with pytest.raises(SubQError):
        locate_divergence_subq(SPEC, failed, verified, client=client, fallback=False)


# ---------- live ----------


@pytest.mark.skipif(not SubQDecisions.is_configured(), reason="no SUBQ_API_KEY")
def test_live_pricing_root_step():
    failed, verified, breaking, fo = _pricing_case()
    client = SubQDecisions()
    div = locate_divergence_subq(
        SPEC, failed, verified, breaking, fo,
        hidden_test_output="FAILED tests/test_tiers.py::test_lower_plus_edge - AssertionError: assert 'basic' == 'plus'",
        client=client, fallback=False,
    )
    print(f"\nlive: step={div.step_index} probs={div.step_probabilities} kind={div.kind} "
          f"kind_conf={div.kind_confidence} generalizes={div.generalizes} usage={client.last_usage}")
    assert div is not None and div.step_index == 2 and "subq" in div.signals
    assert div.step_probabilities["S2"] > 0.5
