#!/usr/bin/env bash
# Server-side Phase 5 provided gene annotation adoption smoke entry point.
# Runs the existing assembly + provided GFF3/proteins/transcripts adoption path
# on the server checkout. Expects function_annotation to stop at the database
# provenance gate; never fabricates provenance.
#
# Usage:
#   bash scripts/server_phase5_provided_annotation_smoke.sh \
#     --manifest    /absolute/path/to/manifest.yaml \
#     --session-id  phase5-provided-ann-YYYYMMDD-a \
#     --data-dir    /absolute/path/to/out
set -euo pipefail

ORCHESTRATOR_ENV="gene_agent"

MANIFEST=""
SESSION_ID=""
DATA_DIR=""

usage() {
  sed -n '2,10p' "$0" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)    MANIFEST="${2:-}";    shift 2 ;;
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
require --session-id  "${SESSION_ID}"
require --data-dir    "${DATA_DIR}"

if [[ ! -f "${MANIFEST}" ]]; then
  printf 'error: manifest not found: %s\n' "${MANIFEST}" >&2
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
DATA_DIR="$(abs_path "${DATA_DIR}")"

run_log="${DATA_DIR}/phase5_provided_annotation_smoke.${SESSION_ID}.log"
mkdir -p "${DATA_DIR}"

echo "== gene-agent provided gene annotation adoption smoke =="
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --session-id  "${SESSION_ID}" \
  --data-dir    "${DATA_DIR}" 2>&1 | tee "${run_log}"
status=${PIPESTATUS[0]}
set -e

if [[ "${status}" -ne 1 ]]; then
  printf 'error: expected gene-agent to stop at function_annotation without database provenance (exit 1), got %s\n' "${status}" >&2
  exit 1
fi
if ! grep -q 'current_stage: function_annotation' "${run_log}"; then
  printf 'error: run did not reach function_annotation; see %s\n' "${run_log}" >&2
  exit 1
fi
if ! grep -q 'supervision_state: failed_terminal' "${run_log}"; then
  printf 'error: run did not stop as failed_terminal; see %s\n' "${run_log}" >&2
  exit 1
fi
if ! grep -qi 'provenance' "${run_log}"; then
  printf 'error: function_annotation failure did not mention provenance; see %s\n' "${run_log}" >&2
  exit 1
fi

gene_bundle="${DATA_DIR}/artifacts/gene_annotation/${SESSION_ID}/gene_annotation_bundle.json"
function_bundle="${DATA_DIR}/artifacts/function_annotation/${SESSION_ID}/function_annotation_bundle.json"

if [[ ! -f "${gene_bundle}" ]]; then
  printf 'error: gene annotation bundle missing: %s\n' "${gene_bundle}" >&2
  exit 1
fi
if [[ -f "${function_bundle}" ]]; then
  printf 'error: function_annotation bundle exists despite missing provenance: %s\n' "${function_bundle}" >&2
  exit 1
fi

artifact_index="${DATA_DIR}/artifacts/${SESSION_ID}/artifacts.jsonl"
conda run -n "${ORCHESTRATOR_ENV}" python - \
  "${gene_bundle}" \
  "${function_bundle}" \
  "${artifact_index}" <<'PY'
import json
import sys
from pathlib import Path

gene_bundle, function_bundle, artifact_index = [Path(arg) for arg in sys.argv[1:]]

gene = json.loads(gene_bundle.read_text(encoding="utf-8"))

checks = [
    (gene.get("status") == "adopted_existing_annotation", "gene bundle status is not adopted_existing_annotation"),
    (gene.get("evidence_profile") == "provided_annotation", "gene evidence_profile is not provided_annotation"),
    (gene.get("candidate_methods") == [], "gene candidate_methods is not empty"),
    (gene.get("genes_gff3_ref") is not None, "gene bundle missing genes_gff3_ref"),
    (gene.get("predicted_proteins_ref") is not None, "gene bundle missing predicted_proteins_ref"),
    (Path(gene["genes_gff3_ref"]).exists(), "genes_gff3_ref file does not exist"),
    (Path(gene["predicted_proteins_ref"]).exists(), "predicted_proteins_ref file does not exist"),
    (not function_bundle.exists(), "function bundle exists despite missing provenance"),
    (artifact_index.exists(), "artifact index is missing"),
]
for ok, message in checks:
    if not ok:
        raise SystemExit(f"error: {message}")

artifact_types = {
    json.loads(line).get("artifact_type")
    for line in artifact_index.read_text(encoding="utf-8").splitlines()
    if line.strip()
}
required = {
    "gene_annotation.provided_gene_models",
    "gene_annotation.provided_predicted_proteins",
    "gene_annotation.bundle",
}
missing = sorted(required - artifact_types)
if missing:
    raise SystemExit(f"error: artifact index missing types: {missing}")
PY

cat <<EOF

== Phase 5 provided annotation smoke reached expected provenance gate ==
run_log: ${run_log}
gene_bundle: ${gene_bundle}

Record these refs in docs/handoff.md and check the task 06 server boxes.
EOF
