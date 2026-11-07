"""Reference: price quote (demo_task/pricing/subtasks.json, "Price quote")."""

from pricing.discounts import discount_rate
from pricing.tiers import tier_for


def quote(amount: float, units: int) -> dict:
    """Tier from the unit price, rate from tier and units, subtotal and total rounded half to even."""
    tier = tier_for(amount)
    rate = discount_rate(tier, units)
    subtotal = amount * units
    total = subtotal * (1 - rate)
    return {"tier": tier, "rate": rate, "subtotal": float(round(subtotal, 2)), "total": float(round(total, 2))}
