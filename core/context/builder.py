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
    # Adopted lessons only, unless the store is explicitly marked to include provisional (ungated) ones,
    # which the demo's B1 phase does when the gate had no signal; the run then says so in its summary.
    hits = store.search_lessons(spec_vec, k=k, adopted_only=not getattr(store, "include_candidate_lessons", False))
    lessons = [l for l, _ in hits]
    return render_lessons(lessons), [l.id for l in lessons]


def render_lessons(lessons: list[Lesson]) -> str:
    if not lessons:
        return ""
    lines = ["Lessons from past failures on similar work (follow them):"]
    for l in lessons:
        lines.append(f"- {l.text}")
    return "\n".join(lines)
