"""Task B's spec tests: the held-out slices' verified hidden tests, not a freshly authored set.

Task B (demo_task/mcp_heldout) is a long task built from the three MCP held-out slices
(eval/heldout/mcp_tool_timeout, mcp_tool_deprecation, mcp_resource_size_cap). Each slice's
hidden_tests/*.py are verified against its reference patch (eval.heldout_runner.check_reference),
so the verdict on Task B uses them directly: `spec_from_hidden_tests` is Task B's `spec_fn` for
core.orchestrator.run_task. The orchestrator then takes its real-repo path (cross_test with
`spec.modules` plus the repo's regression suite).

A Task B subtask names its slice with a `slice:<folder>` tag; the title is the fallback
(the slice folder name itself, or Task B's title for it).

The modules are flattened (`flatten_parametrized`): cross_test keys spec tests by bare
`<file>::<function>`, and a parametrized or anyio-backend item reports as `<function>[<id>]`, which the
harness would count as "not collected" for every run. Flattened, every case is its own top-level test;
on the reference trees the flattened modules pass exactly the cases the originals do (33 / 24 / 24).

`reset_repo` puts a task clone back on its base commit without ever deleting it: the previous
HEAD is kept on a branch and uncommitted edits are stashed, so nothing is lost.
"""

from __future__ import annotations

import ast
import subprocess
from datetime import datetime
from pathlib import Path

from core.harness.spec_tests import SpecTests, spec_node_ids
from core.models import Subtask

HELDOUT_DIR = Path(__file__).parent / "heldout"
SLICE_TAG = "slice:"

# Task B subtask title -> slice folder (fallback when a subtask has no slice tag)
TITLE_TO_SLICE = {
    "Per-tool call timeout": "mcp_tool_timeout",
    "Tool deprecation flags and call warnings": "mcp_tool_deprecation",
    "Resource size cap": "mcp_resource_size_cap",
}


def slice_name(subtask: Subtask) -> str:
    """The held-out slice folder a Task B subtask was built from."""
    for t in subtask.tags:
        if t.startswith(SLICE_TAG) and t[len(SLICE_TAG):]:
            return t[len(SLICE_TAG):]
    if subtask.title in TITLE_TO_SLICE:
        return TITLE_TO_SLICE[subtask.title]
    if (HELDOUT_DIR / subtask.title / "hidden_tests").is_dir():
        return subtask.title
    raise KeyError(f"subtask {subtask.title!r} names no held-out slice (no {SLICE_TAG}<name> tag, unknown title)")


def hidden_test_modules(name: str, heldout_dir: str | Path | None = None, *, flatten: bool = True) -> dict[str, str]:
    """{filename: source} for every hidden_tests/*.py of slice `name` (helpers included, sorted).

    With `flatten` (the default) each module goes through `flatten_parametrized`, so every pytest item
    is a top-level function: the harness (core.harness.crosstest) keys spec tests by bare
    `<file>::<function>` and does not map `<function>[<param>]` items back to them."""
    d = Path(heldout_dir or HELDOUT_DIR) / name / "hidden_tests"
    if not d.is_dir():
        raise FileNotFoundError(f"held-out slice {name}: {d} not found")
    mods = {p.name: p.read_text() for p in sorted(d.glob("*.py"))}
    if flatten:
        mods = {k: flatten_parametrized(v) for k, v in mods.items()}
    if not spec_node_ids(mods):
        raise ValueError(f"held-out slice {name}: no test functions in {d}")
    return mods


# ---------- parametrized tests -> top-level functions ----------

CASE_PREFIX = "_case_"
_ANYIO_FIXTURE = '@pytest.fixture\ndef anyio_backend():\n    return "asyncio"\n'


def _is_parametrize(dec: ast.expr) -> bool:
    """`@pytest.mark.parametrize(...)` (or `@mark.parametrize(...)`)."""
    return isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize" \
        and isinstance(dec.func.value, ast.Attribute) and dec.func.value.attr == "mark"


def _param_sets(dec: ast.Call) -> tuple[list[str], list[list[ast.expr]]] | None:
    """(argnames, rows of value expressions) for a literal parametrize call, else None (left as is)."""
    if len(dec.args) < 2:
        return None
    names_node, values_node = dec.args[0], dec.args[1]
    if isinstance(names_node, ast.Constant) and isinstance(names_node.value, str):
        names = [n.strip() for n in names_node.value.split(",") if n.strip()]
    elif isinstance(names_node, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in names_node.elts):
        names = [e.value for e in names_node.elts]
    else:
        return None
    if not names or not isinstance(values_node, (ast.Tuple, ast.List)):
        return None
    rows: list[list[ast.expr]] = []
    for el in values_node.elts:
        if isinstance(el, ast.Call) and isinstance(el.func, ast.Attribute) and el.func.attr == "param":
            if el.keywords:  # pytest.param(..., marks=/id=): not flattened
                return None
            el = ast.Tuple(elts=list(el.args), ctx=ast.Load()) if len(names) > 1 else (el.args[0] if len(el.args) == 1 else None)
            if el is None:
                return None
        if len(names) == 1:
            rows.append([el])
        elif isinstance(el, (ast.Tuple, ast.List)) and len(el.elts) == len(names):
            rows.append(list(el.elts))
        else:
            return None
    return names, rows


def flatten_parametrized(src: str) -> str:
    """Rewrite a pytest module so that every collected item is a plain top-level test function.

    - `@pytest.mark.parametrize` with literal values: the function becomes `_case_<name>` (not collected)
      and one wrapper `<name>__p<i>` per parameter combination (cartesian product for stacked decorators)
      calls it with that combination; other parameters (fixtures) pass through, async stays async.
      Non-literal parametrizations are left untouched.
    - a module that uses anyio (async tests or `pytest.mark.anyio`) and defines no `anyio_backend` fixture
      gets one returning "asyncio" (what the SDK's tests/conftest.py does), so items are not `[asyncio]`.
    Pass/fail semantics are unchanged: the wrappers run exactly the original cases.
    """
    import itertools

    tree = ast.parse(src)
    body: list[ast.stmt] = []
    changed = False
    for node in tree.body:
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")):
            body.append(node)
            continue
        params = [d for d in node.decorator_list if _is_parametrize(d)]
        sets = [_param_sets(d) for d in params]
        if not params or any(s is None for s in sets):
            body.append(node)
            continue
        changed = True
        others = [d for d in node.decorator_list if not _is_parametrize(d)]
        bound = [n for names, _ in sets for n in names]
        impl_name = CASE_PREFIX + node.name
        impl = type(node)(name=impl_name, args=node.args, body=node.body, decorator_list=[], returns=node.returns,
                          type_comment=None, **({"type_params": []} if hasattr(node, "type_params") else {}))
        body.append(impl)
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        passthrough = [a for a in all_args if a.arg not in bound]
        for i, combo in enumerate(itertools.product(*[rows for _, rows in sets])):
            values = {n: v for (names, _), row in zip(sets, combo) for n, v in zip(names, row)}
            call = ast.Call(func=ast.Name(id=impl_name, ctx=ast.Load()), args=[],
                            keywords=[ast.keyword(arg=n, value=v) for n, v in values.items()]
                            + [ast.keyword(arg=a.arg, value=ast.Name(id=a.arg, ctx=ast.Load())) for a in passthrough])
            ret = ast.Return(value=ast.Await(value=call) if isinstance(node, ast.AsyncFunctionDef) else call)
            wrapper = type(node)(name=f"{node.name}__p{i}",
                                 args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=a.arg) for a in passthrough], kwonlyargs=[],
                                                    kw_defaults=[], defaults=[], vararg=None, kwarg=None),
                                 body=[ret], decorator_list=list(others), returns=None, type_comment=None,
                                 **({"type_params": []} if hasattr(node, "type_params") else {}))
            body.append(wrapper)
    uses_anyio = "anyio" in src or any(isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("test") for n in tree.body)
    has_backend = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "anyio_backend" for n in tree.body)
    if uses_anyio and not has_backend:
        changed = True
        last_import = max((i for i, n in enumerate(body) if isinstance(n, (ast.Import, ast.ImportFrom))), default=-1)
        body[last_import + 1:last_import + 1] = ast.parse(_ANYIO_FIXTURE).body
    if not changed:
        return src
    tree.body = body
    ast.fix_missing_locations(tree)
    doc = "# Rewritten by eval.heldout_specs.flatten_parametrized: one top-level test per parameter set.\n"
    return doc + ast.unparse(tree) + "\n"


def spec_from_hidden_tests(subtask: Subtask, repo: Path | None = None, *, heldout_dir: str | Path | None = None) -> SpecTests:
    """SpecTests for a Task B subtask: its slice's hidden pytest modules, all hidden (no visible tests,
    no function-level cases or properties). Same signature as core.harness.spec_tests.load_or_author;
    `repo` is unused (the tests are fixed files, not authored from the repo)."""
    name = slice_name(subtask)
    return SpecTests(
        subtask_id=subtask.id,
        cases=[],
        expected={},
        properties=[],
        visible_ids=[],
        modules=hidden_test_modules(name, heldout_dir),
        author_id=f"heldout:{name}",
    )


# ---------- repo reset ----------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def is_own_git_repo(repo: Path) -> bool:
    if not (repo / ".git").exists():
        return False
    r = _git(repo, "rev-parse", "--show-toplevel", check=False)
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == repo.resolve()


def repo_state(repo: Path, base_commit: str) -> dict:
    """Read-only facts about a task clone: exists, own git repo, base commit present, HEAD, commits
    ahead of base, dirty files (tracked or untracked, not ignored)."""
    repo = Path(repo)
    out: dict = {"repo": str(repo), "exists": repo.is_dir(), "own_git": False, "has_base": False,
                 "head": None, "at_base": False, "ahead": None, "dirty": []}
    if not out["exists"] or not is_own_git_repo(repo):
        return out
    out["own_git"] = True
    base = _git(repo, "rev-parse", "--verify", "-q", f"{base_commit}^{{commit}}", check=False)
    head = _git(repo, "rev-parse", "HEAD", check=False)
    out["head"] = head.stdout.strip()[:12] or None
    if base.returncode == 0:
        out["has_base"] = True
        full = base.stdout.strip()
        out["at_base"] = head.stdout.strip() == full
        anc = _git(repo, "merge-base", "--is-ancestor", full, "HEAD", check=False)
        if anc.returncode == 0:
            out["ahead"] = int(_git(repo, "rev-list", "--count", f"{full}..HEAD").stdout.strip() or 0)
    out["dirty"] = [ln for ln in _git(repo, "status", "--porcelain").stdout.splitlines() if ln.strip()]
    return out


def reset_repo(repo: Path, base_commit: str, *, branch: str = "swarm", keep_as: str | None = None) -> dict:
    """Put `repo` (a task clone with its own .git) on `branch` at `base_commit`. Never deletes the clone.

    - stale worktrees (from crashed agent sandboxes) are pruned;
    - uncommitted edits and untracked files are stashed (`git stash -u`), not discarded;
    - when HEAD is not already the base commit, it is kept on branch `keep_as`
      (default `demo/pre-reset-<timestamp>`) before `git checkout -f -B <branch> <base_commit>`.
    Returns {"previous_head", "kept_branch", "stashed"}.
    """
    repo = Path(repo)
    if not is_own_git_repo(repo):
        raise RuntimeError(f"{repo} is not a git clone of its own (run demo_task/mcp/setup.sh)")
    if _git(repo, "rev-parse", "--verify", "-q", f"{base_commit}^{{commit}}", check=False).returncode != 0:
        raise RuntimeError(f"{repo} does not contain base commit {base_commit} (run demo_task/mcp/setup.sh)")
    _git(repo, "worktree", "prune", check=False)
    full_base = _git(repo, "rev-parse", f"{base_commit}^{{commit}}").stdout.strip()
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    stashed = False
    if _git(repo, "status", "--porcelain").stdout.strip():
        r = _git(repo, "stash", "push", "-u", "-m", f"reset_repo {datetime.now():%Y%m%d-%H%M%S}", check=False)
        stashed = r.returncode == 0 and "No local changes" not in r.stdout
    kept = None
    if prev != full_base:
        kept = keep_as or f"demo/pre-reset-{datetime.now():%Y%m%d-%H%M%S}"
        _git(repo, "branch", "-f", kept, prev)
    _git(repo, "checkout", "-q", "-f", "-B", branch, full_base)
    return {"previous_head": prev[:12], "kept_branch": kept, "stashed": stashed}
