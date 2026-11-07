"""Near-duplicate lessons bump hit_count and widen their failure region instead of inserting. Owner: D."""

from __future__ import annotations

import numpy as np

from core.config import settings
from core.llm.embeddings import cosine
from core.models import FailureRegion, Lesson
from core.store.base import Store

BASE_RADIUS = 0.35  # euclidean on unit vectors; tune on real embeddings
RADIUS_GROWTH = 0.05


def dedup_or_insert(lesson: Lesson, store: Store) -> Lesson:
    for existing in store.list_lessons():
        if existing.embedding and cosine(existing.embedding, lesson.embedding) >= settings.lesson_dedup_similarity:
            existing.hit_count += 1
            existing.confidence = max(existing.confidence, lesson.confidence)
            store.upsert_lesson(existing)
            _widen_region(existing, lesson, store)
            return existing
    store.upsert_lesson(lesson)
    if lesson.divergence_embedding:
        store.upsert_region(FailureRegion(lesson_id=lesson.id, centroid=lesson.divergence_embedding, radius=BASE_RADIUS, weight=1.0))
    return lesson


def _widen_region(existing: Lesson, new: Lesson, store: Store) -> None:
    # Move the centroid toward the new divergence point and widen slightly.
    regions = [r for r, _ in store.regions_near(new.divergence_embedding or existing.divergence_embedding or [])]
    mine = [r for r in regions if r.lesson_id == existing.id]
    if not mine or not new.divergence_embedding:
        return
    r = mine[0]
    c = np.asarray(r.centroid)
    v = np.asarray(new.divergence_embedding)
    r.centroid = ((c * r.weight + v) / (r.weight + 1)).tolist()
    r.weight += 1
    r.radius += RADIUS_GROWTH
    store.upsert_region(r)
