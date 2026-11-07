"""Reflector: failed run + divergence point + breaking input + expected/majority → lesson. Owner: Ethan Li.

Runs on SubQ (long context: the whole failed path, consensus steps, results).
The lesson is tied to the divergence step's embedding so it defines a failure region.
Pass the harness `result` so the prompt carries what the failed run actually returned next
to what the spec author expected (or, for fuzz evidence, what the majority returned).
"""

from __future__ import annotations

import json

from core.harness.crosstest import run_outcome
from core.llm.clients import get_client, reflector_identity
from core.llm.embeddings import embed_texts
from core.models import AgentRun, CrossTestResult, Divergence, Lesson, Outcome
from core.reflect.dedup import dedup_or_insert
from core.store.base import Store

FALLBACK_CONFIDENCE = 0.2

REFLECT_PROMPT = """An AI coding agent failed a subtask. Independent acceptance tests were written from the spec before any
implementation existed, and other agents solving the same subtask passed them.
Your job: identify the reasoning error at the divergence point and write ONE short, general, actionable lesson that would have prevented it.

## Subtask spec
{spec}

## The failed agent's reasoning path (step index: assumption | action | files)
{path}

## Divergence point (first step that left the consensus path)
step {idx}: {step}

## What the passing agents were reasoning at the same phase
{consensus}

## Breaking evidence
{evidence}

Return JSON: {{"lesson": str, "tags": [str], "confidence": float}}
Rules for the lesson:
- A rule, not a description of this bug. Example: "'over $100' means strictly greater; always test the boundary value."
- One or two sentences. General enough to apply to similar subtasks, specific enough to change behavior.
- confidence in [0, 1]: how sure you are that this step is the root cause.
"""


def reflect(spec: str, failed: AgentRun, div: Divergence, store: Store, result: CrossTestResult | None = None) -> Lesson:
    failed_out = _failed_outcome(failed, div, result)
    reference_out = div.majority_outcome
    reference_label = "spec expects" if _is_spec_case(div, result) else "verified majority"
    prompt = REFLECT_PROMPT.format(
        spec=spec,
        path="\n".join(f"{s.index}: {s.as_text()}" for s in failed.steps) or "(no steps logged)",
        idx=div.step_index,
        step=div.step_text,
        consensus="\n".join(f"- {t}" for t in div.consensus_step_texts) or "(none)",
        evidence=_evidence(div, failed_out, reference_out, reference_label),
    )

    data = _ask(prompt, failed, div)

    step = failed.steps[div.step_index] if 0 <= div.step_index < len(failed.steps) else None
    step_text = step.as_text() if step else div.step_text
    div_vec = (step.embedding if step and step.embedding else None) or embed_texts([step_text])[0]
    [lesson_vec] = embed_texts([data["lesson"]])
    lesson = Lesson(
        text=data["lesson"].strip(),
        tags=[str(t) for t in data.get("tags", [])],
        embedding=lesson_vec,
        divergence_embedding=div_vec,
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
        created_from={
            "subtask_id": failed.subtask_id,
            "run_id": failed.id,
            "agent_id": failed.agent.id,
            "step_index": div.step_index,
            "breaking_input": div.breaking_case.args if div.breaking_case else None,
            "failed_outcome": fmt_outcome(failed_out),
            "expected_outcome": fmt_outcome(reference_out),
            "reference": reference_label,
        },
    )
    return dedup_or_insert(lesson, store)


def _ask(prompt: str, failed: AgentRun, div: Divergence) -> dict:
    """One SubQ call. A malformed or failed reply degrades to a low-confidence lesson built from the
    divergence step, tagged `reflector-error`, so one bad call never stalls the subtask loop."""
    try:
        client = get_client(reflector_identity())
        resp = client.chat([{"role": "user", "content": prompt}])
        data = json.loads(_extract_json(resp["text"]))
        if not isinstance(data, dict) or not isinstance(data.get("lesson"), str) or not data["lesson"].strip():
            raise ValueError("reply has no lesson")
        return data
    except Exception as e:  # noqa: BLE001 — the loop must survive a bad reflector call
        assumption = div.step_text.split(" | ", 1)[0].removeprefix("assumption: ")
        inp = json.dumps(div.breaking_case.args, default=str) if div.breaking_case else "the breaking input"
        return {
            "lesson": f"Re-check the assumption \"{assumption}\" against the spec text before acting on it; it failed on input {inp}.",
            "tags": ["reflector-error", type(e).__name__],
            "confidence": FALLBACK_CONFIDENCE,
        }


def _evidence(div: Divergence, failed_out: Outcome | None, reference_out: Outcome | None, reference_label: str) -> str:
    bc = div.breaking_case
    if bc is None:
        return "(no breaking case recorded)"
    if bc.source in ("pytest", "regression"):
        which = "hidden spec test written from the spec" if bc.source == "pytest" else "existing test in the repository"
        return f"failing test: {bc.args[0] if bc.args else bc.function} ({which})\nresult in the failed agent's sandbox: {fmt_outcome(failed_out)}\nexpected: passes"
    return (
        f"function: {bc.function}\ninput: {json.dumps(bc.args, default=str)}\n"
        f"failed agent returned: {fmt_outcome(failed_out)}\n{reference_label} returned: {fmt_outcome(reference_out)}"
    )


def _failed_outcome(failed: AgentRun, div: Divergence, result: CrossTestResult | None) -> Outcome | None:
    if result is None or div.breaking_case is None:
        return None
    return run_outcome(result, failed.id, div.breaking_case.id)


def _is_spec_case(div: Divergence, result: CrossTestResult | None) -> bool:
    expected = getattr(result, "expected_outcomes", None) or {}
    return bool(div.breaking_case) and div.breaking_case.id in expected


def fmt_outcome(o: Outcome | None) -> str:
    if o is None:
        return "(unknown)"
    if o.timed_out:
        return "timed out"
    if not o.ok:
        return f"raised {o.exception}"
    return json.dumps(o.value, default=str) if not isinstance(o.value, str) else o.value


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 else "{}"
