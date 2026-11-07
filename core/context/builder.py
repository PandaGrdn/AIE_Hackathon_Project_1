"""Lesson retrieval at subtask start. Owner: D.

Embeds the subtask spec, vector-searches adopted lessons, returns the prompt
block and the lesson ids used (the held-out gate needs to know what was in play).
"""

from __future__ import annotations

from core.llm.embeddings import embed_texts
from core.models import Lesson, Subtask
from core.store.base import Store


def build_context(subtask: Subtask, store: Store, k: int = 5) -> tuple[str, list[str]]:
    [spec_vec] = embed_texts([subtask.spec])
    hits = store.search_lessons(spec_vec, k=k, adopted_only=True)
    lessons = [l for l, _ in hits]
    return render_lessons(lessons), [l.id for l in lessons]


def render_lessons(lessons: list[Lesson]) -> str:
    if not lessons:
        return ""
    lines = ["Lessons from past failures on similar work (follow them):"]
    for l in lessons:
        lines.append(f"- {l.text}")
    return "\n".join(lines)
