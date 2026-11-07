# Self-Improving Coding Swarm

**AIE Code Summit Hackathon, Nov 7 2026 · Track: The System That Gets Better**

An autonomous coding system that completes a long, tightly coupled task subtask by subtask. Before any agent runs, an independent test author (Gemini) reads only the subtask spec, the pinned interface and the base repo and writes hidden acceptance tests and properties. SubQ Snap, a decision model, is the judge: it resolves disagreements between the test author and the swarm from the spec alone, confirms which reasoning step caused each failure, and scores every lesson. A swarm of independent agents then implements the subtask; a run is verified when it passes every hidden test and property, and the highest-trust verified implementation is committed. Agents' own tests play no role in the verdict. Each failed agent's reasoning path is compared with the swarm's consensus path in embedding space to find where its reasoning went wrong; a reflector writes a lesson for that error. Lessons, trust-based routing and generated tools are versioned changes that must prove themselves on held-out subtasks or get rolled back.

Full design: [docs/PROJECT_OUTLINE.md](docs/PROJECT_OUTLINE.md)

## Setup

Python 3.12 exactly. Easiest with [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`):

```bash
uv python install 3.12
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env   # fill in keys
pytest
```

Without uv: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`.

## Run

Two long tasks, one folder each under `demo_task/`:

- **`demo_task/mcp/`: the real task.** Per-tool rate limits and quotas on the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) at a pinned commit ([README](demo_task/mcp/README.md), [contract](demo_task/mcp/CONTRACT.md)). Needs setup first: it clones the SDK into `demo_task/mcp/repo/`, installs it with uv and runs its baseline suite.
- **`demo_task/pricing/`: the offline fallback.** A tiered-pricing module on a near-empty package ([README](demo_task/pricing/README.md)). No setup, no network.

```bash
# real task
swarm setup demo_task/mcp                     # clone + uv sync + baseline tests (runs demo_task/mcp/setup.sh)
swarm run demo_task/mcp/task.md               # run the long task end to end (API keys in .env)
swarm run demo_task/mcp/task.md --fake        # offline, against demo_task/mcp/reference

# offline fallback: no API keys, no network
swarm run demo_task/pricing/task.md --fake    # fake swarm/spec/reflector, reference = demo_task/pricing/reference

swarm eval                                    # run held-out eval and print metrics (--fake offline)
swarm changelog                               # every versioned change and its held-out decision
```

`--fake` replaces every LLM call with the deterministic fakes in `eval/fake_swarm.py`; `--reference-dir` defaults to `<task dir>/reference`.

## Layout and owners

Task split, hour by hour: [docs/TASKS.md](docs/TASKS.md). Each person owns disjoint files.

```
core/
  decompose/   long task → subtasks, interface pinning            Ethan Liu
  context/     lesson retrieval, failure-region live check        Ethan Li
  swarm/       router, runner, sandbox, prompts, log_step tool    Lucas Chen
  paths/       step embedder, path alignment, divergence          Ethan Li
  harness/     SubQ spec tests + properties (verdict), fuzz, majority   Ethan Li
  reflect/     reflector, lesson schema, dedup                    Ethan Li
  changes/     change proposer, held-out gate, trust              Ethan Li
  store/       MongoDB + in-memory store                          Ethan Li
  llm/         thin clients for SubQ, Meta, Gemini, Anthropic     Lucas Chen
  harness/subq_judge.py   SubQ Decisions API: ambiguity, root cause, lesson score   Ethan Li
  orchestrator.py   the per-subtask loop                          Ethan Liu
  cli.py                                                          Ethan Liu
  models.py, config.py   shared schemas and settings              Ethan Liu (ask for changes)
demo_task/     one folder per long task: task.md, subtasks.json,   Ethan Liu
               repo/, reference/ (pricing = offline fallback;
               mcp = real task, repo/ cloned by setup.sh)
eval/          held-out subtasks, reference impls, metrics        Ethan Liu
ui/            changelog, trust leaderboard, reasoning map        Ethan Liu
docker/        agent sandbox image                                Lucas Chen
tests/         one file per area, named after it
```

Everything in this repo was built during the hackathon. Third-party libraries are listed in `pyproject.toml`.
