#!/usr/bin/env bash
# Server-side Phase 5 BUSCO evidence smoke entry point.
# Runs assembly_review locally with QUAST and BUSCO. Expects the workflow to
# block at assembly_review with both evaluation signals visible in the review
# request; the smoke does not apply biological thresholds or auto-accept.
#
# Usage:
#   bash scripts/server_phase5_busco_smoke.sh \
#     --manifest    /absolute/path/to/manifest.yaml \
#     --run-config  /absolute/path/to/run_config.json \
#     --session-id  phase5-busco-YYYYMMDD-a \
#     --data-dir    /absolute/path/to/out
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

for path in "${MANIFEST}" "${RUN_CONFIG}"; do
  if [[ ! -f "${path}" ]]; then
    printf 'error: file not found: %s\n' "${path}" >&2
    exit 1
  fi
done
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

run_log="${DATA_DIR}/phase5_busco_smoke.${SESSION_ID}.log"
mkdir -p "${DATA_DIR}"

echo "== gene-agent BUSCO evidence smoke =="
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --run-config  "${RUN_CONFIG}" \
  --session-id  "${SESSION_ID}" \
  --data-dir    "${DATA_DIR}" 2>&1 | tee "${run_log}"
status=${PIPESTATUS[0]}
set -e

if [[ "${status}" -ne 0 ]]; then
  printf 'error: expected gene-agent to block cleanly (exit 0), got %s\n' "${status}" >&2
  exit 1
fi
if ! grep -q 'current_stage: assembly_review' "${run_log}"; then
  printf 'error: run did not stop at assembly_review; see %s\n' "${run_log}" >&2
  exit 1
fi
if ! grep -q 'supervision_state: blocked_human' "${run_log}"; then
  printf 'error: run did not block for review; see %s\n' "${run_log}" >&2
  exit 1
fi

review_request="${DATA_DIR}/artifacts/assembly_review/${SESSION_ID}/assembly_review_request.json"
artifact_index="${DATA_DIR}/artifacts/${SESSION_ID}/artifacts.jsonl"

conda run -n "${ORCHESTRATOR_ENV}" python - \
  "${review_request}" \
  "${artifact_index}" <<'PY'
import json
import sys
from pathlib import Path

review_request, artifact_index = [Path(arg) for arg in sys.argv[1:]]
if not review_request.exists():
    raise SystemExit(f"error: review request missing: {review_request}")
if not artifact_index.exists():
    raise SystemExit(f"error: artifact index missing: {artifact_index}")

request = json.loads(review_request.read_text(encoding="utf-8"))
signals = request.get("signals", [])
by_name = {signal.get("signal_name"): signal for signal in signals}
for name in ["quast", "busco"]:
    signal = by_name.get(name)
    if not signal:
        raise SystemExit(f"error: missing {name} signal")
    if signal.get("status") != "available":
        raise SystemExit(f"error: {name} signal is not available: {signal.get('status')}")
    if not signal.get("metric_refs"):
        raise SystemExit(f"error: {name} signal has no metric refs")

artifact_types = {
    json.loads(line).get("artifact_type")
    for line in artifact_index.read_text(encoding="utf-8").splitlines()
    if line.strip()
}
required = {
    "assembly_review.quast_summary",
    "assembly_review.busco_short_summary",
    "assembly_review.busco_summary",
    "assembly_review.request",
}
missing = sorted(required - artifact_types)
if missing:
    raise SystemExit(f"error: artifact index missing types: {missing}")
PY

cat <<EOF

== Phase 5 BUSCO smoke reached assembly_review with QUAST+BUSCO evidence ==
run_log: ${run_log}
review_request: ${review_request}

Record these refs in docs/handoff.md and check the task 07 server box after server validation.
EOF
