# How we show the system got better

The claim is "the system improves as it works, and only keeps changes that held-out work confirms." Every number below is produced by the run itself (`eval/metrics.py`, the changelog, the snapshot); none is hand-computed.

## The demo recording

One full real run on the MCP long task (7 subtasks, `demo_task/mcp/task.md`) with live agents, recorded as `demo/mcp_snapshot.json` plus the terminal log. The held-out set is the three MCP slices in `eval/heldout/mcp_*` (per-tool timeout, tool deprecation, resource size cap): same repository, same reasoning patterns (registration-time validation, inclusive vs exclusive boundaries, metadata only when configured, exact error messages), never used to produce a lesson.

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
