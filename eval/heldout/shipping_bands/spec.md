# Shipping fee bands by parcel weight

Add `shipping.py` with one function:

`shipping_fee(weight_kg: float) -> float` returns the shipping fee in USD for one parcel of
the given weight in kilograms.

Bands (read the boundaries carefully):

| Weight | Fee |
| --- | --- |
| from 0 kg up to and including 1 kg | 5.00 |
| more than 1 kg, up to and including 5 kg | 9.00 |
| more than 5 kg but less than 20 kg | 15.00 |
| 20 kg or more, up to and including 1000 kg | 20.00 plus 0.25 for every kg above 20 kg (fractions of a kg count proportionally) |

Constraints:

- A weight of exactly 1 kg is in the first band; exactly 5 kg is in the second band; exactly
  20 kg is in the last band (fee 20.00).
- The result is rounded to 2 decimal places, with exact halves rounded to the even digit
  (round half to even). Example: 20.5 kg costs 20.125 before rounding, which becomes 20.12.
- A weight of zero is valid (fee 5.00).
- Negative weights raise `ValueError`.
- Weights above 1000 kg raise `ValueError`; exactly 1000 kg is allowed.
