"""Reference: quote summary (demo_task/pricing/subtasks.json, "Quote summary")."""

import math

TIERS = ("basic", "plus", "pro")


def summarize(quotes: list[dict]) -> dict:
    """Sum of quote totals per tier and overall, each rounded half to even; empty input gives zeros."""
    by_tier: dict[str, list[float]] = {t: [] for t in TIERS}
    for q in quotes:
        tier = q["tier"]
        if tier not in by_tier:
            raise ValueError(f"unknown tier: {tier!r}")
        by_tier[tier].append(q["total"])
    out = {t: float(round(math.fsum(v), 2)) for t, v in by_tier.items()}
    out["grand_total"] = float(round(math.fsum(x for v in by_tier.values() for x in v), 2))
    return out
