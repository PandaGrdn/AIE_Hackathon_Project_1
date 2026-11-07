from core.changes.gate import evaluate_change
from core.models import Change, ChangeKind, ChangeStatus, Lesson, TrustScore
from core.reflect.dedup import dedup_or_insert
from core.store.memory import MemoryStore


def test_trust_score_laplace():
    t = TrustScore(agent_id="a")
    assert t.score == 0.5
    t.agreements = 3
    assert abs(t.score - 4 / 5) < 1e-9


def test_dedup_bumps_hit_count():
    store = MemoryStore()
    vec = [1.0, 0.0, 0.0]
    l1 = dedup_or_insert(Lesson(text="boundary", embedding=vec, divergence_embedding=vec), store)
    l2 = dedup_or_insert(Lesson(text="boundary again", embedding=vec, divergence_embedding=vec), store)
    assert l1.id == l2.id and l2.hit_count == 2
    assert len(store.list_lessons()) == 1
    assert store.regions_near(vec)


def test_gate_adopts_on_improvement_and_rolls_back_otherwise():
    store = MemoryStore()
    lesson = Lesson(text="x", embedding=[1.0, 0.0])
    store.upsert_lesson(lesson)

    def better(ids):
        return {"accuracy": 0.9 if lesson.id in ids else 0.6, "repeat_mistake_rate": 0.1, "false_block_rate": 0.0}

    ch = evaluate_change(Change(kind=ChangeKind.context, ref_id=lesson.id, description="l"), store, better)
    assert ch.status == ChangeStatus.adopted
    assert store.get_lesson(lesson.id).status == ChangeStatus.adopted

    lesson2 = Lesson(text="y", embedding=[0.0, 1.0])
    store.upsert_lesson(lesson2)

    def same(ids):
        return {"accuracy": 0.6, "repeat_mistake_rate": 0.1, "false_block_rate": 0.0}

    ch2 = evaluate_change(Change(kind=ChangeKind.context, ref_id=lesson2.id, description="l2"), store, same)
    assert ch2.status == ChangeStatus.rolled_back
    assert len(store.list_changes()) == 2
