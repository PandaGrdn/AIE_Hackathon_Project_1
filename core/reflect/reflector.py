"""Reflector: failed run + divergence point + breaking input + majority → lesson. Owner: D.

Runs on SubQ (long context: the whole failed path, consensus steps, results).
The lesson is tied to the divergence step's embedding so it defines a failure region.
"""

from __future__ import annotations

import json

from core.llm.clients import get_client, reflector_identity
from core.llm.embeddings import embed_texts
from core.models import AgentRun, Divergence, Lesson
from core.reflect.dedup import dedup_or_insert
from core.store.base import Store

REFLECT_PROMPT = """An AI coding agent failed a subtask. Independent agents solving the same subtask reached a verified majority answer.
Your job: identify the reasoning error at the divergence point and write ONE short, general, actionable lesson that would have prevented it.

## Subtask spec
{spec}

## The failed agent's reasoning path (step index: assumption | action | files)
{path}

## Divergence point (first step that left the consensus path)
step {idx}: {step}

## What the majority was reasoning at the same phase
{consensus}

## Breaking input and outcomes
input: {breaking}
failed agent returned: {failed_out}
majority returned: {majority_out}

Return JSON: {{"lesson": str, "tags": [str], "confidence": float}}
Rules for the lesson:
- A rule, not a description of this bug. Example: "'over $100' means strictly greater; always test the boundary value."
- One or two sentences. General enough to apply to similar subtasks, specific enough to change behavior.
"""


def reflect(spec: str, failed: AgentRun, div: Divergence, store: Store) -> Lesson:
    client = get_client(reflector_identity())
    path = "\n".join(f"{s.index}: {s.as_text()}" for s in failed.steps)
    prompt = REFLECT_PROMPT.format(
        spec=spec,
        path=path,
        idx=div.step_index,
        step=div.step_text,
        consensus="\n".join(f"- {t}" for t in div.consensus_step_texts),
        breaking=json.dumps(div.breaking_case.args if div.breaking_case else None),
        failed_out=_fmt(failed, div),
        majority_out=div.majority_outcome.model_dump() if div.majority_outcome else None,
    )
    resp = client.chat([{"role": "user", "content": prompt}])
    data = json.loads(_extract_json(resp["text"]))

    step = failed.steps[div.step_index]
    div_vec = step.embedding or embed_texts([step.as_text()])[0]
    [lesson_vec] = embed_texts([data["lesson"]])
    lesson = Lesson(
        text=data["lesson"].strip(),
        tags=data.get("tags", []),
        embedding=lesson_vec,
        divergence_embedding=div_vec,
        confidence=float(data.get("confidence", 0.5)),
        created_from={"subtask_id": failed.subtask_id, "run_id": failed.id, "agent_id": failed.agent.id, "breaking_input": div.breaking_case.args if div.breaking_case else None},
    )
    return dedup_or_insert(lesson, store)


def _fmt(failed: AgentRun, div: Divergence) -> str:
    return "(see harness outcome)"  # TODO(D): pass the failed run's outcome on the breaking case


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 else "{}"
