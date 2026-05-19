#!/usr/bin/env bash
# Server-side Phase 5 auto-review smoke (deterministic auto_rule path).
#
# WHAT THIS VALIDATES:
#   - existing-assembly entry → assembly_review deterministic_safe → auto_rule
#     decision → workflow reaches commit_function_annotation → final review
#     decision report is written.
#
# WHAT THIS DOES NOT VALIDATE:
#   - LLM-backed `decided_by="llm_provisional"` review. The deterministic path
#     bypasses `human_review_gate` entirely, so the recommender is never called.
#     For LLM-provisional evidence, run scripts/server_phase5_llm_review_smoke.sh
#     against a raw_inputs fixture with valid GENE_AGENT_LLM_API_KEY.
#
# Usage:
#   bash scripts/server_phase5_auto_review_smoke.sh \
#     --manifest    /abs/path/to/manifest.yaml \
#     --run-config  /abs/path/to/run_config_template.json \
#     --session-id  phase5-auto-review-YYYYMMDD-a \
#     --data-dir    /abs/path/to/out
set -euo pipefail
source "$(dirname "$0")/lib/server_smoke_common.sh"

MANIFEST="data/phase5_auto_review/manifest.yaml"
RUN_CONFIG_TEMPLATE="data/phase5_auto_review/run_config.json"
SESSION_ID=""
DATA_DIR="data/phase5_auto_review/out"

usage() { sed -n '2,21p' "$0" >&2; }

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

require --manifest    "${MANIFEST}"   || { usage; exit 1; }
require --run-config  "${RUN_CONFIG_TEMPLATE}" || { usage; exit 1; }
require --session-id  "${SESSION_ID}" || { usage; exit 1; }
require --data-dir    "${DATA_DIR}"   || { usage; exit 1; }

ensure_file manifest    "${MANIFEST}"
ensure_file run-config  "${RUN_CONFIG_TEMPLATE}"
ensure_conda

MANIFEST="$(abs_path "${MANIFEST}")"
RUN_CONFIG_TEMPLATE="$(abs_path "${RUN_CONFIG_TEMPLATE}")"
DATA_DIR="$(abs_path "${DATA_DIR}")"

mkdir -p "${DATA_DIR}"

# gene_annotation planning sets proteins_fasta_ref to this path string. The
# database_provenance.input_proteins_ref must match it exactly; the smoke
# validates the committed bundle fields rather than requiring the planned FASTA
# to exist on disk.
PROTEINS_REF="${DATA_DIR}/work/gene_annotation/${SESSION_ID}/proteins.fa"

RUN_CONFIG="${DATA_DIR}/run_config_patched.json"
sed "s|__REPLACE_WITH_PROTEINS_FASTA_REF__|${PROTEINS_REF}|g" \
  "${RUN_CONFIG_TEMPLATE}" > "${RUN_CONFIG}"

run_log="${DATA_DIR}/phase5_auto_review_smoke.${SESSION_ID}.log"

echo "== gene-agent auto-review smoke (deterministic auto_rule path) =="
echo "session_id:    ${SESSION_ID}"
echo "data_dir:      ${DATA_DIR}"
echo "proteins_ref:  ${PROTEINS_REF}"
echo "run_config:    ${RUN_CONFIG}"

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
if ! grep -q 'supervision_state: completed' "${run_log}"; then
  printf 'error: run did not reach supervision_state: completed; see %s\n' "${run_log}" >&2
  exit 1
fi

# Note: gene_annotation is planning-only — it sets proteins_fasta_ref to a
# planned path string but never writes the file. validate_database_provenance
# only checks string equality, so the run reaches terminal commit even though
# ${PROTEINS_REF} does not exist on disk. Do not check for the file here.

final_report="${DATA_DIR}/artifacts/final_report/${SESSION_ID}/review_decision_report.json"
function_bundle="${DATA_DIR}/artifacts/function_annotation/${SESSION_ID}/function_annotation_bundle.json"

for path in "${final_report}" "${function_bundle}"; do
  ensure_file "expected artifact" "${path}"
done

# Validate this smoke's specific claim: at least one decision is decided_by=auto_rule
# (the deterministic existing-assembly accept), and the seeded provenance matched
# the planning-only proteins_fasta_ref string used by function_annotation.
conda run -n "${ORCHESTRATOR_ENV}" python - <<PY
import json, sys
report = json.loads(open("${final_report}").read())
decisions = report.get("decisions", [])
if not decisions:
    sys.exit("error: review_decision_report.json has no decisions")
origins = report.get("decisions_by_origin", {})
auto_rule_count = origins.get("auto_rule", 0)
if auto_rule_count < 1:
    sys.exit(f"error: expected at least 1 auto_rule decision, got {origins}")
if report.get("pending_human_confirmations"):
    sys.exit(f"error: deterministic final-report smoke should not have pending confirmations: {report.get('pending_human_confirmations')}")
bundle = json.loads(open("${function_bundle}").read())
if bundle.get("proteins_fasta_ref") != "${PROTEINS_REF}":
    sys.exit(f"error: function bundle proteins_fasta_ref mismatch: {bundle.get('proteins_fasta_ref')!r}")
provenance = bundle.get("database_provenance") or []
if not provenance:
    sys.exit("error: function bundle missing database_provenance")
for index, record in enumerate(provenance):
    if record.get("input_proteins_ref") != "${PROTEINS_REF}":
        sys.exit(f"error: provenance[{index}].input_proteins_ref mismatch: {record.get('input_proteins_ref')!r}")
print(f"decisions: {len(decisions)}, by_origin: {origins}")
print(f"proteins_fasta_ref: {bundle.get('proteins_fasta_ref')}")
PY

cat <<EOF

== Phase 5 auto-review smoke reached expected terminal success ==
run_log:        ${run_log}
final_report:   ${final_report}
function_bundle: ${function_bundle}

Record session_id, exit_code (0), final_report path, and the auto_rule
decision count in docs/handoff.md.
EOF
