# How we show the system got better

The claim is "the system improves as it works, and only keeps changes that held-out work confirms." Every number below is produced by the run itself (`eval/metrics.py`, the changelog, the snapshot); none is hand-computed.

## The demo recording

Two long tasks on the MCP Python SDK (same clone, `demo_task/mcp/repo` at `77001c9`), live agents, recorded by `scripts/record_demo.py` (see `scripts/README.md`):

- **Task A**: per-tool rate limits (`demo_task/mcp/task.md`, 7 subtasks), held-out gate on. The gate scores every candidate change on the three MCP slices in `eval/heldout/mcp_*` (per-tool timeout, tool deprecation, resource size cap): same repository, same reasoning patterns (registration-time validation, inclusive vs exclusive boundaries, metadata only when configured, exact error messages). Lessons come only from Task A's own failures. Output: `demo/mcp_snapshot.json`, `demo/task_a_summary.json`, `demo/task_a.log`.
- **Task B**: the three slices joined into one long task (`demo_task/mcp_heldout/task.md`: timeout → deprecation → size cap, each building on the previous commit). Spec tests are the slices' verified hidden tests (`eval.heldout_specs.spec_from_hidden_tests`, all hidden, no authored tests), held-out gate off. It runs twice from the base commit with the same agents:
  - **B0, baseline**: fresh store, no lessons → `demo/task_b_baseline*.json`, `.log`.
  - **B1, with lessons**: the store from Task A's snapshot (adopted lessons, failure regions, trust) → `demo/task_b_with_lessons*.json`, `.log`. `--b1-no-trust` drops the trust scores so that only lessons and regions differ.

`demo/results.json` reports, per phase: subtasks done, per-subtask verified/failed/errored counts and first-pass verified share (verified runs / runs on the subtask's first and only swarm), lessons created, changes adopted/rolled back (Task A only), repeat-mistake rate (reflected failures in this phase that deduplicated into an existing lesson / all reflected failures in this phase), wall time. Then `b0_vs_b1`:

| Field | B0 vs B1 |
| --- | --- |
| `subtasks_done` | subtasks verified and committed before the first stall |
| `verified_share` | Σ verified / Σ runs over the subtasks each phase attempted, with the delta B1 − B0 |
| `first_pass_verified_share` | per subtask, B0, B1, delta |
| `failed_runs` | runs the harness failed (hidden-test miss or regression) |
| `lessons_needed` | lessons the phase had to create from its own failures (fewer in B1 = Task A's lessons covered them) |
| `repeat_mistake_rate` | per phase, as above; in B1 a failure that lands in a Task A lesson counts as a repeat |
| `lessons_carried_into_b1`, `lessons_in_play_b1` | adopted lessons in the snapshot, and how many the context builder injected |

The claim it supports is narrow: on the same three features, the same swarm does better on a long task when it starts with the lessons Task A's gate adopted. It is not transfer to unseen work: those three slices are the gate's own held-out set, so they took part in choosing which lessons to keep. With 3 subtasks × 3 runs per phase, one run is 11 points of share; always show counts next to shares.

## Primary metric: held-out pass rate, with vs without the change

For every candidate change the gate runs the held-out slices twice, with and without it, and records both on the `Change`:

- `heldout_before.accuracy` → `heldout_after.accuracy`: share of swarm runs that pass the slice's hidden tests.
- A change is adopted only if accuracy rises (or repeat-mistake rate falls) with no new false blocks.

What to show: the Changelog rows (adopted green, rolled back red) and the numbers strip "held-out accuracy first → last". This is the theme's requirement made visible: proof on held-out work, rollback for the rest.

## Secondary metrics

| Metric | Source | What it shows |
| --- | --- | --- |
| Repeat-mistake rate | `eval.metrics.repeat_mistake_rate`: Σ(hit_count−1)/Σhit_count over lessons; also `heldout_repeat_rate_first → last` | Whether failures stop recurring in known failure regions as lessons accumulate |
| First-pass verified share per subtask | `per_subtask[i].verified / runs` from `run_task` | Rising across subtasks 1→7 as lessons are injected; the within-run learning curve |
| Changes adopted vs rolled back | `changes_by_kind` | The gate is selective; a system that adopts everything is not being tested |
| Root-cause confidence | `Divergence.step_probabilities` (SubQ) and `signals` agreement (subq + path) | How sharply failures localize; agreement between SubQ and the embedding path is corroboration |
| Spec-ambiguity flags | `report.ambiguity_flags` | Failures the system refused to charge to an agent because the spec was unclear |
| Cost | tokens and wall time per verified subtask (runner logs; SubQ `usage.input_tokens`, always 0 output) | Practicality |

## The before/after control

The cleanest comparison for the pitch: run the held-out slices with **no lessons** (`swarm eval` on a fresh store) and with **all adopted lessons** (`swarm eval` after the long task). Report accuracy and repeat-mistake rate for both. Same slices, same swarm, only the adopted lessons differ.

## What not to claim

- Not "the agents got smarter": the models are unchanged. The *system* got better through versioned context, routing and tools.
- Not "every lesson helps": show the rolled-back rows too; selectivity is the point.
- Fake (`--fake`) runs demonstrate the loop mechanics; only the real-agent run supports claims about real coding behaviour. Label which is which on screen.
