#!/usr/bin/env bash
# Server-side Phase 4 real-data smoke entry point.
# Calls `gene-agent run` from the server checkout with a manifest, run-config,
# session id, and data directory the operator supplies. Does not copy
# artifacts back anywhere; does not install packages.
#
# Usage:
#   bash scripts/server_phase4_smoke.sh \
#     --manifest    data/phase4_real_smoke/manifest.yaml \
#     --run-config  data/phase4_real_smoke/run_config.json \
#     --session-id  phase4-server-smoke-YYYYMMDD-a \
#     --data-dir    data/phase4_real_smoke/out
set -euo pipefail

ORCHESTRATOR_ENV="gene_agent"

MANIFEST=""
RUN_CONFIG=""
SESSION_ID=""
DATA_DIR=""

usage() {
  sed -n '2,11p' "$0" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)    MANIFEST="${2:-}";    shift 2 ;;
    --run-config)  RUN_CONFIG="${2:-}";  shift 2 ;;
    --session-id)  SESSION_ID="${2:-}";  shift 2 ;;
    --data-dir)    DATA_DIR="${2:-}";    shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2
      usage
      exit 1
      ;;
  esac
done

require() {
  local name="$1" value="$2"
  if [[ -z "${value}" ]]; then
    printf 'error: %s is required\n' "${name}" >&2
    usage
    exit 1
  fi
}

require --manifest    "${MANIFEST}"
require --run-config  "${RUN_CONFIG}"
require --session-id  "${SESSION_ID}"
require --data-dir    "${DATA_DIR}"

if [[ ! -f "${MANIFEST}" ]]; then
  printf 'error: manifest not found: %s\n' "${MANIFEST}" >&2
  exit 1
fi
if [[ ! -f "${RUN_CONFIG}" ]]; then
  printf 'error: run-config not found: %s\n' "${RUN_CONFIG}" >&2
  exit 1
fi
if ! command -v conda >/dev/null 2>&1; then
  printf 'error: conda not found on PATH\n' >&2
  exit 1
fi

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)  printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}

MANIFEST="$(abs_path "${MANIFEST}")"
RUN_CONFIG="$(abs_path "${RUN_CONFIG}")"
DATA_DIR="$(abs_path "${DATA_DIR}")"

echo "== gene-agent run =="
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --run-config  "${RUN_CONFIG}" \
  --session-id  "${SESSION_ID}" \
  --data-dir    "${DATA_DIR}"
status=$?
set -e

cat <<EOF

== expected Phase 4 artifact paths (relative to ${DATA_DIR}) ==
  artifacts/asset_registry/${SESSION_ID}/registry.json
  artifacts/read_qc/${SESSION_ID}/seqkit_stats.tsv
  artifacts/read_qc/${SESSION_ID}/read_qc_summary.json
  artifacts/read_qc/${SESSION_ID}/long_reads.fofn
  artifacts/assembly/${SESSION_ID}/flye/draft_assembly.fasta
  artifacts/assembly_review/${SESSION_ID}/quast/<safe_candidate_id>/quast_out/report.tsv
  artifacts/assembly_review/${SESSION_ID}/quast/<safe_candidate_id>/quast_summary.json
  artifacts/assembly_review/${SESSION_ID}/assembly_review_request.json

Preserve successful artifact refs with the server run records.
EOF

exit "${status}"
