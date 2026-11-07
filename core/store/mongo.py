"""MongoDB Atlas store with Vector Search. Owner: D.

Collections: lessons, failure_regions, steps, trust, changes, runs, manifests.
Vector indexes needed (create in Atlas UI or via `ensure_indexes`):
  lessons.embedding          -> index "lessons_vec"
  failure_regions.centroid   -> index "regions_vec"
  steps.embedding            -> index "steps_vec"   (for the demo map)
"""

from __future__ import annotations

from pymongo import MongoClient

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


class MongoStore:
    def __init__(self, uri: str, db: str):
        self.client = MongoClient(uri)
        self.db = self.client[db]

    def ensure_indexes(self, dim: int) -> None:
        """Create vector search indexes. Idempotent. Requires Atlas M0+ with Vector Search."""
        for coll, field, name in [
            ("lessons", "embedding", "lessons_vec"),
            ("failure_regions", "centroid", "regions_vec"),
            ("steps", "embedding", "steps_vec"),
        ]:
            try:
                self.db[coll].create_search_index(
                    {
                        "name": name,
                        "type": "vectorSearch",
                        "definition": {
                            "fields": [{"type": "vector", "path": field, "numDimensions": dim, "similarity": "cosine"}]
                        },
                    }
                )
            except Exception:  # index exists or cluster tier lacks search
                pass

    # lessons
    def upsert_lesson(self, lesson):
        self.db.lessons.replace_one({"_id": lesson.id}, {"_id": lesson.id, **lesson.model_dump(exclude={"id"})}, upsert=True)

    def get_lesson(self, lesson_id):
        d = self.db.lessons.find_one({"_id": lesson_id})
        return Lesson(id=d.pop("_id"), **d) if d else None

    def search_lessons(self, embedding, k=5, adopted_only=True):
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "lessons_vec",
                    "path": "embedding",
                    "queryVector": embedding,
                    "numCandidates": 50,
                    "limit": k,
                    **({"filter": {"status": ChangeStatus.adopted.value}} if adopted_only else {}),
                }
            },
            {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
        ]
        out = []
        for d in self.db.lessons.aggregate(pipeline):
            score = d.pop("score")
            out.append((Lesson(id=d.pop("_id"), **d), score))
        return out

    def list_lessons(self):
        return [Lesson(id=d.pop("_id"), **d) for d in self.db.lessons.find()]

    # regions
    def upsert_region(self, region):
        self.db.failure_regions.replace_one({"_id": region.id}, {"_id": region.id, **region.model_dump(exclude={"id"})}, upsert=True)

    def regions_near(self, embedding):
        # TODO(D): $vectorSearch on regions_vec, then filter by euclidean distance <= radius
        import numpy as np

        v = np.asarray(embedding)
        hits = []
        for d in self.db.failure_regions.find():
            dist = float(np.linalg.norm(v - np.asarray(d["centroid"])))
            if dist <= d["radius"]:
                hits.append((FailureRegion(id=d.pop("_id"), **d), dist))
        hits.sort(key=lambda x: x[1])
        return hits

    # steps
    def save_steps(self, run, steps):
        if steps:
            self.db.steps.insert_many(
                [{"run_id": run.id, "subtask_id": run.subtask_id, "agent_id": run.agent.id, **s.model_dump()} for s in steps]
            )

    # trust
    def get_trust(self, agent_id):
        d = self.db.trust.find_one({"_id": agent_id})
        return TrustScore(agent_id=d.pop("_id"), **d) if d else TrustScore(agent_id=agent_id)

    def save_trust(self, trust):
        self.db.trust.replace_one({"_id": trust.agent_id}, {"_id": trust.agent_id, **trust.model_dump(exclude={"agent_id"})}, upsert=True)

    # changes
    def save_change(self, change):
        self.db.changes.replace_one({"_id": change.id}, {"_id": change.id, **change.model_dump(exclude={"id"})}, upsert=True)

    def list_changes(self):
        return [Change(id=d.pop("_id"), **d) for d in self.db.changes.find().sort("decided_at", 1)]

    # runs
    def save_manifest(self, manifest):
        self.db.manifests.insert_one({"_id": manifest.id, **manifest.model_dump(exclude={"id"})})

    def save_run(self, run):
        self.db.runs.replace_one({"_id": run.id}, {"_id": run.id, **run.model_dump(exclude={"id"})}, upsert=True)
