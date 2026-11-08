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

## Concept page: `/concept` (`ui/concept.html`)

A second page, same server and endpoints, that draws the swarm as beetles walking their reasoning
paths over a halftone map of the reasoning space. Self-contained (no build step, no CDN); it reads
`/api/steps`, `/api/divergences`, `/api/regions` and `/api/trust`.

Four stacked canvases, bottom to top:

1. `scene` (hidden): the ground at one pixel per halftone dot, from `drawMap2D`.
2. `dots` (WebGL): the halftone. The fragment shader `FS` turns each scene pixel into a dot whose
   radius follows luminance above the ground colour, and a 4x4 Bayer dither thins the dot grid as
   tone falls, so peaks are big dense dots and troughs sparse small ones. Pitch: `pitchCss`
   (buttons fine 3 / medium 4 / coarse 6 css px).
3. `sprites` (2D, `image-rendering: pixelated`, twice the dot grid): step pins, flipped beetles,
   live beetles as 14x16 pixel sprites (`SPRITE_A`/`SPRITE_B` leg beats, `SPRITE_DEAD`, a salt
   `stripe` for the second agent of a company; palettes in `SKIN`), rotated to the heading in
   22.5 degree steps. Nothing on the ground is drawn after a beetle, so the terrain can never
   cover one. (The earlier bug: beetles and ground cells were depth-sorted by horizontal distance
   from the camera, which let cells paint over beetles.)
4. `labels`: crisp text for the flipped beetle and the followed agent.

How it works, top to bottom of the script:

- **PCA**: step embeddings (task runs only; `heldout-*` subtasks are skipped) are projected to two
  components by power iteration, decompressed with a signed power curve so the fake embeddings'
  tight cluster spreads, and scaled so the data spans about 64 world cells.
- **Heightfield** `hCell(ix, iy)`: value-noise dunes everywhere plus a soft-saturating density of
  step positions inside the data bbox (ridges where agents reasoned) minus a gaussian per failure
  region (pits). Memoised per integer cell, so the world is unbounded.
- **Ground tiles** `renderTile`: 32-cell tiles at `PPC` pixels per cell, cached in `world.tiles`:
  elevation ramp (`RAMP`), hillshade from the bilinear slope, contour lines every `CONT` height
  units in bark. `drawMap2D` draws the visible tiles through one canvas transform (yaw, zoom).
- **Beetles** `makeBeetles()`: one per agent, colour from the agent id prefix (`meta`, `gemini`,
  `claude`, `openai`, `subq`, see `COMPANY`). The knots are the run's steps in `step_index` order
  and `spline()` runs a Catmull-Rom curve through them (8 samples per segment), so the path bends
  instead of turning at each step. Every run starts at `START` (the burrow, "the spec") and ends
  at `END` (the mound, "hidden tests pass"); between runs a beetle walks back on an outer arc.
  `crawl()` adds an irregular side-to-side wander to the samples between knots (knots stay put, so
  pins and the flip point are exact); `wait` staggers departures from the burrow. The knot whose step is the divergence's `step_index` is the
  root: the beetle flips there and stays (`world.corpses`).
- **Tracks** `stampTrack()`: a world-space canvas (`track`, `TRACK_R` cells each way at
  `TRACK_PPC` px per cell) that beetles stamp as they walk: two footprints and a faint centre in a
  lighter tint of the company colour. `drawMap2D` draws it over the tiles before the halftone, so
  a track is bigger, coloured dots in the ground itself, and it stays until the loop restarts.
- **Tracks fade**: `stepSim` erodes the track canvas each frame (`destination-out`, about a
  20 s memory), so a track is a recent trail, not a permanent line.
- **Camera**: fits every live beetle in view (extents along the screen axes plus a margin) or
  follows one agent; drag pans and the wheel zooms, which sets `cam.manual` until re-centre.
- **Modes**: `recorded run` shows the whole run at once. `live run` is a simulated stream (Lucas's
  colony idea): an upload card (task, codebase; hardcoded), then per subtask the phases decompose,
  hidden tests, swarm (only that subtask's runs become beetles), verdict, reflect (a flag is
  planted at the SubQ root-cause step, `world.knownPits`), gate (held-out pass rate from the
  hardcoded `HELDOUT` ramp). Later verified beetles passing a known pit get a "steered around"
  label and count. `sim` holds the state, `simTick` advances it, the HUD (`#hud`) shows round,
  phase, counters, latest lesson and a run log. Wire the real pipeline by driving `sim` from the
  orchestrator's events instead of timers.
- **Camera** `updateCamera()`: follows one agent (`world.follow`, from the legend; by default the
  beetle that will flip, then the swarm) or the swarm centroid, backing off with its spread
  (`wantDist`). Top-down, with a slow yaw drift.
- **Beetle** `drawBeetle()` / `drawSprite()`: the pixel sprite at `BEETLE_CELLS` world cells long,
  pixel size rounded to whole sprite pixels.

Knobs worth touching first: `PAL`, `RAMP` and `CONT` (map look), the `density` and `r` lines in
`FS` (dot size and thinning), `SKIN` and the `SPRITE_*` bitmaps, `BEETLE_CELLS`, `SPEED`, `stampTrack` tints, `wantDist` (zoom).
Knobs worth touching first: `PAL`, `RAMP` and `CONT` (map look), `BS` (beetle size), `SPEED`,
`TRAIL_LIFE`, `wantDist` in `updateCamera` (zoom), and the elevation/hillshade mix in `renderTile`.

## /live — the demo page (real-run data, updates while a run is going)

```bash
source .venv/bin/activate
SWARM_SNAPSHOT=demo/pricing_real.json USE_MEMORY_STORE=1 uvicorn ui.server:app --port 8787
open http://localhost:8787/live
```

The server re-reads the snapshot file whenever it changes, so a live run that writes to the same path
shows up on the page within 2 seconds, subtask by subtask:

```bash
swarm run demo_task/pricing/task.md --snapshot demo/pricing_real.json          # needs GEMINI_API_KEY (+ SUBQ_API_KEY for the judge)
swarm run demo_task/pricing/task.md --fake --snapshot demo/pricing_real.json   # offline, no keys
```

If `demo_task/mcp/repo` has been set up, point the gate at the four function slices so it does not send
pricing lessons into the MCP SDK: `--heldout-dir <folder with symlinks to eval/heldout/{letter_grades,library_fines,parking_billing,shipping_bands}>`.

Sections: numbers strip (subtasks verified, lessons, changes adopted vs rolled back, held-out accuracy first→last),
the swarm timeline (one chip per live agent: green verified, red failed, with the lessons each failure produced and the
gate's decision), the held-out gate chart (accuracy before vs after each change), the SubQ root-cause card
(probability per reasoning step, the blamed step, breaking input, agent's result vs expected, the lesson and its SubQ
confidence), the trust leaderboard, the changelog, and the reasoning map (2-D projection of step embeddings; amber ring
= blamed step).

`demo/pricing_real_3of4.json` is a completed real run (six Gemini agents, 3 of 4 subtasks verified, 3 lessons, 2 adopted,
1 rolled back, held-out 0.67 → 0.71) to serve when no live run is available.

`demo/pricing_real.json` is run 6, the headline real run: 4 of 4 subtasks verified by six live Gemini agents (5/6, 6/6, 6/6, 5/6), 2 lessons, 1 adopted (held-out 0.875 -> 0.917), 1 rolled back.
