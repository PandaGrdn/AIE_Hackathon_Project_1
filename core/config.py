"""Settings loaded from .env. Import `settings` anywhere."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # LLM providers
    subq_api_key: str = field(default_factory=lambda: _env("SUBQ_API_KEY"))
    subq_base_url: str = field(default_factory=lambda: _env("SUBQ_BASE_URL", "https://api.subq.ai/v1"))
    subq_model: str = field(default_factory=lambda: _env("SUBQ_MODEL", "subq-snap-1.1"))
    # SubQ Snap is a Decisions API (POST /v1/systemone): probabilities over choices, no text generation.
    subq_decisions_url: str = field(default_factory=lambda: _env("SUBQ_DECISIONS_URL", "https://api.subq.ai/v1/systemone"))

    meta_api_key: str = field(default_factory=lambda: _env("META_API_KEY"))
    meta_base_url: str = field(default_factory=lambda: _env("META_BASE_URL", "https://api.meta.ai/v1"))
    meta_model_code: str = field(default_factory=lambda: _env("META_MODEL_CODE", "muse-wonder-1.0"))
    meta_model_spark: str = field(default_factory=lambda: _env("META_MODEL_SPARK", "muse-spark-1.3"))

    # reasoning_effort for Meta tool-loop (swarm agent) turns: minimal | low | medium | high, "" = model default
    meta_agent_reasoning: str = field(default_factory=lambda: _env("META_AGENT_REASONING", "low"))

    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.8-flash"))
    # A deliberately weaker, hotter swarm member: real diversity is how real failures (and lessons) arise.
    gemini_weak_model: str = field(default_factory=lambda: _env("GEMINI_WEAK_MODEL", "gemini-3.5-flash-lite"))
    # thinking level for Gemini tool-loop (swarm agent) turns: low | medium | high, "" = model default
    gemini_agent_thinking: str = field(default_factory=lambda: _env("GEMINI_AGENT_THINKING", "low"))
    gemini_embed_model: str = field(default_factory=lambda: _env("GEMINI_EMBED_MODEL", "gemini-embedding-2"))
    # Text-generating roles (decomposer, spec-test author, reflector lesson text) run on Gemini;
    # SubQ Snap answers the decision questions (root-cause step, failure kind, generalizes).
    text_provider: str = field(default_factory=lambda: _env("TEXT_PROVIDER", "gemini"))

    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))

    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-5.4-mini"))

    # Store
    mongodb_uri: str = field(default_factory=lambda: _env("MONGODB_URI"))
    mongodb_db: str = field(default_factory=lambda: _env("MONGODB_DB", "swarm"))
    use_memory_store: bool = field(default_factory=lambda: _env("USE_MEMORY_STORE", "1") == "1")

    # Swarm
    swarm_size: int = field(default_factory=lambda: int(_env("SWARM_SIZE", "6")))
    agent_timeout_s: int = field(default_factory=lambda: int(_env("AGENT_TIMEOUT_S", "600")))
    sandbox_backend: str = field(default_factory=lambda: _env("SANDBOX_BACKEND", "worktree"))
    # Directories (repo-relative) whose tests form the regression suite run in every sandbox at verdict time.
    regression_test_paths: tuple[str, ...] = field(default_factory=lambda: tuple(_env("REGRESSION_TEST_PATHS", "tests").replace(",", " ").split()))

    # Paths
    runs_dir: Path = ROOT / "runs"
    sandboxes_dir: Path = ROOT / "sandboxes"

    # Thresholds (see outline §4 verdict rules and §5.7)
    majority_block_threshold: float = 0.75
    majority_ambiguous_threshold: float = 0.50
    divergence_sigma: float = 2.0  # steps > mean + sigma*std from consensus are divergent
    lesson_dedup_similarity: float = 0.90

    # Live-run cost control. Every held-out sweep costs <slices> × <heldout_swarm_size> real agent runs,
    # and each gated change needs a "with" sweep (the "without" sweep is cached per lesson set).
    heldout_swarm_size: int = field(default_factory=lambda: int(_env("HELDOUT_SWARM_SIZE", "2")))
    # Which change kinds go through the held-out gate. Routing (trust) changes are batched per subtask
    # and, when not gated, are applied directly and recorded as adopted with reason "not gated".
    gate_kinds: tuple[str, ...] = field(default_factory=lambda: tuple(_env("GATE_KINDS", "context,skill,tool").replace(",", " ").split()))


settings = Settings()
