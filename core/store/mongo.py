"""MongoDB Atlas store with Vector Search. Owner: Ethan Li.

Collections: lessons, failure_regions, steps, trust, changes, runs, manifests.
Vector indexes (created by `ensure_indexes`, idempotent):
  lessons.embedding          -> "lessons_vec"  (filter field: status)
  failure_regions.centroid   -> "regions_vec"
  steps.embedding            -> "steps_vec"    (for the demo map)

Behaviour matches `MemoryStore` (tests/test_store_parity.py runs the same tests against both).
`$vectorSearch` needs Atlas; on a plain `mongod` (or before the index is built) the vector queries
fall back to a Python scan so the store still works, only slower.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

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

VECTOR_INDEXES = [
    # (collection, vector field, index name, filter fields)
    ("lessons", "embedding", "lessons_vec", ["status"]),
    ("failure_regions", "centroid", "regions_vec", []),
    ("steps", "embedding", "steps_vec", []),
]
REGION_CANDIDATES = 50


class MongoStore:
    def __init__(self, uri: str, db: str):
        self.client = MongoClient(uri, tz_aware=True)
        self.db = self.client[db]
        self._vector_ok = {name: True for _, _, name, _ in VECTOR_INDEXES}  # flipped off after a failed $vectorSearch

    # ---------- indexes ----------

    def ensure_indexes(self, dim: int) -> None:
        """Create the regular and vector-search indexes. Idempotent: existing indexes are left alone.
        Vector indexes need Atlas (M0+ with Vector Search); elsewhere they are skipped silently."""
        self.db.lessons.create_index([("status", ASCENDING)], name="status_1")
        self.db.failure_regions.create_index([("lesson_id", ASCENDING)], name="lesson_id_1")
        self.db.steps.create_index([("run_id", ASCENDING)], name="run_id_1")
        self.db.changes.create_index([("decided_at", ASCENDING)], name="decided_at_1")
        for coll, field, name, filters in VECTOR_INDEXES:
            try:
                existing = {ix.get("name") for ix in self.db[coll].list_search_indexes()}
                if name in existing:
                    continue
                fields = [{"type": "vector", "path": field, "numDimensions": dim, "similarity": "cosine"}]
                fields += [{"type": "filter", "path": f} for f in filters]
                self.db[coll].create_search_index({"name": name, "type": "vectorSearch", "definition": {"fields": fields}})
            except PyMongoError:  # not Atlas, or the tier lacks search: vector queries fall back to a scan
                self._vector_ok[name] = False

    def _vector_search(self, coll: str, index: str, path: str, query: list[float], limit: int, filt: dict | None = None) -> list[dict] | None:
        """Run `$vectorSearch`; None when it is unavailable so the caller scans instead."""
        if not self._vector_ok.get(index, True):
            return None
        stage = {"index": index, "path": path, "queryVector": list(query), "numCandidates": max(50, 10 * limit), "limit": limit}
        if filt:
            stage["filter"] = filt
        try:
            return list(self.db[coll].aggregate([{"$vectorSearch": stage}, {"$addFields": {"score": {"$meta": "vectorSearchScore"}}}]))
        except PyMongoError:
            self._vector_ok[index] = False
            return None

    # ---------- lessons ----------

    def upsert_lesson(self, lesson: Lesson) -> None:
        self.db.lessons.replace_one({"_id": lesson.id}, {"_id": lesson.id, **lesson.model_dump(exclude={"id"})}, upsert=True)

    def get_lesson(self, lesson_id: str) -> Lesson | None:
        d = self.db.lessons.find_one({"_id": lesson_id})
        return _lesson(d) if d else None

    def search_lessons(self, embedding: list[float], k: int = 5, adopted_only: bool = True) -> list[tuple[Lesson, float]]:
        filt = {"status": ChangeStatus.adopted.value} if adopted_only else None
        docs = self._vector_search("lessons", "lessons_vec", "embedding", embedding, k, filt)
        if docs is not None:
            out = []
            for d in docs:
                d.pop("score", None)
                lesson = _lesson(d)
                out.append((lesson, cosine(embedding, lesson.embedding) if lesson.embedding else 0.0))
            out.sort(key=lambda x: -x[1])
            return out
        query = {"embedding": {"$ne": None}, **(filt or {})}
        scored = [(l, cosine(embedding, l.embedding)) for l in (_lesson(d) for d in self.db.lessons.find(query))]
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def list_lessons(self) -> list[Lesson]:
        return [_lesson(d) for d in self.db.lessons.find()]

    # ---------- failure regions ----------

    def upsert_region(self, region: FailureRegion) -> None:
        self.db.failure_regions.replace_one({"_id": region.id}, {"_id": region.id, **region.model_dump(exclude={"id"})}, upsert=True)

    def regions_near(self, embedding: list[float]) -> list[tuple[FailureRegion, float]]:
        """Nearest regions by vector search, then the exact test: euclidean distance <= radius."""
        docs = self._vector_search("failure_regions", "regions_vec", "centroid", embedding, REGION_CANDIDATES)
        if docs is None:
            docs = list(self.db.failure_regions.find())
        v = np.asarray(embedding, dtype=float)
        hits = []
        for d in docs:
            d.pop("score", None)
            region = FailureRegion(id=d.pop("_id"), **d)
            if len(region.centroid) != len(v):
                continue
            dist = float(np.linalg.norm(v - np.asarray(region.centroid, dtype=float)))
            if dist <= region.radius:
                hits.append((region, dist))
        hits.sort(key=lambda x: x[1])
        return hits

    # ---------- reasoning steps ----------

    def save_steps(self, run: AgentRun, steps: list[ReasoningStep]) -> None:
        if steps:
            self.db.steps.insert_many(
                [{"run_id": run.id, "subtask_id": run.subtask_id, "agent_id": run.agent.id, **s.model_dump()} for s in steps]
            )

    # ---------- trust ----------

    def get_trust(self, agent_id: str) -> TrustScore:
        d = self.db.trust.find_one({"_id": agent_id})
        return TrustScore(agent_id=d.pop("_id"), **d) if d else TrustScore(agent_id=agent_id)

    def save_trust(self, trust: TrustScore) -> None:
        self.db.trust.replace_one({"_id": trust.agent_id}, {"_id": trust.agent_id, **trust.model_dump(exclude={"agent_id"})}, upsert=True)

    # ---------- changes (the changelog) ----------

    def save_change(self, change: Change) -> None:
        self.db.changes.replace_one({"_id": change.id}, {"_id": change.id, **change.model_dump(exclude={"id"})}, upsert=True)

    def list_changes(self) -> list[Change]:
        # decided changes in decision order, undecided candidates last (same order as MemoryStore)
        floor = datetime.min.replace(tzinfo=timezone.utc)
        changes = [Change(id=d.pop("_id"), **d) for d in self.db.changes.find()]
        return sorted(changes, key=lambda c: (c.decided_at is None, _aware(c.decided_at) or floor, c.id))

    # ---------- runs ----------

    def save_manifest(self, manifest: RunManifest) -> None:
        self.db.manifests.replace_one({"_id": manifest.id}, {"_id": manifest.id, **manifest.model_dump(exclude={"id"})}, upsert=True)

    def save_run(self, run: AgentRun) -> None:
        self.db.runs.replace_one({"_id": run.id}, {"_id": run.id, **run.model_dump(exclude={"id"})}, upsert=True)


def _lesson(d: dict) -> Lesson:
    return Lesson(id=d.pop("_id"), **d)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
