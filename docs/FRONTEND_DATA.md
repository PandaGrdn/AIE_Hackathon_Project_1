# Frontend: wire the pages to the real recorded runs (brief for Lucas)

Everything the Colony / concept / live pages currently hardcode exists as real data from today's recorded
runs. Replace the hardcoded scenario with these sources; invent nothing, show counts next to shares.

## Serve a snapshot

```bash
source .venv/bin/activate
SWARM_SNAPSHOT=demo/mcp_snapshot.dense.json USE_MEMORY_STORE=1 uvicorn ui.server:app --port 8787
```

Snapshots on the branch (`demo/`):

| File | What it is |
| --- | --- |
| `mcp_snapshot.dense.json` | Task A, the real 4-agent run on the MCP SDK (S0–S3 committed, S4 stalled), with dense reasoning paths |
| `mcp_snapshot.json` | Same run, logged steps only |
| `task_b_baseline.json` | Task B run with no lessons (B0) |
| `task_b_with_lessons.json` | Task B run with Task A's provisional lessons (B1), once it finishes |
| `results.json` | B0 vs B1 comparison (written by `record_demo.py --phase compare`) |
| `task_a_summary.json`, `task_b_*_summary.json` | per-subtask verified/failed counts, retries, lessons, changes, wall time |
| `mcp_fake_snapshot.json`, `pricing_snapshot.json` | offline fake-agent runs (label as simulated if used) |

## Endpoints (ui/server.py) → what to draw

| Endpoint | Fields | Use |
| --- | --- | --- |
| `GET /api/steps` | `run_id, subtask_id, agent_id, step_index (float; fractional = interpolated "message" points), text, embedding, failed` | One beetle per `run_id`; path = steps sorted by `step_index`; 2D position = PCA of `embedding` (index.html already does power-iteration PCA; reuse it); colour by `agent_id` |
| `GET /api/divergences` | `run_id, step_index, step_text, signals, confirmed, step_probabilities {"S0":p,...}, kind, kind_confidence, generalizes, breaking_case, majority_outcome, failed_outcome` | The pit/flag: place it at that run's step `step_index`; the flag card = SubQ probability bars over steps, kind badge, generalizes meter, breaking input + expected vs actual |
| `GET /api/lessons` | `text, status (candidate/adopted/rolled_back), confidence, hit_count, created_from {run_id, subtask_id, agent_id, step_index}` | Flag text; status colour; "provisional" label for `candidate` |
| `GET /api/changes` | `kind (context/routing), status, description, heldout_before/after {accuracy, repeat_mistake_rate}, reason, decided_at` | HUD changelog; adopted green / rolled_back red; show before→after when present |
| `GET /api/trust` | `agent_id, score, agreements, disagreements, history[]` | Leaderboard / beetle size or badge |
| `GET /api/regions` | `lesson_id, centroid, radius, weight` | Dashed circles on the map (project centroid with the same PCA) |
| `GET /api/summary` | counts: changes adopted/rolled back, lessons, held-out first→last, repeat-mistake rate | Numbers strip |

Verdicts: `failed_run_ids` / `verified_run_ids` are in the snapshot and surface as `failed` on each step; a
"fall" = a run in `failed_run_ids`; a run that reaches the mound = `verified_run_ids`.

## Per-subtask phases (the HUD timeline)

From `demo/task_a_summary.json` → `per_subtask[]`: `title, verified, failed, failed_run_ids, verified_run_ids,
lessons[], changes[], committed, retried, first_attempt {verified, failed, failed_run_ids, lessons}`. The
Task A story in data: S0 4/4 → S1 3/4 (lesson rolled back) → S2 1/4 (3 lessons) → S3 4/4 → S4 0/4, retry 0/4 with
4 provisional lessons, stall. Serve the summary as a static file or add `GET /api/summary_file` reading it.

## Live mode

`run_task(..., snapshot_path=...)` rewrites the snapshot after every subtask; `ui/server.py` reloads the file
when it changes (Ethan Li's `/live` page polls every 2 s). For a live demo run: `python scripts/real_run.py
demo_task/mcp/task.md --subtasks 0 --swarm-size 3 --snapshot demo/live.json --isolate` and point
`SWARM_SNAPSHOT=demo/live.json` at it.

## Rules

- No invented numbers or lessons; every pit/flag/row must come from the endpoints above.
- Show counts beside shares (4 runs per subtask; one run = 25 points).
- Lessons with `status: candidate` are provisional (not gate-adopted). Gate decisions on this run were all
  rolled back (0.0 → 0.0 held-out signal): say so, do not hide it.
- SubQ = the decision layer (root-cause step, kind, generalizes). Gemini wrote tests and lesson text. Label
  the SubQ probability as "SubQ P(root cause)".
- Simulated/fake snapshots must be labelled "simulated agents".
