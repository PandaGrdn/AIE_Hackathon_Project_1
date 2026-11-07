"""Swarm agent prompts. Variants exist so identities differ in framing, not just temperature."""

from __future__ import annotations

from core.models import Subtask

_RULES = """Required sequence: explore with `list_files` / `read_file` -> `log_step` -> `write_file` -> `log_step` -> `run_tests` or `run_spec_tests` -> `done`.
Every `write_file`, `run_tests` and `run_spec_tests` call MUST be immediately preceded by its own `log_step` (assumption, action, files_touched); a call without one is REJECTED and does nothing.
Implement exactly the pinned interface (names, params, return types) in the listed files. Do not modify files outside the subtask scope, except your own tests under `tests/test_{slug}.py`.
Stop with `done` as soon as `run_spec_tests` passes, or when you have done your best. Do not explain; act through tools."""

SYSTEM_DEFAULT = """You are an engineer implementing one subtask in an existing repository.
Read the relevant files first so your change fits the codebase, then implement.
""" + _RULES

SYSTEM_ALT = """You are a careful reviewer-turned-implementer. Before writing code, enumerate the ways this subtask could be misread,
then pick the reading that matches the spec literally and state it as the assumption in your first `log_step`.
Inspect the surrounding code before touching it; prefer small, boundary-aware changes.
""" + _RULES

VARIANTS = {"default": SYSTEM_DEFAULT, "alt": SYSTEM_ALT}


def system_prompt(variant: str, subtask: Subtask) -> str:
    return VARIANTS.get(variant, SYSTEM_DEFAULT).format(slug=subtask.title.lower().replace(" ", "_")[:40])


def user_prompt(subtask: Subtask, lessons_block: str, repo_snippets: str) -> str:
    iface = "\n".join(f"- {s.file}: def {s.name}({', '.join(s.params)}) -> {s.returns or 'Any'}" for s in subtask.interface)
    parts = [
        f"## Subtask: {subtask.title}",
        subtask.spec,
        "## Pinned interface (must match exactly)",
        iface or "(none)",
        "## Files in scope",
        "\n".join(f"- {f}" for f in subtask.files) or "(none)",
    ]
    if lessons_block:
        parts += ["## " + lessons_block]
    if repo_snippets:
        parts += ["## Relevant existing code", repo_snippets]
    return "\n\n".join(parts)
