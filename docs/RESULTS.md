# Results: recorded live run, 2026-11-07

Real agents, real repo (MCP Python SDK), no `--fake`. Sources: `demo/task_a_summary.json`, `demo/task_a.log`, `runs/record_demo.log`, `demo/task_b_baseline_summary.json`, `demo/mcp_snapshot.json`. Time window 14:51-16:05 PT (22:51-00:05 UTC); the first session ran 22:51-23:08 UTC, the final Task A pass ran 23:31-00:05 UTC.

## 1. What was run

- Task A: "Per-tool rate limits and quotas for MCPServer", 7 subtasks, held-out gate on (3 slices: tool timeout, tool deprecation, resource size cap).
- Task B baseline (B0): "Tool timeouts, deprecation and resource size caps", 3 subtasks, no lessons, fresh store.
- Swarm: gemini-3.8-flash, gemini-3.5-flash-lite (weak, hot), claude-sonnet-5-5, meta muse-spark-1.3. 4 agents per subtask; 1 agent per held-out slice (`heldout_swarm_size 1`).
- Hidden spec tests: Gemini 3.8 Flash, calibrated against a reference implementation (tests the reference failed were dropped). Tests per subtask: 14, 25, 17, 22, 28, 24, 19.
- SubQ Snap 1.1 (Decisions API): localizes the root-cause step, classifies the failure kind, estimates P(generalizes) for each failed run. It judges, it does not write.
- Budgets: 3,000,000 tokens per run (1,000,000 in the first session), 480 s agent timeout, 40 max turns, regression paths `tests/server tests/shared`.
- Wall time of the final pass: Task A 2013 s (S2-S4 only, resumed), B0 1269 s (ran concurrently).

## 2. Task A, per subtask

"Verified" = passes every hidden spec test. Counts are runs out of 4 agents.

| Subtask | Hidden tests | Verified | Retried | Lessons written | Gate decision | Committed |
|---|---|---|---|---|---|---|
| S0 Limit policy model | 14 | 4/4 | no | 0 | none | yes |
| S1 Attach policies to tools | 25 | 3/4 | no | 1 | 1 rolled back | yes |
| S2 Enforcement on call | 17 | 1/4 | no (final pass) | 3 | 3 rolled back | yes (claude-t2) |
| S3 Client-visible metadata | 22 | 4/4 | no | 0 | none | yes |
| S4 Session scope | 17 per log (28 in calibrate output) | 0/4, retry 0/4 | yes, 4 provisional lessons | 8 | none (no change committed) | no, stalled |
| S5, S6 | 24, 19 | not attempted | | | | |

- 4 of 7 subtasks committed; stalled at S4.
- Final pass (S2-S4): 16 runs, 5 verified (31.3%). First session (S0-S2, 1M budget): 7 of 12 runs verified.
- S1 in the first session had one run end with "token budget exceeded" (1,038,119 > 1,000,000 tokens); it is counted as failed.
- Open discrepancy: the S4 log header reports 17 hidden tests, the calibration list has 28 for S4. Not resolved here.
- Task A gate summary: 7 changes, 4 routing changes adopted without gating (trust scores), 3 context (lesson) changes rolled back. Whole snapshot: 18 lessons (14 candidate, 4 rolled_back, 0 adopted), 12 changes (8 routing adopted, 4 context rolled_back).

## 3. The S2 arc

1. Session 1 (1M budget): all 4 S2 runs ended with "token budget exceeded", 0/4.
2. Budget raised to 3M with history compaction: 0/4 again at 23:14 UTC; no lessons were carried, and the run crashed while writing its summary (fixed).
3. 23:20 UTC: 0/4, then 4 provisional lessons from those failures, retry gave 1/4 verified.
4. That 1/4 was reported "ambiguous, no verified majority; not advancing": a majority-agreement rule bug. Fixed to an absolute hidden-test verdict (one verified run is enough to commit the best one).
5. 23:31 UTC final pass: S2 gave 1/4 at first attempt (claude-t2), committed. The 3 lessons from the 3 failures were gated and all rolled back.

## 4. SubQ root-cause statistics (snapshot, n = 18 divergences)

- Total runs in snapshot: 62 (12 verified, 33 failed, rest errored). 18 failed runs have a divergence record; all 18 have SubQ step probabilities and all 18 have "subq" in `signals`.
- Max step probability: median 0.45, range 0.18-0.83. p >= 0.7: 3 of 18 (17%). p >= 0.5: 8 of 18 (44%).
- Kind: bug 17, acceptable_variation 1. Kind confidence >= 0.9: 11 of 18.
- P(generalizes): median 0.26, range 0.12-0.65; 2 of 18 above 0.5.
- Signals: subq only 15; path + subq 1; subq + path + subq 1; subq + subq-moved 1. `confirmed` true: 2 of 18.
- Agreement with the embedding-path signal: only 2 divergences carry both signals; in 2 of 2 the SubQ argmax step equals the path step. n = 2 is too small to support a rate.
- Observation from the lesson text: several lessons are process advice (do not use the test runner to dump files, read truncated files in chunks), not feature-specific advice.

## 5. Gate outcome

- Every lesson scored 0.0 -> 0.0 on the held-out slices (`heldout_accuracy_first 0.0`). No lesson was adopted by the gate.
- Cause: the three MCP held-out slices are too hard for a 1-2 agent swarm; none passed any hidden test with or without a lesson, so the gate had no signal to separate lessons. A 0.0 -> 0.0 result is "no measurable improvement", not evidence that the lessons are bad.
- "Selective" claim therefore holds in one direction only: the gate refused to keep anything it could not confirm (4 context changes rolled back in the snapshot, 14 lessons left as candidates). It did not confirm any lesson as useful.
- Routing (trust) changes are applied without gating by design.

## 6. Task B

### B0 baseline (no lessons), `demo/task_b_baseline_summary.json`

| Subtask | First attempt verified | Final verified | Retried | Committed |
|---|---|---|---|---|
| Per-tool call timeout | 1/4 | 1/4 | no | yes |
| Tool deprecation flags | 0/4 | 2/4 | yes (own provisional lessons) | yes |
| Resource size cap | 0/4 | 0/4 (retry 0/4) | yes | no, stalled |

- 2 of 3 subtasks done; 3 of 20 runs verified (15%), 9 failed, 8 errored.
- 15 lessons written, 13 changes (3 routing adopted, 10 candidates), 1 repeat mistake of 16 reflected failures (6.3%). Wall 1269 s.
- The summary's `first_pass_verified_share` for deprecation is 0.25 (computed over all 8 runs); the first-attempt block shows 0/4.
- "No lessons" here means no lessons from Task A; the retry step uses provisional lessons from the subtask's own failed first attempt.

### B1 (with provisional lessons): pending

B1 injects Task A's provisional (ungated) lessons, because the gate adopted none. Caveat: this is not a gated-lessons comparison. Fill from `demo/results.json` when it exists:

| Field | B0 | B1 |
|---|---|---|
| subtasks_done | 2/3 | TBD |
| verified_share | 0.15 (3/20) | TBD |
| first_pass_verified_share, timeout | 0.25 | TBD |
| first_pass_verified_share, deprecation | 0.00 (first attempt) | TBD |
| first_pass_verified_share, size cap | 0.00 | TBD |
| failed_runs | 9 | TBD |
| lessons_needed (lessons created) | 15 | TBD |

With 3 subtasks and 4 runs each, one run is 8 points of share; report counts next to shares.

## 7. Engineering fixes made during the run

- Token budget: 1M -> 3M per run, plus history compaction; S1 and all S2 runs had hit the 1M cap.
- Verdict: majority-agreement rule replaced by an absolute hidden-test verdict (S2 1/4 was wrongly "ambiguous").
- Retry with lessons: after a subtask with no verified run, retry once with that attempt's provisional lessons, tagged as provisional.
- Spec calibration: hidden tests the reference implementation failed were dropped (`scripts/calibrate_specs.py`).
- Summary crash at the end of a run (23:08 UTC) fixed; resume from `--start-subtask 2` on the `record-demo/a` branch added.
- Held-out gate: pass fraction of hidden tests as slice accuracy; `--regate-blind`, `--max`, `--out` for gate_leftovers.

## 8. What we would claim, and what we would not

Claim:
- The loop ran end to end with real agents: hidden-test verification, per-failure root-cause localization by SubQ (18 divergences, all with step probabilities), lesson generation, gating, rollback, retry, commit.
- 4 of 7 Task A subtasks committed with independently written tests; the gate rolled back 4 lesson changes and adopted none.
- Failures were recorded honestly: 3 harness bugs and one budget cap were found and fixed during the run.

Do not claim:
- That agents got smarter (models are unchanged), or that lessons improved pass rates: no lesson was confirmed by the gate (0.0 -> 0.0).
- That SubQ localization is accurate: only 3 of 18 have p >= 0.7 and only 2 divergences can be cross-checked against the path signal.
- That B1 vs B0 is a gated before/after: B1 uses ungated provisional lessons, and the held-out slices are Task B's own subtasks.
- Anything from the `--fake` runs as evidence of real coding behavior.
