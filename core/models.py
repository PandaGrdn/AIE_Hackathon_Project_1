"""Shared schemas. Every component speaks these types."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- task decomposition ----------


class FunctionSig(BaseModel):
    """One pinned function every implementation must expose."""

    name: str
    file: str  # repo-relative path
    params: list[str]  # "name: type" strings
    returns: str | None = None


class Subtask(BaseModel):
    id: str = Field(default_factory=new_id)
    index: int
    title: str
    spec: str  # what to build, written from the task, never from an implementation
    interface: list[FunctionSig]
    files: list[str]
    depends_on: list[str] = []
    tags: list[str] = []


class LongTask(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    description: str
    repo_path: str
    subtasks: list[Subtask] = []


# ---------- agents and reasoning ----------


class AgentIdentity(BaseModel):
    """A (model, config) pair. Trust is tracked per identity."""

    id: str
    provider: str  # subq | meta | gemini | anthropic | openai
    model: str
    temperature: float = 0.2
    prompt_variant: str = "default"


class ReasoningStep(BaseModel):
    """What an agent logs via `log_step` before every action."""

    index: int
    assumption: str
    action: str
    files_touched: list[str] = []
    ts: datetime = Field(default_factory=now)
    embedding: list[float] | None = None

    def as_text(self) -> str:
        touches = ", ".join(self.files_touched) or "-"
        return f"assumption: {self.assumption} | action: {self.action} | touches: {touches}"


class AgentRun(BaseModel):
    id: str = Field(default_factory=new_id)
    subtask_id: str
    agent: AgentIdentity
    sandbox_path: str
    steps: list[ReasoningStep] = []
    diff: str = ""
    tests_path: str | None = None
    lessons_used: list[str] = []
    error: str | None = None
    duration_s: float = 0.0


# ---------- cross-testing ----------


class Outcome(BaseModel):
    """Result of one implementation on one input."""

    ok: bool
    value: Any = None
    exception: str | None = None  # exception type name
    timed_out: bool = False


class TestCase(BaseModel):
    id: str = Field(default_factory=new_id)
    function: str
    args: list[Any]
    kwargs: dict[str, Any] = {}
    source: str = "fuzz"  # fuzz | seed | agent:<id> | lesson:<id>


class CrossTestResult(BaseModel):
    subtask_id: str
    cases: list[TestCase]
    # outcomes[run_id][case_id]
    outcomes: dict[str, dict[str, Outcome]]
    # majority value per case_id, agreement share, and run_ids in the majority group
    majority: dict[str, "MajorityVote"]
    failed_runs: list[str]
    verified_runs: list[str]


class MajorityVote(BaseModel):
    case_id: str
    agreement: float  # trust-weighted share in the largest group, 0..1
    group_run_ids: list[str]
    representative: Outcome


class Divergence(BaseModel):
    """Where a failed run's reasoning left the consensus path."""

    run_id: str
    step_index: int
    step_text: str
    distance: float
    consensus_step_texts: list[str]
    breaking_case: TestCase | None = None
    majority_outcome: Outcome | None = None


# ---------- self-improvement ----------


class ChangeStatus(StrEnum):
    candidate = "candidate"
    adopted = "adopted"
    rolled_back = "rolled_back"


class Lesson(BaseModel):
    id: str = Field(default_factory=new_id)
    text: str
    tags: list[str] = []
    embedding: list[float] | None = None  # embedding of the divergence step
    divergence_embedding: list[float] | None = None
    hit_count: int = 1
    confidence: float = 0.5
    status: ChangeStatus = ChangeStatus.candidate
    version: int = 1
    created_from: dict[str, Any] = {}  # {task_id, subtask_id, run_id, breaking_input}
    created_at: datetime = Field(default_factory=now)


class FailureRegion(BaseModel):
    id: str = Field(default_factory=new_id)
    lesson_id: str
    centroid: list[float]
    radius: float
    weight: float = 1.0


class ChangeKind(StrEnum):
    context = "context"  # a lesson injected into prompts
    skill = "skill"  # a lesson promoted to a reusable skill file
    routing = "routing"  # a trust-score / agent-selection change
    tool = "tool"  # a generated tool


class Change(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: ChangeKind
    ref_id: str  # lesson id, agent id, or tool id
    description: str
    status: ChangeStatus = ChangeStatus.candidate
    version: int = 1
    heldout_before: dict[str, float] = {}
    heldout_after: dict[str, float] = {}
    decided_at: datetime | None = None
    reason: str = ""


class TrustScore(BaseModel):
    agent_id: str
    agreements: int = 0
    disagreements: int = 0
    history: list[float] = []

    @property
    def score(self) -> float:
        # Laplace-smoothed agreement rate (outline §5.10)
        return (self.agreements + 1) / (self.agreements + self.disagreements + 2)


class RunManifest(BaseModel):
    """Everything needed to reproduce a subtask run."""

    id: str = Field(default_factory=new_id)
    task_id: str
    subtask_id: str
    agent_ids: list[str]
    lessons_used: list[str]
    seeds: dict[str, int] = {}
    container_digests: dict[str, str] = {}
    created_at: datetime = Field(default_factory=now)
