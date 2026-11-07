# Late-return library fines with caps

Add `fines.py` with two functions.

1. `late_fine(days_late: int, item_type: str) -> float` returns the fine in USD for one item.
   - Daily rate and cap per item type (item types are exact, lowercase strings):

     | item_type | per day late | cap |
     | --- | --- | --- |
     | `"book"` | 0.10 | 5.00 |
     | `"dvd"` | 1.00 | 10.00 |
     | `"laptop"` | 5.00 | 50.00 |

   - Fine = `days_late` times the daily rate, but never more than the cap. Reaching the cap
     exactly is charged the cap (a book 50 days late owes 5.00; 51 days late still owes 5.00).
   - The result is rounded to 2 decimal places (round half to even), so a book 3 days late
     owes exactly 0.3.
   - `days_late` of 0 means returned on time: fine 0.0.
   - Negative `days_late` raises `ValueError`.
   - Any other `item_type` (including `"Book"`, `""`, `" book"`) raises `ValueError`.

2. `top_fined(fines: dict[str, float], n: int) -> list[str]` returns the names of the patrons
   with the largest outstanding fines.
   - `fines` maps patron name to total fine owed.
   - Only patrons who owe more than 0 are included; a patron owing exactly 0 is left out.
   - Order: largest fine first. Patrons with equal fines are ordered by name, ascending
     (plain string comparison).
   - Return at most `n` names. `n` of 0 returns `[]`; `n` larger than the number of eligible
     patrons returns all of them.
   - An empty dict returns `[]`.
   - Negative `n` raises `ValueError`. Any negative fine raises `ValueError`.
