"""Live failure-region check during a run. Owner: D.

Every new reasoning step is embedded and compared with known failure regions.
A hit returns the matching lesson so the runner can inject it before the agent acts.
"""

from __future__ import annotations

from core.llm.embeddings import embed_texts
from core.models import Lesson, ReasoningStep
from core.store.base import Store


class LiveCheck:
    def __init__(self, store: Store):
        self.store = store
        self.hits: list[tuple[int, str]] = []  # (step_index, lesson_id)

    def check(self, step: ReasoningStep) -> Lesson | None:
        if step.embedding is None:
            [step.embedding] = embed_texts([step.as_text()])
        for region, _dist in self.store.regions_near(step.embedding):
            lesson = self.store.get_lesson(region.lesson_id)
            if lesson is not None:
                self.hits.append((step.index, lesson.id))
                return lesson
        return None
