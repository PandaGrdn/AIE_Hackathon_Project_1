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

## MCP slices

Three held-out feature slices on the same repo and commit as the MCP demo task
(`demo_task/mcp/CONTRACT.md`, "Held-out feature slices"). Each slice is independent of the
others and of the demo feature: its patch applies on its own to bare `77001c9`. They use a
repo-patch format instead of `reference/`:

```
heldout/
  mcp_tool_timeout/
    spec.md            # self-contained subtask spec (API, behaviours, exact messages, ValueError cases)
    interface.json     # FunctionSig list; methods as "ClassName.method", repo-relative files
    base_commit        # 77001c9
    reference.patch    # one `git format-patch` commit implementing the slice (no tests)
    hidden_tests/
      test_tool_timeout.py   # acceptance tests: pass on base+patch, fail on bare base
    README.md
```

| Folder | Adds | Patterns targeted |
| --- | --- | --- |
| `mcp_tool_timeout` | `timeout_seconds` on `Tool` / `add_tool` / `@tool`; `ToolManager.call_tool` uses `anyio.fail_after` | `<= 0` raises `ValueError` at every registration entry point; exact `:g` message (`0.0123456789` -> `0.0123457s`) with no `Error executing tool` prefix; `None` = unlimited, tiny positive values valid; a tool's own `TimeoutError` is not a timeout |
| `mcp_tool_deprecation` | `deprecated: bool \| str` on `Tool` / `add_tool` / `@tool`; `deprecation_message(name, deprecated)` | `_meta` only when deprecated, merged without mutating the author's dict; exact messages with and without a replacement, using the registered name; empty-string hint raises `ValueError`; warning only on successful results |
| `mcp_resource_size_cap` | `max_bytes` on `add_resource` / `@resource` / `ResourceManager`; `content_size(content)` | strict `>` boundary (exactly `max_bytes` passes); UTF-8 bytes, not characters; size of JSON-converted content; concrete template URI in the message; `ResourceError` (not `Unexpected...`), wire code `INTERNAL_ERROR`; `<= 0` raises `ValueError`; cap not listed |

Each `hidden_tests` module was checked against the patched repo (all pass, under 1 s) and
against bare `77001c9` (H1: 22 of 24 fail, the two that pass guard unchanged behaviour; H2 and
H3 fail at import because the new helpers do not exist). With each patch applied,
`tests/server tests/shared` stays at 2389 passed and the whole `tests/` tree stays at 6231 passed.

`load_heldout` detects these folders as `kind="patch"`. `check_reference` materializes
`demo_task/mcp/repo` at `base_commit` with `reference.patch` applied and runs the hidden tests in
`tests/_hidden_<name>/`. Under that runner: H1 has 24/24 passing on the reference and 22 failing on
bare; H2 has 24/24 on the reference and a collection error on bare; H3 has 33/33 on the reference
and a collection error on bare. The hidden tests import the new helpers from their defining modules
(`tools/base.py`, `resources/base.py`), so the slices do not depend on edits to the packages'
`__init__.py`, which the interface-derived `files` list leaves out.
