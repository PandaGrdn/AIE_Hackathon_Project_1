#!/usr/bin/env bash
# Apply the per-tool limits reference series (S1..S7) to an MCP Python SDK
# checkout at commit 77001c9, then run the suite.
#
# Usage: apply.sh <repo-path> [--no-test]
#
# Each patch is applied with `git am --3way`; if that fails, the am is aborted
# and the patch is retried with `git apply --3way --index` and committed with
# the patch's own subject. The script stops at the first patch that cannot be
# applied either way.
set -euo pipefail

usage() {
    echo "usage: $0 <repo-path> [--no-test]" >&2
    exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
repo=$1
run_tests=1
if [[ $# -eq 2 ]]; then
    [[ $2 == "--no-test" ]] || usage
    run_tests=0
fi

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
patches=(
    01-limits-model.patch
    02-tool-policies.patch
    03-enforcement.patch
    04-metadata-context.patch
    05-session-scope.patch
    06-status-resource.patch
    07-cli-limits.patch
)

git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "error: $repo is not a git work tree" >&2
    exit 1
}
if [[ -n $(git -C "$repo" status --porcelain --untracked-files=no) ]]; then
    echo "error: $repo has uncommitted changes to tracked files" >&2
    exit 1
fi

# `git am` and `git commit` need an identity; fall back to a neutral one.
export GIT_AUTHOR_NAME=${GIT_AUTHOR_NAME:-$(git -C "$repo" config user.name || echo "limits-reference")}
export GIT_AUTHOR_EMAIL=${GIT_AUTHOR_EMAIL:-$(git -C "$repo" config user.email || echo "limits-reference@localhost")}
export GIT_COMMITTER_NAME=${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}
export GIT_COMMITTER_EMAIL=${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}

echo "base: $(git -C "$repo" log --oneline -1)"
for patch in "${patches[@]}"; do
    path="$here/$patch"
    [[ -f $path ]] || { echo "error: missing $path" >&2; exit 1; }
    if git -C "$repo" am --3way --quiet "$path"; then
        echo "applied (am):    $patch"
        continue
    fi
    git -C "$repo" am --abort >/dev/null 2>&1 || true
    if git -C "$repo" apply --3way --index "$path"; then
        subject=$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//p' "$path" | head -1)
        git -C "$repo" commit --quiet -m "$subject"
        echo "applied (apply): $patch"
        continue
    fi
    echo "error: $patch does not apply; stopping" >&2
    exit 1
done
echo "head: $(git -C "$repo" log --oneline -1)"

if [[ $run_tests -eq 1 ]]; then
    cd "$repo"
    uv sync --all-extras --python 3.12 --quiet
    # `python -m pytest` rather than the `pytest` script: a copied .venv keeps
    # console scripts whose shebang points at the original checkout.
    uv run python -m pytest -q tests/server tests/shared -p no:cacheprovider
fi
