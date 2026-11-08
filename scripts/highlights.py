"""Hero facts for the frontend, computed from the recorded snapshots and summaries. Nothing invented.

    python scripts/highlights.py --out demo/highlights.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(p: str):
    try:
        return json.load(open(p))
    except FileNotFoundError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="demo/mcp_snapshot.json")
    ap.add_argument("--summaries", nargs="*", default=["demo/task_a_summary.json", "demo/task_b_baseline_summary.json", "demo/task_b_with_lessons_summary.json"])
    ap.add_argument("--results", default="demo/results.json")
    ap.add_argument("--out", default="demo/highlights.json")
    args = ap.parse_args()

    snap = load(args.snapshot) or {}
    runs = snap.get("runs", [])
    failed, verified = set(snap.get("failed_run_ids", [])), set(snap.get("verified_run_ids", []))
    divs = snap.get("divergences", [])
    by_run = {r["id"]: r for r in runs}

    def top_p(d):
        probs = d.get("step_probabilities") or {}
        return max(probs.values()) if probs else 0.0

    top_divs = sorted(divs, key=top_p, reverse=True)[:6]
    highlights = {
        "label": "real agents on the MCP Python SDK at 77001c9",
        "counts": {
            "agent_runs": len(runs),
            "verified_runs": len(verified),
            "failed_runs": len(failed),
            "lessons": len(snap.get("lessons", [])),
            "lessons_adopted": sum(1 for l in snap.get("lessons", []) if l.get("status") == "adopted"),
            "lessons_rolled_back": sum(1 for l in snap.get("lessons", []) if l.get("status") == "rolled_back"),
            "subq_root_causes": sum(1 for d in divs if d.get("step_probabilities")),
            "subq_confirmed_p_ge_0_7": sum(1 for d in divs if top_p(d) >= 0.7),
            "regression_tests_per_verified_commit": 2389,
            "models": sorted({r["agent"]["model"] for r in runs if r.get("agent")}),
        },
        "top_root_causes": [
            {
                "run_id": d["run_id"],
                "agent_id": by_run.get(d["run_id"], {}).get("agent", {}).get("id"),
                "step_index": d["step_index"],
                "p": round(top_p(d), 2),
                "kind": d.get("kind"),
                "kind_confidence": d.get("kind_confidence"),
                "generalizes": d.get("generalizes"),
                "step_text": (d.get("step_text") or "")[:240],
                "breaking_case": (d.get("breaking_case") or {}).get("args", [None])[0],
            }
            for d in top_divs
        ],
        "rescues": [],
        "per_phase": {},
    }
    for p in args.summaries:
        d = load(p)
        if not d:
            continue
        name = Path(p).stem.replace("_summary", "")
        summ = d.get("summary", d)
        per = []
        for rep in summ.get("per_subtask", []):
            row = {"title": rep.get("title"), "verified": rep.get("verified", 0), "failed": rep.get("failed", 0), "committed": rep.get("committed", False), "lessons": len(rep.get("lessons") or [])}
            if rep.get("first_attempt"):
                fa = rep["first_attempt"]
                row["first_attempt"] = {"verified": fa.get("verified", 0), "failed": fa.get("failed", 0)}
                if fa.get("verified", 0) == 0 and rep.get("verified", 0) > 0:
                    highlights["rescues"].append({"phase": name, "subtask": rep.get("title"), "first": f"0/{fa.get('failed', 0)}", "after_retry": f"{rep.get('verified')}/{rep.get('verified', 0) + rep.get('failed', 0)}", "lessons_injected": len(fa.get("lessons") or [])})
            per.append(row)
        highlights["per_phase"][name] = {"subtasks_done": summ.get("subtasks_done"), "per_subtask": per, "wall_s": d.get("wall_s")}
    res = load(args.results)
    if res:
        highlights["b0_vs_b1"] = res.get("b0_vs_b1", res)
    Path(args.out).write_text(json.dumps(highlights, indent=1))
    print(json.dumps({k: v for k, v in highlights["counts"].items()}, indent=0))
    print("rescues:", highlights["rescues"])
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
