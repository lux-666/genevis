#!/usr/bin/env bash
# Server-side code regression runner for gene-agent.
# Mirrors the Mac developer test loop so a server operator can confirm the
# checkout before running real data. It runs Python regression and compile
# checks only; it is not a real-data or biological-tool smoke test. Stops on
# the first non-zero exit. Never installs packages.
#
# Usage:
#   bash scripts/server_test.sh
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
cd "${REPO_ROOT}"

ORCHESTRATOR_ENV="gene_agent"
PYCACHE_PREFIX="/tmp/gene_agent_pycache"

if ! command -v conda >/dev/null 2>&1; then
  printf 'error: conda not found on PATH\n' >&2
  exit 1
fi

echo "scope: server-side code regression and compile checks only"
echo "note: this does not run real biological tools or real-data smoke tests"

echo "== pytest =="
PYTHONPYCACHEPREFIX="${PYCACHE_PREFIX}" conda run -n "${ORCHESTRATOR_ENV}" \
  pytest -q

echo "== compileall =="
PYTHONPYCACHEPREFIX="${PYCACHE_PREFIX}" conda run -n "${ORCHESTRATOR_ENV}" \
  python -m compileall app tests

echo "== git diff --check =="
git diff --check

echo "== server code tests passed =="
