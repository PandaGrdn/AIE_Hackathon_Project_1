"""Trust leaderboard for the UI: agents sorted by Laplace-smoothed score with counts and recent history."""

from core.changes.trust import HISTORY_POINTS, leaderboard
from core.models import TrustScore
from core.store.memory import MemoryStore


def _trust(store, agent_id, agree, disagree, history):
    store.save_trust(TrustScore(agent_id=agent_id, agreements=agree, disagreements=disagree, history=history))


def test_leaderboard_sorted_by_score_with_counts_and_last_history_points():
    store = MemoryStore()
    _trust(store, "weak", 1, 3, [0.5, 0.33, 0.25, 0.33])
    _trust(store, "strong", 5, 0, [0.5] + [round(i / 10, 2) for i in range(6, 20)])
    rows = leaderboard(store, ["weak", "strong"])
    assert [r["agent_id"] for r in rows] == ["strong", "weak"]
    strong, weak = rows
    assert strong["agreements"] == 5 and strong["disagreements"] == 0
    assert abs(strong["score"] - 6 / 7) < 1e-9
    assert len(strong["history"]) == HISTORY_POINTS == 10
    assert strong["history"][-1] == 1.9 and strong["history"][0] == 1.0
    assert weak["history"] == [0.5, 0.33, 0.25, 0.33]


def test_leaderboard_includes_unseen_agents_at_the_prior_score():
    store = MemoryStore()
    _trust(store, "a", 2, 0, [0.67, 0.75])
    rows = leaderboard(store, ["a", "never-ran"])
    assert [r["agent_id"] for r in rows] == ["a", "never-ran"]
    assert rows[1] == {"agent_id": "never-ran", "score": 0.5, "agreements": 0, "disagreements": 0, "history": []}


def test_leaderboard_ties_break_by_agent_id_and_result_is_json_plain():
    store = MemoryStore()
    rows = leaderboard(store, ["b", "a"])
    assert [r["agent_id"] for r in rows] == ["a", "b"]
    assert all(isinstance(r["score"], float) for r in rows)
