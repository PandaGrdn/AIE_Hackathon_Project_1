# Self-Improving Coding Swarm

**AIE Code Summit Hackathon, Nov 7 2026 · Track: The System That Gets Better**

An autonomous coding system that completes a long, tightly coupled task subtask by subtask. For each subtask a swarm of independent agents implements it, cross-testing picks the verified majority, and that result is committed. Each failed agent's reasoning path is compared with the swarm's consensus path in embedding space to find where its reasoning went wrong; a reflector writes a lesson for that error. Lessons, trust-based routing and generated tools are versioned changes that must prove themselves on held-out subtasks or get rolled back.

Full design: [docs/PROJECT_OUTLINE.md](docs/PROJECT_OUTLINE.md)

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in keys
pytest
```

## Run

```bash
swarm run demo_task/task.md          # run the long task end to end
swarm eval                            # run held-out eval and print metrics
swarm reflect --run-id <id>           # re-run reflection on a stored run
```

## Layout and owners

Task split, hour by hour: [docs/TASKS.md](docs/TASKS.md). Each person owns disjoint files.

```
core/
  decompose/   long task → subtasks, interface pinning            Ethan Liu
  context/     lesson retrieval, failure-region live check        Ethan Li
  swarm/       router, runner, sandbox, prompts, log_step tool    Lucas Chen
  paths/       step embedder, path alignment, divergence          Ethan Li
  harness/     hypothesis strategies, cross-test, majority        Ethan Li
  reflect/     reflector, lesson schema, dedup                    Ethan Li
  changes/     change proposer, held-out gate, trust              Ethan Li
  store/       MongoDB + in-memory store                          Ethan Li
  llm/         thin clients for SubQ, Meta, Gemini, Anthropic     Lucas Chen
  orchestrator.py   the per-subtask loop                          Ethan Liu
  cli.py                                                          Ethan Liu
  models.py, config.py   shared schemas and settings              Ethan Liu (ask for changes)
demo_task/     the long task and the repo it runs against         Ethan Liu
eval/          held-out subtasks, reference impls, metrics        Ethan Liu
ui/            changelog, trust leaderboard, reasoning map        Ethan Liu
docker/        agent sandbox image                                Lucas Chen
tests/         one file per area, named after it
```

Everything in this repo was built during the hackathon. Third-party libraries are listed in `pyproject.toml`.
