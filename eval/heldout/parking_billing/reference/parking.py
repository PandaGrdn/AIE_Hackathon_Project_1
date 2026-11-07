"""Reference: parking duration billing with a grace period."""

from __future__ import annotations

GRACE_MINUTES = 15
HOURLY_RATE = 2.5
DAILY_CAP = 18.0
MAX_MINUTES = 24 * 60


def parking_charge(minutes: int) -> float:
    if minutes < 0:
        raise ValueError("minutes must not be negative")
    if minutes > MAX_MINUTES:
        raise ValueError("stay must not exceed 24 hours")
    if minutes <= GRACE_MINUTES:
        return 0.0
    started_hours = -(-minutes // 60)  # ceiling division
    return min(started_hours * HOURLY_RATE, DAILY_CAP)
