"""Reference: tier classification (demo_task/pricing/subtasks.json, "Tier classification")."""


def tier_for(amount: float) -> str:
    """'basic' under 100, 'plus' from 100 up to and including 1000, 'pro' over 1000."""
    if amount < 0:
        raise ValueError(f"negative amount: {amount!r}")
    if amount < 100:
        return "basic"
    if amount <= 1000:
        return "plus"
    return "pro"
