"""Retry-rescue statistic from the recorded summaries.

For every subtask whose first attempt had zero verified runs, the loop reflected the failures into lessons
and retried once with them injected. This counts how often that retry produced a verified run.

    python scripts/rescue_stats.py demo/task_a_summary.json demo/task_b_baseline_summary.json demo/task_b_with_lessons_summary.json
"""

from __future__ import annotations

import json
import sys


def main(paths: list[str]) -> int:
    rows = []
    for p in paths:
        try:
            d = json.load(open(p))
        except FileNotFoundError:
            continue
        summ = d.get("summary", d)
        for rep in summ.get("per_subtask", []):
            if rep.get("retried") or rep.get("first_attempt"):
                first = rep.get("first_attempt") or {}
                rows.append((p.split("/")[-1].replace("_summary.json", ""), rep.get("title", "?")[:40],
                             first.get("verified", 0), first.get("failed", 0), rep.get("verified", 0), rep.get("failed", 0),
                             len(first.get("lessons") or [])))
    if not rows:
        print("no retried subtasks found")
        return 0
    print(f"{'phase':16} {'subtask':40} {'first':>7} {'retry':>7} {'lessons injected':>16}")
    rescued = 0
    runs_first = runs_retry = ver_first = ver_retry = 0
    for phase, title, v1, f1, v2, f2, nl in rows:
        print(f"{phase:16} {title:40} {v1}/{v1 + f1:<5} {v2}/{v2 + f2:<5} {nl:>16}")
        rescued += int(v1 == 0 and v2 > 0)
        runs_first += v1 + f1
        runs_retry += v2 + f2
        ver_first += v1
        ver_retry += v2
    print(f"\nall-fail subtasks: {len(rows)}; rescued by the retry with lessons: {rescued}")
    print(f"verified share before retry: {ver_first}/{runs_first} = {ver_first / max(runs_first, 1):.0%}; after: {ver_retry}/{runs_retry} = {ver_retry / max(runs_retry, 1):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["demo/task_a_summary.json", "demo/task_b_baseline_summary.json", "demo/task_b_with_lessons_summary.json"]))
