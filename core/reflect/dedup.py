"""Near-duplicate lessons bump hit_count and widen their failure region instead of inserting. Owner: Ethan Li.

Confirmation (outline §5.7): a lesson whose divergence step was confirmed by counterfactual replay
creates its failure region immediately. An unconfirmed lesson is stored with `confidence <= 0.3` and
gets no region until a second hit lands on it; the second hit is the confirmation.
"""

from __future__ import annotations

import numpy as np

from core.config import settings
from core.llm.embeddings import cosine
from core.models import FailureRegion, Lesson
from core.store.base import Store

BASE_RADIUS = 0.35  # euclidean on unit vectors; tune on real embeddings (see note at the bottom)
RADIUS_GROWTH = 0.05
UNCONFIRMED_MAX_CONFIDENCE = 0.3


def dedup_or_insert(lesson: Lesson, store: Store, confirmed: bool = True) -> Lesson:
    if not confirmed:
        lesson.confidence = min(lesson.confidence, UNCONFIRMED_MAX_CONFIDENCE)
    for existing in store.list_lessons():
        if existing.embedding and lesson.embedding and cosine(existing.embedding, lesson.embedding) >= settings.lesson_dedup_similarity:
            existing.hit_count += 1
            existing.confidence = max(existing.confidence, lesson.confidence)
            store.upsert_lesson(existing)
            _widen_region(existing, lesson, store)  # a second hit creates the region if the first was unconfirmed
            return existing
    store.upsert_lesson(lesson)
    if confirmed and lesson.divergence_embedding:
        store.upsert_region(FailureRegion(lesson_id=lesson.id, centroid=lesson.divergence_embedding, radius=BASE_RADIUS, weight=1.0))
    return lesson


def _region_of(existing: Lesson, new: Lesson, store: Store) -> FailureRegion | None:
    for vec in (new.divergence_embedding, existing.divergence_embedding):
        if not vec:
            continue
        for r, _ in store.regions_near(vec):
            if r.lesson_id == existing.id:
                return r
    return None


def _widen_region(existing: Lesson, new: Lesson, store: Store) -> None:
    centroid = new.divergence_embedding or existing.divergence_embedding
    if not centroid:
        return
    r = _region_of(existing, new, store)
    if r is None:
        # first region for a lesson that was stored unconfirmed: the repeat hit confirms it
        store.upsert_region(FailureRegion(lesson_id=existing.id, centroid=centroid, radius=BASE_RADIUS, weight=1.0))
        return
    if not new.divergence_embedding:
        return
    # Move the centroid toward the new divergence point and widen slightly.
    c = np.asarray(r.centroid)
    v = np.asarray(new.divergence_embedding)
    r.centroid = ((c * r.weight + v) / (r.weight + 1)).tolist()
    r.weight += 1
    r.radius += RADIUS_GROWTH
    store.upsert_region(r)


# Threshold note (Nov 7 2026): BASE_RADIUS and settings.lesson_dedup_similarity were NOT tuned on real
# embeddings. No GEMINI_API_KEY was available in the session that owned this file, so the values above
# are the original defaults. On unit vectors euclidean d and cosine s relate as d = sqrt(2 - 2s), so
# BASE_RADIUS = 0.35 corresponds to cosine >= 0.939 and dedup similarity 0.90 to d <= 0.447.
