# Pricing task (offline fallback)

A small, self-contained long task: build a tiered-pricing module (`tier_for` → `discount_rate` →
`quote` → `summarize`) in the near-empty `pricing` package under `repo/`. Each subtask depends on
the previous one's pinned interface, and the specs are full of boundary traps (exactly 100 and
1000, "at least 50", round half to even, the empty list). It needs no external repo and no network,
which makes it the demo fallback when the real MCP task cannot run.

- `task.md` — the task description (`repo: repo`).
- `subtasks.json` — the hand-authored decomposition; `decompose` loads it instead of calling an LLM.
- `repo/` — the starting codebase (only `pricing/__init__.py`).
- `reference/` — correct implementations mirroring the repo layout (`reference/pricing/tiers.py`, …).
  Used only by `--fake`, never shown to agents.

## Run

```bash
swarm run demo_task/pricing/task.md --fake   # offline: fake swarm/spec/reflector, reference = ./reference
swarm run demo_task/pricing/task.md          # real swarm (needs API keys in .env)
```

`repo/` lives inside this checkout, so without its own `.git` the orchestrator copies verified
files in but does not commit them. For per-subtask commits run
`git init && git add -A && git commit -m init` inside `repo/` first.
