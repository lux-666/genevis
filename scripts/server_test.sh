#!/usr/bin/env bash
# Server-side code regression runner for gene-agent.
# Mirrors the Mac developer test loop so a server operator can confirm the
# checkout before running real data. It runs Python regression and compile
# checks only; it is not a real-data or biological-tool smoke test. Stops on
# the first non-zero exit. Run it from the activated orchestrator environment;
# it never selects an environment or installs packages.
#
# Usage:
#   bash scripts/server_test.sh
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
cd "${REPO_ROOT}"

PYCACHE_PREFIX="/tmp/gene_agent_pycache"

if ! command -v python >/dev/null 2>&1; then
  printf 'error: python not found; activate the orchestrator environment first\n' >&2
  exit 1
fi

if [[ ! -f "docs/examples/envs.yaml.example" ]]; then
  printf 'error: checkout is missing docs/examples/envs.yaml.example; restore a complete checkout before testing\n' >&2
  exit 1
fi

INSTALL_COMMAND="python -m pip install -e '.[dev]'"

if ! python -c 'from importlib.metadata import version; version("gene-agent")' >/dev/null 2>&1; then
  printf "error: gene-agent is not installed in this Python environment; run: %s\n" "${INSTALL_COMMAND}" >&2
  exit 1
fi

if ! python -m pip check >/dev/null 2>&1; then
  printf "error: this Python environment has unresolved package dependencies; run: %s\n" "${INSTALL_COMMAND}" >&2
  exit 1
fi

if ! python -c 'import pytest' >/dev/null 2>&1; then
  printf "error: pytest is missing; install all project and test dependencies with: %s\n" "${INSTALL_COMMAND}" >&2
  exit 1
fi

echo "scope: server-side code regression and compile checks only"
echo "note: this does not run real biological tools or real-data smoke tests"

echo "== pytest =="
PYTHONPYCACHEPREFIX="${PYCACHE_PREFIX}" python -m pytest -q

echo "== compileall =="
PYTHONPYCACHEPREFIX="${PYCACHE_PREFIX}" python -m compileall app tests

echo "== git diff --check =="
git diff --check

echo "== server code tests passed =="
