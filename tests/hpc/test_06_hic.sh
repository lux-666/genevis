#!/usr/bin/env bash
# ============================================================
#  Test 06 — Hi-C Scaffolding: interactive planning + execution
# ============================================================
#  What it tests:
#    1. AutoHiC method subgraph (internally Juicer + 3D-DNA)
#    2. Hi-C shell script generation correctness
#    3. RunConfig Hi-C schema validation
#    4. Fixed AutoHiC pipeline with no method selection
#    5. Sparse AutoHiC recommendation plus mandatory human approval
#    6. Hi-C artifacts published to new layout
#    7. Distinct missing-requirements and execution-failure outcomes
#    8. Unit tests pass
#
#  Prerequisites:
#    - An assembly FASTA (from Test 04 or existing)
#    - Hi-C paired reads; model paths come from packages.autohic.models
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 06: Hi-C Scaffolding"

# ---- Verify input data ----
if [[ -n "${REVIEW_ASSEMBLY:-}" && ! -s "$REVIEW_ASSEMBLY" ]]; then
    fail "REVIEW_ASSEMBLY is missing or empty: ${REVIEW_ASSEMBLY}"
    summary "Test 06: Hi-C Scaffolding (invalid explicit assembly)" || true
    exit 1
fi
if [[ -n "${REVIEW_ASSEMBLY:-}" ]] && ! grep -q '^>' "$REVIEW_ASSEMBLY" 2>/dev/null; then
    fail "REVIEW_ASSEMBLY is not a FASTA file: ${REVIEW_ASSEMBLY}"
    summary "Test 06: Hi-C Scaffolding (invalid explicit assembly)" || true
    exit 1
fi
if [[ -z "${HIC_R1:-}" && -z "${HIC_R2:-}" ]]; then
    warn "Hi-C reads not configured (HIC_R1/HIC_R2 in hpc_env.sh)"
    info "Running schema + unit tests only (no real Hi-C execution)"
    HIC_AVAILABLE=false
elif [[ -z "${HIC_R1:-}" || -z "${HIC_R2:-}" ]]; then
    fail "HIC_R1 and HIC_R2 must be configured together"
    summary "Test 06: Hi-C Scaffolding (incomplete explicit inputs)" || true
    exit 1
elif [[ ! -s "$HIC_R1" || ! -s "$HIC_R2" ]]; then
    [[ -s "$HIC_R1" ]] || fail "HIC_R1 is missing or empty: ${HIC_R1}"
    [[ -s "$HIC_R2" ]] || fail "HIC_R2 is missing or empty: ${HIC_R2}"
    summary "Test 06: Hi-C Scaffolding (invalid explicit inputs)" || true
    exit 1
else
    assert_file_nonempty "$HIC_R1" "Hi-C R1"
    assert_file_nonempty "$HIC_R2" "Hi-C R2"
    HIC_AVAILABLE=true
fi

# ---- Unit tests ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    cd "${PROJECT_ROOT}"
    info "Running unit tests: test_hic_scaffolding.py"
    run_pytest tests/test_hic_scaffolding.py -v --tb=short 2>&1 && \
        pass "unit tests: test_hic_scaffolding.py" || fail "unit tests: test_hic_scaffolding.py"

    info "Running unit tests: test_run_config.py (Hi-C schema)"
    run_pytest tests/test_run_config.py -v --tb=short -k "hic or HiC or hic_scaffolding" 2>&1 && \
        pass "unit tests: test_run_config.py (Hi-C)" || fail "unit tests: test_run_config.py (Hi-C)"

    info "Running unit tests: test_ask_interactive.py"
    run_pytest tests/test_ask_interactive.py -v --tb=short 2>&1 && \
        pass "unit tests: test_ask_interactive.py" || fail "unit tests: test_ask_interactive.py"
    cd "${SCRIPT_DIR}"
fi

# ---- Schema validation: Hi-C RunConfig ----
cd "${PROJECT_ROOT}"
info "Verifying Hi-C schema additions"

python3 -c "
from app.schemas.run_config import RunConfig, HiCScaffoldingRunConfig
import json

# Test 1: Valid Hi-C config parses correctly
rc_data = {
    'local_exec': ['hic_scaffolding'],
    'hic_scaffolding': {
        'autohic_parameters': {
            'restriction_enzyme': '${HIC_RESTRICTION_ENZYME}',
            'threads': ${ASSEMBLY_THREADS},
        },
        'retry_on_execution_failure': True,
        'max_execution_retries': 1,
        'max_review_retries': 1,
    },
}
rc = RunConfig.model_validate(rc_data)
assert rc.hic_scaffolding is not None, 'hic_scaffolding config is None'
assert rc.hic_scaffolding.autohic_parameters.restriction_enzyme == '${HIC_RESTRICTION_ENZYME}'
assert rc.hic_scaffolding.retry_on_execution_failure is True
assert rc.hic_scaffolding.max_execution_retries == 1
assert rc.hic_scaffolding.max_review_retries == 1
assert 'methods' not in HiCScaffoldingRunConfig.model_fields
print('HiCScaffoldingRunConfig: fixed AutoHiC pipeline')
print('Hi-C RunConfig validation: PASS')
" 2>&1 && pass "HiCScaffoldingRunConfig schema validation" || fail "HiCScaffoldingRunConfig schema validation"

python3 -c "
from app.schemas.run_config import AutoHiCRunParameters
params = AutoHiCRunParameters()
assert params.restriction_enzyme is None
assert params.threads == 10
assert 'error_model_ref' not in AutoHiCRunParameters.model_fields
assert 'chromosome_model_ref' not in AutoHiCRunParameters.model_fields
print('AutoHiC runtime defaults and external-model contract: correct')
" 2>&1 && pass "AutoHiC runtime parameter contract" || fail "AutoHiC runtime parameter contract"

# The Hi-C stage rejects legacy method-selection fields.
python3 -c "
from app.schemas.run_config import HiCScaffoldingRunConfig
from pydantic import ValidationError
try:
    HiCScaffoldingRunConfig.model_validate({'methods': ['autohic']})
except ValidationError:
    print('legacy Hi-C method selection rejected')
else:
    raise AssertionError('legacy methods field must be rejected')
" 2>&1 && pass "fixed AutoHiC schema" || fail "fixed AutoHiC schema"

python3 -c "
from app.stages.hic_scaffolding.parameters.catalog import load_autohic_parameter_catalog
catalog = load_autohic_parameter_catalog()
dynamic = {name for name, spec in catalog.parameters.items() if spec.level == 2}
assert dynamic == {
    'restriction_enzyme',
    'number_of_edit_rounds',
    'translocation_adjust',
    'inversion_adjust',
    'debris_adjust',
    'error_max_epoch',
    'error_min_len',
    'error_max_len',
    'error_filter_score',
    'error_filter_iou_score',
}, dynamic
print('AutoHiC planner exposes the complete adjustable L2 catalog')
" 2>&1 && pass "AutoHiC planning contract" || fail "AutoHiC planning contract"

cd "${SCRIPT_DIR}"

# ---- Real Hi-C execution (if data available) ----
if [[ "$HIC_AVAILABLE" == "true" ]]; then
    info "Starting real Hi-C scaffolding test"

    # Prefer an explicitly configured assembly, otherwise use the preferred
    # artifact from the newest Test 04 session, then any existing assembly
    # artifact.
    PREV_ASM="${REVIEW_ASSEMBLY:-}"
    if [[ -z "$PREV_ASM" ]]; then
        PREV_ASM=$(latest_test04_assembly || true)
    fi
    if [[ -z "$PREV_ASM" ]]; then
        PREV_ASM=$(latest_hpc_assembly || true)
    fi
    if [[ -z "$PREV_ASM" ]]; then
        info "[SKIP] No assembly FASTA is available for Hi-C scaffolding."
        info "Resume the upstream assembly/review session first, or set REVIEW_ASSEMBLY."
        exit 77
    fi
    info "Using assembly: ${PREV_ASM}"

    if run_configured_method_tool \
        "hic_scaffolding" "hic_scaffolding.candidate.autohic" \
        python3.9 -c 'import torch, typer; print("AutoHiC Python dependencies available")'; then
        pass "AutoHiC Python environment available"
    else
        fail "AutoHiC Python dependencies are missing from the configured method environment"
        summary "Test 06: Hi-C Scaffolding (runtime preflight failed)" || true
        exit 1
    fi

    SID="hpc-test06-$(date +%Y%m%d-%H%M%S)"
    MANIFEST="${TEST_DATA_ROOT}/_test06_manifest.yaml"
    mkdir -p "${TEST_DATA_ROOT}"

    HIC_PATHS="${HIC_R1},${HIC_R2}"

    write_manifest "$MANIFEST" "$SID" \
        "hic_reads_registered:${HIC_PATHS}:illumina:paired" \
        "assembly_fasta_registered:${PREV_ASM}:unknown:unknown"

    # Write run-config with Hi-C
    RC="${TEST_DATA_ROOT}/_test06_rc.json"
    write_run_config_stages "$RC" "hic_scaffolding"
    python3 - "$RC" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)
config["requested_stages"] = ["hic_scaffolding"]
config["hic_scaffolding"] = {
    "autohic_parameters": {"threads": 10},
    "retry_on_execution_failure": False,
    "max_execution_retries": 0,
    "max_review_retries": 1,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
PY

    # Run
    cd "${PROJECT_ROOT}"
    info "Running Hi-C scaffolding session: ${SID}"
    RUN_EXITCODE=0
    if GA run \
        --manifest "$MANIFEST" \
        --session-id "$SID" \
        --data-dir "$TEST_DATA_ROOT" \
        --run-config "$RC" \
        --checkpointer "$CHECKPOINTER" \
        2>&1; then
        pass "Hi-C session invocation exited cleanly"
    else
        RUN_EXITCODE=$?
        warn "Hi-C invocation returned ${RUN_EXITCODE}; inspecting durable state"
    fi

    PARAMETER_RESUME_STATUS=0
    resume_configured_parameter_gates "$SID" || PARAMETER_RESUME_STATUS=$?
    if (( PARAMETER_RESUME_STATUS == 3 )); then
        info "[SKIP] Hi-C session is intentionally paused at parameter_approval."
        exit 77
    elif (( PARAMETER_RESUME_STATUS != 0 )); then
        fail "Hi-C parameter-approval handling failed"
    elif (( RUN_EXITCODE != 0 )) && (( ${HPC_PARAMETER_APPROVALS_RESUMED:-0} == 0 )); then
        fail "Hi-C invocation failed with no durable parameter gate resumed"
    fi

    GATE_STATUS=0
    report_session_gate "$SID" || GATE_STATUS=$?
    if (( GATE_STATUS == 3 )); then
        info "[SKIP] Hi-C session is waiting for its stage-specific human decision."
        exit 77
    elif (( GATE_STATUS != 0 )); then
        fail "could not determine the Hi-C durable session state"
    elif (( RUN_EXITCODE != 0 )); then
        fail "Hi-C session invocation returned a CLI error outside a human gate"
    fi

    # Verify outputs
    RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
    HIC_ARTIFACT="${RUN_DIR}/artifacts/hic_scaffolding/autohic"
    HIC_WORK="${RUN_DIR}/work/hic_scaffolding/autohic"

    assert_dir_exists "$HIC_ARTIFACT" "Hi-C artifact dir"

    # Shell script
    AUTOHIC_SCRIPT=$(find "$HIC_WORK" -name "run_autohic.sh" 2>/dev/null | head -1 || true)
    if [[ -n "$AUTOHIC_SCRIPT" ]]; then
        pass "AutoHiC shell script generated: $(basename "$AUTOHIC_SCRIPT")"
        assert_file_contains "$AUTOHIC_SCRIPT" "autohic.py.*-c.*cfg-autohic.txt" "script invokes AutoHiC with its config"
        assert_file_contains "$AUTOHIC_SCRIPT" "published artifact conflicts" "AutoHiC script rejects conflicting replay output"
    else
        fail "Hi-C shell script not found"
    fi

    # Output files
    SCAFFOLDED=$(find "$HIC_ARTIFACT" \( -name "*scaffolded*.fasta" -o -name "*FINAL*.fasta" \) 2>/dev/null | head -1 || true)
    HIC_FILE=$(find "$HIC_ARTIFACT" -name "*.hic" 2>/dev/null | head -1 || true)
    ASSEMBLY_FILE=$(find "$HIC_ARTIFACT" -name "*.assembly" 2>/dev/null | head -1 || true)
    HTML_REPORT=$(find "$HIC_ARTIFACT" -name "result.html" 2>/dev/null | head -1 || true)

    [[ -n "$SCAFFOLDED" ]] && assert_file_nonempty "$SCAFFOLDED" "scaffolded assembly FASTA" || fail "scaffolded FASTA not found"
    [[ -n "$HIC_FILE" ]]   && assert_file_nonempty "$HIC_FILE"   "Hi-C contact map (.hic)" || fail ".hic file not found"
    [[ -n "$ASSEMBLY_FILE" ]] && assert_file_nonempty "$ASSEMBLY_FILE" "Juicebox assembly file" || fail ".assembly file not found"
    [[ -n "$HTML_REPORT" ]] && assert_file_nonempty "$HTML_REPORT" "AutoHiC HTML report" || fail "result.html not found"

    # Inspect
    cd "${PROJECT_ROOT}"
    GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --trace 2>&1 && \
        pass "inspect Hi-C session" || fail "inspect Hi-C session"

    if [[ -f "${RUN_DIR}/ledger/events.jsonl" ]]; then
        assert_ledger_has_method_attempt "${RUN_DIR}/ledger/events.jsonl" "hic_scaffolding.candidate.autohic"
        assert_no_duplicate_method_attempts "${RUN_DIR}/ledger/events.jsonl" "Hi-C ledger has no duplicate attempts"
    else
        fail "Hi-C ledger events.jsonl not found"
    fi
else
    if (( _FAIL > 0 )); then
        summary "Test 06: Hi-C Scaffolding (contract checks failed)" || true
        exit 1
    fi
    info "[SKIP] Real AutoHiC execution requires paired Hi-C reads and both pretrained models."
    info "Schema and unit checks passed, but this step did not claim a real-data PASS."
    exit 77
fi

summary "Test 06: Hi-C Scaffolding"
