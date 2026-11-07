# Self-Improving Coding Swarm — Project Outline (name TBD)

**Track:** The System That Gets Better
**Hackathon:** AIE Code Summit Hackathon, Nov 7 2026 · Submissions due 5:00 PM

---

## 1. One-liner

An autonomous coding system that completes a long, tightly coupled software task as a sequence of subtasks. For each subtask a swarm of independent agents implements it, cross-testing picks the verified majority, and that result is committed. Every agent that failed is reflected on: its **reasoning path is compared with the swarm's consensus path in embedding space** to find the exact step where its reasoning went wrong, and a **lesson** is written to fix that reasoning error. Lessons, trust-based routing and generated tools are **versioned changes** that must prove themselves on held-out subtasks or get rolled back.

**It finds where the reasoning broke, writes the fix, and keeps it only if held-out work proves it.**

---

## 2. Problem

- Coding agents stall on long, tightly coupled tasks: one wrong assumption early on cascades through later steps.
- When they fail, they retry blindly. Nothing records *why* the reasoning went wrong, so the same mistake recurs.
- "Self-improvement" is easy to claim and hard to verify: without held-out evaluation, a change that looks helpful may just be noise.

---

## 3. Core idea (semantic)

| Concept | Meaning |
| --- | --- |
| **Long task** | A hard, multi-file task decomposed into an ordered list of subtasks, each with a pinned interface |
| **Swarm** | N (6–8) sandboxed agents from different model families that each implement the current subtask independently |
| **Reasoning step** | A structured log entry an agent writes before each action: `{assumption, action, files_touched}` |
| **Reasoning path** | An agent's ordered reasoning steps, each embedded as a vector |
| **Consensus path** | The region in embedding space where the majority agents' paths cluster at each phase of the subtask |
| **Spec tests** | Test cases, expected values and properties written by an independent model from the spec, the pinned interface and the existing codebase only, before any implementation exists |
| **Cross-test** | Running every implementation on the same spec tests, fuzzed inputs and properties; the trust-weighted majority output is the verified result. Agents' own tests are evidence, not the verdict |
| **Verified majority** | The implementation(s) agreeing with the majority on every input; one is committed and the long task advances |
| **Divergence point** | For a failed agent, the first reasoning step that leaves the consensus path: the suspected reasoning error |
| **Reflector** | Reads the failed agent's path, its divergence point, the breaking input and the consensus steps at that phase; writes a lesson that resolves the reasoning error |
| **Lesson** | A short rule tied to the divergence point's embedding, e.g. *"'over $100' means strictly greater; always test the boundary."* |
| **Failure region** | The embedding neighbourhood around a lesson's divergence point; radius and weight grow with repeat hits |
| **Change** | Any versioned modification to the system: a new lesson (context/skill), a trust-score update (routing), or a generated tool |
| **Held-out gate** | Each change runs against held-out subtasks with and without it; adopted only if results improve, else rolled back |
| **Trust score** | Per agent identity, running agreement with the verified majority; sets vote weight and which agents are routed to which subtask types |

---

## 4. Pipeline (technical)

```
Long task
  │
  ├─ 0. Decomposer ─────────── task → ordered subtasks with pinned interfaces + dependency order
  │
  └─ for each subtask:
       │
       ├─ 1. Context Builder ──── embed subtask; vector search lessons + failure regions
       │        → relevant lessons injected into each agent's prompt
       │
       ├─ 2. Router ────────────── pick N agents by trust score for this subtask type
       │
       ├─ 3. Swarm Runner ──────── N sandboxes at current repo state; each agent implements,
       │        logs reasoning via log_step, runs the VISIBLE spec tests via run_spec_tests
       │        live check: if a new step enters a known failure region → inject that lesson now
       │
       ├─ 4. Path Embedder ─────── embed every reasoning step → vector store
       │
       ├─ 5. Cross-Test Harness ── spec tests (written before the swarm, never from an impl)
       │        + fuzzed inputs from the interface + spec properties; agents' tests as tie-breaker
       │        → trust-weighted majority per input → verified majority implementation
       │
       ├─ 6. Commit + Advance ──── merge the verified implementation; next subtask starts from it
       │        no majority → subtask marked ambiguous, re-decomposed or escalated
       │
       ├─ 7. Reflector ─────────── for each failed agent:
       │        path divergence + locality to the failing test + live-test timing → candidate step
       │        counterfactual replay confirms it → lesson resolving the reasoning error
       │
       ├─ 8. Change Proposer ───── lesson → context change; trust update → routing change;
       │        repeated failure cluster → tool proposal (e.g. a boundary-case test generator)
       │
       ├─ 9. Held-out Gate ─────── run held-out subtasks with vs. without each change
       │        → adopt (versioned) or roll back
       │
       └─ 10. Store ────────────── lessons, failure regions, trust, change log, manifests → MongoDB
```

### Change types (the theme's four levers)

| Lever | What changes | Produced by |
| --- | --- | --- |
| **Context** | Lessons injected into prompts, retrieved by subtask embedding or triggered live by failure regions | Reflector |
| **Skills** | Lessons promoted to reusable skill files when adopted and hit repeatedly | Change Proposer |
| **Routing** | Trust scores decide which agents run on which subtask types and how much their vote counts | Trust updater |
| **Tools** | When several lessons cluster in one region, propose a tool that automates the fix (e.g. boundary-input generator, schema-diff checker) | Change Proposer |

Every change gets a version, a `candidate → adopted | rolled_back` status, and the held-out scores that decided it.

---

## 5. Component details

### 5.1 Decomposer
- Input: the long task description + repo.
- LLM produces an ordered subtask list with pinned interfaces (`ast`-extractable names, signatures, files) and dependencies.
- Subtasks are the unit of swarm work; interfaces make implementations comparable.

### 5.2 Context Builder
- Embeds the subtask spec; vector search over `lessons` (top-k, tag-filtered).
- Also loads failure regions for the live check in 5.3.
- Logs lessons used per run (needed by the held-out gate).

### 5.3 Swarm Runner
- One container per agent at the current repo state (Docker; fallback: git worktrees).
- Agent mix: Meta Muse Code, Muse Spark, Gemini, Claude, 1–2 open models. Each (model, config) is an identity with a trust score.
- Required tool `log_step(assumption, action, files_touched)` before every edit or command.
- **Live check:** each new step is embedded and compared with failure regions; a hit injects the matching lesson into the agent's context before it acts.
- **Live verification against independent tests.** Agents get a `run_spec_tests` tool that runs the subtask's *visible* spec tests (see split below) against their current code and returns pass/fail per case. They never see the hidden set or the properties. Each call is logged as a step so a failing live test is tied to the reasoning step that preceded it.
- **Visible / hidden split.** The spec-test author's output is split once per subtask and cached: about 40% of cases are visible (live feedback), the rest plus all properties are hidden (verdict only). A visible test can still fail at verdict time, but the verdict is decided on the hidden set and properties, so iterating against the visible set can't game it.
- Output per agent: `impl/`, `tests/`, `steps.jsonl` (includes `run_spec_tests` results per step).

### 5.4 Path Embedder
- Embeds the structured step string `"assumption: … | action: … | touches: …"` (not raw chain-of-thought; raw text clusters by topic, not by reasoning pattern).
- Gemini embeddings or `sentence-transformers`; stored in MongoDB Atlas Vector Search with `{task_id, subtask_id, agent_id, step_index, outcome}`.

### 5.5 Cross-Test Harness

**Test independence rule: no test used for a verdict is derived from any implementation.** Tests come from the spec, the pinned interface and the pre-existing codebase, and are written before the swarm runs.

Four test sources, in order of weight:

| Source | Who writes it | Sees | Weight |
| --- | --- | --- | --- |
| **Spec tests** | A dedicated test-author model, never one of the implementers for that subtask | Spec, pinned interface, existing repo at the base commit | Highest: a concrete expected value per case |
| **Spec properties** | Same author | Same | High: metamorphic rules that hold regardless of the exact answer, e.g. "`tier_for` is monotonic in amount", "`quote` total never exceeds subtotal", "`summarize([])` is all zeros" |
| **Fuzz** | `hypothesis` strategies from the type hints + boundary seeds from the types | Interface only | Medium: no expected value; the majority is the oracle |
| **Agent tests** | Each swarm agent | Its own implementation | Lowest: tie-breaker and reflector evidence only; never the sole reason for a verdict |

- Spec tests and properties are generated once per subtask and cached, so every run of that subtask (including held-out re-runs) uses the same independent tests.
- Every implementation runs on every input in an isolated subprocess with a timeout.
- Equivalence rules: float tolerance, unordered collections as sets, same exception type.
- Trust-weighted majority per input; agents outside the majority on any input are "failed" for this subtask.
- **Two oracles.** When the spec test's expected value disagrees with the swarm majority, that is a spec-ambiguity flag sent to the reflector and the decomposer, not a failure charged to any agent. When they agree, the case counts double.
- Properties catch what majority voting cannot: a bug the whole swarm shares still violates a spec property.

### 5.6 Commit + Advance
- The highest-trust implementation in the majority group is committed; its tests and the shrunk counterexamples join the repo's test suite.
- Agreement ≤50% → subtask flagged ambiguous; decomposer splits it or the system asks for clarification. The long task never silently advances on a non-verified step.

### 5.7 Reflector

**Correlating a failure to a reasoning region.** A failing test alone says *that* the agent was wrong; the divergence point says *where in its reasoning*. Three signals are combined, strongest first, and the result is confirmed before it becomes a lesson:

1. **Path divergence.** Align the failed path with the consensus path by subtask phase; candidate = first step whose distance to the consensus cluster exceeds the swarm's own spread (needs ≥3 verified runs; with fewer, fall back to signal 2 only).
2. **Locality to the failure.** Among candidate steps, prefer ones whose `files_touched` include the file of the failing function, and whose `assumption` text is semantically close to the failing case (embedding of "function + failing input + expected vs actual"). A step that is both off-consensus and local to the failure wins.
3. **Live-test timing.** If the agent ran `run_spec_tests` during the run, the step just before the first time the (visible) failing case went red is a strong candidate; if the case was never green, the earliest local step is.

**Confirmation (counterfactual replay).** Re-run the failed agent from the chosen step with the consensus assumption injected into its context. If the hidden failing case now passes, the step is a confirmed root cause and the lesson is written. If not, try the next candidate (at most 2 retries); if none confirms, write the lesson anyway but mark it `confidence ≤ 0.3` and it does not create a failure region until a second hit confirms it. Confirmed lessons create regions immediately.

- Input to the reflector model: the failed path, the confirmed divergence step, the consensus steps at that phase, the minimal breaking input, the agent's output vs. the majority's / expected output.
- Output: `{lesson_text, divergence_embedding, tags, breaking_input, confidence}`. A rule that resolves the reasoning error, not a description of the bug.
- Dedup: near-identical lesson (vector similarity above threshold) → increment `hit_count`, widen the failure region.

### 5.8 Change Proposer
- Lesson → candidate context change.
- Trust deltas → candidate routing change.
- ≥3 adopted lessons in one region → candidate tool: LLM writes a small script (e.g. generate boundary inputs for comparison operators), registered as a tool for future agents.

### 5.9 Held-out Gate
- Held-out set: subtasks from the same repo (or a second repo) never used to produce any lesson, with reference implementations.
- For each candidate change: run the swarm on held-out subtasks with and without it. Adopt if majority accuracy rises or repeat-mistake rate drops, with no new false blocks; else roll back.
- Version every decision; the change log is the demo's centerpiece.

### 5.10 Trust Scores
- `score = (agreements + 1) / (agreements + disagreements + 2)` per agent identity, updated after each verified subtask.
- Drives vote weight and routing; a consistently low-trust identity is replaced by a fresh config.

### 5.11 Store — MongoDB
- `lessons`: text, embedding, tags, hit_count, version, status, created_from.
- `failure_regions`: centroid, radius, weight, lesson_id.
- `steps`: embedded reasoning steps.
- `trust`, `changes` (changelog with held-out scores), `runs` (manifests: model IDs, prompts, seeds, lessons used, container digests).

---

## 6. Tech stack

| Layer | Tool | Why |
| --- | --- | --- |
| Reflector + long-context review | **Subquadratic SubQ Snap 1.1** (console.subq.ai, 100M free tokens) | Needs the failed path, consensus paths, results and lesson history in one call |
| Swarm agents | **Meta Model API — Muse Code, Muse Spark** (dev.meta.ai, $100 credits) | Independent implementers; Best Use of Meta |
| Swarm agents + embeddings | **Gemini** (Google DeepMind guide) | Another family; embedding model for steps and lessons |
| Swarm agents | Claude, 1–2 open models (optional) | Diversity |
| Lessons, regions, steps, trust, changelog | **MongoDB Atlas + Vector Search** (code `MKT-BUILDERS-100`) | Retrieval by embedding; versioned change log |
| Agent framework (optional) | **Fetch.ai uAgents** | Swarm agents as uAgents for the Best Use of Fetch.ai prize |
| Sandboxing | Docker (fallback: git worktrees) | Isolated repo copy per agent |
| Property testing | `hypothesis`, `pytest` | Fuzzed inputs, shrinking |
| Interface extraction | Python `ast` | Pin signatures per subtask |
| Dimensionality reduction (demo) | UMAP | 2D view of reasoning paths and failure regions |
| Orchestration | Python 3.12, `asyncio` | Parallel swarm runs |
| Demo UI | Minimal page: reasoning map, changelog, trust leaderboard, task progress | Not the main feature — the self-improving loop is |

Credit deadlines: **Meta credit claims close 3:00 PM today** and need billing set up first. Get SubQ tokens before building.

---

## 7. Repo layout

```
project/
├── README.md
├── core/
│   ├── decompose/      # long task → subtasks, interface pinning
│   ├── context/        # lesson retrieval, failure-region live check
│   ├── swarm/          # router, runner, sandbox, prompts, log_step tool
│   ├── paths/          # step embedder, alignment, divergence detection
│   ├── harness/        # hypothesis strategies, cross-test matrix, trust-weighted majority
│   ├── reflect/        # reflector prompt, lesson schema, dedup
│   ├── changes/        # change proposer, versioning, held-out gate, adopt/rollback
│   └── store/          # MongoDB collections and vector indexes
├── demo_task/          # the long task + repo used in the demo
├── eval/               # held-out subtasks, reference impls, metrics script
├── ui/                 # reasoning map, changelog, leaderboard
└── docker/
```

---

## 8. Build plan (10:45 AM – 5:00 PM)

| Time | A — Task & eval | B — Swarm | C — Paths & harness | D — Reflector, changes, store |
| --- | --- | --- | --- | --- |
| 10:45–11:30 | Claim credits; pick repo + long task; write decomposer; reference impls for 3–4 subtasks | Docker sandbox; `log_step` tool | Step embedder + MongoDB vector index | SubQ hello-world; collections schema |
| 11:30–2:00 | Held-out subtask set with references | Parallel runner on Muse/Gemini/Claude; router by trust | Cross-test matrix; divergence detection | Reflector prompt; lesson store + dedup; failure regions |
| 2:00–3:30 | Bug-prone subtasks (boundaries, ordering, coupling) | Wire loop end to end: commit + advance | Live failure-region check in runner | Change proposer; held-out gate; versioned changelog |
| 3:30–4:30 | Run full long task; collect numbers | Cache a full run as demo fallback | Fix bugs | UI: map, changelog, leaderboard; rehearse |
| 4:30–5:00 | Record 1-min video | Repo public, README | Final fixes | Submit, add all teammates |

**Cut order if short on time:** Fetch.ai → tool generation → UI polish → trust-based routing (fall back to fixed N) → live failure-region check (fall back to retrieval at subtask start). Never cut: decomposer, swarm, cross-test, reflector with divergence detection, held-out gate with rollback, changelog.

---

## 9. Demo script (3 min)

1. **0:00** — The long task: e.g. add a tiered-pricing module across 4 coupled files. Subtask 1 starts; swarm runs; verified majority commits.
2. **0:40** — Subtask 2: one agent fails cross-test on input `100` (boundary). Its reasoning path leaves the consensus at step 3: *"assumed 'over' includes equal."* Reflector writes the lesson; a failure region appears on the map.
3. **1:30** — Held-out gate runs the change with vs. without: repeat-mistake rate drops → **adopted v1**. A second candidate (a routing change) doesn't help → **rolled back**. Both shown in the changelog.
4. **2:10** — Subtask 3: a different agent's step enters the failure region live; lesson injected; it passes. Trust leaderboard updates.
5. **2:45** — Numbers: subtasks completed, repeat-mistake rate falling, changes adopted vs. rolled back, held-out accuracy before vs. after.

Fallback: replay the cached run if APIs fail live.

---

## 10. Eval metrics

| Metric | Target |
| --- | --- |
| Long-task completion (subtasks verified and committed) | Full task |
| Held-out accuracy before vs. after adopted changes | Rising |
| Repeat-mistake rate (failures matching an adopted lesson's region) | Falling |
| Changes adopted vs. rolled back | Report both |
| Divergence points confirmed (lesson resolved the failure on rerun) | Report share |
| Trust-weighted vs. plain majority accuracy | Report |
| Cost and time per subtask | Report |

---

## 11. Q&A one-liners

- **How is this "getting better"?** Every failure becomes a versioned lesson, routing or tool change; held-out subtasks decide if it stays.
- **Why reasoning paths?** They tell us *where* the thinking went wrong, so the lesson fixes the cause, not the symptom.
- **Closed-source models?** We embed the structured steps agents log, not hidden states; works with any API.
- **Shared blind spots?** Different families, trust-weighted votes, failure regions that warn even when the swarm agrees.
- **Expensive?** Swarm per subtask, cached; low-trust agents are dropped; lessons reduce retries.
- **What did you build today?** Everything in the repo; libraries listed in the README.
