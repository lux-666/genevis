#!/usr/bin/env bash
# Server-side environment check for gene-agent Phase 4 acceptance.
# Reports the current commit/branch and verifies the conda envs and tools the
# Phase 4 mainline needs. Never installs anything; never modifies the
# filesystem. Exits non-zero on any missing piece so a server operator can
# fix the environment before running the Phase 4 smoke.
#
# Usage:
#   bash scripts/server_check.sh
set -euo pipefail

ORCHESTRATOR_ENV="gene_agent"
EXEC_ENV="env_geno_exec"
EXEC_TOOLS=("seqkit" "flye" "quast.py" "jellyfish" "genomescope.R")

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

ok() {
  printf 'ok: %s\n' "$1"
}

echo "== gene-agent server check =="

if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  printf 'commit: %s\n' "$(git rev-parse HEAD)"
  printf 'branch: %s\n' "$(git rev-parse --abbrev-ref HEAD)"
else
  printf 'commit: <not a git checkout>\n'
fi

if ! command -v conda >/dev/null 2>&1; then
  fail "conda not found on PATH; install Miniconda/Mambaforge before running this check"
fi
ok "conda present at $(command -v conda)"

if ! conda run -n "${ORCHESTRATOR_ENV}" python -c 'import sys; print(sys.version)' >/dev/null 2>&1; then
  fail "conda env '${ORCHESTRATOR_ENV}' cannot run python; create or repair it (see docs/deployment/mac_dev_server_pull.md)"
fi
ok "${ORCHESTRATOR_ENV} runs python"

if ! conda env list | awk '{print $1}' | grep -qx "${EXEC_ENV}"; then
  fail "conda env '${EXEC_ENV}' not found; create it with the bioinformatics tool stack before running Phase 4"
fi
ok "${EXEC_ENV} env exists"

missing_tools=()
for tool in "${EXEC_TOOLS[@]}"; do
  if conda run -n "${EXEC_ENV}" command -v "${tool}" >/dev/null 2>&1; then
    ok "${EXEC_ENV} has ${tool}"
  else
    missing_tools+=("${tool}")
  fi
done

if (( ${#missing_tools[@]} > 0 )); then
  printf 'error: missing tools in %s:' "${EXEC_ENV}" >&2
  for tool in "${missing_tools[@]}"; do
    printf ' %s' "${tool}" >&2
  done
  printf '\n' >&2
  fail "install the missing tool(s) into '${EXEC_ENV}' (e.g. conda install -n ${EXEC_ENV} -c bioconda <tool>) and rerun"
fi

echo "== server check passed =="
