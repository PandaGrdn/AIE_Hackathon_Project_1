"""Reference: shipping fee bands by weight."""

from __future__ import annotations


def shipping_fee(weight_kg: float) -> float:
    if weight_kg < 0:
        raise ValueError("weight must not be negative")
    if weight_kg > 1000:
        raise ValueError("weight must not exceed 1000 kg")
    if weight_kg <= 1:
        fee = 5.0
    elif weight_kg <= 5:
        fee = 9.0
    elif weight_kg < 20:
        fee = 15.0
    else:
        fee = 20.0 + 0.25 * (weight_kg - 20)
    return round(fee, 2)
