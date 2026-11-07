"""SubQ Snap Decisions API client and the reflector's root-cause localization call.

SubQ Snap is not a chat model: `POST /v1/systemone` takes a `state` (text or JSON) and a set of
named `questions` (noul = yes/no probability, choice = distribution over named options,
score = distribution over ordered levels) and returns calibrated probabilities. The reflector uses
it to answer three questions about a failed run (outline §5.7):

- root_step: which labelled step [S<i>] of the failed reasoning path caused the failure,
- kind: bug | spec_ambiguity | acceptable_variation,
- generalizes: would a lesson from this help on other subtasks.

`locate_divergence_subq` turns the answers into a `Divergence`, and falls back to the
embedding-based `find_divergence` when SubQ is unavailable or answers "none".
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from core.config import settings
from core.models import AgentRun, Divergence, Outcome, ReasoningStep, TestCase
from core.paths.divergence import N_BINS, _phase_bin, consensus_path, find_divergence

MAX_STATE_CHARS = 300_000
MAX_SPEC_CHARS = 4_000
MAX_STEP_CHARS = 2_000
MAX_TEST_OUTPUT_CHARS = 20_000
MAX_CHOICE_OPTIONS = 255

KIND_OPTIONS: dict[str, str] = {
    "bug": "The agent misread or violated a requirement the spec states unambiguously; the consensus reading is the correct one.",
    "spec_ambiguity": "The spec wording reasonably admits the agent's reading as well as the consensus reading; the failure comes from an ambiguous or underspecified requirement.",
    "acceptable_variation": "Both behaviours satisfy the spec; the failing evidence reflects an allowed difference, not an error.",
}


class SubQError(Exception):
    """Non-2xx response (or transport failure, status 0) from the Decisions API."""

    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(f"SubQ {status} {error_type}: {message}")
        self.status = status
        self.error_type = error_type
        self.message = message


class _hybridmethod:
    """Callable on the class (checks settings) or on an instance (checks that instance)."""

    def __init__(self, f):
        self.f = f

    def __get__(self, obj, cls):
        return lambda: self.f(obj if obj is not None else cls)


class SubQDecisions:
    def __init__(
        self,
        api_key: str | None = None,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        *,
        transport: httpx.BaseTransport | None = None,
        backoff: float = 1.0,
    ):
        self.api_key = settings.subq_api_key if api_key is None else api_key
        self.url = url or settings.subq_decisions_url
        self.model = model or settings.subq_model
        self.timeout = timeout
        self.backoff = backoff
        self._transport = transport
        self.last_usage: dict[str, Any] = {}

    @_hybridmethod
    def is_configured(self_or_cls) -> bool:
        if isinstance(self_or_cls, SubQDecisions):
            return bool(self_or_cls.api_key)
        return bool(settings.subq_api_key)

    def decide(self, state: str | dict | list, questions: dict[str, dict]) -> dict[str, Any]:
        """Ask `questions` about `state`; returns the `answers` object. Retries once on 429/5xx."""
        if not self.api_key:
            raise SubQError(0, "not_configured", "SUBQ_API_KEY is not set")
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout, transport=self._transport) as http:
            for attempt in range(2):
                try:
                    r = http.post(self.url, json=body, headers=headers)
                except httpx.TransportError as e:
                    if attempt == 0:
                        time.sleep(self.backoff)
                        continue
                    raise SubQError(0, "network_error", f"{type(e).__name__}: {e}") from e
                if (r.status_code == 429 or r.status_code >= 500) and attempt == 0:
                    time.sleep(self._retry_delay(r))
                    continue
                break
        if not r.is_success:
            raise _error_from(r)
        data = r.json()
        self.last_usage = data.get("usage") or {}
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise SubQError(r.status_code, "bad_response", "response has no 'answers' object")
        return answers

    def _retry_delay(self, r: httpx.Response) -> float:
        try:
            return min(float(r.headers.get("retry-after", "")), 5.0)
        except ValueError:
            return self.backoff


def _error_from(r: httpx.Response) -> SubQError:
    error_type, message = "http_error", r.text[:500]
    try:
        detail = r.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        error_type = str(detail.get("error_type") or error_type)
        message = str(detail.get("message") or message)
    elif isinstance(detail, str):
        message = detail
    elif isinstance(detail, list):  # FastAPI-style validation errors
        error_type, message = "validation_error", json.dumps(detail)[:500]
    return SubQError(r.status_code, error_type, message)


# ---------- state and questions ----------


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 15] + " …[truncated]"


def _label(step: ReasoningStep) -> str:
    return f"S{step.index}"


def _fmt_outcome(o: Outcome | None) -> str:
    if o is None:
        return "unknown"
    if o.timed_out:
        return "timed out"
    if not o.ok:
        return f"raised {o.exception or 'an exception'}"
    return f"returned {json.dumps(o.value, default=str)}"


def _fmt_call(c: TestCase) -> str:
    parts = [json.dumps(a, default=str) for a in c.args]
    parts += [f"{k}={json.dumps(v, default=str)}" for k, v in sorted(c.kwargs.items())]
    return f"{c.function}({', '.join(parts)})"


def _dedupe(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _consensus_groups(failed: AgentRun, verified: list[AgentRun]) -> list[tuple[str, list[int], list[str]]]:
    """[(heading, failed step indices aligned to it, consensus step texts)], deterministic order.

    Uses the embedding consensus path's phase bins; when no verified step has an embedding,
    groups the verified runs' steps by the same step index instead.
    """
    groups: list[tuple[str, list[int], list[str]]] = []
    cons = consensus_path(verified)
    if cons:
        n = len(failed.steps)
        aligned: dict[int, list[int]] = {}
        for s in failed.steps:
            aligned.setdefault(_phase_bin(s.index, n), []).append(s.index)
        for b in sorted(cons):
            idxs = aligned.get(b, [])
            lo, hi = round(100 * b / N_BINS), round(100 * (b + 1) / N_BINS)
            aligns = ", ".join(f"S{i}" for i in idxs) or "no failed step"
            groups.append((f"phase {b + 1}/{N_BINS} ({lo}-{hi}% of path; aligns with {aligns})", idxs, _dedupe(cons[b][3])))
        return groups
    for s in sorted(failed.steps, key=lambda s: s.index):
        texts = _dedupe([v.as_text() for run in verified for v in run.steps if v.index == s.index])
        if texts:
            groups.append((f"step {s.index} (aligns with S{s.index}; by step index, no embeddings)", [s.index], texts))
    return groups


def _consensus_for_step(failed: AgentRun, verified: list[AgentRun], step_index: int, k: int) -> list[str]:
    for _, idxs, texts in _consensus_groups(failed, verified):
        if step_index in idxs:
            return texts[:k]
    return []


def build_localization_state(
    spec: str,
    failed: AgentRun,
    verified: list[AgentRun],
    breaking: tuple[TestCase, Outcome] | None,
    failed_outcome: Outcome | None,
    hidden_test_output: str | None = None,
    max_consensus_per_phase: int = 3,
    max_chars: int = MAX_STATE_CHARS,
) -> tuple[str, list[str]]:
    """The `state` text for the localization call and the ordered step labels ["S0", ...]."""
    steps = sorted(failed.steps, key=lambda s: s.index)
    labels = [_label(s) for s in steps]
    path_lines = [f"[{_label(s)}] {_clip(s.as_text(), MAX_STEP_CHARS)}" for s in steps]
    groups = _consensus_groups(failed, verified)

    ev = []
    if breaking is not None:
        case, majority = breaking
        ev.append(f"breaking case: {_fmt_call(case)}  (source: {case.source})")
        ev.append(f"expected / majority outcome: {_fmt_outcome(majority)}")
    else:
        ev.append("breaking case: none recorded")
    ev.append(f"failed agent's outcome: {_fmt_outcome(failed_outcome)}")
    if hidden_test_output:
        t = hidden_test_output
        if len(t) > MAX_TEST_OUTPUT_CHARS:  # keep the head and the tail, where pytest puts the summary
            half = MAX_TEST_OUTPUT_CHARS // 2
            t = t[:half] + "\n…[truncated]…\n" + t[-half:]
        ev.append("hidden test output:\n" + t)

    def render(spec_chars: int, per_phase: int, cons_chars: int) -> str:
        out = [
            "TASK: localize the root cause of a failed implementation in the agent's reasoning path.",
            "",
            "SUBTASK SPEC",
            _clip(spec.strip(), spec_chars),
            "",
            f"FAILED AGENT REASONING PATH (run {failed.id}, agent {failed.agent.id}, {len(steps)} steps)",
            *path_lines,
            "",
            f"CONSENSUS at each phase (assumptions of {len(verified)} verified runs that passed)",
        ]
        if not groups:
            out.append("(no consensus available)")
        elif per_phase == 0:
            out.append("(omitted for length)")
        for heading, _, texts in groups if per_phase else []:
            out.append(f"{heading}:")
            out += [f"  - {_clip(t, cons_chars)}" for t in texts[:per_phase]]
        out += ["", "FAILING EVIDENCE", *ev]
        return "\n".join(out)

    # Trim consensus first (fewer texts per phase, then shorter, then none), then the spec.
    attempts = [(MAX_SPEC_CHARS, max_consensus_per_phase, MAX_STEP_CHARS)]
    attempts += [(MAX_SPEC_CHARS, 1, MAX_STEP_CHARS), (MAX_SPEC_CHARS, 1, 300), (MAX_SPEC_CHARS, 0, 0)]
    attempts += [(1_000, 0, 0), (0, 0, 0)]
    for spec_chars, per_phase, cons_chars in attempts:
        state = render(spec_chars, min(per_phase, max_consensus_per_phase), cons_chars)
        if len(state) <= max_chars:
            return state, labels
    return _clip(state, max_chars), labels


def build_localization_questions(labels: list[str], steps: list[ReasoningStep] | None = None) -> dict:
    """root_step (choice over labels + none), kind (choice), generalizes (noul)."""
    by_label = {_label(s): s for s in steps or []}
    opts: dict[str, str | None] = {}
    for lab in labels[: MAX_CHOICE_OPTIONS - 1]:
        s = by_label.get(lab)
        opts[lab] = f"step {lab}: {_clip(s.assumption, 160)}" if s else None
    opts["none"] = "No single step of the failed path explains the failing evidence."
    return {
        "root_step": {
            "type": "choice",
            "instructions": (
                "Which step of the FAILED AGENT REASONING PATH is the root cause of the FAILING EVIDENCE? "
                "Pick the earliest step whose assumption departs from the SUBTASK SPEC and the CONSENSUS "
                "at the same phase and leads to the failed agent's outcome. Later steps that only carry "
                "out an earlier wrong assumption are not the root cause."
            ),
            "criteria": opts,
        },
        "kind": {
            "type": "choice",
            "instructions": "What kind of failure is this, judged against the SUBTASK SPEC wording?",
            "criteria": dict(KIND_OPTIONS),
        },
        "generalizes": {
            "type": "noul",
            "instructions": (
                "Would a short rule learned from this root-cause step help agents avoid a similar "
                "mistake on other, different subtasks?"
            ),
            "criteria": {
                "true": "The mistake is a general reasoning pattern (e.g. boundary inclusivity, rounding mode, empty input) that recurs across tasks.",
                "false": "The mistake is specific to this subtask's details and would not transfer.",
            },
        },
    }


# ---------- localization ----------


def _with_path_signal(d: Divergence | None) -> Divergence | None:
    if d is not None:
        d.signals = ["path"]
    return d


def _attach_breaking(d: Divergence | None, breaking: tuple[TestCase, Outcome] | None) -> Divergence | None:
    if d is not None and breaking is not None:
        d.breaking_case, d.majority_outcome = breaking
    return d


def locate_divergence_subq(
    spec: str,
    failed: AgentRun,
    verified: list[AgentRun],
    breaking: tuple[TestCase, Outcome] | None = None,
    failed_outcome: Outcome | None = None,
    hidden_test_output: str | None = None,
    client: SubQDecisions | None = None,
    fallback: bool = True,
) -> Divergence | None:
    """Ask SubQ Snap which step of `failed` caused the failure; falls back to `find_divergence`."""
    client = client or SubQDecisions()
    path_div = find_divergence(failed, verified) if verified else None
    state, labels = build_localization_state(spec, failed, verified, breaking, failed_outcome, hidden_test_output)
    if not labels:
        return _attach_breaking(_with_path_signal(path_div), breaking)
    try:
        answers = client.decide(state, build_localization_questions(labels, failed.steps))
    except SubQError:
        if not fallback:
            raise
        return _attach_breaking(_with_path_signal(path_div), breaking)

    root = answers.get("root_step") or {}
    probs = {k: float(v) for k, v in (root.get("probabilities") or {}).items() if k in labels}
    kind_ans = answers.get("kind") or {}
    kind = kind_ans.get("choice") if kind_ans.get("choice") in KIND_OPTIONS else None
    kind_conf = kind_ans.get("confidence")
    if kind_conf is None and kind is not None:
        kind_conf = (kind_ans.get("probabilities") or {}).get(kind)
    gen = (answers.get("generalizes") or {}).get("noul")

    by_label = {_label(s): s for s in failed.steps}
    choice = root.get("choice")
    if choice in by_label:
        step = by_label[choice]
        agrees = path_div is not None and path_div.step_index == step.index
        div = Divergence(
            run_id=failed.id,
            step_index=step.index,
            step_text=step.as_text(),
            distance=path_div.distance if agrees else 0.0,
            consensus_step_texts=_consensus_for_step(failed, verified, step.index, 5),
            signals=["subq", "path"] if agrees else ["subq"],
        )
    elif not fallback:
        return None
    elif path_div is not None:
        div = _with_path_signal(path_div)
    elif probs:  # "none" with no embedding fallback: take SubQ's most likely step anyway
        lab = max(probs, key=lambda k: (probs[k], -labels.index(k)))
        step = by_label[lab]
        div = Divergence(
            run_id=failed.id, step_index=step.index, step_text=step.as_text(), distance=0.0,
            consensus_step_texts=_consensus_for_step(failed, verified, step.index, 5), signals=["subq"],
        )
    else:
        return None

    div.step_probabilities = probs
    div.kind = kind
    div.kind_confidence = float(kind_conf) if kind_conf is not None else None
    div.generalizes = float(gen) if gen is not None else None
    return _attach_breaking(div, breaking)
