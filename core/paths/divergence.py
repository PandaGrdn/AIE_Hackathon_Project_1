"""Failure correlation: where in a failed run's reasoning did it go wrong? Owner: Ethan Li.

Three signals are combined (outline §5.7) and the ones that pointed at the chosen step are recorded
in `Divergence.signals`:

  path         Paths have different lengths, so each step is mapped to a phase in [0, 1] by
               index / path length. The consensus path is the centroid of the verified runs' step
               embeddings per phase bin; a step is off-consensus when its distance to the centroid
               exceeds the verified runs' own spread by `sigma` standard deviations. Needs >= 3
               verified runs; with fewer the signal is skipped (the spread is meaningless).
  locality     Steps whose `files_touched` include the failing function's file (or, for a pytest
               case, any file in the subtask) and whose assumption embedding is close to an
               embedding of "function + failing input + expected vs actual".
  live_timing  If the agent ran `run_spec_tests`, the step right before the failing case first went
               red (after having been green); if it was never green, the earliest local step.

`find_divergence` keeps its original signature; the extra signals need `breaking` (from
`crosstest.breaking_case`) and `subtask`. `rank_candidates` returns every step ordered by score so
the counterfactual replay (`core.reflect.replay`) can try the next candidates.
"""

from __future__ import annotations

import json
from typing import Callable

import numpy as np

from core.config import settings
from core.llm.embeddings import cosine, embed_texts
from core.models import AgentRun, CrossTestResult, Divergence, Outcome, Subtask, TestCase

N_BINS = 8
MIN_VERIFIED_FOR_PATH = 3
LOCALITY_SIM_THRESHOLD = 0.7  # cosine between a step's embedding and the failure evidence
PATH_WEIGHT = 1.0
LOCALITY_FILE_WEIGHT = 1.0
LIVE_WEIGHT = 1.5
SIGNAL_ORDER = ("path", "locality", "live_timing")

Embed = Callable[[list[str]], list[list[float]]]


def _phase_bin(i: int, n: int) -> int:
    if n <= 1:
        return 0
    return min(N_BINS - 1, int(i / n * N_BINS))


def consensus_path(verified: list[AgentRun]) -> dict[int, tuple[np.ndarray, float, float, list[str]]]:
    """phase_bin -> (centroid, mean_dist, std_dist, step_texts)."""
    bins: dict[int, list[tuple[np.ndarray, str]]] = {b: [] for b in range(N_BINS)}
    for run in verified:
        n = len(run.steps)
        for s in run.steps:
            if s.embedding is not None:
                bins[_phase_bin(s.index, n)].append((np.asarray(s.embedding), s.as_text()))
    out = {}
    for b, items in bins.items():
        if not items:
            continue
        vecs = np.stack([v for v, _ in items])
        centroid = vecs.mean(axis=0)
        dists = np.linalg.norm(vecs - centroid, axis=1)
        out[b] = (centroid, float(dists.mean()), float(dists.std() + 1e-6), [t for _, t in items])
    return out


def _fmt(o: Outcome | None) -> str:
    if o is None:
        return "unknown"
    if o.timed_out:
        return "timed out"
    if not o.ok:
        return f"raised {o.exception}"
    return o.value if isinstance(o.value, str) else json.dumps(o.value, default=str)


def evidence_text(case: TestCase, expected: Outcome | None, failed_out: Outcome | None, subtask: Subtask | None) -> str:
    """The failure as one sentence, embedded and compared with each step's assumption (locality signal)."""
    if case.source in ("pytest", "regression"):
        node = case.args[0] if case.args else case.function
        return f"test {node} expected to pass but {_fmt(failed_out)}"
    sig = next((s for s in (subtask.interface if subtask else []) if s.name == case.function), None)
    where = f" in {sig.file}" if sig else ""
    return f"{case.function}{where} on input {json.dumps(case.args, default=str)} expected {_fmt(expected)} but got {_fmt(failed_out)}"


def target_files(case: TestCase, subtask: Subtask | None) -> set[str]:
    """Files a step must touch to count as local to the failure."""
    if subtask is None:
        return set()
    for sig in subtask.interface:
        if sig.name == case.function and case.source not in ("pytest", "regression"):
            return {sig.file}
    return set(subtask.files)


def _path_scores(failed: AgentRun, verified: list[AgentRun], sigma: float) -> tuple[dict[int, float], dict[int, bool], dict[int, list[str]]]:
    """step_index -> distance to its phase centroid, -> crossed the threshold, -> consensus texts."""
    cons = consensus_path(verified)
    n = len(failed.steps)
    dist: dict[int, float] = {}
    crossed: dict[int, bool] = {}
    texts: dict[int, list[str]] = {}
    enough = len(verified) >= MIN_VERIFIED_FOR_PATH
    for s in failed.steps:
        b = _phase_bin(s.index, n)
        if s.embedding is None or b not in cons:
            continue
        centroid, mean_d, std_d, t = cons[b]
        d = float(np.linalg.norm(np.asarray(s.embedding) - centroid))
        dist[s.index] = d
        crossed[s.index] = enough and (d - mean_d) / std_d > sigma
        texts[s.index] = t[:5]
    return dist, crossed, texts


def _locality_scores(failed: AgentRun, case: TestCase, expected: Outcome | None, failed_out: Outcome | None, subtask: Subtask | None, embed: Embed) -> tuple[dict[int, float], set[int]]:
    """step_index -> locality score (file hit + evidence similarity); the set of steps touching a target file."""
    targets = target_files(case, subtask)
    file_hits = {s.index for s in failed.steps if any(f in targets for f in s.files_touched)}
    scores: dict[int, float] = {}
    evidence_vec: list[float] | None = None
    if any(s.embedding is not None for s in failed.steps):
        try:
            [evidence_vec] = embed([evidence_text(case, expected, failed_out, subtask)])
        except Exception:  # embeddings down: locality falls back to file hits only
            evidence_vec = None
    for s in failed.steps:
        score = LOCALITY_FILE_WEIGHT if s.index in file_hits else 0.0
        if evidence_vec is not None and s.embedding is not None and len(s.embedding) == len(evidence_vec):
            sim = cosine(evidence_vec, s.embedding)
            if sim >= LOCALITY_SIM_THRESHOLD:
                score += sim
        if score > 0:
            scores[s.index] = score
    return scores, file_hits


def _live_nominee(failed: AgentRun, case_id: str, file_hits: set[int]) -> int | None:
    seen_green = False
    for s in failed.steps:
        if not s.live_results or case_id not in s.live_results:
            continue
        if s.live_results[case_id]:
            seen_green = True
            continue
        if seen_green:
            return s.index  # the step right before the case first went red
        local = [i for i in sorted(file_hits) if i <= s.index]
        return local[0] if local else s.index  # never green: earliest local step
    return None


def rank_candidates(
    failed: AgentRun,
    verified: list[AgentRun],
    sigma: float | None = None,
    *,
    result: CrossTestResult | None = None,
    breaking: tuple[TestCase, Outcome] | None = None,
    subtask: Subtask | None = None,
    failed_outcome: Outcome | None = None,
    embed: Embed = embed_texts,
) -> list[Divergence]:
    """Every step of `failed` as a Divergence, best candidate first. `signals` on each names the signals
    that pointed at that step; a step nobody pointed at has `signals == []`."""
    sigma = settings.divergence_sigma if sigma is None else sigma
    if not failed.steps:
        return []
    if breaking is None and result is not None:
        from core.harness.crosstest import breaking_case  # lazy: crosstest pulls in the LLM clients

        breaking = breaking_case(result, failed.id)
    case, expected = breaking if breaking else (None, None)
    if failed_outcome is None and result is not None and case is not None:
        failed_outcome = result.outcomes.get(failed.id, {}).get(case.id)

    dist, crossed, texts = _path_scores(failed, verified, sigma)
    loc_scores: dict[int, float] = {}
    file_hits: set[int] = set()
    live_idx: int | None = None
    if case is not None:
        loc_scores, file_hits = _locality_scores(failed, case, expected, failed_outcome, subtask, embed)
        live_idx = _live_nominee(failed, case.id, file_hits)

    scored: list[tuple[float, int, Divergence]] = []
    first_cross = next((s.index for s in failed.steps if crossed.get(s.index)), None)
    for s in failed.steps:
        score, signals = 0.0, []
        if crossed.get(s.index):
            score += PATH_WEIGHT if s.index == first_cross else PATH_WEIGHT / 2
            signals.append("path")
        if s.index in loc_scores:
            score += loc_scores[s.index]
            signals.append("locality")
        if live_idx == s.index:
            score += LIVE_WEIGHT
            signals.append("live_timing")
        signals.sort(key=SIGNAL_ORDER.index)
        d = dist.get(s.index, 0.0)
        div = Divergence(
            run_id=failed.id, step_index=s.index, step_text=s.as_text(), distance=d,
            consensus_step_texts=texts.get(s.index, []), breaking_case=case, majority_outcome=expected, signals=signals,
        )
        # no signal: fall back to raw distance from consensus so the legacy "most distant step" still ranks first
        scored.append((score, d, div))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2].step_index))
    return [d for _, _, d in scored]


def find_divergence(
    failed: AgentRun,
    verified: list[AgentRun],
    sigma: float | None = None,
    *,
    result: CrossTestResult | None = None,
    breaking: tuple[TestCase, Outcome] | None = None,
    subtask: Subtask | None = None,
    failed_outcome: Outcome | None = None,
    embed: Embed = embed_texts,
) -> Divergence | None:
    """The best candidate step, or None when the run logged no steps (or nothing can be scored at all)."""
    ranked = rank_candidates(failed, verified, sigma, result=result, breaking=breaking, subtask=subtask, failed_outcome=failed_outcome, embed=embed)
    if not ranked:
        return None
    best = ranked[0]
    if not best.signals and best.distance == 0.0 and not verified:
        return None  # nothing to go on: no consensus, no evidence
    return best
