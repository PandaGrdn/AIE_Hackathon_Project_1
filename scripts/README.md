# scripts

## real_run.py — one live swarm run

Runs the real loop (`core.orchestrator.run_task`) on chosen subtasks with live agents:

```bash
source .venv/bin/activate
python scripts/real_run.py demo_task/mcp/task.md --subtasks 0 --swarm-size 3 \
    --snapshot demo/mcp_real_s1.json --reset-repo
```

- **Decomposition** is offline (`subtasks.json` next to the task file); `--subtasks 0,2` or `1-3` picks indices.
- **Roster**: `swarm_identities()` filtered by `provider_available()`, then narrowed by `--agents`
  (default `gemini-t2,gemini-t7-alt,meta-spark-t5`; `all` = every identity with a key), capped at `--swarm-size`.
  The router is handed exactly this roster.
- **Spec tests**: `load_or_author` (Gemini author, cached under `runs/spec_tests/<hash>.json`). The script
  checks the modules parse, import the target module and have at least 5 tests.
- **Verdict**: hidden spec modules plus the regression suite (`--regression`, default
  `tests/server tests/shared`) run in every sandbox on the base repo's `.venv` python, with the sandbox's
  `src/` placed ahead of the venv's editable install.
- **Root cause**: SubQ Snap when `SUBQ_API_KEY` is set. **Lessons**: Gemini.
- **Held-out gate: off** (`run_heldout=None`). Proposed changes are stored as candidates and not gated.
  The snapshot's `real_run` key says so.
- Budgets per agent: `--agent-timeout 360` seconds, `--max-turns 40`, `--token-budget 1000000` (cumulative; every turn resends the history). A run stopped by a budget is still graded on what it wrote. `--n-fuzz 10` sets fuzz draws per
  interface function; fuzz results are evidence only when spec tests exist.
- `--isolate` runs on a fresh `git clone --local` of the task repo under `runs/real_repo/<task>` (base commit,
  `.venv` symlinked), so a winning commit and the agents' worktrees never touch `demo_task/mcp/repo`. Use it
  when anything else (the fake path, the UI demo) shares the task repo.
- `--reset-repo` runs `git -C <repo> checkout -q <base_commit> -B swarm` (the base commit comes from
  `<task dir>/base_commit`) and prunes stale worktrees.

Output: per run, the verdict, steps, diff size, time, turns and tokens, hidden/visible spec-test results,
regression failures, the SubQ root-cause step for each failed run, and the lesson text. The same data
goes into the snapshot under `real_run`. Each run also gets `runs/<run_id>/` (code copy + `meta.json`).

Exit code 0 when every selected subtask verified and committed.

## record_demo.py — the two-task demo recording

Task A (rate limits, 7 subtasks, held-out gate on the three MCP slices) learns lessons; Task B
(`demo_task/mcp_heldout`: timeout → deprecation → size cap, built from those slices) runs twice, without and
with them. See docs/METRICS.md, "The demo recording", for what `results.json` reports.

```bash
source .venv/bin/activate
python scripts/record_demo.py --dry-run --check-references   # preconditions + plan; no API calls, no repo changes
python scripts/record_demo.py --swarm-size 3 --heldout-swarm-size 1 --out demo/ --phase all
```

| `--phase` | Store | Spec tests | Gate | Writes (under `--out`) |
| --- | --- | --- | --- | --- |
| `a` | fresh | authored (`load_or_author`) | on, `eval/heldout/mcp_*` only | `mcp_snapshot.json`, `task_a_summary.json`, `task_a.log` |
| `b0` | fresh | slices' hidden tests | off | `task_b_baseline.json`, `task_b_baseline_summary.json`, `task_b_baseline.log` |
| `b1` | `load_snapshot(mcp_snapshot.json)` | slices' hidden tests | off | `task_b_with_lessons.json`, `task_b_with_lessons_summary.json`, `task_b_with_lessons.log` |
| `compare` | none | none | none | `results.json`, `compare.log` (no API calls) |

- `--phase all` runs a, b0, b1, compare and skips a phase whose summary already exists (resume after a crash);
  `--redo` reruns it. A phase that raises stops the sequence, still writes its summary (`error`) and snapshot.
- **Repo**: each agent phase resets `demo_task/mcp/repo` to the base commit on branch `swarm`
  (`eval.heldout_specs.reset_repo`: worktrees pruned, edits stashed, old HEAD kept as `record-demo/pre-<phase>`;
  the clone is never deleted) and keeps its result on branch `record-demo/<phase>`.
- **Roster**: `--agents` (default `gemini-t2,gemini-t7-alt,meta-spark-t5`, as real_run.py); the router sees
  exactly that list, so b0 and b1 route the same agents. `all` = every identity with a key, ranked by trust.
- **Task B spec tests**: `eval.heldout_specs.spec_from_hidden_tests` returns the slice's `hidden_tests/*.py`,
  all hidden, flattened so every parametrized case (and the anyio backend) is its own top-level test, which is
  how `cross_test` keys spec tests.
- `--b1-no-trust` drops Task A's trust scores in b1 so only lessons and failure regions differ from b0.
- Budgets as real_run.py: `--agent-timeout 360`, `--max-turns 40`, `--token-budget 1000000`, `--n-fuzz 10`,
  `--regression "tests/server tests/shared"`.
