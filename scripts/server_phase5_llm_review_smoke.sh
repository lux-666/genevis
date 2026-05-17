#!/usr/bin/env bash
# Server-side Phase 5 LLM-provisional review smoke.
#
# WHAT THIS VALIDATES:
#   - raw_inputs entry with review_policy.mode=auto reaches `human_review_gate`,
#     calls the recommender, writes an `assembly_review.review_recommendation`
#     artifact, applies the synthetic resume, records `auto_decision_committed`
#     with `decided_by="llm_provisional"`, and advances past assembly_review.
#   - The workflow then blocks at `hic_scaffolding` because Hi-C is
#     `auto_decidable=false` in auto mode. That blocked state is the expected
#     terminal condition for this smoke; reaching it proves the LLM provisional
#     review path is functional.
#
# REQUIRES:
#   - GENE_AGENT_LLM_API_KEY exported in the environment (or .env loaded by run.py).
#   - assembly local execution (Flye) and QUAST in env_geno_exec on the host.
#
# WHAT THIS DOES NOT VALIDATE:
#   - Final review report artifact. That requires reaching the
#     function_annotation terminal commit, which auto mode cannot reach without
#     a human Hi-C decision. Run server_phase5_auto_review_smoke.sh for that.
#
# Usage:
#   bash scripts/server_phase5_llm_review_smoke.sh \
#     --manifest    /abs/path/to/manifest.yaml \
#     --run-config  /abs/path/to/run_config.json \
#     --session-id  phase5-llm-review-YYYYMMDD-a \
#     --data-dir    /abs/path/to/out
set -euo pipefail
source "$(dirname "$0")/lib/server_smoke_common.sh"

MANIFEST="data/phase5_llm_review/manifest.yaml"
RUN_CONFIG="data/phase5_llm_review/run_config.json"
SESSION_ID=""
DATA_DIR="data/phase5_llm_review/out"

usage() { sed -n '2,28p' "$0" >&2; }

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

require --manifest    "${MANIFEST}"   || { usage; exit 1; }
require --run-config  "${RUN_CONFIG}" || { usage; exit 1; }
require --session-id  "${SESSION_ID}" || { usage; exit 1; }
require --data-dir    "${DATA_DIR}"   || { usage; exit 1; }

if [[ -z "${GENE_AGENT_LLM_API_KEY:-}" ]]; then
  printf 'error: GENE_AGENT_LLM_API_KEY must be exported for this smoke\n' >&2
  exit 1
fi

ensure_file manifest    "${MANIFEST}"
ensure_file run-config  "${RUN_CONFIG}"
ensure_conda

MANIFEST="$(abs_path "${MANIFEST}")"
RUN_CONFIG="$(abs_path "${RUN_CONFIG}")"
DATA_DIR="$(abs_path "${DATA_DIR}")"

mkdir -p "${DATA_DIR}"
run_log="${DATA_DIR}/phase5_llm_review_smoke.${SESSION_ID}.log"

echo "== gene-agent LLM-provisional review smoke =="
echo "session_id:  ${SESSION_ID}"
echo "data_dir:    ${DATA_DIR}"
echo "run_config:  ${RUN_CONFIG}"

set +e
conda run -n "${ORCHESTRATOR_ENV}" gene-agent run \
  --manifest    "${MANIFEST}" \
  --run-config  "${RUN_CONFIG}" \
  --session-id  "${SESSION_ID}" \
  --data-dir    "${DATA_DIR}" 2>&1 | tee "${run_log}"
status=${PIPESTATUS[0]}
set -e

# Expected: blocked_human at hic_scaffolding (Hi-C is auto_decidable=false in auto mode).
if ! grep -q 'current_stage: hic_scaffolding' "${run_log}"; then
  printf 'error: run did not reach hic_scaffolding; see %s\n' "${run_log}" >&2
  exit 1
fi
if ! grep -q 'supervision_state: blocked_human' "${run_log}"; then
  printf 'error: run did not stop as blocked_human at Hi-C; see %s\n' "${run_log}" >&2
  exit 1
fi

events_path="${DATA_DIR}/ledger/${SESSION_ID}/events.jsonl"
ensure_file ledger "${events_path}"

recommendation_artifact="${DATA_DIR}/artifacts/assembly_review/${SESSION_ID}/assembly_review_review_recommendation.json"
ensure_file recommendation-artifact "${recommendation_artifact}"

# Validate ledger evidence: auto_decision_committed with llm_provisional, and
# the assembly_review review_decision event has decided_by=llm_provisional.
conda run -n "${ORCHESTRATOR_ENV}" python - <<PY
import json, sys, pathlib

events = [json.loads(line) for line in pathlib.Path("${events_path}").read_text().splitlines() if line.strip()]
auto_committed = [
    e for e in events
    if e.get("event_type") == "auto_decision_committed"
    and e.get("stage") == "assembly_review"
    and e.get("decided_by") == "llm_provisional"
]
if not auto_committed:
    sys.exit("error: no auto_decision_committed event with decided_by=llm_provisional for assembly_review")

ar_decisions = [
    e for e in events
    if e.get("event_type") == "review_decision"
    and e.get("stage") == "assembly_review"
]
if not ar_decisions:
    sys.exit("error: no review_decision event for assembly_review")
for e in ar_decisions:
    if e.get("decided_by") != "llm_provisional":
        sys.exit(f"error: assembly_review review_decision has decided_by={e.get('decided_by')!r}, expected llm_provisional")
    if not e.get("rationale_artifact_ref"):
        sys.exit("error: assembly_review review_decision missing rationale_artifact_ref")

rec = json.loads(pathlib.Path("${recommendation_artifact}").read_text())
for field in ("model", "prompt_path", "decisions_allowed", "review_request", "recommendation"):
    if field not in rec:
        sys.exit(f"error: recommendation artifact missing field: {field}")

print(f"auto_decision_committed events: {len(auto_committed)}")
print(f"review_decision events (assembly_review): {len(ar_decisions)}")
print(f"recommendation artifact model: {rec.get('model')}")
PY

cat <<EOF

== Phase 5 LLM-provisional review smoke reached expected blocked-at-hic state ==
run_log:                ${run_log}
recommendation_artifact: ${recommendation_artifact}
ledger:                  ${events_path}

Record session_id, the recommendation artifact ref, and the
auto_decision_committed event timestamp in docs/handoff.md.
EOF
