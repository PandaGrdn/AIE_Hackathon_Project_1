# Task split — 3 people, 3 hours

Rule: **each person owns a disjoint set of files.** Nobody edits another owner's files; if you need
a change there, message them. Everyone commits straight to `main`, small commits, `git pull --rebase`
before every push. Disjoint files = no conflicts.

**Shared files (Ethan Liu only):** `core/models.py`, `core/config.py`, `pyproject.toml`, `README.md`.
Need a new field or dependency? Post it in chat; Ethan Liu adds it within minutes.

**Tests:** add yours under `tests/` in a file named for your area (`test_swarm_*.py`, `test_harness_*.py`, …).

---

## Ethan Liu — orchestration, decomposition, eval, demo

**Owns:** `core/orchestrator.py`, `core/cli.py`, `core/decompose/`, `eval/`, `demo_task/`, `docs/`, `ui/`, shared files above.

| Hour | Deliverable |
| --- | --- |
| 1 | `git init` + commit in `demo_task/repo`. Decompose `demo_task/task.md` with SubQ and check the 4 subtasks have correct pinned interfaces. Write 3 held-out subtasks in `eval/heldout/` (spec, interface.json, reference impl) on different domains with the same bug patterns. |
| 2 | Implement `eval/heldout_runner._run_one` (swarm with lesson ids → cross-test with reference as an always-trusted run → `correct`, `repeats`). Add `git commit` in `orchestrator._commit`. Run subtask 1 end to end once Lucas's runner works. |
| 3 | Full run on all 4 subtasks; cache the run as the demo fallback (`runs/demo/`). `eval/metrics.py` numbers. Minimal `ui/` (changelog + trust table is enough). Record the video, submit. |

## Lucas Chen — swarm agents and LLM clients

**Owns:** `core/llm/`, `core/swarm/`, `docker/`, `.env.example`.

| Hour | Deliverable |
| --- | --- |
| 1 | Get keys working: SubQ, Meta, Gemini, Anthropic. Finish tool-call mapping in `GeminiClient.chat` and `AnthropicClient.chat` (`core/llm/clients.py`). One agent implements demo subtask 1 for real via `run_one`, producing `steps.jsonl`-style `ReasoningStep`s and a diff. Implement the `run_spec_tests` tool in `runner.py` + `sandbox.py`: runs the subtask's **visible** spec cases against the sandbox and returns pass/fail per case; record the result on the preceding `ReasoningStep` (`live_results`). |
| 2 | Full roster of 6 in parallel (`run_swarm`), timeouts and token budgets enforced, `Sandbox.diff()` for the copy backend. Make agents reliably call `log_step` before every action (tighten `prompts.py` if they skip it). |
| 3 | Reliability: retries on API errors, graceful failure per agent, cost/time logging per run. Help with the live run and demo fallback. |

## Ethan Li — harness, reasoning paths, reflector, changes, store

**Owns:** `core/harness/`, `core/paths/`, `core/reflect/`, `core/changes/`, `core/store/`.

| Hour | Deliverable |
| --- | --- |
| 1 | **Spec tests** (`core/harness/spec_tests.py`): a test-author model writes cases with expected values and metamorphic properties from spec + interface + base repo only, cached per subtask, run before the swarm; **split into visible (~40%) and hidden** (`SpecTests.visible_ids`); wire into `cross_test` as `source="spec"` / `source="property"` with the verdict on hidden + properties only. Reflector live on SubQ: `reflect()` returns a real lesson from a failed run (`core/reflect/reflector.py`; pass the failed run's actual outcome instead of the `_fmt` stub). |
| 2b | **Failure correlation** (`core/paths/divergence.py`): combine path divergence with locality (files_touched ∋ failing function's file; assumption close to the failing case) and live-test timing (`live_results` on steps); add counterfactual replay in `core/reflect/` (re-run from the candidate step with the consensus assumption; confirmed → region now, else `confidence ≤ 0.3`, no region until a second hit). |
| 1b | In `cross_test`: a run is **verified iff it passes every hidden spec case and property**; fuzz majority is secondary evidence only and never fails a run alone. No cross-running of agents' own tests. Spec-vs-majority disagreement surfaces as an ambiguity flag, not a failure. |
| 2 | MongoDB: `MongoStore` wired, `ensure_indexes`, `regions_near` via `$vectorSearch`. Tune `dedup.py` thresholds (`BASE_RADIUS`, similarity) on real Gemini embeddings. Check `find_divergence` picks the right step on real paths; adjust `divergence_sigma` / `N_BINS`. |
| 3 | `proposer.py`: cluster adopted lessons → `ChangeKind.tool` proposal (reflector writes a small tool). Trust leaderboard data for the UI. Help debug the gate on the full run. |
| any | **Fuzzer gaps** found against the held-out references (`core/harness/strategies.py`): (a) `str` params draw random text, so an enum-like arg (`item_type`) never gets a valid value — have the spec-test author emit the valid literals and feed them to the strategy; (b) `int` draws ±10000 so bounded domains (scores 0–100) are almost all out of range — bias toward the boundary values the spec names; (c) no boundary examples for `dict[...]`, so seed cases pass `None` and implementations disagree on the exception type — add `{}` and a one-entry dict as boundaries. |

---

## Integration checkpoints

- **+1h:** Lucas's one agent run + Ethan Li's reflector + Ethan Liu's decomposition → run `swarm run demo_task/task.md --no-heldout` on subtask 1 together.
- **+2h:** full swarm + Mongo + held-out runner → a lesson is adopted or rolled back on a real held-out run.
- **+2h30:** freeze features. Full run, cache it, fix only what breaks the demo.
- **+3h:** video recorded, repo public, submitted.

## If behind

Cut in this order: tool generation → UI → Mongo (stay on `MemoryStore`) → full 6-agent roster (use 3).
Never cut: swarm run, cross-test, reflector lesson, held-out adopt/rollback, changelog output.
