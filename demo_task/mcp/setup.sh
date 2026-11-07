#!/usr/bin/env bash
# Prepare demo_task/mcp/repo: the MCP Python SDK at the pinned base commit, deps installed, suite green.
#
#   bash demo_task/mcp/setup.sh            # or: swarm setup demo_task/mcp
#   bash demo_task/mcp/setup.sh --reset    # throw away swarm commits/edits, back to the base commit
#   bash demo_task/mcp/setup.sh --no-tests # skip the baseline test run
#
# Idempotent: an existing clone that contains the base commit is kept as is (including subtask
# commits the orchestrator made on top of it). The clone has its own .git, which is what lets
# orchestrator._commit commit each verified subtask; it is gitignored and never vendored.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HERE/repo"
URL="https://github.com/modelcontextprotocol/python-sdk"
BASE="$(tr -d '[:space:]' < "$HERE/base_commit")"
BRANCH="swarm"   # subtask commits land here, on top of BASE

RESET=0
TESTS=1
for arg in "$@"; do
  case "$arg" in
    --reset) RESET=1 ;;
    --no-tests) TESTS=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || { echo "uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }
unset VIRTUAL_ENV  # the outer project's venv must not leak into the SDK's

if [ -d "$REPO/.git" ]; then
  if ! git -C "$REPO" cat-file -e "$BASE^{commit}" 2>/dev/null; then
    git -C "$REPO" fetch -q origin
  fi
  if ! git -C "$REPO" cat-file -e "$BASE^{commit}" 2>/dev/null; then
    echo "$REPO exists but does not contain $BASE; move it away and re-run" >&2
    exit 1
  fi
  if [ "$RESET" = 1 ]; then
    git -C "$REPO" checkout -q -f -B "$BRANCH" "$BASE"
    git -C "$REPO" clean -fdq
    echo "reset $REPO to $BASE"
  elif git -C "$REPO" merge-base --is-ancestor "$BASE" HEAD; then
    ahead="$(git -C "$REPO" rev-list --count "$BASE..HEAD")"
    echo "repo present at $(git -C "$REPO" rev-parse --short HEAD) ($ahead commits on top of $BASE); skipping clone"
  else
    echo "$REPO HEAD is not based on $BASE; re-run with --reset" >&2
    exit 1
  fi
else
  if [ -e "$REPO" ]; then
    echo "$REPO exists but is not a git clone; move it away and re-run" >&2
    exit 1
  fi
  echo "cloning $URL at $BASE into $REPO"
  # blobless clone: full commit graph (so the short SHA resolves and history is there), blobs on demand
  git clone -q --filter=blob:none --no-checkout "$URL" "$REPO"
  git -C "$REPO" checkout -q -B "$BRANCH" "$BASE"
fi

echo "base commit: $(git -C "$REPO" rev-parse "$BASE")"

cd "$REPO"
uv sync -q --all-extras --python 3.12

if [ "$TESTS" = 1 ]; then
  echo "running baseline suite: uv run pytest -q tests/server tests/shared -p no:cacheprovider"
  log="$(mktemp)"
  set +e
  uv run pytest -q tests/server tests/shared -p no:cacheprovider >"$log" 2>&1
  rc=$?
  set -e
  summary="$(grep -E '[0-9]+ (passed|failed|error)' "$log" | tail -1 | sed -E 's/^[[:space:]=]+//; s/[[:space:]=]+$//')"
  if [ "$rc" != 0 ]; then
    tail -40 "$log"
    rm -f "$log"
    echo "baseline suite FAILED (exit $rc): $summary" >&2
    exit "$rc"
  fi
  rm -f "$log"
  echo "baseline suite: $summary"
fi
echo "ready: $REPO"
