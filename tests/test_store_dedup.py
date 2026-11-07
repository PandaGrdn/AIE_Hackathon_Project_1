"""dedup_or_insert: confirmed lessons create a failure region now; unconfirmed ones wait for a second hit."""

from core.models import Lesson
from core.reflect.dedup import UNCONFIRMED_MAX_CONFIDENCE, dedup_or_insert
from core.store.memory import MemoryStore

VEC = [1.0, 0.0, 0.0]


def test_default_is_confirmed_and_keeps_todays_behaviour():
    store = MemoryStore()
    l = dedup_or_insert(Lesson(text="t", embedding=VEC, divergence_embedding=VEC, confidence=0.8), store)
    assert l.confidence == 0.8
    assert store.regions_near(VEC)


def test_unconfirmed_lesson_is_capped_and_has_no_region():
    store = MemoryStore()
    l = dedup_or_insert(Lesson(text="t", embedding=VEC, divergence_embedding=VEC, confidence=0.9), store, confirmed=False)
    assert l.confidence == UNCONFIRMED_MAX_CONFIDENCE == 0.3
    assert store.get_lesson(l.id) is not None
    assert store.regions_near(VEC) == []


def test_second_hit_on_unconfirmed_lesson_creates_the_region():
    store = MemoryStore()
    l1 = dedup_or_insert(Lesson(text="t", embedding=VEC, divergence_embedding=VEC, confidence=0.9), store, confirmed=False)
    l2 = dedup_or_insert(Lesson(text="t again", embedding=VEC, divergence_embedding=VEC, confidence=0.6), store, confirmed=False)
    assert l1.id == l2.id and l2.hit_count == 2
    regions = store.regions_near(VEC)
    assert len(regions) == 1 and regions[0][0].lesson_id == l1.id


def test_confirmed_hit_on_existing_region_widens_it():
    store = MemoryStore()
    dedup_or_insert(Lesson(text="t", embedding=VEC, divergence_embedding=VEC), store)
    r0 = store.regions_near(VEC)[0][0].radius
    dedup_or_insert(Lesson(text="t again", embedding=VEC, divergence_embedding=[0.9, 0.1, 0.0]), store)
    r1 = store.regions_near(VEC)[0][0]
    assert r1.radius > r0 and r1.weight == 2
