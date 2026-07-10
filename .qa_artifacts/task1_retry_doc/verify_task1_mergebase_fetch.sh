#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
TARGET_TEST="tests/test_task1_retry_doc.py"
PYTHON_BIN="$ROOT/.venv/bin/python"
SCRATCH="$ROOT/.qa_artifacts/task1_retry_doc/tmp_worktrees"
NON_TASK_WORKTREE="$SCRATCH/not-task"

cleanup() {
  git -C "$ROOT" worktree remove --force "$NON_TASK_WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

echo "== fetch origin main =="
timeout 60 git -C "$ROOT" fetch origin main

echo "== origin/main contains merge-base guard =="
origin_test="$(git -C "$ROOT" show "origin/main:$TARGET_TEST")"
printf '%s\n' "$origin_test" | rg -n "def test_no_py_changed"
printf '%s\n' "$origin_test" | rg -n '\["git", "merge-base", "HEAD", "origin/main"\]'
printf '%s\n' "$origin_test" | rg -n "非 task#1 doc-only lane"

head_sha="$(git -C "$ROOT" rev-parse HEAD)"
origin_sha="$(git -C "$ROOT" rev-parse origin/main)"
fetch_sha="$(git -C "$ROOT" rev-parse FETCH_HEAD)"
echo "HEAD=$head_sha"
echo "origin/main=$origin_sha"
echo "FETCH_HEAD=$fetch_sha"
git -C "$ROOT" merge-base --is-ancestor HEAD origin/main
echo "HEAD_ANCESTOR_OF_ORIGIN_MAIN=true"
git -C "$ROOT" merge-base --is-ancestor origin/main HEAD
echo "ORIGIN_MAIN_ANCESTOR_OF_HEAD=true"
test "$head_sha" = "$origin_sha"
test "$origin_sha" = "$fetch_sha"

echo "== exact acceptance command availability =="
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "missing executable: $PYTHON_BIN" >&2
  exit 1
fi
echo "$("$PYTHON_BIN" --version)"

echo "== current task#1 lane pytest =="
current_output="$("$PYTHON_BIN" -m pytest "$TARGET_TEST" -q -rA)"
printf '%s\n' "$current_output"
printf '%s\n' "$current_output" | rg "PASSED tests/test_task1_retry_doc.py::test_no_py_changed"
printf '%s\n' "$current_output" | rg "11 passed"
if printf '%s\n' "$current_output" | rg -q "SKIPPED|failed"; then
  echo "current task#1 lane unexpectedly skipped or failed" >&2
  exit 1
fi

echo "== non-task lane skip control =="
cleanup
mkdir -p "$SCRATCH"
git -C "$ROOT" worktree add --detach "$NON_TASK_WORKTREE" HEAD >/dev/null
non_task_output="$(cd "$NON_TASK_WORKTREE" && "$PYTHON_BIN" -m pytest "$TARGET_TEST" -q -rA)"
printf '%s\n' "$non_task_output"
printf '%s\n' "$non_task_output" | rg "SKIPPED \\[1\\].*非 task#1 doc-only lane"
printf '%s\n' "$non_task_output" | rg "10 passed, 1 skipped"
if printf '%s\n' "$non_task_output" | rg -q "test_no_py_changed PASSED"; then
  echo "non-task lane reported pass instead of skip" >&2
  exit 1
fi

echo "QA_VERIFY_TASK1_MERGEBASE_FETCH=PASS"
