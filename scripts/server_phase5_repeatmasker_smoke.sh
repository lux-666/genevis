#!/usr/bin/env bash
# Server-side Phase 5 existing-assembly RepeatMasker smoke entry point.
# Runs the existing assembly -> RepeatMasker -> gene planning backbone on the
# server checkout. It expects function_annotation to stop when database
# provenance is absent; it never fabricates provenance.
#
# Usage:
#   bash scripts/server_phase5_repeatmasker_smoke.sh \
#     --manifest    data/phase5_existing_asm_smoke/manifest.yaml \
#     --run-config  data/phase5_existing_asm_smoke/run_config.json \
#     --session-id  phase5-existing-asm-YYYYMMDD-a \
#     --data-dir    data/phase5_existing_asm_smoke/out
set -euo pipefail

ORCHESTRATOR_ENV="gene_agent"

MANIFEST="data/phase5_existing_asm_smoke/manifest.yaml"
RUN_CONFIG="data/phase5_existing_asm_smoke/run_config.json"
SESSION_ID=""
DATA_DIR="data/phase5_existing_asm_smoke/out"

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

run_log="${DATA_DIR}/phase5_repeatmasker_smoke.${SESSION_ID}.log"
mkdir -p "${DATA_DIR}"

echo "== gene-agent existing-assembly RepeatMasker smoke =="
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --run-config  "${RUN_CONFIG}" \
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

repeat_bundle="${DATA_DIR}/artifacts/repeat_annotation/${SESSION_ID}/repeat_annotation_bundle.json"
gene_bundle="${DATA_DIR}/artifacts/gene_annotation/${SESSION_ID}/gene_annotation_bundle.json"
function_bundle="${DATA_DIR}/artifacts/function_annotation/${SESSION_ID}/function_annotation_bundle.json"

for path in "${repeat_bundle}" "${gene_bundle}"; do
  if [[ ! -f "${path}" ]]; then
    printf 'error: expected artifact missing: %s\n' "${path}" >&2
    exit 1
  fi
done

softmasked=$(find "${DATA_DIR}/artifacts/repeat_annotation/${SESSION_ID}/repeatmasker" -name softmasked_genome.fasta -type f | head -n 1)
repeat_gff=$(find "${DATA_DIR}/artifacts/repeat_annotation/${SESSION_ID}/repeatmasker" -name repeat_annotation.gff3 -type f | head -n 1)
if [[ -z "${softmasked}" || -z "${repeat_gff}" ]]; then
  printf 'error: RepeatMasker outputs missing under %s\n' "${DATA_DIR}/artifacts/repeat_annotation/${SESSION_ID}/repeatmasker" >&2
  exit 1
fi
if [[ -f "${function_bundle}" ]]; then
  printf 'error: function_annotation bundle exists despite missing provenance: %s\n' "${function_bundle}" >&2
  exit 1
fi

artifact_index="${DATA_DIR}/artifacts/${SESSION_ID}/artifacts.jsonl"
conda run -n "${ORCHESTRATOR_ENV}" python - \
  "${repeat_bundle}" \
  "${gene_bundle}" \
  "${function_bundle}" \
  "${artifact_index}" \
  "${softmasked}" \
  "${repeat_gff}" <<'PY'
import json
import sys
from pathlib import Path

repeat_bundle, gene_bundle, function_bundle, artifact_index, softmasked, repeat_gff = [
    Path(arg) for arg in sys.argv[1:]
]

repeat = json.loads(repeat_bundle.read_text(encoding="utf-8"))
gene = json.loads(gene_bundle.read_text(encoding="utf-8"))

checks = [
    (repeat.get("status") == "executed", "repeat bundle status is not executed"),
    (repeat.get("repeat_library_source") == "provided", "repeat library source is not provided"),
    (repeat.get("softmasked_genome_ref") == str(softmasked), "repeat softmasked ref does not match file"),
    (repeat.get("repeat_annotation_gff3_ref") == str(repeat_gff), "repeat GFF3 ref does not match file"),
    (gene.get("genome_input_source") == "softmasked_genome", "gene input source is not softmasked_genome"),
    (
        gene.get("genome_input_ref") == repeat.get("softmasked_genome_ref"),
        "gene genome_input_ref does not match repeat softmasked_genome_ref",
    ),
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
    "repeat_annotation.softmasked_genome",
    "repeat_annotation.gff3",
    "gene_annotation.bundle",
}
missing = sorted(required - artifact_types)
if missing:
    raise SystemExit(f"error: artifact index missing types: {missing}")
PY

cat <<EOF

== Phase 5 RepeatMasker smoke reached expected provenance gate ==
run_log: ${run_log}
repeat_bundle: ${repeat_bundle}
softmasked_genome: ${softmasked}
repeat_annotation_gff3: ${repeat_gff}
gene_bundle: ${gene_bundle}

Record these refs in docs/handoff.md and check the task 03 server boxes.
EOF
