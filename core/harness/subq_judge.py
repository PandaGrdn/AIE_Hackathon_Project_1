"""SubQ as the verifier: the Decisions API (`POST /v1/systemone`). Owner: Ethan Li.

SubQ Snap 1.1 is a decision model, not a text generator: it reads a long `state` (up to 384K
tokens) and answers named questions with calibrated probabilities. It never writes code or
prose, so it cannot be fooled into "agreeing" with an implementation it is shown; it only
judges. We use it for the judgments a fuzz-and-majority harness cannot make:

  - `judge_ambiguity`: the spec author's expected value and the swarm majority disagree on a
    case. SubQ sees the initial spec + the case + both answers (never any code) and says who
    follows the spec.
  - `judge_root_cause`: which of a failed agent's reasoning steps is the reasoning error behind
    the failing test (a `choice` over step indices + "none").
  - `judge_lesson`: would following this lesson have prevented the failure (`noul`), used as the
    lesson's confidence.

Question types: noul (p of yes), choice (option + probabilities + confidence),
score (ordered levels). Only input tokens are billed.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from core.config import settings

DECISIONS_PATH = "/systemone"
TIMEOUT_S = 60.0
RETRIES = 2


class SubQJudge:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = settings.subq_api_key if api_key is None else api_key
        self.base_url = (base_url or settings.subq_base_url).rstrip("/")
        self.model = model or settings.subq_model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def decide(self, state: Any, questions: dict[str, dict]) -> dict[str, Any]:
        """Raw call. `state` is a string, object or array; returns the `answers` object."""
        body = {"model": self.model, "state": state, "questions": questions}
        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                r = httpx.post(self.base_url + DECISIONS_PATH, headers={"Authorization": f"Bearer {self.api_key}"}, json=body, timeout=TIMEOUT_S)
                if r.status_code in (429, 502, 503) and attempt < RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()["answers"]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last = e
                if attempt < RETRIES:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"SubQ decisions call failed: {last}")

    # --- the three verifier judgments ---

    def judge_ambiguity(self, spec: str, function: str, args: list, author_expected: Any, majority_value: Any) -> float:
        """P(the spec author's expected outcome is what the spec requires). SubQ sees no code."""
        state = {
            "spec": spec,
            "call": f"{function}({', '.join(json.dumps(a, default=str) for a in args)})",
            "answer_A": _fmt(author_expected),
            "answer_B": _fmt(majority_value),
        }
        q = {
            "author_right": {
                "type": "noul",
                "instructions": "Two candidate results are given for one call. Reading the spec literally, is answer_A the result the spec requires?",
                "criteria": {"true": "answer_A follows from the spec text as written", "false": "answer_B follows from the spec, or the spec does not determine the result"},
            }
        }
        return float(self.decide(state, q)["author_right"]["noul"])

    def judge_root_cause(self, spec: str, steps: list[str], failing: str, failed_result: str, expected: str) -> tuple[int | None, dict[str, float], float]:
        """Which reasoning step is the error behind the failing test? Returns (step index or None,
        probabilities by option, confidence). Steps are given as 'index: assumption | action | files'."""
        state = {"spec": spec, "reasoning_path": steps, "failing_test": failing, "agent_result": failed_result, "expected": expected}
        criteria = {f"step_{i}": s[:300] for i, s in enumerate(steps)}
        criteria["none"] = "No logged step contains the mistaken assumption; the error is elsewhere or unlogged"
        q = {
            "root_cause": {
                "type": "choice",
                "instructions": "Which reasoning step states the assumption that caused the failing test? Pick the earliest step whose assumption contradicts the spec on this input.",
                "criteria": criteria,
            }
        }
        a = self.decide(state, q)["root_cause"]
        choice = a.get("choice", "none")
        idx = int(choice.removeprefix("step_")) if choice.startswith("step_") else None
        return idx, {k: float(v) for k, v in a.get("probabilities", {}).items()}, float(a.get("confidence", 0.0))

    def judge_lesson(self, spec: str, divergence_step: str, failing: str, lesson: str) -> float:
        """P(an agent that followed this lesson at the divergence step would have passed the failing test)."""
        state = {"spec": spec, "divergence_step": divergence_step, "failing_test": failing, "lesson": lesson}
        q = {
            "prevents": {
                "type": "noul",
                "instructions": "If the agent had followed the lesson at the divergence step, would the failing test have passed?",
                "criteria": {"true": "The lesson corrects exactly the mistaken assumption that produced the failure", "false": "The lesson is unrelated, too vague, or describes the bug without fixing the reasoning"},
            }
        }
        return float(self.decide(state, q)["prevents"]["noul"])


def _fmt(v: Any) -> str:
    if isinstance(v, dict) and set(v) == {"raises"}:
        return f"raises {v['raises']}"
    return json.dumps(v, default=str) if not isinstance(v, str) else v
