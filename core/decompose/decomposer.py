"""Long task → ordered subtasks with pinned interfaces. Owner: A.

The spec for each subtask is written from the task description and the repo,
never from any implementation. Interfaces are pinned so every swarm
implementation can be called the same way by the harness.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.llm.clients import get_client, reflector_identity
from core.models import FunctionSig, LongTask, Subtask

DECOMPOSE_PROMPT = """You are decomposing a software task into ordered subtasks for independent implementers.

Task:
{description}

Repository tree:
{tree}

Return JSON: {{"subtasks": [{{"title": str, "spec": str, "files": [str], "depends_on_titles": [str],
"interface": [{{"name": str, "file": str, "params": ["name: type", ...], "returns": str}}], "tags": [str]}}]}}

Rules:
- Each subtask must be independently implementable given the previous ones are done.
- Pin every public function an implementer must expose: exact name, file, params with types, return type.
- Specs describe goals and constraints, not how to implement them.
- 3 to 6 subtasks.
"""


def _tree(repo: Path, max_files: int = 200) -> str:
    files = [str(p.relative_to(repo)) for p in sorted(repo.rglob("*.py")) if ".git" not in p.parts]
    return "\n".join(files[:max_files])


def decompose(task: LongTask) -> LongTask:
    """Fill `task.subtasks` using the reflector model (SubQ) as the decomposer."""
    repo = Path(task.repo_path)
    client = get_client(reflector_identity())
    resp = client.chat([{"role": "user", "content": DECOMPOSE_PROMPT.format(description=task.description, tree=_tree(repo))}])
    data = json.loads(_extract_json(resp["text"]))

    by_title: dict[str, str] = {}
    subtasks: list[Subtask] = []
    for i, s in enumerate(data["subtasks"]):
        st = Subtask(
            index=i,
            title=s["title"],
            spec=s["spec"],
            files=s.get("files", []),
            interface=[FunctionSig(**f) for f in s.get("interface", [])],
            tags=s.get("tags", []),
        )
        by_title[st.title] = st.id
        st.depends_on = [by_title[t] for t in s.get("depends_on_titles", []) if t in by_title]
        subtasks.append(st)
    task.subtasks = subtasks
    return task


def load_task(path: str | Path) -> LongTask:
    """A task file is markdown with a front-matter-ish header: first line `# title`, then the description.
    A `repo:` line names the repo path relative to the task file."""
    p = Path(path)
    text = p.read_text()
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else p.stem
    repo = "repo"
    for ln in lines:
        if ln.startswith("repo:"):
            repo = ln.split(":", 1)[1].strip()
    return LongTask(title=title, description=text, repo_path=str((p.parent / repo).resolve()))


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 else "{}"
