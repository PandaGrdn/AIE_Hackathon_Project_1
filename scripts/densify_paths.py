#!/usr/bin/env python3
"""Densify per-run reasoning paths in a UI snapshot with the agent's per-turn message text.

Each run logs only 3-12 `log_step` steps; runs/<run_id>/transcript.json also holds the assistant text of every
tool-loop turn. This inserts those texts (sentence-chunked, embedded) as intermediate steps with fractional
`index` values, `action: "message"` and `kind: "message"`. Everything else in the snapshot is untouched.

    python scripts/densify_paths.py demo/mcp_snapshot.json [--in-place] [--only-failed] [--force]

Note: chunks are embedded with embed_texts (Gemini if a key is set, else fake_embed); the logged steps in the
snapshot should have been embedded the same way, otherwise embedding dims differ.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import embeddings  # noqa: E402

MIN_CHARS, MAX_CHARS, BATCH = 20, 400, 32


def chunk_text(text: str) -> list[str]:
    """Split on newlines and '. ' boundaries; merge pieces up to MAX_CHARS; drop chunks under MIN_CHARS."""
    pieces: list[str] = []
    for line in (text or "").splitlines():
        pieces += [p.strip() for p in re.split(r"(?<=[.!?])\s+", line) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        while len(p) > MAX_CHARS:  # hard-wrap pathological long sentences
            cut = p.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MIN_CHARS else MAX_CHARS
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + 1 + len(p) > MAX_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= MIN_CHARS]


def _dt(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def turn_chunks(transcript: dict, logged: list[dict]) -> list[tuple[int, str]]:
    """[(gap, chunk)] in turn order. gap g means: after logged[g-1], before logged[g] (0..len(logged)).

    Timestamps are used when every turn and logged step carries one; otherwise (the runner's transcript has none)
    the gap is the number of `log_step` calls made in earlier turns, i.e. the turn's text precedes its own log_step.
    """
    turns = transcript.get("turns") or []
    step_ts = [_dt(s.get("ts")) for s in logged]
    turn_ts = [_dt(t.get("ts")) for t in turns]
    use_ts = bool(turns) and all(step_ts) and all(turn_ts)
    out: list[tuple[int, str]] = []
    seen_log = 0
    for t, turn in enumerate(turns):
        if use_ts:
            gap = sum(1 for x in step_ts if x <= turn_ts[t])
        else:
            gap = min(seen_log, len(logged))
        seen_log += sum(1 for c in turn.get("tool_calls") or [] if c.get("name") == "log_step")
        for c in chunk_text(turn.get("text") or ""):
            out.append((gap, c))
        # Newer transcripts carry a one-line `summary` per tool call (what was written/read/run); each is an
        # action point on the path. log_step calls are skipped: they are the logged steps themselves.
        for call in turn.get("tool_calls") or []:
            summ = (call.get("summary") or "").strip()
            if summ and call.get("name") != "log_step" and len(summ) >= 20:
                out.append((gap, summ[:400]))
    return out


def _is_msg(s: dict) -> bool:
    return s.get("kind") == "message"


def densify(snapshot: dict, runs_dir: Path, only_failed: bool = False, force: bool = False,
            log=print) -> dict:
    steps = snapshot.get("steps") or []
    failed = set(snapshot.get("failed_run_ids") or [])
    run_ids = list(dict.fromkeys(s.get("run_id") for s in steps if s.get("run_id")))
    new_by_run: dict[str, list[dict]] = {}
    for rid in run_ids:
        if only_failed and rid not in failed:
            continue
        have_msgs = any(_is_msg(s) for s in steps if s.get("run_id") == rid)
        if have_msgs and not force:
            log(f"{rid}: skipped (already densified)")
            continue
        path = runs_dir / rid / "transcript.json"
        if not path.exists():
            log(f"{rid}: no transcript.json")
            continue
        try:
            transcript = json.loads(path.read_text())
        except (OSError, ValueError):
            log(f"{rid}: unreadable transcript")
            continue
        logged = sorted((s for s in steps if s.get("run_id") == rid and not _is_msg(s)),
                        key=lambda s: float(s.get("index", 0)))
        if not logged:
            log(f"{rid}: no logged steps")
            continue
        items = turn_chunks(transcript, logged)
        vecs: list = []
        texts = [c for _, c in items]
        for i in range(0, len(texts), BATCH):
            vecs += embeddings.embed_texts(texts[i:i + BATCH])
        by_gap: dict[int, list[tuple[str, list]]] = {}
        for (g, c), v in zip(items, vecs):
            by_gap.setdefault(g, []).append((c, v))
        new: list[dict] = []
        n = len(logged)
        for g, lst in by_gap.items():
            lo_s = logged[g - 1] if g > 0 else None
            hi_s = logged[g] if g < n else None
            lo = float(lo_s["index"]) if lo_s else (float(hi_s["index"]) - 1)
            hi = float(hi_s["index"]) if hi_s else (lo + 1)
            lo_t, hi_t = _dt((lo_s or {}).get("ts")), _dt((hi_s or {}).get("ts"))
            ref = lo_s or hi_s
            m = len(lst)
            for j, (c, v) in enumerate(lst):
                frac = (j + 1) / (m + 1)
                d = {"run_id": rid, "subtask_id": ref.get("subtask_id"), "agent_id": ref.get("agent_id"),
                     "index": round(lo + frac * (hi - lo), 4), "assumption": c, "action": "message",
                     "files_touched": [], "embedding": v, "live_results": None, "kind": "message"}
                if lo_t and hi_t:
                    d["ts"] = (lo_t + (hi_t - lo_t) * frac).isoformat()
                elif ref.get("ts") is not None:
                    d["ts"] = ref["ts"]
                new.append(d)
        new_by_run[rid] = new
        log(f"{rid}: logged={n} messages_added={len(new)} total={n + len(new)}")

    # rebuild the steps list: drop replaced old messages, put each message right after the logged step it follows
    out: list[dict] = []
    seen_first: set[str] = set()
    for s in steps:
        rid = s.get("run_id")
        if rid in new_by_run and _is_msg(s):
            continue
        if rid in new_by_run and rid not in seen_first:
            seen_first.add(rid)
        out.append(s)
    for rid, new in new_by_run.items():
        last = max((i for i, s in enumerate(out) if s.get("run_id") == rid), default=len(out) - 1)
        out[last + 1:last + 1] = new
        # stable order inside the run's block: sort the block by index
        idx = [i for i, s in enumerate(out) if s.get("run_id") == rid]
        block = sorted((out[i] for i in idx), key=lambda s: float(s.get("index", 0)))
        for i, s in zip(idx, block):
            out[i] = s
    result = dict(snapshot)
    result["steps"] = out
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("snapshot")
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--runs-dir", default="runs/")
    ap.add_argument("--only-failed", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    src = Path(a.snapshot)
    res = densify(json.loads(src.read_text()), Path(a.runs_dir), a.only_failed, a.force)
    dst = src if a.in_place else src.with_suffix(".dense.json")
    dst.write_text(json.dumps(res, indent=1))
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
