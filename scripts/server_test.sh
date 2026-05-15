#!/usr/bin/env bash
# Server-side test runner for gene-agent.
# Mirrors the Mac developer test loop so a server operator can confirm the
# checkout passes before the Phase 4 real-data smoke. Stops on the first
# non-zero exit. Never installs packages.
#
# Usage:
#   bash scripts/server_test.sh
set -euo pipefail

ORCHESTRATOR_ENV="gene_agent"
PYCACHE_PREFIX="/tmp/gene_agent_pycache"

if ! command -v conda >/dev/null 2>&1; then
  printf 'error: conda not found on PATH\n' >&2
  exit 1
fi

echo "== unittest =="
PYTHONPYCACHEPREFIX="${PYCACHE_PREFIX}" conda run -n "${ORCHESTRATOR_ENV}" \
  python -m unittest discover -s tests -t . -p 'test*.py' -v

echo "== compileall =="
PYTHONPYCACHEPREFIX="${PYCACHE_PREFIX}" conda run -n "${ORCHESTRATOR_ENV}" \
  python -m compileall app tests

echo "== git diff --check =="
git diff --check

echo "== server tests passed =="
