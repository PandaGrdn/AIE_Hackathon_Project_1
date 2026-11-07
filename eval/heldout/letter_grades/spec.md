# Letter grades from scores

Add `grades.py` with two functions.

1. `letter_grade(score: int) -> str` maps one integer score to a letter:
   - `"A"`: at least 90
   - `"B"`: at least 80 and below 90
   - `"C"`: at least 70 and below 80
   - `"D"`: at least 60 and below 70
   - `"F"`: below 60

   Valid scores are 0 to 100 inclusive. A score below 0 or above 100 raises `ValueError`.
   Exactly 90, 80, 70 and 60 earn the higher letter (90 is an `"A"`, 59 is an `"F"`).

2. `final_score(scores: list[int]) -> int | None` gives a student's overall score from a list
   of scores:
   - The arithmetic mean of the scores, rounded to the nearest whole number, with exact
     halves rounded to the even number (round half to even): a mean of 88.5 becomes 88,
     a mean of 89.5 becomes 90, a mean of 86.5 becomes 86. Non-halves round to nearest
     (a mean of 88.67 becomes 89).
   - An empty list returns `None`. It does not raise.
   - If any score is below 0 or above 100, raise `ValueError`.
   - The course letter is `letter_grade(final_score(scores))`, but `final_score` itself
     returns the integer.
