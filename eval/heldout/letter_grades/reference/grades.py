"""Reference: letter grades from scores."""

from __future__ import annotations


def _check(score: int) -> None:
    if score < 0 or score > 100:
        raise ValueError("score must be between 0 and 100")


def letter_grade(score: int) -> str:
    _check(score)
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def final_score(scores: list[int]) -> int | None:
    for s in scores:
        _check(s)
    if not scores:
        return None
    return round(sum(scores) / len(scores))  # built-in round is half to even
