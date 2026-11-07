"""Change proposer: context/skill proposals as before, plus ChangeKind.tool when >= 3 adopted lessons cluster."""

from core.changes.proposer import TOOL_AFTER_CLUSTER, cluster_lessons, propose_changes, tool_ref_id, write_tool_stub
from core.models import ChangeKind, ChangeStatus, Lesson
from core.store.memory import MemoryStore

A = [1.0, 0.0, 0.0]
A2 = [0.98, 0.15, 0.0]
A3 = [0.97, 0.0, 0.2]
B = [0.0, 1.0, 0.0]


def _adopted(text, div, store=None, hits=1):
    l = Lesson(text=text, embedding=div, divergence_embedding=div, status=ChangeStatus.adopted, hit_count=hits)
    if store is not None:
        store.upsert_lesson(l)
    return l


def test_context_and_skill_proposals_unchanged():
    store = MemoryStore()
    fresh = Lesson(text="new", embedding=B, divergence_embedding=B)
    promoted = _adopted("seen a lot", A, store, hits=3)
    changes = propose_changes([fresh, promoted], store)
    kinds = {c.kind for c in changes}
    assert ChangeKind.context in kinds and ChangeKind.skill in kinds
    assert ChangeKind.tool not in kinds  # one adopted lesson is no cluster


def test_cluster_lessons_is_greedy_on_cosine():
    ls = [_adopted("a", A), _adopted("a2", A2), _adopted("b", B), _adopted("a3", A3)]
    clusters = cluster_lessons(ls, threshold=0.9)
    assert sorted(sorted(l.text for l in c) for c in clusters) == [["a", "a2", "a3"], ["b"]]


def test_three_adopted_lessons_in_one_region_propose_a_tool_naming_them():
    store = MemoryStore()
    a, a2, a3 = (_adopted(t, v, store) for t, v in [("over means strictly greater", A), ("test the boundary", A2), ("round after summing", A3)])
    _adopted("unrelated", B, store)
    changes = propose_changes([], store)
    tools = [c for c in changes if c.kind == ChangeKind.tool]
    assert len(tools) == 1
    t = tools[0]
    assert t.status == ChangeStatus.candidate
    assert all(l.text in t.description for l in (a, a2, a3))
    assert "unrelated" not in t.description
    assert t.ref_id == tool_ref_id([a, a2, a3]) == tool_ref_id([a3, a, a2])  # order-independent, stable


def test_tool_is_not_proposed_twice_for_the_same_cluster():
    store = MemoryStore()
    ls = [_adopted(t, v, store) for t, v in [("x", A), ("y", A2), ("z", A3)]]
    first = propose_changes([], store)
    assert sum(c.kind == ChangeKind.tool for c in first) == 1
    store.save_change(first[0])
    again = propose_changes([], store)
    assert not any(c.kind == ChangeKind.tool for c in again)
    assert TOOL_AFTER_CLUSTER == 3 and len(ls) == 3


def test_candidate_lessons_do_not_count_toward_a_tool():
    store = MemoryStore()
    for t, v in [("x", A), ("y", A2)]:
        _adopted(t, v, store)
    store.upsert_lesson(Lesson(text="z", embedding=A3, divergence_embedding=A3, status=ChangeStatus.candidate))
    assert not any(c.kind == ChangeKind.tool for c in propose_changes([], store))


def test_write_tool_stub_returns_python_source_without_an_llm():
    ls = [_adopted(t, v) for t, v in [("x", A), ("y", A2), ("z", A3)]]
    src = write_tool_stub(ls)
    assert src.startswith("def ") or "\ndef " in src
    assert all(l.text in src for l in ls)
    compile(src, "<tool>", "exec")
