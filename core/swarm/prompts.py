"""Swarm agent prompts. Variants exist so identities differ in framing, not just temperature."""

from __future__ import annotations

from core.models import Subtask

SYSTEM_DEFAULT = """You are an engineer implementing one subtask in an existing repository.
You MUST call `log_step` before every `write_file` or `run_tests` call, stating the assumption you are relying on.
Implement exactly the pinned interface. Write tests in `tests/test_{slug}.py` that cover edge cases and boundaries.
Call `done` when finished. Do not explain; act through tools."""

SYSTEM_ALT = """You are a careful reviewer-turned-implementer. Before writing code, enumerate the ways this subtask could be misread,
then pick the reading that matches the spec literally. Call `log_step` before every file write or test run with the assumption you are acting on.
Implement the pinned interface exactly, write boundary tests in `tests/test_{slug}.py`, and call `done`."""

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
