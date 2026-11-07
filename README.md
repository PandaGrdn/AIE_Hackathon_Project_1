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

## Layout

```
core/
  decompose/   long task → subtasks, interface pinning            (owner: A)
  context/     lesson retrieval, failure-region live check        (owner: D)
  swarm/       router, runner, sandbox, prompts, log_step tool    (owner: B)
  paths/       step embedder, path alignment, divergence          (owner: C)
  harness/     hypothesis strategies, cross-test, majority        (owner: C)
  reflect/     reflector, lesson schema, dedup                    (owner: D)
  changes/     change proposer, held-out gate, versioning         (owner: D)
  store/       MongoDB + in-memory store                          (owner: D)
  llm/         thin clients for SubQ, Meta, Gemini, Anthropic     (owner: B)
  orchestrator.py   the per-subtask loop
  cli.py
demo_task/     the long task and the repo it runs against         (owner: A)
eval/          held-out subtasks, reference impls, metrics        (owner: A)
ui/            reasoning map, changelog, trust leaderboard
docker/        agent sandbox image
tests/
```

Everything in this repo was built during the hackathon. Third-party libraries are listed in `pyproject.toml`.
