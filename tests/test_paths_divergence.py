"""Failure correlation: path divergence + locality + live-test timing (outline §5.7, TASKS row 2b)."""

import numpy as np

from core.llm.embeddings import fake_embed
from core.models import AgentIdentity, AgentRun, FunctionSig, Outcome, ReasoningStep, Subtask, TestCase
from core.paths.divergence import evidence_text, find_divergence, rank_candidates

DIM = 32


def _run(vectors, rid, files=None, live=None, assumptions=None) -> AgentRun:
    agent = AgentIdentity(id=f"agent-{rid}", provider="meta", model="x")
    steps = []
    for i, v in enumerate(vectors):
        steps.append(
            ReasoningStep(
                index=i,
                assumption=(assumptions or {}).get(i, f"a{i}"),
                action=f"do{i}",
                embedding=v,
                files_touched=(files or {}).get(i, []),
                live_results=(live or {}).get(i),
            )
        )
    return AgentRun(id=rid, subtask_id="s", agent=agent, sandbox_path="/tmp", steps=steps)


def _base(n=4, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.standard_normal(DIM).tolist() for _ in range(n)], rng


def _verified(base, rng, k):
    return [_run([(np.asarray(v) + rng.normal(0, 0.01, DIM)).tolist() for v in base], f"v{i}") for i in range(k)]


def _subtask() -> Subtask:
    return Subtask(
        index=0,
        title="tiers",
        spec="tier_for(amount) returns the tier",
        interface=[FunctionSig(name="tier_for", file="pricing/tiers.py", params=["amount: float"], returns="str")],
        files=["pricing/tiers.py", "pricing/__init__.py"],
    )


def _breaking():
    case = TestCase(id="c1", function="tier_for", args=[100.0], source="spec")
    return case, Outcome(ok=True, value="silver")


# ---- (a) path divergence ----


def test_path_signal_fires_with_three_verified_runs():
    base, rng = _base()
    verified = _verified(base, rng, 3)
    bad = [list(v) for v in base]
    bad[2] = (np.asarray(base[2]) + 5.0).tolist()
    div = find_divergence(_run(bad, "f"), verified, sigma=2.0)
    assert div is not None and div.step_index == 2
    assert div.signals == ["path"]


def test_path_signal_skipped_with_fewer_than_three_verified_runs():
    base, rng = _base()
    verified = _verified(base, rng, 2)
    bad = [list(v) for v in base]
    bad[2] = (np.asarray(base[2]) + 5.0).tolist()
    div = find_divergence(_run(bad, "f"), verified, sigma=2.0)
    # legacy fallback still returns the most distant step, but no signal is claimed for it
    assert div is not None and div.step_index == 2
    assert "path" not in div.signals


# ---- (b) locality ----


def test_locality_prefers_step_touching_failing_file_with_matching_assumption():
    base, _ = _base()
    case, expected = _breaking()
    sub = _subtask()
    failed_out = Outcome(ok=True, value="gold")
    text = evidence_text(case, expected, failed_out, sub)
    [evidence_vec] = fake_embed([text], dim=DIM)
    vecs = [list(v) for v in base]
    vecs[1] = evidence_vec  # step 1's assumption embeds exactly like the evidence
    failed = _run(vecs, "f", files={1: ["pricing/tiers.py"], 3: ["pricing/tiers.py"]})
    div = find_divergence(failed, [], sigma=2.0, breaking=(case, expected), subtask=sub, failed_outcome=failed_out, embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 1
    assert div.signals == ["locality"]
    assert div.breaking_case == case and div.majority_outcome == expected


def test_locality_file_match_alone_beats_unrelated_steps():
    base, _ = _base()
    case, expected = _breaking()
    failed = _run([list(v) for v in base], "f", files={2: ["pricing/tiers.py"], 3: ["README.md"]})
    div = find_divergence(failed, [], sigma=2.0, breaking=(case, expected), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 2
    assert div.signals == ["locality"]


def test_locality_for_pytest_case_uses_subtask_files():
    base, _ = _base()
    case = TestCase(id="pt_1", function="test_boundary", args=["test_spec.py::test_boundary"], source="pytest")
    failed = _run([list(v) for v in base], "f", files={0: ["docs/x.md"], 3: ["pricing/__init__.py"]})
    div = find_divergence(failed, [], sigma=2.0, breaking=(case, Outcome(ok=True, value="passed")), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 3
    assert "locality" in div.signals


# ---- (c) live-test timing ----


def test_live_timing_picks_step_where_case_first_went_red():
    base, _ = _base()
    case, expected = _breaking()
    live = {0: {"c1": True}, 2: {"c1": False}, 3: {"c1": False}}
    failed = _run([list(v) for v in base], "f", live=live)
    div = find_divergence(failed, [], sigma=2.0, breaking=(case, expected), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 2
    assert div.signals == ["live_timing"]


def test_live_timing_never_green_picks_earliest_local_step():
    base, _ = _base()
    case, expected = _breaking()
    live = {3: {"c1": False}}
    failed = _run([list(v) for v in base], "f", live=live, files={1: ["pricing/tiers.py"], 2: ["pricing/tiers.py"]})
    div = find_divergence(failed, [], sigma=2.0, breaking=(case, expected), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 1
    assert "live_timing" in div.signals and "locality" in div.signals


# ---- combination ----


def test_path_plus_locality_agree_and_both_are_recorded():
    base, rng = _base()
    verified = _verified(base, rng, 4)
    case, expected = _breaking()
    bad = [list(v) for v in base]
    bad[2] = (np.asarray(base[2]) + 5.0).tolist()
    failed = _run(bad, "f", files={2: ["pricing/tiers.py"]})
    div = find_divergence(failed, verified, sigma=2.0, breaking=(case, expected), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 2
    assert div.signals == ["path", "locality"]
    assert div.consensus_step_texts  # consensus texts at that phase come along for the reflector


def test_live_timing_outranks_a_lone_path_outlier():
    base, rng = _base()
    verified = _verified(base, rng, 4)
    case, expected = _breaking()
    bad = [list(v) for v in base]
    bad[1] = (np.asarray(base[1]) + 5.0).tolist()
    failed = _run(bad, "f", live={0: {"c1": True}, 3: {"c1": False}})
    div = find_divergence(failed, verified, sigma=2.0, breaking=(case, expected), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert div is not None and div.step_index == 3
    assert div.signals == ["live_timing"]


def test_rank_candidates_orders_by_score_and_covers_every_step():
    base, rng = _base()
    verified = _verified(base, rng, 4)
    case, expected = _breaking()
    bad = [list(v) for v in base]
    bad[2] = (np.asarray(base[2]) + 5.0).tolist()
    failed = _run(bad, "f", files={2: ["pricing/tiers.py"], 0: ["pricing/tiers.py"]})
    ranked = rank_candidates(failed, verified, sigma=2.0, breaking=(case, expected), subtask=_subtask(), embed=lambda t: fake_embed(t, dim=DIM))
    assert [d.step_index for d in ranked][:2] == [2, 0]
    assert len(ranked) == 4
    assert all(d.run_id == "f" for d in ranked)


def test_no_steps_returns_none():
    assert find_divergence(_run([], "f"), [], sigma=2.0) is None
