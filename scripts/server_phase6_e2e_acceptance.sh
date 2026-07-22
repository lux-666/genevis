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

if [[ "${status_a}" -ne 0 ]]; then
  printf 'error: Phase A expected exit 0 (blocked_human), got %s\n' "${status_a}" >&2
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
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent inspect \
  --session-id "${SESSION_ID}" \
  --data-dir   "${DATA_DIR}" \
  --json > "${inspect_out}"
inspect_status=$?
set -e
if [[ "${inspect_status}" -ne 0 ]]; then
  printf 'error: Phase B inspect exited %s; see %s\n' "${inspect_status}" "${inspect_out}" >&2
  if [[ -s "${inspect_out}" ]]; then
    cat "${inspect_out}" >&2
  fi
  exit 1
fi
echo "inspect_out: ${inspect_out}"

decision_helper="${DATA_DIR}/phase6_build_assembly_review_decision.py"
cat > "${decision_helper}" <<'PY'
import json
import sys
from pathlib import Path

inspect_path, decision_path, session_id = sys.argv[1:]

try:
    data = json.loads(Path(inspect_path).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    sys.exit(f"error: could not read inspect JSON {inspect_path}: {exc}")


def review_payload_candidates(summary):
    candidates = []
    interrupt = summary.get("active_interrupt")
    if isinstance(interrupt, dict) and isinstance(interrupt.get("candidates"), list):
        candidates.extend(interrupt["candidates"])

    artifact = summary.get("latest_review_request_artifact") or {}
    artifact_path = artifact.get("file_path") if isinstance(artifact, dict) else None
    if artifact_path:
        try:
            payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            candidates.extend(payload["candidates"])

    reqs = summary.get("decision_requirements") or {}
    refs = reqs.get("candidate_final_assembly_refs") or {}
    for candidate_id in reqs.get("candidate_ids") or []:
        if candidate_id in refs:
            candidates.append({"candidate_id": candidate_id, "final_assembly_ref": refs[candidate_id]})
    return candidates


choices = []
seen = set()
for index, candidate in enumerate(review_payload_candidates(data)):
    if not isinstance(candidate, dict):
        continue
    candidate_id = candidate.get("candidate_id")
    assembly_ref = candidate.get("final_assembly_ref") or candidate.get("assembly_ref")
    if not candidate_id or not assembly_ref:
        continue
    key = (str(candidate_id), str(assembly_ref))
    if key in seen:
        continue
    seen.add(key)
    choices.append(
        {
            "candidate_id": str(candidate_id),
            "assembly_ref": str(assembly_ref),
            "status": str(candidate.get("status") or ""),
            "ref_exists": Path(str(assembly_ref)).exists(),
            "order": index,
        }
    )

if not choices:
    reqs = data.get("decision_requirements") or {}
    sys.exit(
        "error: no assembly_review candidate with final_assembly_ref in inspect output; "
        f"decision_requirements keys={sorted(reqs.keys())}"
    )

selected = min(
    choices,
    key=lambda item: (
        not item["ref_exists"],
        item["status"] != "executed",
        item["order"],
    ),
)
if not selected["ref_exists"]:
    sys.exit(
        "error: no candidate final_assembly_ref exists on disk; "
        f"checked={[(c['candidate_id'], c['assembly_ref'], c['status']) for c in choices]}"
    )

decision = {
    "session_id": session_id,
    "stage": "assembly_review",
    "decision": "accept",
    "candidate_id": selected["candidate_id"],
    "final_assembly_ref": selected["assembly_ref"],
    "decided_by": "human",
    "notes": ["phase6 e2e acceptance: operator approved"],
}
Path(decision_path).write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
print(f"wrote decision: {decision_path}")
print(f"candidate_id:  {selected['candidate_id']}")
print(f"assembly_ref:  {selected['assembly_ref']}")
PY

set +e
conda run -n "${ORCHESTRATOR_ENV}" python \
  "${decision_helper}" "${inspect_out}" "${decision_file}" "${SESSION_ID}"
decision_var_status=$?
set -e
if [[ "${decision_var_status}" -ne 0 ]]; then
  printf 'error: Phase B could not build assembly_review decision file from %s\n' "${inspect_out}" >&2
  exit 1
fi
if [[ ! -s "${decision_file}" ]]; then
  printf 'error: Phase B decision helper exited 0 but did not write %s\n' "${decision_file}" >&2
  exit 1
fi

echo "decision_file: ${decision_file}"

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
set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent inspect \
  --session-id "${SESSION_ID}" \
  --data-dir   "${DATA_DIR}" \
  --json > "${final_inspect}"
final_inspect_status=$?
set -e
if [[ "${final_inspect_status}" -ne 0 ]]; then
  printf 'error: Phase C final inspect exited %s; see %s\n' "${final_inspect_status}" "${final_inspect}" >&2
  if [[ -s "${final_inspect}" ]]; then
    cat "${final_inspect}" >&2
  fi
  exit 1
fi
echo "final_inspect: ${final_inspect}"

verify_helper="${DATA_DIR}/phase6_verify_final_inspect.py"
cat > "${verify_helper}" <<'PY'
import json, sys
from pathlib import Path

inspect_path, data_dir, session_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
try:
    data = json.loads(Path(inspect_path).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    sys.exit(f"error: could not read final inspect JSON {inspect_path}: {exc}")

supervision = data.get("supervision_state")
if supervision != "completed":
    next_action = data.get("next_action")
    sys.exit(
        "error: expected supervision_state=completed, "
        f"got {supervision}; next_action={next_action}"
    )

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

set +e
conda run -n "${ORCHESTRATOR_ENV}" python \
  "${verify_helper}" "${final_inspect}" "${DATA_DIR}" "${SESSION_ID}"
phase_c_status=$?
set -e
if [[ "${phase_c_status}" -ne 0 ]]; then
  printf 'error: Phase C verification failed for %s\n' "${final_inspect}" >&2
  exit 1
fi

cat <<EOF

========================================
Phase 6 End-to-End Acceptance: PASSED
========================================
session_id:    ${SESSION_ID}
run_log:       ${run_log}
resume_log:    ${resume_log}
inspect_final: ${final_inspect}
decision_file: ${decision_file}

Preserve this summary with the server run records.
========================================
EOF
