# UI (secondary: the loop is the product)

One page that shows what the swarm learned and decided, for the 3-minute demo. No build step,
no CDN: `ui/index.html` is plain HTML/CSS/JS served by a small FastAPI app (`ui/server.py`).
It refreshes every 3 seconds and only redraws sections whose data changed.

## Run

```bash
source .venv/bin/activate
uv pip install -e ".[dev]"

# live store (MemoryStore when USE_MEMORY_STORE=1, else MongoDB from MONGODB_URI)
uvicorn ui.server:app --reload

# cached demo run (fallback when APIs are down)
SWARM_SNAPSHOT=runs/demo_snapshot.json uvicorn ui.server:app --reload
```

Open http://127.0.0.1:8000.

A fresh process with `USE_MEMORY_STORE=1` has an empty store, so for a separate UI process
either point it at MongoDB or load a snapshot. To embed the UI in the orchestrator process
instead, build the app around its live store: `make_app(store)`.

Store resolution happens on the first request: the store passed to `make_app(store)`, else
`SWARM_SNAPSHOT`, else `core.store.get_store()`.

## Dumping a run for the cached demo

```python
from ui.server import snapshot_store, write_snapshot, load_snapshot

write_snapshot(store, "runs/demo_snapshot.json")  # json.dumps(snapshot_store(store)) to a file
store = load_snapshot("runs/demo_snapshot.json")  # -> MemoryStore
```

## Snapshot format

A plain JSON dict of lists of `model_dump(mode="json")` outputs. Only `lessons`, `changes`,
`trust` and `steps` are required; every key is optional on load.

```jsonc
{
  "version": 1,
  "lessons":  [ /* Lesson.model_dump(mode="json") */ ],
  "changes":  [ /* Change.model_dump(mode="json") */ ],
  "trust":    [ /* TrustScore.model_dump(mode="json"); a "score" key is ignored on load */ ],
  "steps":    [ /* {"run_id", "subtask_id", "agent_id", **ReasoningStep.model_dump(mode="json")},
                   the same shape store.save_steps writes */ ],
  "regions":  [ /* FailureRegion.model_dump(mode="json") */ ],
  "runs":     [ /* AgentRun.model_dump(mode="json") */ ],
  "failed_run_ids":   ["<run id>", ...],
  "verified_run_ids": ["<run id>", ...],
  "divergences": [ /* {**Divergence.model_dump(mode="json"), "failed_outcome": Outcome | null},
                      oldest first */ ]
}
```

`divergences` is optional and the snapshot `version` stays 1: older snapshots without the key
still load, with `store.divergences = []`. On load each entry becomes a `Divergence` on
`store.divergences`, and a non-null `failed_outcome` goes to `store.failed_outcomes[run_id]`.

`failed_run_ids` and `verified_run_ids` colour the reasoning map. The store has no cross-test
verdicts, so pass `CrossTestResult.failed_runs` and `verified_runs` here (or set
`store.failed_run_ids` and `store.verified_run_ids` before snapshotting). Without them a run counts as
failed if `AgentRun.error` is set or a lesson's `created_from.run_id` names it. Otherwise its
verdict is unknown (`failed: null`). A step dict that has its own `failed` key overrides all of this.

### Divergences (the Root cause cards)

The UI reads `store.divergences`, a plain list in the order the reflector produced them (oldest
first). Items may be `Divergence` models or plain dicts. A MongoStore without that attribute is
read from a `divergences` collection if one exists. What each card needs:

- `run_id`, `step_index`, `step_text`, `signals`, `confirmed`: always set by the localizer.
- `step_probabilities`, `kind`, `kind_confidence`, `generalizes`: set by `locate_divergence_subq`
  when SubQ answered. A path-only divergence leaves them empty, and its card says so.
- `breaking_case`, `majority_outcome`: the failing input and the expected (majority) outcome.
- The **actual** outcome is not a `Divergence` field. Put the failed run's own `Outcome` on the
  breaking case in `store.failed_outcomes[run_id]`, or use a dict item with a `failed_outcome` key.
  Without it the card shows "not recorded".
- Agent and subtask come from the stored run (`save_run`), else its stored steps, else a lesson's
  `created_from`. A dict item may also carry `agent_id` / `subtask_id` directly.

## Endpoints

| Path | Returns |
| --- | --- |
| `GET /` | `index.html` |
| `GET /api/changes` | `Change` list, newest `decided_at` first (undecided candidates at the top) |
| `GET /api/lessons` | `Lesson` list, by hit_count then confidence |
| `GET /api/trust` | `[{agent_id, score, agreements, disagreements, history}]`, highest score first |
| `GET /api/steps` | `[{run_id, subtask_id, agent_id, step_index, text, embedding, failed}]`, where `failed` is true/false/null |
| `GET /api/divergences` | `[{run_id, agent_id, subtask_id, step_index, step_text, signals, confirmed, step_probabilities, kind, kind_confidence, generalizes, breaking_case, majority_outcome, failed_outcome, distance, consensus_step_texts, seq}]`, newest first (`seq` is the recording order, 0 = oldest) |
| `GET /api/regions` | `FailureRegion` list (drawn on the map) |
| `GET /api/summary` | `eval.metrics.summarize(store)`, or `{"error": ...}` if it fails |

## What the page shows, top to bottom

- **Numbers strip**: changes adopted and rolled back, lessons, and held-out accuracy first → last (from `/api/summary`).
- **Changelog**: every change with status (adopted green, rolled back red, candidate grey), kind,
  description, held-out accuracy and repeat-mistake rate before → after (green when better,
  red when worse), and the gate's reason.
- **Root cause**: one card per localized failed run, newest first (from `/api/divergences`).
  It shows the agent, subtask and run, then the failing input with expected vs actual. Next is a
  horizontal bar chart of SubQ Snap's P(root cause) for each step `S0..Sn` of that run. The chosen
  step is outlined in blue and its full text appears beside the evidence. Step labels come from
  the run's stored steps plus any labels in `step_probabilities`. The card also has a kind badge
  (bug red, spec ambiguity amber, acceptable variation grey) with its confidence, a "generalizes"
  meter, the four signals (`subq`, `path`, `locality`, `live_timing`, lit when they pointed at
  this step) and whether counterfactual replay confirmed the step.
- **Trust leaderboard**: agents by Laplace-smoothed agreement score, with agree/disagree counts
  and an SVG sparkline of score history (green if it ended at or above where it started, red if below).
- **Lessons**: lesson text, hit count, status, confidence.
- **Reasoning map**: 2D PCA of step embeddings, computed in the browser by power iteration.
  Verified-run steps are grey, failed-run steps red, and steps from runs with no verdict are hollow.
  Failure regions are dashed red circles, and their radius is only indicative because projection
  distorts distances. Hover a point to see the step text, agent, run and step index.
  The root-cause step of each localized run has an amber ring, and hovering it shows the SubQ
  probability (or "path signal" when there is none). Other steps of that run show their SubQ
  probability on hover too.

## Known issue outside ui/

`MemoryStore.list_changes()` sorts on `decided_at or hash(id)` and raises `TypeError` once
decided and undecided changes are mixed. The UI works around it (it reads `store.changes`
directly and hands `summarize` a proxy with a safe sort), but the CLI and other callers still hit it.
