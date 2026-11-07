"""Reference: late-return library fines with caps."""

from __future__ import annotations

_RATES: dict[str, tuple[float, float]] = {
    "book": (0.10, 5.00),
    "dvd": (1.00, 10.00),
    "laptop": (5.00, 50.00),
}


def late_fine(days_late: int, item_type: str) -> float:
    if days_late < 0:
        raise ValueError("days_late must not be negative")
    if item_type not in _RATES:
        raise ValueError(f"unknown item type: {item_type!r}")
    rate, cap = _RATES[item_type]
    return round(min(days_late * rate, cap), 2)


def top_fined(fines: dict[str, float], n: int) -> list[str]:
    if n < 0:
        raise ValueError("n must not be negative")
    if any(v < 0 for v in fines.values()):
        raise ValueError("fines must not be negative")
    owing = [(name, v) for name, v in fines.items() if v > 0]
    owing.sort(key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in owing[:n]]
