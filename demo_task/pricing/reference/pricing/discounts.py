"""Reference: discount rates (demo_task/pricing/subtasks.json, "Discount rates")."""

BASE_RATE = {"basic": 0.0, "plus": 0.05, "pro": 0.10}
# base + 0.02, written as literals so the result compares equal to the decimal values
BONUS_RATE = {"basic": 0.02, "plus": 0.07, "pro": 0.12}
BONUS_UNITS = 50


def discount_rate(tier: str, units: int) -> float:
    """Base rate by tier, plus an additive 2 points when units is at least 50."""
    if not isinstance(tier, str) or tier not in BASE_RATE:
        raise ValueError(f"unknown tier: {tier!r}")
    if units < 0:
        raise ValueError(f"negative units: {units!r}")
    if units >= BONUS_UNITS:
        return BONUS_RATE[tier]
    return BASE_RATE[tier]
