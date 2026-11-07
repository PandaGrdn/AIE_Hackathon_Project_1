# Tiered pricing module

repo: repo

Add a tiered-pricing module to the `pricing` package. Four coupled pieces:

1. `pricing/tiers.py` — `tier_for(amount: float) -> str`: returns `"basic"` for amounts under 100, `"plus"` for amounts from 100 up to and including 1000, and `"pro"` for amounts over 1000. Amounts are USD. Negative amounts raise `ValueError`.
2. `pricing/discounts.py` — `discount_rate(tier: str, units: int) -> float`: basic 0%, plus 5%, pro 10%; any tier gets an extra 2% when `units` is at least 50. Unknown tier raises `ValueError`.
3. `pricing/quote.py` — `quote(amount: float, units: int) -> dict`: returns `{"tier": str, "rate": float, "subtotal": float, "total": float}` where `subtotal = amount * units` and `total = subtotal * (1 - rate)`, rounded to 2 decimals (round half to even).
4. `pricing/summary.py` — `summarize(quotes: list[dict]) -> dict`: totals by tier: `{"basic": float, "plus": float, "pro": float, "grand_total": float}`. An empty list returns zeros.

Deliberately bug-prone spots: the boundary at exactly 100 and 1000, "at least 50" vs "over 50", rounding mode, and the empty-list case. Each subtask depends on the previous one's interface.
