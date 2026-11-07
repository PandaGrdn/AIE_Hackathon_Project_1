# Held-out subtasks

One folder per subtask, never used to produce a lesson:

```
heldout/
  boundary_fee/
    spec.md            # the subtask spec
    interface.json     # [{"name": ..., "file": ..., "params": ["x: float"], "returns": "float"}]
    reference/         # a correct implementation at the pinned interface
      fees.py
```

Target 3–5 subtasks that exercise the same bug-prone patterns as the long task
(boundaries, inclusive vs exclusive, rounding, empty inputs) on different domains.
