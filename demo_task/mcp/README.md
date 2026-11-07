# MCP task: per-tool rate limits and quotas

The real long-horizon demo task. The swarm adds per-tool rate limits and quotas to the high-level
server of the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), pinned at commit
`77001c9` (`base_commit`): a pure policy model and decision function, policies attached to tools,
enforcement in `call_tool`, limit metadata in `list_tools`, session-scoped budgets, a status resource
and an `mcp limits` CLI subcommand. Each subtask builds on the previous commit and must keep the
SDK's existing suite green. Nothing like this exists upstream. The pinned names, files, error
messages and held-out slices are in [CONTRACT.md](CONTRACT.md); the task the swarm reads is
[task.md](task.md), decomposed by [subtasks.json](subtasks.json).

| Path | What |
| --- | --- |
| `task.md`, `subtasks.json` | task description (`repo: repo`) and hand-authored decomposition |
| `reference/` | the reference as a patch series, one per subtask (`01-limits-model.patch` … `07-cli-limits.patch`, with `apply.sh`); `--fake` only, never shown to agents |
| `setup.sh`, `base_commit` | clone + install script and the pinned commit |
| `repo/` | the SDK clone, created by `setup.sh`; gitignored, has its own `.git` so every verified subtask is committed there |

Held-out slices for the gate live in `eval/heldout/mcp_*/`.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and network for the first run.

```bash
swarm setup demo_task/mcp          # same as: bash demo_task/mcp/setup.sh
```

This clones the SDK into `repo/` (blobless clone, checked out on branch `swarm` at `77001c9`), runs
`uv sync --all-extras --python 3.12` inside it, and runs the baseline suite
(`uv run pytest -q tests/server tests/shared -p no:cacheprovider`, about 2389 tests in ~11 s),
printing the count. Re-running it keeps an existing clone, including subtask commits on top of the
base. `bash demo_task/mcp/setup.sh --reset` discards them and returns to the base commit;
`--no-tests` skips the baseline run.

## Run

```bash
# offline: fake spec/swarm/reflector driven by the reference patch series (SubQ root-cause still runs when SUBQ_API_KEY is set)
swarm run demo_task/mcp/task.md --fake --snapshot demo/mcp_snapshot.json

# real swarm (API keys in .env); reset first to start from the base commit
bash demo_task/mcp/setup.sh --reset --no-tests
swarm run demo_task/mcp/task.md
```

### What `--fake` does here

`reference/` holds one patch per subtask (`01-limits-model.patch` … `07-cli-limits.patch`, same order as
`subtasks.json`), so `--fake` takes the patch-series path instead of the pricing-style reference dir:

- **Decomposition**: the precomputed `subtasks.json` (no LLM).
- **Reference for subtask i**: `repo` at HEAD (subtasks < i already committed there) + patch i, materialized
  once under `runs/mcp_ref/<i>/repo` and reused while HEAD and the patch are unchanged.
- **Spec tests**: patch i's test module (`tests/server/mcpserver/test_limits_s<i>_*.py`), all hidden; the
  verdict runs it plus the repo's own `tests/server` + `tests/shared` in every sandbox with the repo's
  `.venv` interpreter and the sandbox's `src/` first on `sys.path` (set `REGRESSION_TEST_PATHS` to change the
  regression dirs).
- **Swarm**: every run is a copy of the reference tree (hard links, no `.git`/`.venv`) under `runs/<run id>/`;
  the last run flips one comparison or `is None` check inside patch i's added source lines, the first flip
  patch i's tests catch (cached in `runs/mcp_ref/<i>/mutant_0.json`), with an off-consensus reasoning step
  such as "a call exactly per_seconds old is still inside the window".
- **Commit**: the winner's `subtask.files` and `tests/` are copied into `repo/` and committed on branch `swarm`.
- **Held-out gate**: `eval/heldout/` with the same fakes; the `mcp_*` patch slices use the slice's own
  `reference.patch` for faithful runs and a mutant its `hidden_tests/` catch.

Because each run commits into `repo/`, `--fake` resets the clone to `base_commit` first whenever it has
commits or edits on top of it (`--reset-repo` forces it, `--no-reset-repo` continues from the current HEAD;
`bash demo_task/mcp/setup.sh --reset` does the same by hand). `--reference-dir DIR` switches back to the
pricing-style reference dir. If the MCP task cannot run (no network, setup fails), fall back to the offline
pricing task: `swarm run demo_task/pricing/task.md --fake`.
