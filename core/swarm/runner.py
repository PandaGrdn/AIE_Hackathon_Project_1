"""Run N agents in parallel on one subtask. Owner: Lucas Chen.

Each agent: own sandbox, lessons in prompt, tool loop with `log_step` recorded as
ReasoningSteps, live failure-region check before each action, `run_spec_tests`
against the subtask's visible spec tests (results tied to the preceding step).
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from core.config import settings
from core.context.builder import render_lessons
from core.context.live_check import LiveCheck
from core.harness.crosstest import _call
from core.harness.equivalence import values_equal
from core.harness.spec_tests import visible_module_sources
from core.llm.clients import get_client
from core.llm.embeddings import embed_texts
from core.models import AgentIdentity, AgentRun, ReasoningStep, Subtask
from core.store.base import Store
from core.swarm.prompts import system_prompt, user_prompt
from core.swarm.sandbox import Sandbox
from core.swarm.tools import AGENT_TOOLS, GATED_TOOLS

MAX_TURNS = 40
TOKEN_BUDGET = 400_000
REJECT_MSG = "REJECTED: call log_step first (assumption, action, files_touched), then retry this call."
REJECT_NUDGE = "\nYou have been rejected 3 times in a row. Every write_file/run_tests/run_spec_tests needs its OWN log_step immediately before it. Call log_step now."
NO_TOOLS_NUDGE = "Use the tools. Call log_step, then write_file or run_tests/run_spec_tests, then done."

META: dict[str, dict[str, Any]] = {}
_TOOL_NAMES = {t["function"]["name"] for t in AGENT_TOOLS}
_console = Console()


@dataclass
class _Stats:
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rejections: int = 0
    text_calls: int = 0  # tool calls recovered from "[name] {args}" text replies
    consecutive_rejections: int = 0
    armed: bool = False
    extra_lessons: list[str] = field(default_factory=list)


def run_meta(run: AgentRun, stats: _Stats | None = None) -> dict[str, Any]:
    if stats is None and run.id in META:
        return META[run.id]
    s = stats or _Stats()
    return {
        "agent_id": run.agent.id,
        "provider": run.agent.provider,
        "model": run.agent.model,
        "duration_s": round(run.duration_s, 3),
        "turns": s.turns,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "steps": len(run.steps),
        "rejections": s.rejections,
        "text_calls": s.text_calls,
        "error": run.error,
        "lessons_used": list(run.lessons_used),
    }


_TEXT_CALL = re.compile(r"^\s*\[(\w+)\]\s*(\{.*\})\s*$", re.S)


def _text_tool_calls(text: str) -> list[dict[str, Any]]:
    """`[write_file] {'path': ..., 'content': ...}` written as plain text -> one tool call, when the name is a real
    tool and the args parse as a Python/JSON dict literal; otherwise []."""
    m = _TEXT_CALL.match(text or "")
    if not m or m.group(1) not in _TOOL_NAMES:
        return []
    try:
        args = ast.literal_eval(m.group(2))
    except (ValueError, SyntaxError):
        try:
            args = json.loads(m.group(2))
        except json.JSONDecodeError:
            return []
    return [{"name": m.group(1), "arguments": args}] if isinstance(args, dict) else []


def _fmt_args(case) -> str:
    parts = [repr(a) for a in case.args] + [f"{k}={v!r}" for k, v in case.kwargs.items()]
    return ", ".join(parts)


def _fmt_outcome(o) -> str:
    if o.timed_out:
        return "TIMEOUT"
    return repr(o.value) if o.ok else f"raised {o.exception}"


def _run_spec_tests(sb, subtask: Subtask, spec, run: AgentRun) -> str:
    if spec is None:
        return "No spec tests are available for this subtask."
    results: dict[str, bool] = {}
    lines: list[str] = []
    # pytest-module spec tests with the hidden test functions stripped (harness helper), then visible function cases
    modules = visible_module_sources(spec) if getattr(spec, "modules", None) else getattr(spec, "visible_modules", None)
    if modules:
        results = dict(sb.run_pytest_modules(modules))
        lines = [f"{'PASS' if ok else 'FAIL'} {tid}" for tid, ok in results.items()]
        if not results:  # collection failed (e.g. the module under test does not import yet): show why
            lines = ["no visible spec test ran; pytest output:", getattr(sb, "last_pytest_output", "")[-2500:]]
        elif not all(results.values()):  # failures: the assertion text is what lets the agent fix them
            lines += ["pytest output (tail):", getattr(sb, "last_pytest_output", "")[-2500:]]
    if getattr(spec, "visible", None):
        sigs = {s.name: s for s in subtask.interface}
        for case in spec.visible:
            expected = spec.expected[case.id]
            label = f"{case.function}({_fmt_args(case)})"
            sig = sigs.get(case.function)
            if sig is None:
                results[case.id] = False
                lines.append(f"FAIL {label}: no pinned function named {case.function}")
                continue
            out = _call(str(sb.path), sig, case)
            if isinstance(expected, dict) and "raises" in expected:
                ok = not out.ok and out.exception == expected["raises"]
                exp_s = f"raises {expected['raises']}"
            else:
                ok = out.ok and values_equal(out.value, expected)
                exp_s = repr(expected)
            results[case.id] = ok
            lines.append(f"PASS {label}" if ok else f"FAIL {label}: expected {exp_s} got {_fmt_outcome(out)}")
    if run.steps:
        run.steps[-1].live_results = results
    passed = sum(results.values())
    return "\n".join([f"{passed}/{len(results)} visible spec tests passed", *lines])


def _dispatch(name: str, args: dict, sb, subtask: Subtask, spec, run: AgentRun, live: LiveCheck, st: _Stats) -> tuple[str, bool]:
    """Execute one tool call. Returns (result_text, finished)."""
    if name in GATED_TOOLS and not st.armed:
        st.rejections += 1
        st.consecutive_rejections += 1
        return REJECT_MSG + (REJECT_NUDGE if st.consecutive_rejections >= 3 else ""), False
    try:
        if name == "log_step":
            step = ReasoningStep(index=len(run.steps), assumption=args.get("assumption", ""), action=args.get("action", ""), files_touched=args.get("files_touched", []) or [])
            [step.embedding] = embed_texts([step.as_text()])
            run.steps.append(step)
            st.armed = True
            st.consecutive_rejections = 0
            result = "logged"
            lesson = live.check(step)
            if lesson:
                result += f"\nWARNING — this reasoning matches a known failure. Lesson: {lesson.text}"
                run.lessons_used.append(lesson.id)
        elif name == "list_files":
            files = sb.list_files(args.get("pattern") or "**/*")
            result = "\n".join(files) or "(no matches)"
        elif name == "read_file":
            result = sb.read_file(args["path"])
        elif name == "write_file":
            sb.write_file(args["path"], args["content"])
            result = f"wrote {args['path']}"
        elif name == "run_tests":
            path = args.get("path")
            result = sb.run_tests(path) if path else sb.run_tests()
        elif name == "run_spec_tests":
            result = _run_spec_tests(sb, subtask, spec, run)
        elif name == "done":
            return "ok", True
        else:
            return f"error: unknown tool {name}", False
    except (ValueError, FileNotFoundError, KeyError) as e:
        return f"error: {type(e).__name__}: {e}", False
    if name in GATED_TOOLS:
        st.armed = False
        st.consecutive_rejections = 0
    return result, False


def run_one(subtask: Subtask, agent: AgentIdentity, repo: Path, lessons_block: str, lesson_ids: list[str], store: Store, spec=None) -> AgentRun:
    run = AgentRun(subtask_id=subtask.id, agent=agent, sandbox_path="", lessons_used=list(lesson_ids))
    t0 = time.time()
    deadline = t0 + settings.agent_timeout_s
    st = _Stats()
    trace: list[dict[str, Any]] = []  # per-turn timing, usage and tool calls -> runs/<id>/transcript.json
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
                if time.time() >= deadline:
                    run.error = f"timeout: agent exceeded {settings.agent_timeout_s}s"
                    break
                t_turn = time.time()
                resp = client.chat(messages, tools=AGENT_TOOLS)
                st.turns += 1
                trace.append({"turn": st.turns, "chat_s": round(time.time() - t_turn, 1), "usage": resp.get("usage"),
                              "text": (resp.get("text") or "")[:2000],
                              "tool_calls": [{"name": c.get("name"), "arg_keys": sorted((c.get("arguments") or {}).keys())} for c in resp.get("tool_calls") or []]})
                usage = resp.get("usage") or {}
                st.input_tokens += int(usage.get("input_tokens") or 0)
                st.output_tokens += int(usage.get("output_tokens") or 0)
                if st.input_tokens + st.output_tokens > TOKEN_BUDGET:
                    run.error = f"token budget exceeded: {st.input_tokens + st.output_tokens} > {TOKEN_BUDGET}"
                    break
                if not resp.get("tool_calls"):
                    # The history shows calls as "[name] {args}" text, and some models answer in that format
                    # instead of a function call: accept it rather than burn a turn on a nudge.
                    resp["tool_calls"] = _text_tool_calls(resp.get("text", ""))
                    st.text_calls += len(resp["tool_calls"])
                if not resp.get("tool_calls"):
                    messages.append({"role": "assistant", "content": resp.get("text", "")})
                    messages.append({"role": "user", "content": NO_TOOLS_NUDGE})
                    continue
                finished = False
                for call in resp["tool_calls"]:
                    name, args = call["name"], call.get("arguments") or {}
                    result, finished = _dispatch(name, args, sb, subtask, spec, run, live, st)
                    messages.append({"role": "assistant", "content": f"[{name}] {args}"})
                    messages.append({"role": "user", "content": f"[{name} result]\n{result}"})
                    if finished:
                        break
                if finished:
                    break
        except Exception as e:  # noqa: BLE001 - keep the swarm alive if one agent dies
            run.error = f"{type(e).__name__}: {e}"
        try:
            run.diff = sb.diff()
            keep = settings.runs_dir / run.id
            # no bytecode: .pyc files compiled in the sandbox carry its path as co_filename, which breaks tests
            # that compare warning/traceback filenames once the code is graded from runs/<id>
            shutil.copytree(sb.path, keep, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"), dirs_exist_ok=True)
            run.sandbox_path = str(keep)
        except Exception as e:  # noqa: BLE001
            run.error = run.error or f"{type(e).__name__}: {e}"

    run.duration_s = time.time() - t0
    meta = run_meta(run, st)
    META[run.id] = meta
    try:
        keep = settings.runs_dir / run.id
        keep.mkdir(parents=True, exist_ok=True)
        (keep / "meta.json").write_text(json.dumps(meta, indent=2))
        (keep / "transcript.json").write_text(json.dumps({"turns": trace, "messages": messages}, indent=1, default=str))
    except (OSError, NameError):
        pass
    status = "ok" if run.error is None else f"ERROR {run.error[:60]}"
    _console.print(f"[{agent.id}] {run.duration_s:.1f}s {len(run.steps)} steps {st.input_tokens / 1000:.1f}k in / {st.output_tokens / 1000:.1f}k out tokens {st.rejections} rejected {status}")
    store.save_steps(run, run.steps)
    store.save_run(run)
    return run


async def run_swarm(subtask: Subtask, agents: list[AgentIdentity], repo: Path, lesson_ids: list[str], store: Store, spec=None) -> list[AgentRun]:
    lessons = [l for l in (store.get_lesson(i) for i in lesson_ids) if l]
    block = render_lessons(lessons)
    loop = asyncio.get_running_loop()
    tasks = [loop.run_in_executor(None, run_one, subtask, a, repo, block, lesson_ids, store, spec) for a in agents]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    runs: list[AgentRun] = []
    for agent, r in zip(agents, results):
        if isinstance(r, BaseException):
            r = AgentRun(subtask_id=subtask.id, agent=agent, sandbox_path="", lessons_used=list(lesson_ids), error=f"{type(r).__name__}: {r}")
        runs.append(r)
    return runs
