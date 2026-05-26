#!/usr/bin/env bash
# Phase 6 task 02: HPC end-to-end acceptance smoke.
#
# Validates one raw-read-to-terminal session exercising:
#   raw_long_reads -> read_qc (SeqKit) -> genome_survey (planning) ->
#   assembly (Flye) -> assembly_review (QUAST, strict human gate) ->
#   [durable resume] -> hic_scaffolding (skip) -> repeat_annotation
#   (RepeatMasker) -> gene_annotation (planning) -> function_annotation
#   (provenance) -> done/completed.
#
# The script runs in THREE phases:
#   Phase A: initial run, expected to block at assembly_review
#   Phase B: inspect + resume with operator decision
#   Phase C: verify terminal state and artifacts
#
# Usage:
#   bash scripts/server_phase6_e2e_acceptance.sh \
#     --manifest    /abs/path/to/manifest.yaml \
#     --run-config  /abs/path/to/run_config.json \
#     --session-id  phase6-e2e-YYYYMMDD-a \
#     --data-dir    /abs/path/to/out
set -euo pipefail
source "$(dirname "$0")/lib/server_smoke_common.sh"

MANIFEST=""
RUN_CONFIG=""
SESSION_ID=""
DATA_DIR=""

usage() { sed -n '2,24p' "$0" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)    MANIFEST="${2:-}";    shift 2 ;;
    --run-config)  RUN_CONFIG="${2:-}";  shift 2 ;;
    --session-id)  SESSION_ID="${2:-}";  shift 2 ;;
    --data-dir)    DATA_DIR="${2:-}";    shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) printf 'error: unknown argument: %s\n' "$1" >&2; usage; exit 1 ;;
  esac
done

require --manifest    "${MANIFEST}"    || { usage; exit 1; }
require --run-config  "${RUN_CONFIG}"  || { usage; exit 1; }
require --session-id  "${SESSION_ID}"  || { usage; exit 1; }
require --data-dir    "${DATA_DIR}"    || { usage; exit 1; }

ensure_file manifest   "${MANIFEST}"
ensure_file run-config "${RUN_CONFIG}"
ensure_conda

MANIFEST="$(abs_path "${MANIFEST}")"
RUN_CONFIG="$(abs_path "${RUN_CONFIG}")"
DATA_DIR="$(abs_path "${DATA_DIR}")"
mkdir -p "${DATA_DIR}"

run_log="${DATA_DIR}/phase6_e2e_acceptance.${SESSION_ID}.log"
decision_file="${DATA_DIR}/decision_assembly_review.json"

cat <<EOF
========================================
Phase 6 End-to-End Acceptance
========================================
session_id:  ${SESSION_ID}
manifest:    ${MANIFEST}
run_config:  ${RUN_CONFIG}
data_dir:    ${DATA_DIR}
========================================
EOF

# ─── Phase A: initial run, expect block at assembly_review ───
echo ""
echo "== Phase A: initial run (expect block at assembly_review) =="
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --run-config  "${RUN_CONFIG}" \
  --session-id  "${SESSION_ID}" \
  --data-dir    "${DATA_DIR}" 2>&1 | tee "${run_log}"
status_a=${PIPESTATUS[0]}
set -e

if [[ "${status_a}" -ne 1 ]]; then
  printf 'error: Phase A expected exit 1 (blocked_human), got %s\n' "${status_a}" >&2
  exit 1
fi
if ! grep -q 'supervision_state: blocked_human' "${run_log}"; then
  printf 'error: Phase A did not reach blocked_human; see %s\n' "${run_log}" >&2
  exit 1
fi
echo ""
echo "== Phase A passed: blocked at assembly_review as expected =="

# ─── Phase B: inspect + build decision + resume ───
echo ""
echo "== Phase B: inspect, build decision, and resume =="

inspect_out="${DATA_DIR}/inspect_blocked.${SESSION_ID}.json"
conda run -n "${ORCHESTRATOR_ENV}" gene-agent inspect \
  --session-id "${SESSION_ID}" \
  --data-dir   "${DATA_DIR}" \
  --json > "${inspect_out}"

eval "$(conda run -n "${ORCHESTRATOR_ENV}" python - "${inspect_out}" <<'PY'
import json, sys
data = json.loads(open(sys.argv[1]).read())
reqs = data.get("decision_requirements") or {}
candidates = reqs.get("candidate_ids") or []
if not candidates:
    sys.exit("error: no candidate_ids in inspect output")
candidate_id = candidates[0]
refs = reqs.get("candidate_final_assembly_refs") or {}
assembly_ref = refs.get(candidate_id, "")
if not assembly_ref:
    sys.exit(f"error: no final_assembly_ref for candidate {candidate_id}")
print(f'candidate_id="{candidate_id}"')
print(f'assembly_ref="{assembly_ref}"')
PY
)"

conda run -n "${ORCHESTRATOR_ENV}" python - \
  "${decision_file}" "${SESSION_ID}" "${candidate_id}" "${assembly_ref}" <<'PY'
import json, sys
path, session_id, candidate_id, assembly_ref = sys.argv[1:]
decision = {
    "session_id": session_id,
    "stage": "assembly_review",
    "decision": "accept",
    "candidate_id": candidate_id,
    "final_assembly_ref": assembly_ref,
    "notes": ["phase6 e2e acceptance: operator approved"]
}
with open(path, "w") as f:
    json.dump(decision, f, indent=2)
print(f"wrote decision: {path}")
PY

echo "decision_file: ${decision_file}"
echo "candidate_id:  ${candidate_id}"
echo "assembly_ref:  ${assembly_ref}"

resume_log="${DATA_DIR}/phase6_e2e_resume.${SESSION_ID}.log"
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent resume \
  --session-id    "${SESSION_ID}" \
  --decision-file "${decision_file}" \
  --data-dir      "${DATA_DIR}" 2>&1 | tee "${resume_log}"
status_b=${PIPESTATUS[0]}
set -e

if [[ "${status_b}" -ne 0 ]]; then
  printf 'error: Phase B resume exited %s; expected 0 (completed). See %s\n' \
    "${status_b}" "${resume_log}" >&2
  exit 1
fi
echo ""
echo "== Phase B passed: resume completed successfully =="

# ─── Phase C: verify terminal state and artifacts ───
echo ""
echo "== Phase C: verify terminal state and artifacts =="

final_inspect="${DATA_DIR}/inspect_final.${SESSION_ID}.json"
conda run -n "${ORCHESTRATOR_ENV}" gene-agent inspect \
  --session-id "${SESSION_ID}" \
  --data-dir   "${DATA_DIR}" \
  --json > "${final_inspect}"

conda run -n "${ORCHESTRATOR_ENV}" python - \
  "${final_inspect}" "${DATA_DIR}" "${SESSION_ID}" <<'PY'
import json, sys
from pathlib import Path

inspect_path, data_dir, session_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
data = json.loads(open(inspect_path).read())

supervision = data.get("supervision_state")
if supervision != "completed":
    sys.exit(f"error: expected supervision_state=completed, got {supervision}")

arts = data_dir / "artifacts"
final_report = arts / "final_report" / session_id / "review_decision_report.json"
function_bundle = arts / "function_annotation" / session_id / "function_annotation_bundle.json"
repeat_bundle = arts / "repeat_annotation" / session_id / "repeat_annotation_bundle.json"
read_qc_summary = arts / "read_qc" / session_id / "read_qc_summary.json"
assembly_dir = arts / "assembly" / session_id / "flye"
ledger = data_dir / "ledger" / session_id / "events.jsonl"
checkpoint_db = data_dir / "checkpoints" / "checkpoints.sqlite"

required = {
    "final_report": final_report,
    "function_bundle": function_bundle,
    "repeat_bundle": repeat_bundle,
    "read_qc_summary": read_qc_summary,
    "ledger": ledger,
    "checkpoint_db": checkpoint_db,
}
missing = {k: v for k, v in required.items() if not v.exists()}
if missing:
    sys.exit(f"error: missing artifacts: {missing}")

if not assembly_dir.exists():
    sys.exit(f"error: assembly flye dir missing: {assembly_dir}")

events = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
method_attempts = [e for e in events if e.get("event_type") == "method_attempt_started"]
attempt_keys = [f"{e.get('stage')}/{e.get('method_name')}" for e in method_attempts]
from collections import Counter
dupes = {k: v for k, v in Counter(attempt_keys).items() if v > 1}
if dupes:
    sys.exit(f"error: duplicate method attempts after resume: {dupes}")

report = json.loads(final_report.read_text())
decisions = report.get("decisions", [])
origins = report.get("decisions_by_origin", {})

print(f"supervision:      {supervision}")
print(f"decisions:        {len(decisions)}")
print(f"by_origin:        {origins}")
print(f"ledger_events:    {len(events)}")
print(f"method_attempts:  {len(method_attempts)}")
print(f"duplicate_check:  PASS (no duplicates)")
print(f"final_report:     {final_report}")
print(f"function_bundle:  {function_bundle}")
print(f"repeat_bundle:    {repeat_bundle}")
PY

cat <<EOF

========================================
Phase 6 End-to-End Acceptance: PASSED
========================================
session_id:    ${SESSION_ID}
run_log:       ${run_log}
resume_log:    ${resume_log}
inspect_final: ${final_inspect}
decision_file: ${decision_file}

Record these in docs/handoff.md under Phase 6 End-to-End Acceptance.
========================================
EOF
