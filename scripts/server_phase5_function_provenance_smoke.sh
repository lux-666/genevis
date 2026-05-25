#!/usr/bin/env bash
# Server-side Phase 5 function annotation provenance completion smoke.
#
# Validates the true terminal path:
#   existing assembly + provided gene annotation proteins + explicit database
#   provenance -> function_annotation bundle -> final report -> done.
#
# This smoke does not run InterProScan, eggNOG-mapper, DIAMOND, KofamScan, or
# download annotation databases. It verifies provenance and planned refs only.
#
# Usage:
#   bash scripts/server_phase5_function_provenance_smoke.sh \
#     --manifest    /abs/path/to/manifest.yaml \
#     --run-config  /abs/path/to/run_config_template.json \
#     --session-id  phase5-function-provenance-YYYYMMDD-a \
#     --data-dir    /abs/path/to/out
set -euo pipefail
source "$(dirname "$0")/lib/server_smoke_common.sh"

MANIFEST="data/phase5_auto_review/manifest.yaml"
RUN_CONFIG_TEMPLATE="data/phase5_auto_review/run_config.json"
SESSION_ID=""
DATA_DIR="data/phase5_function_provenance/out"

usage() { sed -n '2,17p' "$0" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)    MANIFEST="${2:-}";             shift 2 ;;
    --run-config)  RUN_CONFIG_TEMPLATE="${2:-}";  shift 2 ;;
    --session-id)  SESSION_ID="${2:-}";           shift 2 ;;
    --data-dir)    DATA_DIR="${2:-}";             shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) printf 'error: unknown argument: %s\n' "$1" >&2; usage; exit 1 ;;
  esac
done

require --manifest    "${MANIFEST}" || { usage; exit 1; }
require --run-config  "${RUN_CONFIG_TEMPLATE}" || { usage; exit 1; }
require --session-id  "${SESSION_ID}" || { usage; exit 1; }
require --data-dir    "${DATA_DIR}" || { usage; exit 1; }

ensure_file manifest "${MANIFEST}"
ensure_file run-config "${RUN_CONFIG_TEMPLATE}"
ensure_conda

MANIFEST="$(abs_path "${MANIFEST}")"
RUN_CONFIG_TEMPLATE="$(abs_path "${RUN_CONFIG_TEMPLATE}")"
DATA_DIR="$(abs_path "${DATA_DIR}")"
mkdir -p "${DATA_DIR}"

PROTEINS_REF=$(conda run -n "${ORCHESTRATOR_ENV}" python - "${MANIFEST}" <<'PY'
import sys
from pathlib import Path

import yaml

manifest_path = Path(sys.argv[1]).expanduser().resolve(strict=False)
manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
for asset in manifest.get("assets", []):
    if asset.get("asset_type") == "predicted_proteins":
        paths = asset.get("paths") or []
        if len(paths) != 1:
            raise SystemExit("error: predicted_proteins asset must have exactly one path")
        proteins_ref = Path(paths[0]).expanduser()
        if not proteins_ref.is_absolute():
            proteins_ref = manifest_path.parent / proteins_ref
        print(proteins_ref.resolve(strict=False))
        break
else:
    raise SystemExit("error: manifest must include a predicted_proteins asset")
PY
)
ensure_file predicted-proteins "${PROTEINS_REF}"

RUN_CONFIG="${DATA_DIR}/run_config_patched.json"
sed "s|__REPLACE_WITH_PROTEINS_FASTA_REF__|${PROTEINS_REF}|g" \
  "${RUN_CONFIG_TEMPLATE}" > "${RUN_CONFIG}"

run_log="${DATA_DIR}/phase5_function_provenance_smoke.${SESSION_ID}.log"

echo "== gene-agent function provenance completion smoke =="
echo "session_id:   ${SESSION_ID}"
echo "data_dir:     ${DATA_DIR}"
echo "proteins_ref: ${PROTEINS_REF}"
echo "run_config:   ${RUN_CONFIG}"

set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --run-config  "${RUN_CONFIG}" \
  --session-id  "${SESSION_ID}" \
  --data-dir    "${DATA_DIR}" 2>&1 | tee "${run_log}"
status=${PIPESTATUS[0]}
set -e

if [[ "${status}" -ne 0 ]]; then
  printf 'error: gene-agent exited with %s; expected 0 (completed). See %s\n' "${status}" "${run_log}" >&2
  exit 1
fi
if ! grep -q 'current_stage: done' "${run_log}" || ! grep -q 'supervision_state: completed' "${run_log}"; then
  printf 'error: run did not reach done/completed; see %s\n' "${run_log}" >&2
  exit 1
fi

final_report="${DATA_DIR}/artifacts/final_report/${SESSION_ID}/review_decision_report.json"
function_bundle="${DATA_DIR}/artifacts/function_annotation/${SESSION_ID}/function_annotation_bundle.json"
ledger="${DATA_DIR}/ledger/${SESSION_ID}/events.jsonl"

for path in "${final_report}" "${function_bundle}" "${ledger}"; do
  ensure_file expected-artifact "${path}"
done

conda run -n "${ORCHESTRATOR_ENV}" python - \
  "${function_bundle}" \
  "${final_report}" \
  "${ledger}" \
  "${PROTEINS_REF}" <<'PY'
import json
import sys
from pathlib import Path

bundle_path, final_report_path, ledger_path, proteins_ref = [Path(arg) for arg in sys.argv[1:]]
bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
report = json.loads(final_report_path.read_text(encoding="utf-8"))

if bundle.get("proteins_fasta_ref") != str(proteins_ref):
    raise SystemExit(f"error: proteins_fasta_ref mismatch: {bundle.get('proteins_fasta_ref')!r}")
provenance = bundle.get("database_provenance") or []
if not provenance:
    raise SystemExit("error: function bundle missing database_provenance")
for index, record in enumerate(provenance):
    if record.get("input_proteins_ref") != str(proteins_ref):
        raise SystemExit(f"error: provenance[{index}].input_proteins_ref mismatch")
    if not record.get("database_source_ref"):
        raise SystemExit(f"error: provenance[{index}] missing database_source_ref")

for ref_key in ("gene_function_tsv_ref", "go_annotation_table_ref", "kegg_annotation_table_ref", "pfam_interpro_table_ref"):
    planned_ref = bundle.get(ref_key)
    if not planned_ref:
        raise SystemExit(f"error: function bundle missing planned ref {ref_key}")
    if Path(planned_ref).exists():
        raise SystemExit(f"error: planned function table was unexpectedly materialized: {planned_ref}")

events = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
registered = {
    event.get("artifact", {}).get("artifact_type")
    for event in events
    if event.get("event_type") == "artifact_registered"
}
required = {"function_annotation.plan", "function_annotation.bundle", "final_report.review_decisions"}
missing = sorted(required - registered)
if missing:
    raise SystemExit(f"error: ledger missing artifact_registered events: {missing}")

print(f"provenance_records: {len(provenance)}")
print(f"final_report_artifact_count: {report.get('artifact_count')}")
PY

cat <<EOF

== Phase 5 function provenance smoke reached terminal success ==
run_log:         ${run_log}
final_report:    ${final_report}
function_bundle: ${function_bundle}
proteins_ref:    ${PROTEINS_REF}

Record session_id, exit_code (0), final_report path, function_bundle path, and
proteins_ref in docs/handoff.md.
EOF
