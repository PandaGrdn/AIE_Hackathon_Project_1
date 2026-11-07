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


PRECOMPUTED_NAME = "subtasks.json"


def precomputed_path(task: LongTask) -> Path:
    """Where a hand-authored decomposition for `task` lives.

    `load_task` resolves `repo_path` as `<task_file_dir>/<repo:>`, and by convention `repo:` names a
    direct child of the task file's directory, so the task file's directory is `repo_path`'s parent.
    """
    return Path(task.repo_path).parent / PRECOMPUTED_NAME


def decompose(task: LongTask, precomputed: str | Path | None = None) -> LongTask:
    """Fill `task.subtasks`.

    Uses a precomputed decomposition (`precomputed`, else `<task_file_dir>/subtasks.json`) when one
    exists; otherwise asks the reflector model (SubQ). Both paths go through `subtasks_from_json`,
    which validates the result.
    """
    path = Path(precomputed) if precomputed is not None else precomputed_path(task)
    if path.is_file():
        data = json.loads(path.read_text())
        want = data.get("task_title")
        if want is not None and want != task.title:
            raise ValueError(f"{path} decomposes task {want!r}, not {task.title!r}")
    elif precomputed is not None:
        raise FileNotFoundError(f"precomputed decomposition not found: {path}")
    else:
        data = _decompose_llm(task)
    task.subtasks = subtasks_from_json(data)
    return task


def _decompose_llm(task: LongTask) -> dict:
    repo = Path(task.repo_path)
    client = get_client(reflector_identity())
    resp = client.chat([{"role": "user", "content": DECOMPOSE_PROMPT.format(description=task.description, tree=_tree(repo))}])
    return json.loads(_extract_json(resp["text"]))


def subtasks_from_json(data: dict) -> list[Subtask]:
    """Convert the decomposer JSON shape (see DECOMPOSE_PROMPT) into ordered, validated Subtasks."""
    validate_decomposition(data)
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
        st.depends_on = [by_title[t] for t in s.get("depends_on_titles", [])]
        by_title[st.title] = st.id
        subtasks.append(st)
    return subtasks


def validate_decomposition(data: dict) -> None:
    """Raise ValueError unless `data` is a well-formed decomposition.

    Checks: non-empty `subtasks` list; each has a title and spec; titles are unique; every
    `depends_on_titles` entry names an earlier subtask (so list order is a valid dependency order);
    every interface function's file is one of its subtask's `files`; params are "name: type"; and no
    two pinned functions share the same (file, name).
    """
    if not isinstance(data, dict) or not isinstance(data.get("subtasks"), list) or not data["subtasks"]:
        raise ValueError("decomposition must be a JSON object with a non-empty 'subtasks' list")
    errors: list[str] = []
    seen_titles: set[str] = set()
    pinned: dict[tuple[str, str], str] = {}
    for i, s in enumerate(data["subtasks"]):
        title = s.get("title") if isinstance(s, dict) else None
        if not title or not isinstance(title, str):
            errors.append(f"subtask #{i} has no title")
            continue
        where = f"subtask #{i} {title!r}"
        if title in seen_titles:
            errors.append(f"{where}: duplicate title")
        if not s.get("spec"):
            errors.append(f"{where}: empty spec")
        for dep in s.get("depends_on_titles", []):
            if dep == title:
                errors.append(f"{where}: depends on itself")
            elif dep not in seen_titles:
                errors.append(f"{where}: depends on {dep!r}, which is not an earlier subtask")
        files = set(s.get("files", []))
        for f in s.get("interface", []):
            name, file = f.get("name"), f.get("file")
            if not name or not file:
                errors.append(f"{where}: interface entry missing name or file: {f!r}")
                continue
            if file not in files:
                errors.append(f"{where}: {name} is pinned in {file}, which is not in the subtask's files {sorted(files)}")
            for prm in f.get("params", []):
                if not isinstance(prm, str) or ":" not in prm:
                    errors.append(f"{where}: {name} param {prm!r} is not 'name: type'")
            key = (file, name)
            if key in pinned:
                errors.append(f"{where}: {file}:{name} is already pinned by {pinned[key]!r}")
            else:
                pinned[key] = title
        seen_titles.add(title)
    if errors:
        raise ValueError("invalid decomposition:\n  " + "\n  ".join(errors))


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
