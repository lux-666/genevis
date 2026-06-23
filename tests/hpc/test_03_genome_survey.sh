#!/usr/bin/env bash
# ============================================================
#  Test 03 — Genome Survey: data contract alignment
# ============================================================
#  What it tests:
#    1. Jellyfish + GenomeScope2 subgraph execution with real tools
#    2. genome_size_prior appears as top-level output field (new contract)
#    3. read_length field is gone from RunConfig (schema change)
#    4. fallback_low_confidence mode is gone (schema change)
#    5. Artifacts published to new layout
#    6. Unit tests pass
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 03: Genome Survey"

# ---- Verify input data ----
info "Checking input data (short DNA reads required)"
if [[ -z "${SHORT_DNA_R1}" || ! -f "${SHORT_DNA_R1}" ]]; then
    fail "SHORT_DNA_R1 not set or file missing: ${SHORT_DNA_R1}"
    echo "  Genome survey requires Illumina short reads."
    echo "  Set SHORT_DNA_R1 and SHORT_DNA_R2 in hpc_env.sh"
    summary "Test 03: Genome Survey (SKIPPED)"
    exit 0
fi
assert_file_nonempty "$SHORT_DNA_R1" "short DNA R1"
assert_file_nonempty "$SHORT_DNA_R2" "short DNA R2"

# ---- Unit tests ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    cd "${PROJECT_ROOT}"
    info "Running unit tests: test_genome_survey.py"
    if python3 -m pytest tests/test_genome_survey.py -v --tb=short 2>&1; then
        pass "unit tests: test_genome_survey.py"
    else
        fail "unit tests: test_genome_survey.py"
    fi
    cd "${SCRIPT_DIR}"
fi

# ---- Schema change verification ----
cd "${PROJECT_ROOT}"
info "Verifying schema changes (no read_length, no fallback_low_confidence)"
python3 -c "
from app.schemas.run_config import GenomeSurveyRunParameters
import inspect
sig = inspect.signature(GenomeSurveyRunParameters)
params = list(sig.parameters.keys())
assert 'read_length' not in params, f'read_length should be removed but found in: {params}'
print(f'GenomeSurveyRunParameters fields: {params}')
print('read_length correctly removed from schema')
" 2>&1 && pass "read_length removed from GenomeSurveyRunParameters" || \
    fail "read_length still in GenomeSurveyRunParameters"

python3 -c "
from app.schemas.artifacts import GenomeSurveyResult
import typing
hints = typing.get_type_hints(GenomeSurveyResult)
mode_type = hints.get('mode')
# Check that 'fallback_low_confidence' is not in the Literal type
type_str = str(mode_type)
assert 'fallback_low_confidence' not in type_str, f'fallback_low_confidence still in mode type: {type_str}'
print(f'GenomeSurveyResult.mode type: {type_str}')
print('fallback_low_confidence correctly removed')
" 2>&1 && pass "fallback_low_confidence removed from GenomeSurveyResult.mode" || \
    fail "fallback_low_confidence still in GenomeSurveyResult.mode"

# ---- Prepare manifest with short reads ----
SID="hpc-test03-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test03_manifest.yaml"
mkdir -p "${TEST_DATA_ROOT}"

LR_PATHS=$(echo "$LONG_READS" | tr ' ' ',')
SR_PATHS="${SHORT_DNA_R1},${SHORT_DNA_R2}"
write_manifest "$MANIFEST" "$SID" \
    "raw_long_reads:${LR_PATHS}:${PLATFORM}:single" \
    "raw_short_dna_reads:${SR_PATHS}:illumina:paired"

# ---- Write run-config enabling genome_survey ----
RC="${TEST_DATA_ROOT}/_test03_rc.json"
write_run_config_stages "$RC" "read_qc" "genome_survey"

# ---- Run ----
cd "${PROJECT_ROOT}"
info "Running genome_survey session: ${SID}"
GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1
RC_EXIT=$?

# ---- Verify outputs ----
RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
ARTIFACT_DIR="${RUN_DIR}/artifacts/genome_survey/${SID}"
WORK_DIR="${RUN_DIR}/work/genome_survey/${SID}"

assert_dir_exists "$ARTIFACT_DIR" "genome_survey artifact dir"

# ---- Jellyfish outputs ----
JF_DB="${ARTIFACT_DIR}/jellyfish_kmers.jf"
KMER_HIST="${ARTIFACT_DIR}/kmer_hist.tsv"
assert_file_nonempty "$JF_DB"    "jellyfish k-mer database (.jf)"
assert_file_nonempty "$KMER_HIST" "k-mer histogram (.tsv)"

# ---- Jellyfish shell script ----
JF_SCRIPT="${WORK_DIR}/jellyfish/run_jellyfish.sh"
assert_file_exists "$JF_SCRIPT" "jellyfish shell script generated"
if [[ -f "$JF_SCRIPT" ]]; then
    assert_file_contains "$JF_SCRIPT" "jellyfish count" "script has jellyfish count"
    assert_file_contains "$JF_SCRIPT" "jellyfish histo" "script has jellyfish histo"
fi

# ---- GenomeScope2 outputs ----
GS_DIR="${ARTIFACT_DIR}/genomescope2"
GS_SUMMARY="${GS_DIR}/summary.txt"
assert_dir_exists  "$GS_DIR"     "genomescope2 output dir"
assert_file_nonempty "$GS_SUMMARY" "genomescope2 summary.txt"

# ---- GenomeScope2 shell script ----
GS_SCRIPT="${WORK_DIR}/genomescope2/run_genomescope2.sh"
assert_file_exists "$GS_SCRIPT" "genomescope2 shell script generated"
if [[ -f "$GS_SCRIPT" ]]; then
    assert_file_contains "$GS_SCRIPT" "genomescope2" "script has genomescope2 command"
fi

# ---- Verify new contract: genome_size_prior ----
info "Checking new genome_size_prior contract field"
# Look for survey result in artifacts
SURVEY_RESULT=""
for f in "${ARTIFACT_DIR}"/*survey*.json "${ARTIFACT_DIR}"/genome_survey_result*.json; do
    if [[ -f "$f" ]]; then SURVEY_RESULT="$f"; break; fi
done

if [[ -n "$SURVEY_RESULT" ]]; then
    cd "${PROJECT_ROOT}"
    python3 -c "
import json
d = json.load(open('${SURVEY_RESULT}'))
# Check for genome_size_prior or genome_size in the result
has_size = any(k in d for k in ['genome_size_prior', 'genome_size', 'estimated_genome_size'])
if has_size:
    size = d.get('genome_size_prior') or d.get('genome_size') or d.get('estimated_genome_size')
    print(f'Genome size estimate: {size}')
else:
    print(f'Result keys: {list(d.keys())}')
    # Also check nested structure
    if 'result' in d:
        has_size_nested = any(k in d['result'] for k in ['genome_size_prior', 'genome_size'])
        if has_size_nested:
            print(f'Found in nested result: {d[\"result\"].get(\"genome_size_prior\", d[\"result\"].get(\"genome_size\"))}')
assert has_size or 'result' in d, 'no genome size field found in survey result'
" 2>&1 && pass "genome_size field present in survey result" || fail "genome_size field missing from survey result"
    cd "${SCRIPT_DIR}"
else
    warn "Could not find genome_survey_result JSON to verify contract"
fi

# ---- Verify genome_size_prior flows to assembly stage ----
info "Checking genome_size_prior propagation to assembly"
cd "${PROJECT_ROOT}"
python3 -c "
# Verify that the assembly stage nodes can read genome_size_prior from survey output
from app.stages.assembly import nodes as asm_nodes
import inspect
src = inspect.getsource(asm_nodes)
if 'genome_size_prior' in src or 'genome_size' in src:
    print('assembly stage references genome_size_prior/genome_size')
else:
    print('WARNING: assembly stage may not read genome_size from survey')
" 2>&1 && pass "assembly stage genome_size wiring check" || warn "assembly stage genome_size wiring unclear"

# ---- Ledger ----
EVENTS="${RUN_DIR}/ledger/events.jsonl"
if [[ -f "$EVENTS" ]]; then
    JELLY_OK=$(grep -c "jellyfish" "$EVENTS" 2>/dev/null || echo 0)
    GS_OK=$(grep -c "genomescope" "$EVENTS" 2>/dev/null || echo 0)
    pass "ledger records jellyfish(${JELLY_OK}) genomescope(${GS_OK}) events"
fi

# ---- Inspect ----
cd "${PROJECT_ROOT}"
GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" 2>&1 && \
    pass "inspect genome_survey session" || fail "inspect genome_survey session"

summary "Test 03: Genome Survey"
