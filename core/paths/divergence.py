"""Reasoning-path alignment and divergence detection. Owner: C.

Paths have different lengths, so each step is mapped to a phase in [0, 1] by its
index / path length. The consensus path is the centroid of the verified runs'
step embeddings in each phase bin. A failed run's divergence point is the first
step whose distance to its phase centroid exceeds the verified runs' own spread
by `sigma` standard deviations.
"""

from __future__ import annotations

import numpy as np

from core.config import settings
from core.models import AgentRun, Divergence

N_BINS = 8


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


def find_divergence(failed: AgentRun, verified: list[AgentRun], sigma: float | None = None) -> Divergence | None:
    sigma = settings.divergence_sigma if sigma is None else sigma
    cons = consensus_path(verified)
    if not cons:
        return None
    n = len(failed.steps)
    best: Divergence | None = None
    for s in failed.steps:
        if s.embedding is None:
            continue
        b = _phase_bin(s.index, n)
        if b not in cons:
            continue
        centroid, mean_d, std_d, texts = cons[b]
        d = float(np.linalg.norm(np.asarray(s.embedding) - centroid))
        z = (d - mean_d) / std_d
        if z > sigma:
            return Divergence(run_id=failed.id, step_index=s.index, step_text=s.as_text(), distance=d, consensus_step_texts=texts[:5])
        # Fall back to the most distant step if nothing crosses the threshold.
        if best is None or d > best.distance:
            best = Divergence(run_id=failed.id, step_index=s.index, step_text=s.as_text(), distance=d, consensus_step_texts=texts[:5])
    return best
