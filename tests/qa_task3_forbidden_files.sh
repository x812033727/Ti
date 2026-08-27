#!/usr/bin/env bash
set -euo pipefail

forbidden_path() {
  case "$1" in
    studio/config.py|studio/flow.py|studio/orchestrator.py|CLAUDE.md|.github|.github/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

base_ref="${QA_BASE_REF:-}"
head_ref="${QA_HEAD_REF:-}"
context="env"
range_mode=1

if [[ -n "$base_ref" && -z "$head_ref" || -z "$base_ref" && -n "$head_ref" ]]; then
  echo "FAIL: QA_BASE_REF and QA_HEAD_REF must be provided together" >&2
  exit 1
fi

if [[ -z "$base_ref" && -z "$head_ref" ]]; then
  if command -v gh >/dev/null 2>&1 \
    && pr_base="$(gh pr view --json baseRefName --jq .baseRefName 2>/dev/null)" \
    && pr_head="$(gh pr view --json headRefOid --jq .headRefOid 2>/dev/null)" \
    && [[ -n "$pr_base" && -n "$pr_head" ]]; then
    if git rev-parse --verify --quiet "origin/${pr_base}" >/dev/null; then
      base_ref="$(git merge-base "origin/${pr_base}" "$pr_head")"
    else
      base_ref="$pr_base"
    fi
    head_ref="$pr_head"
    context="gh-pr"
  else
    context="worktree"
    range_mode=0
    echo "WARN: no current PR detected; checking staged, unstaged, and untracked files" >&2
  fi
fi

if (( range_mode != 0 )); then
  git rev-parse --verify --quiet "$base_ref" >/dev/null
  git rev-parse --verify --quiet "$head_ref" >/dev/null
fi

changed=()
declare -A seen=()
add_changed_files() {
  local path
  while IFS= read -r -d '' path; do
    if [[ -z "${seen[$path]+x}" ]]; then
      seen["$path"]=1
      changed+=("$path")
    fi
  done
}

if (( range_mode != 0 )); then
  add_changed_files < <(git diff --name-only -z "$base_ref" "$head_ref")
else
  add_changed_files < <(git diff --name-only -z)
  add_changed_files < <(git diff --cached --name-only -z)
  add_changed_files < <(git ls-files --others --exclude-standard -z)
fi

violations=()
flow_touched=0
for path in "${changed[@]}"; do
  if forbidden_path "$path"; then
    violations+=("$path")
  fi
  if [[ "$path" == "studio/flow.py" ]]; then
    flow_touched=1
  fi
done

echo "context: ${context}"
if (( range_mode != 0 )); then
  echo "base: $(git rev-parse "$base_ref")"
  echo "head: $(git rev-parse "$head_ref")"
else
  echo "base: worktree"
  echo "head: worktree"
fi
echo "changed_files: ${#changed[@]}"

if (( ${#violations[@]} > 0 )); then
  echo "FAIL: forbidden files changed:"
  printf ' - %s\n' "${violations[@]}"
  exit 1
fi

if (( flow_touched != 0 )); then
  echo "FAIL: studio/flow.py was changed; marker parsing and dual-route logic must remain untouched"
  exit 1
fi

echo "PASS: forbidden files absent from diff"
echo "PASS: studio/flow.py absent from diff; marker parsing strings and dual-route logic untouched by this range"
