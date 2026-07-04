#!/usr/bin/env bash
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON_WRAPPER:-./python}"

run() {
  printf '\n[task3] %s\n' "$*"
  "$@"
}

run timeout 60 "$PY" -m ruff check .
run timeout 60 "$PY" -m ruff format --check .
run timeout 60 "$PY" -m pytest \
  tests/core/test_orchestrator_appraisal.py \
  tests/core/test_orchestrator_dispatch.py \
  tests/core/test_token_usage.py \
  tests/test_session_usage_budget.py \
  tests/test_frontend_compat.py \
  tests/test_task3_wiring_acceptance_qa.py \
  -q -p no:cacheprovider

if command -v node >/dev/null 2>&1; then
  run timeout 60 node tests/frontend_handleevent_smoke.mjs
else
  printf '\n[task3] skip node smoke: node not found\n'
fi
