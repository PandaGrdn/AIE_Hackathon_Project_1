# Demo runbook (3 min, 5:00 PM)

Judging: Technical demo 35%, Difficulty 30%, Impact 20%, Creativity 15%. Spend the time on the
loop working and on the Root cause card (SubQ), not on UI polish.

Run everything from the repo root with the venv active (`swarm` only exists in `.venv`).

```bash
source .venv/bin/activate
```

## 1. Pre-flight (start 10 minutes before)

Keys in `.env`: `SUBQ_API_KEY` (root-cause localization), `GEMINI_API_KEY` (text roles, embeddings),
`META_API_KEY` (swarm agents). The `--fake` path needs none of them for the run itself.

1. MCP task setup (needs network and uv; skip if `demo_task/mcp/repo/` already exists and is clean).

```bash
swarm setup demo_task/mcp
```

   Expect: a clone on branch `swarm` at `77001c9`, `uv sync`, then a baseline of about 2389 tests
   passing in ~11 s. No keys needed.

2. Reset the MCP repo to the base commit (do this before every MCP run).

```bash
bash demo_task/mcp/setup.sh --reset --no-tests
```

3. Cached pricing snapshot (the live-demo fallback and the proof the run works). Runs ~20 s, prints
   the summary dict, the metrics dict and the changes table. Expect 4 subtasks completed.

```bash
swarm run demo_task/pricing/task.md --fake --snapshot demo/pricing_snapshot.json
```

4. Cached MCP snapshot (the first thing shown on stage). Fake swarm against `demo_task/mcp/reference`.

```bash
swarm run demo_task/mcp/task.md --fake --snapshot demo/mcp_snapshot.json
```

   Expect 7 subtasks. Confirm the file exists and is non-trivial (a few MB):

```bash
ls -lh demo/pricing_snapshot.json demo/mcp_snapshot.json
```

5. Reset the MCP repo again so it is clean (step 4 commits into `demo_task/mcp/repo/`).

```bash
bash demo_task/mcp/setup.sh --reset --no-tests
```

6. Start the UI on the MCP snapshot (second terminal, keep it running).

```bash
SWARM_SNAPSHOT=demo/mcp_snapshot.json USE_MEMORY_STORE=1 uvicorn ui.server:app --port 8787
```

7. Open http://localhost:8787. Check all five sections render: numbers strip, Changelog, Root cause,
   Trust leaderboard, Reasoning map. Hover one point on the map. The page polls every 3 s.
8. Check the Root cause card shows SubQ bars (probabilities over S0..Sn, chosen step outlined blue).
   If the card says there is no SubQ data, it is a path-only divergence (see section 3).
9. Pre-type the live command in terminal 1 (do not press Enter). Terminal font large, window
   maximized, notifications off.

```bash
swarm run demo_task/pricing/task.md --fake
```

Without keys: `--fake` replaces decomposer, spec author, swarm and reflector, and uses deterministic
embeddings, so snapshots and the live run still work. The SubQ fields in a fresh `--fake` snapshot
depend on whether `core/reflect` calls SubQ there; if no key or SubQ errors, divergences fall back to
the embedding path/locality signals and the card says "path signal". The existing
`demo/pricing_snapshot.json` already holds one SubQ-scored divergence (step S1 at 0.9998, kind
`bug` 0.80, generalizes 0.98) from an earlier run, so do not overwrite it in pre-flight step 3 unless the
new one is checked to have the same Root cause card. Real (non-fake) runs need all three keys
and are not part of the 3-minute demo.

## 2. The 3-minute script

Windows: A = browser on the UI, B = terminal. Keep A fullscreen at the start.

**0:00 to 0:20, Window A (numbers strip, top of page).** Say:

> "Coding agents fail on long tasks because one wrong assumption early cascades, and when they
> retry they never learn why. We built a swarm that finds the exact step where an agent's reasoning
> broke, writes the fix, and keeps it only if held-out work proves it. This is a real run: seven
> coupled subtasks adding per-tool rate limits and quotas to the MCP Python SDK, a feature that
> does not exist upstream."

Point at the numbers strip: subtasks done, changes adopted and rolled back, held-out accuracy
first to last.

**0:20 to 0:45, Window A (Reasoning map).** Say: "Every dot is one reasoning step from one agent,
embedded. Grey is steps from verified runs, red is from failed runs, the amber ring is the step our
reflector blames. Verdicts come from hidden tests an independent verifier wrote from the spec alone,
never from agents' own tests." Hover the amber-ringed point.

**0:45 to 1:15, Window B (live run).** Press Enter on the pre-typed command. Say while it runs
(about 20 s): "Same loop, live, offline mode so the room's wifi cannot hurt us. For each subtask:
independent verifier writes hidden tests, a swarm implements, verified implementation is committed,
failures are reflected on, and every proposed change goes through a held-out gate." Let the changes
table print. Do not narrate the log lines.

**1:15 to 2:00, Window A (Root cause card). This is the SubQ moment.** Say:

> "This is where Subquadratic comes in, and we use SubQ Snap as a decisions API, not a chat
> model. We hand it the whole failed reasoning path, the consensus path and the failing test in one
> context. It returns calibrated probabilities over which step is the root cause, with zero output
> tokens. Here: step S1 at 99.98%. The agent assumed 'up to N' excludes N, so it compared with less-than
> and got the 1000 boundary wrong. SubQ also classifies it: a bug, not a spec ambiguity, and it says the
> lesson generalizes at 98%, so the lesson starts with high confidence."

Point in order: bars, blue outlined step with its text, kind badge, generalizes meter, the four signal
chips, expected vs actual.

**2:00 to 2:35, Window A (Changelog).** Say: "Every lesson, routing change and tool is a versioned
change. The gate reruns held-out subtasks with and without it. Adopted is green, rolled back is red, and
you see held-out accuracy and repeat-mistake rate before to after. A change that does not help gets
rolled back, which is how we know the improvement is not noise." Point at one adopted row and one
rolled-back row with its reason.

**2:35 to 2:50, Window A (Trust leaderboard).** Say: "Agents earn trust by agreeing with verified
outcomes. Trust weights votes and routes work, so the swarm shifts toward agents that are right."

**2:50 to 3:00, Window A (numbers strip).** Say: "Seven subtasks on a real open-source repo,
the existing 2,389-test suite kept green, N changes adopted, M rolled back, held-out accuracy X to Y.
It finds where the reasoning broke, writes the fix, and keeps it only if held-out work proves it."
Read N, M, X, Y off the screen, do not guess them.

## 3. Fallback matrix

| Failure | Do this |
| --- | --- |
| Live run hangs or exceeds 30 s | Ctrl-C. Say "that is the offline replay, here is the cached run" and move to Window A. Nothing in sections 0:00 to 0:45 or 1:15 on depends on it. |
| Live run errors on start | `bash demo_task/mcp/setup.sh --reset --no-tests` is irrelevant to pricing; just run `swarm run demo_task/pricing/task.md --fake` once more, then fall back to Window A. |
| UI will not load | Check `uvicorn` terminal for the error. Restart it. If still down, switch snapshot: `SWARM_SNAPSHOT=demo/pricing_snapshot.json`. If the browser is the problem, `curl -s localhost:8787/api/summary` and `curl -s localhost:8787/api/divergences \| head -c 1500` and narrate from JSON. Last resort: show the `swarm run` table in the terminal and explain the card verbally. |
| Port 8787 busy | `lsof -ti :8787 \| xargs kill`, restart; or use `--port 8788` and the matching URL. |
| MCP snapshot missing or empty | Use `demo/pricing_snapshot.json` (committed, 2.3 MB, has the SubQ-scored divergence). Change the opening to "tiered pricing module, four coupled subtasks". |
| SubQ errors or key missing | The run continues: the reflector falls back to path divergence, locality and live-timing signals. The Root cause card shows "path signal" and no probability bars. Say: "SubQ is the primary localizer when its key is live; this is the offline fallback and it agrees on the step." Then show the cached SubQ card from the pricing snapshot. |
| Gemini errors | Only affects non-fake runs. Demo path is `--fake`, so ignore. |
| MongoDB unreachable | Always run `USE_MEMORY_STORE=1`; Mongo is not in the demo path. |
| Projector low-res | Browser zoom 150 to 175% (Cmd +). Root cause card is the densest element: scroll it to the top of the screen and hover less. Terminal font 20 pt or larger. |
| Reasoning map slow | Skip it. Open at the Root cause card and cut 0:20 to 0:45; give those seconds to the Changelog. |
| UI stale after a rerun | Hard refresh (Cmd+Shift+R). Snapshot is read on first request, so restart `uvicorn` after regenerating a snapshot. |

## 4. Q&A one-liners

- **Cost?** One swarm per subtask, cached by spec; SubQ Snap is zero output tokens and about 500 input
  tokens per localization; low-trust agents get dropped and lessons cut retries.
- **Shared blind spots?** Verdicts come from hidden tests written from the spec, not from the swarm's
  agreement, so a unanimous wrong swarm still fails. Different model families and trust-weighted votes add
  diversity, and failure regions warn even when the swarm agrees.
- **Gameable?** Agents never see the hidden tests, their own tests are not in the verdict, and the verifier
  never sees any implementation. Changes are judged on held-out subtasks that never produced a lesson.
- **Why reasoning paths?** A failing test says that the agent was wrong; the divergence step says where.
  The lesson fixes the cause (the assumption), not the symptom (one input).
- **Closed-source models?** We embed the structured steps agents log (assumption, action, files), not hidden
  states, so it works with any API.
- **What did you build today?** Everything in the repo (README says so); third-party libraries are in
  `pyproject.toml`. The MCP SDK is the task input, pinned at `77001c9`, and the feature does not exist upstream.
- **Why SubQ as a decisions API, not a judge?** A judge writes prose we would have to parse. Snap gives
  calibrated probabilities over a fixed choice set (root-step labels, bug or spec_ambiguity or
  acceptable_variation, generalizes), at zero output tokens, over the whole path in one 384K context. We route on
  those numbers: `spec_ambiguity` goes to the decomposer instead of blaming the agent, and low `generalizes`
  lowers lesson confidence. It corroborates, and degrades to path signals without a key.
- **Is the self-improvement real or noise?** The gate adopts only if held-out accuracy rises or repeat-mistake
  rate drops with no new false blocks; otherwise it rolls back, and both outcomes are in the changelog.
- **Was the live run real?** It is the offline `--fake` mode: deterministic fakes with a boundary-bug mutant
  and reference implementations. Say so plainly; the real-LLM path is the same orchestrator.

## 5. Submission checklist (by 5:00 PM)

- [ ] Repo is public (GitHub, Settings, Change visibility). Open it in a private window to confirm.
- [ ] `git status` clean on the submitted branch; merged to `main`; `.env` not committed (`git ls-files | grep -x .env` prints nothing).
- [ ] README setup works from a clean clone (`uv python install 3.12`, `uv pip install -e ".[dev]"`, `swarm run demo_task/pricing/task.md --fake`).
- [ ] `demo/mcp_snapshot.json` and `demo/pricing_snapshot.json` committed (or note how to regenerate).
- [ ] All team members added on the submission form and as repo collaborators: Ethan Liu, Ethan Li, Lucas Chen.
- [ ] Sponsor tools named in the README and the submission text: Subquadratic (SubQ Snap, root-cause
  localization), Meta (Muse models as swarm agents), Gemini (text roles and embeddings), MongoDB (store, vector
  search). Name only what actually ran; MongoDB is optional in the demo path (`USE_MEMORY_STORE=1`).
- [ ] Track named: The System That Gets Better.
- [ ] Video uploaded and linked (1 minute, shot list below).

### 1-minute video shot list

Screen-record at 150% browser zoom, voiceover; no live run needed.

| Time | Shot | Line |
| --- | --- | --- |
| 0:00 to 0:10 | UI numbers strip over the title | The one-liner: finds where the reasoning broke, writes the fix, keeps it only if held-out work proves it. |
| 0:10 to 0:20 | Terminal: `swarm run ... --fake` sped up, table prints | Seven coupled subtasks on the MCP SDK, verified by hidden tests from the spec alone. |
| 0:20 to 0:40 | Root cause card, zoom on the SubQ bars and badges | SubQ Snap as a decisions API: probability per step, bug vs spec ambiguity, generalizes. |
| 0:40 to 0:52 | Changelog, one adopted row and one rolled-back row | Versioned changes, held-out before to after, rollback when it does not help. |
| 0:52 to 1:00 | Numbers strip, sponsor names on screen | Built with Subquadratic, Meta, Gemini and MongoDB. |
