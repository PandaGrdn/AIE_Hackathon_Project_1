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
| 1 | Get keys working: SubQ, Meta, Gemini, Anthropic. Finish tool-call mapping in `GeminiClient.chat` and `AnthropicClient.chat` (`core/llm/clients.py`). One agent implements demo subtask 1 for real via `run_one`, producing `steps.jsonl`-style `ReasoningStep`s and a diff. |
| 2 | Full roster of 6 in parallel (`run_swarm`), timeouts and token budgets enforced, `Sandbox.diff()` for the copy backend. Make agents reliably call `log_step` before every action (tighten `prompts.py` if they skip it). |
| 3 | Reliability: retries on API errors, graceful failure per agent, cost/time logging per run. Help with the live run and demo fallback. |

## Ethan Li — harness, reasoning paths, reflector, changes, store

**Owns:** `core/harness/`, `core/paths/`, `core/reflect/`, `core/changes/`, `core/store/`.

| Hour | Deliverable |
| --- | --- |
| 1 | Reflector live on SubQ: `reflect()` returns a real lesson from a failed run (`core/reflect/reflector.py`; pass the failed run's actual outcome instead of the `_fmt` stub). Cross-test matrix in `core/harness/crosstest.py` (each run's `tests/` × every impl). |
| 2 | MongoDB: `MongoStore` wired, `ensure_indexes`, `regions_near` via `$vectorSearch`. Tune `dedup.py` thresholds (`BASE_RADIUS`, similarity) on real Gemini embeddings. Check `find_divergence` picks the right step on real paths; adjust `divergence_sigma` / `N_BINS`. |
| 3 | `proposer.py`: cluster adopted lessons → `ChangeKind.tool` proposal (reflector writes a small tool). Trust leaderboard data for the UI. Help debug the gate on the full run. |

---

## Integration checkpoints

- **+1h:** Lucas's one agent run + Ethan Li's reflector + Ethan Liu's decomposition → run `swarm run demo_task/task.md --no-heldout` on subtask 1 together.
- **+2h:** full swarm + Mongo + held-out runner → a lesson is adopted or rolled back on a real held-out run.
- **+2h30:** freeze features. Full run, cache it, fix only what breaks the demo.
- **+3h:** video recorded, repo public, submitted.

## If behind

Cut in this order: tool generation → UI → Mongo (stay on `MemoryStore`) → full 6-agent roster (use 3).
Never cut: swarm run, cross-test, reflector lesson, held-out adopt/rollback, changelog output.
