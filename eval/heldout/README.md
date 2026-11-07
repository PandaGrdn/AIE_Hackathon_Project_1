# Held-out subtasks

One folder per subtask, never used to produce a lesson:

```
heldout/
  shipping_bands/
    spec.md            # the subtask spec (goals, constraints, explicit boundaries)
    interface.json     # [{"name": ..., "file": ..., "params": ["x: float"], "returns": "float"}]
    reference/         # a correct implementation at the pinned interface
      shipping.py      # path = reference/<FunctionSig.file>
```

The set exercises the same bug-prone patterns as the long task (`demo_task/task.md`,
tiered pricing) on unrelated domains, so a lesson that fixes a reasoning error (not a
pricing fact) should transfer here. Specs state the boundaries but do not point out which
ones are traps; that list lives only in this README.

| Folder | Functions | Patterns targeted |
| --- | --- | --- |
| `shipping_bands` | `shipping_fee(weight_kg: float) -> float` | inclusive upper bounds (1 kg, 5 kg) vs exclusive "less than 20" next to inclusive "20 or more"; round half to even at 2 dp (20.5 kg -> 20.125 -> 20.12); zero valid; negative and > 1000 kg raise `ValueError` |
| `letter_grades` | `letter_grade(score: int) -> str`, `final_score(scores: list[int]) -> int \| None` | "at least" thresholds at 90/80/70/60; round half to even on the mean (88.5 -> 88, 89.5 -> 90); empty list returns `None` (no raise); out-of-range and negative scores raise `ValueError` |
| `parking_billing` | `parking_charge(minutes: int) -> float` | inclusive grace period (15 min free, 16 charged); "more than 15"; ceiling on started hours at exact multiples (60 = 1 h, 61 = 2 h); inclusive cap (18.00 from 421 min); 1440 allowed, 1441 and negative raise `ValueError` |
| `library_fines` | `late_fine(days_late: int, item_type: str) -> float`, `top_fined(fines: dict[str, float], n: int) -> list[str]` | cap as inclusive ceiling; float rounding of 0.10/day (3 days -> 0.3); exact string match of item types; ordering/aggregation: descending by fine with ties by name, "more than 0" excludes zero, `n = 0` and empty dict return `[]`, negative `n` or fine raises `ValueError` |

`tests/test_heldout_refs.py` checks every reference against its interface and the exact
boundary values from its spec.
