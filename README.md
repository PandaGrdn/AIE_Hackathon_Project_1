# Self-Improving Coding Swarm

**AIE Code Summit Hackathon, Nov 7 2026 · Track: The System That Gets Better**

An autonomous coding system that completes a long, tightly coupled task subtask by subtask. Before any agent runs, an independent test author (Gemini) reads only the subtask spec, the pinned interface and the base repo and writes hidden acceptance tests and properties. SubQ Snap, a decision model, is the judge: it resolves disagreements between the test author and the swarm from the spec alone, confirms which reasoning step caused each failure, and scores every lesson. A swarm of independent agents then implements the subtask; a run is verified when it passes every hidden test and property, and the highest-trust verified implementation is committed. Agents' own tests play no role in the verdict. Each failed agent's reasoning path is compared with the swarm's consensus path in embedding space to find where its reasoning went wrong; a reflector writes a lesson for that error. Lessons, trust-based routing and generated tools are versioned changes that must prove themselves on held-out subtasks or get rolled back.

Full design: [docs/PROJECT_OUTLINE.md](docs/PROJECT_OUTLINE.md) · Recorded-run results: [docs/RESULTS.md](docs/RESULTS.md) · Metrics: [docs/METRICS.md](docs/METRICS.md) · Demo runbook: [docs/DEMO.md](docs/DEMO.md)

## What happened in the recorded live run (Nov 7, 14:51–16:40 PT)

Real agents (Gemini 3.8 Flash, Gemini 3.5 Flash-Lite, Claude Sonnet 5.5, Meta Muse Spark 1.3), 4 per subtask, on the MCP Python SDK at `77001c9`. Hidden spec tests were written by Gemini from the spec alone and calibrated against a reference; the full 2,389-test regression suite ran in every sandbox. SubQ Snap 1.1 (Decisions API) localized the root-cause reasoning step for every failed run.

- **Task A** (per-tool rate limits, 7 subtasks): 4 of 7 subtasks verified and committed (S0 4/4, S1 3/4, S2 1/4, S3 4/4; S4 0/4 then 0/4 on the retry with 4 injected lessons, stalled). 62 agent runs, 18 lessons, 18 SubQ root-cause decisions (3 at P ≥ 0.7; 17 classified `bug`). The held-out gate rolled back every lesson it scored (no held-out signal with 1–2 agents per slice): 0 adopted.
- **Retry with lessons rescues all-fail subtasks** (the loop learning inside a subtask): when a first attempt had 0 verified runs, the failures were reflected into lessons and the subtask retried once with them injected. Counts per phase are in [docs/RESULTS.md](docs/RESULTS.md) (`scripts/rescue_stats.py` computes them from the summaries).
- **Task B** (held-out long task: tool timeouts → deprecation → resource size cap), run twice: baseline (no lessons) and with Task A's provisional lessons injected. The comparison is in `demo/results.json` and RESULTS.md. These lessons were mostly tool-use habits, not SDK semantics, so cross-task transfer is small; we say so.
- Offline fake-agent run on the same 7 subtasks (`demo/mcp_fake_snapshot.json`, labelled simulated): 7/7, 2 lessons adopted by the gate, held-out 0.50 → 0.71, 1 rolled back.

Snapshots for the UI are in `demo/` (`mcp_snapshot.dense.json` is the real Task A run with interpolated reasoning paths).

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
