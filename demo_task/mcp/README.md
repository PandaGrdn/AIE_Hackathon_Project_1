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
| `reference/` | correct implementation mirroring the repo layout (`reference/src/mcp/server/mcpserver/limits.py`, …); `--fake` only, never shown to agents |
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
# offline: fake decomposer/spec/swarm/reflector, reference dir = demo_task/mcp/reference (the default)
swarm run demo_task/mcp/task.md --fake

# real swarm (API keys in .env); reset first to start from the base commit
bash demo_task/mcp/setup.sh --reset --no-tests
swarm run demo_task/mcp/task.md
```

`--reference-dir` defaults to `<task dir>/reference`, so pass it only to point at a different
reference. If the MCP task cannot run (no network, setup fails), fall back to the offline pricing
task: `swarm run demo_task/pricing/task.md --fake`.
