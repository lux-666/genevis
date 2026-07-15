#!/usr/bin/env bash
# Exercise only the production assembly parameter LLM boundary. No bioinformatics
# tools are started; the test replays an existing prompt/profile artifact pair.
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 08: Live Assembly Parameter LLM"

PROMPT_PAYLOAD="${1:-${PARAMETER_LLM_SMOKE_PROMPT:-}}"
if [[ -z "$PROMPT_PAYLOAD" && -d "${TEST_DATA_ROOT}/runs" ]]; then
    PROMPT_PAYLOAD=$(find "${TEST_DATA_ROOT}/runs" -type f -path '*/parameter_attempt_*/prompt_payload.json' 2>/dev/null | sort | tail -n 1 || true)
fi
if [[ -z "$PROMPT_PAYLOAD" ]]; then
    info "[SKIP] No parameter prompt payload found."
    info "Pass one explicitly: bash tests/hpc/test_08_parameter_llm.sh /path/to/prompt_payload.json"
    exit 77
fi
assert_file_nonempty "$PROMPT_PAYLOAD" "parameter prompt payload"

INPUT_PROFILE="${PARAMETER_LLM_SMOKE_PROFILE:-$(dirname "$PROMPT_PAYLOAD")/input_profile.json}"
assert_file_nonempty "$INPUT_PROFILE" "parameter input profile"

ATTEMPTS="${PARAMETER_LLM_SMOKE_ATTEMPTS:-1}"
if [[ ! "$ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
    fail "PARAMETER_LLM_SMOKE_ATTEMPTS must be a positive integer"
fi
if (( _FAIL > 0 )); then
    summary "Test 08: Live Assembly Parameter LLM" || true
    exit 1
fi

RUN_ID="parameter-llm-smoke-$(date +%Y%m%d-%H%M%S)"
OUTPUT_DIR="${TEST_DATA_ROOT}/parameter_llm_smoke/${RUN_ID}"
mkdir -p "$OUTPUT_DIR"

info "Prompt payload: ${PROMPT_PAYLOAD}"
info "Input profile:  ${INPUT_PROFILE}"
info "Live attempts:  ${ATTEMPTS}"
info "Output:         ${OUTPUT_DIR}"

cd "${PROJECT_ROOT}"
if python3 -m app.diagnostics.parameter_recommendation_smoke \
    --prompt-payload "$PROMPT_PAYLOAD" \
    --input-profile "$INPUT_PROFILE" \
    --output-dir "$OUTPUT_DIR" \
    --attempts "$ATTEMPTS"; then
    pass "all live LLM responses passed DTO and parameter catalog validation"
else
    fail "one or more live LLM responses failed; inspect ${OUTPUT_DIR}"
fi

summary "Test 08: Live Assembly Parameter LLM"
