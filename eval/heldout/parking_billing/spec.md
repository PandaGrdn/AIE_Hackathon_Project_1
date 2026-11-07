# Parking duration billing with a grace period

Add `parking.py` with one function:

`parking_charge(minutes: int) -> float` returns the charge in USD for one parking stay of
the given whole number of minutes.

Rules:

- Grace period: a stay of 15 minutes or less is free (0.0). Exactly 15 minutes is free;
  16 minutes is charged.
- A stay of more than 15 minutes is charged 2.50 for every started hour, counted from the
  start of the stay (the grace minutes are not deducted). 16 to 60 minutes is one hour
  (2.50); 61 minutes is two hours (5.00).
- Daily cap: the charge never exceeds 18.00. Seven hours (420 minutes) costs 17.50;
  421 minutes would be 20.00 uncapped and is charged 18.00.
- The maximum stay is 24 hours: exactly 1440 minutes is allowed (18.00); more than 1440
  minutes raises `ValueError`.
- Negative minutes raise `ValueError`. Zero minutes is valid and free.
