"""Demo UI server. Read-only JSON over the store plus one static page.

Run:
    uvicorn ui.server:app --reload
    SWARM_SNAPSHOT=runs/demo_snapshot.json uvicorn ui.server:app

Store resolution (lazy, on first request):
    1. the store passed to make_app(store)
    2. SWARM_SNAPSHOT=<path>  -> load_snapshot(path) into a MemoryStore
    3. core.store.get_store()  (MemoryStore when USE_MEMORY_STORE=1, else MongoStore)

Snapshot format (see ui/README.md): a plain JSON dict of lists of model_dump() outputs:
    {
      "version": 1,
      "lessons":  [Lesson.model_dump(mode="json"), ...],
      "changes":  [Change.model_dump(mode="json"), ...],
      "trust":    [TrustScore.model_dump(mode="json"), ...],
      "steps":    [{"run_id", "subtask_id", "agent_id", **ReasoningStep.model_dump(mode="json")}, ...],
      "regions":  [FailureRegion.model_dump(mode="json"), ...],      # optional
      "runs":     [AgentRun.model_dump(mode="json"), ...],           # optional
      "failed_run_ids":   ["run id", ...],                          # optional
      "verified_run_ids": ["run id", ...],                          # optional
      "divergences": [{**Divergence.model_dump(mode="json"),        # optional
                       "failed_outcome": Outcome | null}, ...]      # (failed agent's own outcome)
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse

from core.models import AgentRun, Change, Divergence, FailureRegion, Lesson, Outcome, ReasoningStep, TrustScore
from core.store.memory import MemoryStore

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"
CONCEPT_HTML = HERE / "concept.html"
SNAPSHOT_VERSION = 1


# ---------- store accessors (duck-typed over MemoryStore and MongoStore) ----------


def _list_trust(store) -> list[TrustScore]:
    if isinstance(getattr(store, "trust", None), dict):
        return list(store.trust.values())
    db = getattr(store, "db", None)
    if db is not None:
        return [TrustScore(agent_id=d.pop("_id"), **d) for d in db.trust.find()]
    return []


def _list_steps(store) -> list[dict]:
    if isinstance(getattr(store, "steps", None), list):
        return [dict(s) for s in store.steps]
    db = getattr(store, "db", None)
    if db is not None:
        return list(db.steps.find({}, {"_id": 0}))
    return []


def _list_regions(store) -> list[FailureRegion]:
    if isinstance(getattr(store, "regions", None), dict):
        return list(store.regions.values())
    db = getattr(store, "db", None)
    if db is not None:
        return [FailureRegion(id=d.pop("_id"), **d) for d in db.failure_regions.find()]
    return []


def _list_changes(store) -> list[Change]:
    # MemoryStore.list_changes sorts on `decided_at or hash(id)`, which raises TypeError when
    # decided and undecided changes are mixed; read the dict directly and sort here instead.
    if isinstance(getattr(store, "changes", None), dict):
        return list(store.changes.values())
    return list(store.list_changes())


def _list_runs(store) -> list[AgentRun]:
    if isinstance(getattr(store, "runs", None), dict):
        return list(store.runs.values())
    db = getattr(store, "db", None)
    if db is not None:
        return [AgentRun(id=d.pop("_id"), **d) for d in db.runs.find()]
    return []


def _dump(x) -> dict:
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    return dict(jsonable_encoder(x))


def _list_divergences(store) -> list[dict]:
    """Divergences in the order they were recorded (oldest first), as JSON dicts.

    Items of `store.divergences` may be `Divergence` models or plain dicts; extra dict keys
    (agent_id, subtask_id, failed_outcome) are kept."""
    items = getattr(store, "divergences", None)
    if items is None:
        db = getattr(store, "db", None)
        items = list(db.divergences.find({}, {"_id": 0})) if db is not None else []
    return [_dump(d) for d in items or ()]


def _failed_outcome(store, run_id: str | None, entry: dict):
    """The failed agent's own outcome on the breaking case: entry["failed_outcome"], else
    store.failed_outcomes[run_id] (Outcome or dict), else None."""
    o = entry.get("failed_outcome")
    if o is None and run_id is not None:
        o = (getattr(store, "failed_outcomes", None) or {}).get(run_id)
    return _dump(o) if o is not None else None


def _run_owners(store) -> dict[str, tuple[str | None, str | None]]:
    """run_id -> (agent_id, subtask_id) from stored runs, steps and lessons' created_from."""
    out: dict[str, tuple[str | None, str | None]] = {}

    def put(rid, agent, sub):
        if not rid:
            return
        a, st = out.get(rid, (None, None))
        out[rid] = (a or agent, st or sub)

    for r in _list_runs(store):
        put(r.id, r.agent.id, r.subtask_id)
    for d in _list_steps(store):
        put(d.get("run_id"), d.get("agent_id"), d.get("subtask_id"))
    for l in store.list_lessons():
        cf = l.created_from or {}
        put(cf.get("run_id"), cf.get("agent_id"), cf.get("subtask_id"))
    return out


DIVERGENCE_FIELDS = (
    "run_id", "agent_id", "subtask_id", "step_index", "step_text", "signals", "confirmed",
    "step_probabilities", "kind", "kind_confidence", "generalizes", "breaking_case",
    "majority_outcome", "failed_outcome", "distance", "consensus_step_texts",
)


def divergence_view(store) -> list[dict]:
    """What GET /api/divergences returns: newest first, with agent and subtask filled in."""
    owners = _run_owners(store)
    raw = _list_divergences(store)
    out = []
    for seq, d in enumerate(raw):
        rid = d.get("run_id")
        agent, sub = owners.get(rid, (None, None))
        e = {k: d.get(k) for k in DIVERGENCE_FIELDS}
        e["agent_id"] = d.get("agent_id") or agent
        e["subtask_id"] = d.get("subtask_id") or sub
        e["failed_outcome"] = _failed_outcome(store, rid, d)
        e["signals"] = list(d.get("signals") or [])
        e["confirmed"] = bool(d.get("confirmed"))
        e["step_probabilities"] = dict(d.get("step_probabilities") or {})
        e["consensus_step_texts"] = list(d.get("consensus_step_texts") or [])
        e["seq"] = seq  # recording order, 0 = oldest
        out.append(e)
    return jsonable_encoder(out[::-1])


def _run_verdicts(store) -> tuple[set[str], set[str]]:
    """(failed run ids, verified run ids) from whatever evidence the store holds.

    Explicit lists (store.failed_run_ids / store.verified_run_ids, set by load_snapshot or by
    the orchestrator) win. Otherwise a run counts as failed if it errored or a lesson was
    reflected from it (lessons are only written from failed runs)."""
    failed = set(getattr(store, "failed_run_ids", None) or ())
    verified = set(getattr(store, "verified_run_ids", None) or ())
    for run in _list_runs(store):
        if run.error:
            failed.add(run.id)
    for lesson in store.list_lessons():
        rid = (lesson.created_from or {}).get("run_id")
        if rid:
            failed.add(rid)
    return failed, verified - failed


def _step_text(s: dict) -> str:
    try:
        return ReasoningStep.model_validate(s).as_text()
    except Exception:
        return s.get("text") or f"assumption: {s.get('assumption', '')} | action: {s.get('action', '')}"


def _ts(dt: datetime | None) -> float:
    if dt is None:
        return float("inf")  # undecided candidates sort to the top
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------- snapshot ----------


def snapshot_store(store) -> dict:
    """Dump a store to a JSON-serialisable dict (the SWARM_SNAPSHOT format)."""
    failed, verified = _run_verdicts(store)
    return jsonable_encoder({
        "version": SNAPSHOT_VERSION,
        "lessons": [l.model_dump(mode="json") for l in store.list_lessons()],
        "changes": [c.model_dump(mode="json") for c in _list_changes(store)],
        "trust": [t.model_dump(mode="json") for t in _list_trust(store)],
        "steps": _list_steps(store),
        "regions": [r.model_dump(mode="json") for r in _list_regions(store)],
        "runs": [r.model_dump(mode="json") for r in _list_runs(store)],
        "failed_run_ids": sorted(failed),
        "verified_run_ids": sorted(verified),
        "divergences": [
            {**d, "failed_outcome": _failed_outcome(store, d.get("run_id"), d)}
            for d in _list_divergences(store)
        ],
    })


def write_snapshot(store, path: str | os.PathLike) -> Path:
    """Convenience: snapshot_store(store) written to `path` as JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshot_store(store), indent=1))
    return p


def store_from_snapshot(data: dict) -> MemoryStore:
    store = MemoryStore()
    for d in data.get("lessons", []):
        store.upsert_lesson(Lesson.model_validate(d))
    for d in data.get("changes", []):
        store.save_change(Change.model_validate(d))
    for d in data.get("trust", []):
        d = {k: v for k, v in d.items() if k != "score"}  # score is a computed property
        store.save_trust(TrustScore.model_validate(d))
    for d in data.get("regions", []):
        store.upsert_region(FailureRegion.model_validate(d))
    for d in data.get("runs", []):
        store.save_run(AgentRun.model_validate(d))
    store.steps.extend(dict(s) for s in data.get("steps", []))
    store.failed_run_ids = set(data.get("failed_run_ids", []))
    store.verified_run_ids = set(data.get("verified_run_ids", []))
    store.divergences = []
    store.failed_outcomes = {}
    for d in data.get("divergences") or []:  # absent in snapshots written before divergences
        div = Divergence.model_validate(d)
        store.divergences.append(div)
        if d.get("failed_outcome") is not None:
            store.failed_outcomes[div.run_id] = Outcome.model_validate(d["failed_outcome"])
    return store


def load_snapshot(path: str | os.PathLike) -> MemoryStore:
    """Load a SWARM_SNAPSHOT JSON file into a fresh MemoryStore."""
    return store_from_snapshot(json.loads(Path(path).read_text()))


class _SafeChanges:
    """Proxy that forwards everything to the store but sorts list_changes safely (oldest first,
    undecided last), so eval.metrics.summarize works on a MemoryStore with undecided changes."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def list_changes(self):
        return sorted(_list_changes(self._store), key=lambda c: _ts(c.decided_at))


def _default_store():
    snap = os.environ.get("SWARM_SNAPSHOT")
    if snap:
        return load_snapshot(snap)
    from core.store import get_store

    return get_store()


# ---------- app ----------


def make_app(store=None) -> FastAPI:
    app = FastAPI(title="Swarm demo UI")
    state: dict[str, Any] = {"store": store}

    def get() -> Any:
        if state["store"] is None:
            state["store"] = _default_store()
        return state["store"]

    app.state.get_store = get

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(INDEX_HTML, media_type="text/html")

    @app.get("/concept", include_in_schema=False)
    def concept():
        return FileResponse(CONCEPT_HTML, media_type="text/html")

    @app.get("/api/changes")
    def changes():
        cs = sorted(_list_changes(get()), key=lambda c: _ts(c.decided_at), reverse=True)
        return [c.model_dump(mode="json") for c in cs]

    @app.get("/api/lessons")
    def lessons():
        ls = sorted(get().list_lessons(), key=lambda l: (-l.hit_count, -l.confidence))
        return [l.model_dump(mode="json") for l in ls]

    @app.get("/api/trust")
    def trust():
        ts = sorted(_list_trust(get()), key=lambda t: -t.score)
        return [
            {
                "agent_id": t.agent_id,
                "score": t.score,
                "agreements": t.agreements,
                "disagreements": t.disagreements,
                "history": t.history,
            }
            for t in ts
        ]

    @app.get("/api/steps")
    def steps():
        s = get()
        failed, verified = _run_verdicts(s)
        out = []
        for d in _list_steps(s):
            rid = d.get("run_id")
            if "failed" in d:
                f = d["failed"]
            elif rid in failed:
                f = True
            elif rid in verified:
                f = False
            else:
                f = None  # not derivable
            out.append({
                "run_id": rid,
                "subtask_id": d.get("subtask_id"),
                "agent_id": d.get("agent_id"),
                "step_index": d.get("index", d.get("step_index")),
                "text": _step_text(d),
                "embedding": d.get("embedding"),
                "failed": f,
            })
        return jsonable_encoder(out)

    @app.get("/api/divergences")
    def divergences():
        return divergence_view(get())

    @app.get("/api/regions")
    def regions():
        return [r.model_dump(mode="json") for r in _list_regions(get())]

    @app.get("/api/summary")
    def summary():
        try:
            from eval.metrics import summarize

            return jsonable_encoder(summarize(_SafeChanges(get())))
        except Exception as e:  # missing/broken metrics must not break the page
            return {"error": f"{type(e).__name__}: {e}"}

    return app


app = make_app()
