"""Run N agents in parallel on one subtask. Owner: B.

Each agent: own sandbox, lessons in prompt, tool loop with `log_step` recorded as
ReasoningSteps, live failure-region check before each action.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.config import settings
from core.context.live_check import LiveCheck
from core.context.builder import render_lessons
from core.llm.clients import get_client
from core.llm.embeddings import embed_texts
from core.models import AgentIdentity, AgentRun, ReasoningStep, Subtask
from core.store.base import Store
from core.swarm.prompts import system_prompt, user_prompt
from core.swarm.sandbox import Sandbox
from core.swarm.tools import AGENT_TOOLS

MAX_TURNS = 40


def run_one(subtask: Subtask, agent: AgentIdentity, repo: Path, lessons_block: str, lesson_ids: list[str], store: Store) -> AgentRun:
    run = AgentRun(subtask_id=subtask.id, agent=agent, sandbox_path="", lessons_used=list(lesson_ids))
    t0 = time.time()
    client = get_client(agent)
    live = LiveCheck(store)

    with Sandbox(repo, run.id) as sb:
        run.sandbox_path = str(sb.path)
        messages = [
            {"role": "system", "content": system_prompt(agent.prompt_variant, subtask)},
            {"role": "user", "content": user_prompt(subtask, lessons_block, repo_snippets="")},
        ]
        try:
            for _ in range(MAX_TURNS):
                resp = client.chat(messages, tools=AGENT_TOOLS)
                if not resp["tool_calls"]:
                    messages.append({"role": "assistant", "content": resp["text"]})
                    messages.append({"role": "user", "content": "Use the tools. Call log_step, then write_file/run_tests, then done."})
                    continue
                finished = False
                for call in resp["tool_calls"]:
                    name, args = call["name"], call["arguments"]
                    if name == "log_step":
                        step = ReasoningStep(index=len(run.steps), assumption=args.get("assumption", ""), action=args.get("action", ""), files_touched=args.get("files_touched", []))
                        [step.embedding] = embed_texts([step.as_text()])
                        run.steps.append(step)
                        lesson = live.check(step)
                        result = "logged"
                        if lesson:
                            result += f"\nWARNING — this reasoning matches a known failure. Lesson: {lesson.text}"
                            run.lessons_used.append(lesson.id)
                    elif name == "write_file":
                        sb.write_file(args["path"], args["content"])
                        result = f"wrote {args['path']}"
                    elif name == "run_tests":
                        result = sb.run_tests()
                    elif name == "run_spec_tests":
                        # TODO(Lucas): run spec.visible cases via harness._call against sb.path,
                        # compare to spec.expected, set run.steps[-1].live_results = {case_id: passed},
                        # and return per-case pass/fail with expected vs actual on failures.
                        result = "run_spec_tests not wired yet"
                    elif name == "done":
                        finished = True
                        result = "ok"
                    else:
                        result = f"unknown tool {name}"
                    messages.append({"role": "assistant", "content": f"[{name}] {args}"})
                    messages.append({"role": "user", "content": f"[{name} result]\n{result}"})
                if finished:
                    break
        except Exception as e:  # keep the swarm alive if one agent dies
            run.error = f"{type(e).__name__}: {e}"
        run.diff = sb.diff()
        # Keep the implementation around for the harness: copy out before the sandbox is removed.
        keep = settings.runs_dir / run.id
        import shutil

        shutil.copytree(sb.path, keep, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
        run.sandbox_path = str(keep)

    run.duration_s = time.time() - t0
    store.save_steps(run, run.steps)
    store.save_run(run)
    return run


async def run_swarm(subtask: Subtask, agents: list[AgentIdentity], repo: Path, lesson_ids: list[str], store: Store) -> list[AgentRun]:
    lessons = [l for l in (store.get_lesson(i) for i in lesson_ids) if l]
    block = render_lessons(lessons)
    loop = asyncio.get_running_loop()
    tasks = [loop.run_in_executor(None, run_one, subtask, a, repo, block, lesson_ids, store) for a in agents]
    return await asyncio.gather(*tasks)
