"""Pin public interfaces with `ast` so implementations are comparable. Owner: A."""

from __future__ import annotations

import ast
from pathlib import Path

from core.models import FunctionSig


def extract_interface(repo: Path, files: list[str]) -> list[FunctionSig]:
    """All top-level public functions in the given files, with param and return annotations."""
    sigs: list[FunctionSig] = []
    for rel in files:
        path = repo / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                params = []
                for a in node.args.args:
                    ann = ast.unparse(a.annotation) if a.annotation else "Any"
                    params.append(f"{a.arg}: {ann}")
                returns = ast.unparse(node.returns) if node.returns else None
                sigs.append(FunctionSig(name=node.name, file=rel, params=params, returns=returns))
    return sigs


def diff_interface(expected: list[FunctionSig], actual: list[FunctionSig]) -> list[str]:
    """Names in `expected` that `actual` does not expose with the same arity."""
    have = {(s.name, s.file, len(s.params)) for s in actual}
    return [f"{s.file}:{s.name}/{len(s.params)}" for s in expected if (s.name, s.file, len(s.params)) not in have]
