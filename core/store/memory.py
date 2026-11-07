"""In-memory store for local dev and tests. Same interface as MongoStore."""

from __future__ import annotations

import numpy as np

from core.llm.embeddings import cosine
from core.models import (
    AgentRun,
    Change,
    ChangeStatus,
    FailureRegion,
    Lesson,
    ReasoningStep,
    RunManifest,
    TrustScore,
)


class MemoryStore:
    def __init__(self):
        self.lessons: dict[str, Lesson] = {}
        self.regions: dict[str, FailureRegion] = {}
        self.steps: list[dict] = []
        self.trust: dict[str, TrustScore] = {}
        self.changes: dict[str, Change] = {}
        self.manifests: dict[str, RunManifest] = {}
        self.runs: dict[str, AgentRun] = {}

    # lessons
    def upsert_lesson(self, lesson):
        self.lessons[lesson.id] = lesson

    def get_lesson(self, lesson_id):
        return self.lessons.get(lesson_id)

    def search_lessons(self, embedding, k=5, adopted_only=True):
        cands = [
            l for l in self.lessons.values()
            if l.embedding is not None and (not adopted_only or l.status == ChangeStatus.adopted)
        ]
        scored = [(l, cosine(embedding, l.embedding)) for l in cands]
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def list_lessons(self):
        return list(self.lessons.values())

    # regions
    def upsert_region(self, region):
        self.regions[region.id] = region

    def regions_near(self, embedding):
        v = np.asarray(embedding)
        hits = []
        for r in self.regions.values():
            d = float(np.linalg.norm(v - np.asarray(r.centroid)))
            if d <= r.radius:
                hits.append((r, d))
        hits.sort(key=lambda x: x[1])
        return hits

    # steps
    def save_steps(self, run, steps):
        for s in steps:
            self.steps.append({"run_id": run.id, "subtask_id": run.subtask_id, "agent_id": run.agent.id, **s.model_dump()})

    # trust
    def get_trust(self, agent_id):
        return self.trust.setdefault(agent_id, TrustScore(agent_id=agent_id))

    def save_trust(self, trust):
        self.trust[trust.agent_id] = trust

    # changes
    def save_change(self, change):
        self.changes[change.id] = change

    def list_changes(self):
        # decided changes in decision order, undecided candidates last
        from datetime import datetime, timezone

        floor = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(self.changes.values(), key=lambda c: (c.decided_at is None, c.decided_at or floor, c.id))

    # runs
    def save_manifest(self, manifest):
        self.manifests[manifest.id] = manifest

    def save_run(self, run):
        self.runs[run.id] = run
