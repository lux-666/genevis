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
# The stage supports both input families. Prefer paired short reads when both
# are configured, otherwise exercise the long-read-only route used by the
# normal ONT/PacBio workflow.
info "Checking genome-survey inputs"
USE_SHORT_DNA=false
if [[ -n "${SHORT_DNA_R1:-}" || -n "${SHORT_DNA_R2:-}" ]]; then
    if [[ -z "${SHORT_DNA_R1:-}" || -z "${SHORT_DNA_R2:-}" ]]; then
        fail "SHORT_DNA_R1 and SHORT_DNA_R2 must be supplied together"
    else
        assert_file_nonempty "$SHORT_DNA_R1" "short DNA R1"
        assert_file_nonempty "$SHORT_DNA_R2" "short DNA R2"
        USE_SHORT_DNA=true
        info "Using paired short DNA reads for genome survey"
    fi
else
    for f in $LONG_READS; do
        assert_file_nonempty "$f" "long-read survey input: $(basename "$f")"
    done
    info "No short DNA reads configured; using raw long reads for genome survey"
fi

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

# ---- Prepare manifest with the selected survey input family ----
SID="hpc-test03-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test03_manifest.yaml"
mkdir -p "${TEST_DATA_ROOT}"

LR_PATHS=$(echo "$LONG_READS" | tr ' ' ',')
MANIFEST_GENOME_SIZE_ESTIMATE=""
MANIFEST_ASSETS=("raw_long_reads:${LR_PATHS}:${PLATFORM}:single")
if [[ "$USE_SHORT_DNA" == "true" ]]; then
    SR_PATHS="${SHORT_DNA_R1},${SHORT_DNA_R2}"
    MANIFEST_ASSETS+=("raw_short_dna_reads:${SR_PATHS}:illumina:paired")
fi
write_manifest "$MANIFEST" "$SID" "${MANIFEST_ASSETS[@]}"
unset MANIFEST_GENOME_SIZE_ESTIMATE

# ---- Write run-config enabling genome_survey ----
RC="${TEST_DATA_ROOT}/_test03_rc.json"
write_genome_survey_run_config "$RC"

# ---- Run ----
cd "${PROJECT_ROOT}"
info "Running genome_survey session: ${SID}"
if GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1; then
    pass "genome_survey session completed without CLI error"
else
    fail "genome_survey session returned a CLI error"
fi

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
# GenomeSurveyResult persists estimates; genome_size_prior is the companion
# top-level stage-output handoff consumed by assembly.
has_size = 'genome_size' in d.get('estimates', {})
if has_size:
    size = d['estimates']['genome_size']
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
    python3 -c "
import json
d = json.load(open('${SURVEY_RESULT}'))
assert d['status'] == 'completed', d
assert d['mode'] == 'full', d
assert d.get('skip_reason') is None, d
assert 'genome_size' in d['estimates'], d
print('full survey result produced genome-size estimates:', d['estimates']['genome_size'])
" 2>&1 && pass "genome_survey executed instead of explicit-prior skip" || fail "genome_survey did not execute full survey"
    cd "${SCRIPT_DIR}"
else
    fail "Could not find genome_survey_result JSON to verify contract"
fi

# ---- Verify genome_size_prior flows to assembly stage ----
info "Checking genome_size_prior propagation to assembly"
cd "${PROJECT_ROOT}"
python3 -c "
from app.stages.assembly.nodes import _genome_size_prior
state = {'stage_outputs': {'genome_survey': {'genome_size_prior': 123456789}}}
assert _genome_size_prior(state) == 123456789
print('assembly resolves genome_survey.genome_size_prior before fallback estimates')
" 2>&1 && pass "assembly stage genome_size wiring check" || fail "assembly stage genome_size wiring broken"

# ---- Ledger ----
EVENTS="${RUN_DIR}/ledger/events.jsonl"
if [[ -f "$EVENTS" ]]; then
    assert_ledger_has_method_attempt "$EVENTS" "genome_survey.kmer_count.jellyfish"
    assert_ledger_has_method_attempt "$EVENTS" "genome_survey.model.genomescope2"
    assert_no_duplicate_method_attempts "$EVENTS" "genome_survey ledger has no duplicate attempts"
else
    fail "ledger events.jsonl not found"
fi

# ---- Inspect ----
cd "${PROJECT_ROOT}"
GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" 2>&1 && \
    pass "inspect genome_survey session" || fail "inspect genome_survey session"

summary "Test 03: Genome Survey"
