#!/usr/bin/env bash
# Server-side environment check for gene-agent.
# Reports the current commit/branch and verifies the fixed method/package
# closure needed by the authoritative five-stage contig template. Assembly is
# intentionally absent from that startup closure because its method is selected
# only after upstream evidence exists; the selected method is checked at stage
# entry against the frozen dependency snapshot. It
# also validates the host BUSCO/NCBI database registry used by Assembly Review.
# It does not run biological tools or validate the LLM endpoint or real input
# files; `gene-agent run` performs those checks before freeze. Never
# installs anything; never modifies the filesystem. Exits non-zero on any
# missing piece so a server operator can fix the environment before running.
#
# Usage:
#   bash scripts/server_check.sh
set -euo pipefail

CALLER_CWD="$(pwd -P)"
ENV_CONFIG_OVERRIDE="${GENE_AGENT_ENVS_CONFIG:-}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
cd "${REPO_ROOT}"

ORCHESTRATOR_ENV="gene_agent"
CONTIG_RUN_CONFIG="docs/examples/contig_run_config.template.json"
if [[ -z "${ENV_CONFIG_OVERRIDE}" ]]; then
  ENVS_CONFIG="config/envs.yaml"
elif [[ "${ENV_CONFIG_OVERRIDE}" = /* ]]; then
  ENVS_CONFIG="${ENV_CONFIG_OVERRIDE}"
else
  ENVS_CONFIG="${CALLER_CWD}/${ENV_CONFIG_OVERRIDE}"
fi

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

ok() {
  printf 'ok: %s\n' "$1"
}

echo "== gene-agent server check =="
echo "scope: orchestrator prerequisites, fixed contig tool/package closure, and host databases"
echo "environment config: ${ENVS_CONFIG}"
echo "note: gene-agent run separately validates the LLM endpoint and real inputs before freeze"

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
  fail "conda env '${ORCHESTRATOR_ENV}' cannot run python; create or repair it (see docs/deployment/README.md)"
fi
ok "${ORCHESTRATOR_ENV} runs python"

# The TUI uses sqlite checkpoints for durable execution and review, so the
# orchestrator environment must carry langgraph-checkpoint-sqlite.
if ! conda run -n "${ORCHESTRATOR_ENV}" python -c 'from langgraph.checkpoint.sqlite import SqliteSaver' >/dev/null 2>&1; then
  fail "conda env '${ORCHESTRATOR_ENV}' is missing langgraph-checkpoint-sqlite; run 'pip install -e .' inside that env (see docs/deployment/README.md)"
fi
ok "${ORCHESTRATOR_ENV} has langgraph-checkpoint-sqlite"

if [[ ! -f "${CONTIG_RUN_CONFIG}" ]]; then
  fail "authoritative contig run config not found: ${CONTIG_RUN_CONFIG}"
fi
if [[ ! -f "${ENVS_CONFIG}" ]]; then
  fail "environment mapping not found: ${ENVS_CONFIG}"
fi

readiness_output=""
if ! readiness_output="$(PYTHONDONTWRITEBYTECODE=1 \
  GENE_AGENT_ENVS_CONFIG="${ENVS_CONFIG}" conda run \
  -n "${ORCHESTRATOR_ENV}" \
  python -c '
import sys

from app.execution.run_readiness import (
    assembly_candidate_method_scope,
    effective_method_closure,
    preflight_run_readiness,
)
from app.schemas.run_config import load_run_config

run_config_path, env_config_path = sys.argv[1:]
try:
    run_config = load_run_config(run_config_path)
    closure = effective_method_closure(run_config)
    methods = preflight_run_readiness(
        run_config,
        effective_methods=closure,
        config_path=env_config_path,
        llm_probe=lambda: None,
    )
except (OSError, TypeError, ValueError) as exc:
    print(f"CONTIG_READINESS_ERROR: {exc}")
else:
    print("contig methods checked: " + ", ".join(methods))
    print("Assembly method selection: deferred to stage entry")
    print(
        "registered Assembly candidates (not startup-preflighted): "
        + ", ".join(assembly_candidate_method_scope())
    )
' "${CONTIG_RUN_CONFIG}" "${ENVS_CONFIG}")"; then
  fail "could not invoke the authoritative contig readiness check"
fi
printf '%s\n' "${readiness_output}"
if [[ "${readiness_output}" == *"CONTIG_READINESS_ERROR:"* ]]; then
  fail "fix the reported host tool/package/database readiness issue, then rerun"
fi
ok "fixed contig tools/packages and host databases are ready; Assembly candidate readiness is checked after stage-owned method selection"

echo "== server check passed =="
