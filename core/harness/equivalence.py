"""When do two outcomes count as 'the same'? Owner: C.

Rules (outline §5.5): float tolerance, unordered collections as multisets,
same exception type counts as agreement, timeouts never agree with anything.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from core.models import Outcome

REL_TOL = 1e-9
ABS_TOL = 1e-9


def _norm(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 9) if math.isfinite(v) else repr(v)
    if isinstance(v, (set, frozenset)):
        return ("set", tuple(sorted(map(repr, v))))
    if isinstance(v, dict):
        return ("dict", tuple(sorted((repr(k), _norm(x)) for k, x in v.items())))
    if isinstance(v, (list, tuple)):
        return (type(v).__name__, tuple(_norm(x) for x in v))
    return v


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL) or (math.isnan(a) and math.isnan(b))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b):
        return all(values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        return all(values_equal(a[k], b[k]) for k in a)
    if isinstance(a, (set, frozenset)) and isinstance(b, (set, frozenset)):
        return Counter(map(repr, a)) == Counter(map(repr, b))
    return a == b


def equivalent(a: Outcome, b: Outcome) -> bool:
    if a.timed_out or b.timed_out:
        return False
    if a.ok != b.ok:
        return False
    if not a.ok:
        return a.exception == b.exception
    return values_equal(a.value, b.value)


def outcome_key(o: Outcome) -> Any:
    """Hashable group key consistent with `equivalent` for the common cases."""
    if o.timed_out:
        return ("timeout", id(o))
    if not o.ok:
        return ("exc", o.exception)
    return ("val", _norm(o.value))
