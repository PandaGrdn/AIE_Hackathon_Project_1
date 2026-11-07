import copy
import json
from pathlib import Path

import pytest

from core.decompose import decomposer
from core.decompose.decomposer import decompose, load_task, subtasks_from_json
from core.decompose.interfaces import extract_interface

ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = ROOT / "demo_task" / "pricing"
TASK_FILE = TASK_DIR / "task.md"
SUBTASKS_FILE = TASK_DIR / "subtasks.json"

EXPECTED = [
    ("tier_for", "pricing/tiers.py", ["amount: float"], "str"),
    ("discount_rate", "pricing/discounts.py", ["tier: str", "units: int"], "float"),
    ("quote", "pricing/quote.py", ["amount: float", "units: int"], "dict"),
    ("summarize", "pricing/summary.py", ["quotes: list[dict]"], "dict"),
]


@pytest.fixture
def offline(monkeypatch):
    def no_llm(*a, **k):
        raise AssertionError("decompose must not construct an LLM client when subtasks.json exists")

    monkeypatch.setattr(decomposer, "get_client", no_llm)


def _raw() -> dict:
    return json.loads(SUBTASKS_FILE.read_text())


def test_demo_task_decomposes_offline(offline):
    task = decompose(load_task(TASK_FILE))
    assert len(task.subtasks) == 4
    assert [s.index for s in task.subtasks] == [0, 1, 2, 3]
    got = [(f.name, f.file, f.params, f.returns) for s in task.subtasks for f in s.interface]
    assert got == EXPECTED
    for s in task.subtasks:
        assert {f.file for f in s.interface} <= set(s.files)


def test_dependency_order(offline):
    subs = decompose(load_task(TASK_FILE)).subtasks
    ids = [s.id for s in subs]
    assert subs[0].depends_on == []
    assert subs[1].depends_on == [ids[0]]
    assert subs[2].depends_on == [ids[0], ids[1]]
    assert subs[3].depends_on == [ids[2]]
    for i, s in enumerate(subs):
        assert all(ids.index(d) < i for d in s.depends_on)


def test_specs_state_boundaries(offline):
    specs = [s.spec for s in decompose(load_task(TASK_FILE)).subtasks]
    tiers, discounts, quote, summary = specs
    assert "100 <= amount <= 1000" in tiers and "amount > 1000" in tiers and "ValueError" in tiers
    assert "units >= 50" in discounts and "ValueError" in discounts
    assert "round half to even" in quote and "round half to even" in summary
    assert '"grand_total": 0.0' in summary


def test_pinned_interfaces_match_ast_extraction(tmp_path):
    # A stub written to the pinned signatures must extract to the very same FunctionSigs.
    subs = subtasks_from_json(_raw())
    for s in subs:
        for f in s.interface:
            path = tmp_path / f.file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"def {f.name}({', '.join(f.params)}) -> {f.returns}:\n    ...\n")
    pinned = [f for s in subs for f in s.interface]
    assert extract_interface(tmp_path, [f.file for f in pinned]) == pinned


def test_falls_back_to_llm_without_precomputed(monkeypatch, tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "task.md").write_text("# T\n\nrepo: repo\n")

    class Fake:
        def chat(self, messages, tools=None):
            return {"text": "```json\n" + json.dumps(_raw()) + "\n```", "tool_calls": []}

    monkeypatch.setattr(decomposer, "get_client", lambda identity: Fake())
    assert len(decompose(load_task(tmp_path / "task.md")).subtasks) == 4


def test_pricing_task_paths():
    task = load_task(TASK_FILE)
    assert Path(task.repo_path) == TASK_DIR / "repo"
    assert decomposer.precomputed_path(task) == SUBTASKS_FILE


@pytest.mark.parametrize("name", ["pricing", "mcp"])
def test_precomputed_found_next_to_task_file(offline, tmp_path, name):
    # demo_task/<name>/{task.md, subtasks.json, repo/}; the mcp files may not exist yet, so use a copy
    d = tmp_path / "demo_task" / name
    (d / "repo").mkdir(parents=True)
    (d / "task.md").write_text("# Tiered pricing module\n\nrepo: repo\n")
    (d / "subtasks.json").write_text(SUBTASKS_FILE.read_text())
    task = load_task(d / "task.md")
    assert Path(task.repo_path) == (d / "repo").resolve()
    assert decomposer.precomputed_path(task) == (d / "subtasks.json").resolve()
    assert len(decompose(task).subtasks) == 4


def test_precomputed_follows_task_file_not_repo(offline, tmp_path):
    # `repo:` may point outside the task's folder; subtasks.json is still the one next to task.md
    (tmp_path / "elsewhere" / "repo").mkdir(parents=True)
    d = tmp_path / "task"
    d.mkdir()
    (d / "task.md").write_text("# Tiered pricing module\n\nrepo: ../elsewhere/repo\n")
    (d / "subtasks.json").write_text(SUBTASKS_FILE.read_text())
    task = load_task(d / "task.md")
    assert Path(task.repo_path) == (tmp_path / "elsewhere" / "repo").resolve()
    assert decomposer.precomputed_path(task) == (d / "subtasks.json").resolve()
    assert len(decompose(task).subtasks) == 4


def test_precomputed_for_other_task_is_rejected(offline):
    task = load_task(TASK_FILE)
    task.title = "Something else"
    with pytest.raises(ValueError, match="decomposes task"):
        decompose(task)


def test_rejects_duplicate_titles():
    data = _raw()
    data["subtasks"][1]["title"] = data["subtasks"][0]["title"]
    with pytest.raises(ValueError, match="duplicate title"):
        subtasks_from_json(data)


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d["subtasks"][1].update(depends_on_titles=["Nope"]), "not an earlier subtask"),
        (lambda d: d["subtasks"][0].update(depends_on_titles=["Discount rates"]), "not an earlier subtask"),
        (lambda d: d["subtasks"][0]["interface"][0].update(file="pricing/other.py"), "not in the subtask's files"),
        (
            lambda d: d["subtasks"][1]["interface"].append(copy.deepcopy(d["subtasks"][0]["interface"][0]))
            or d["subtasks"][1]["files"].append("pricing/tiers.py"),
            "already pinned",
        ),
        (lambda d: d.update(subtasks=[]), "non-empty"),
    ],
)
def test_rejects_invalid(mutate, msg):
    data = _raw()
    mutate(data)
    with pytest.raises(ValueError, match=msg):
        subtasks_from_json(data)
