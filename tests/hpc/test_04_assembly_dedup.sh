#!/usr/bin/env bash
# ============================================================
#  Test 04 — Assembly + Assembly Dedup: new stage pipeline
# ============================================================
#  What it tests:
#    1. Assembly stage produces draft_assembly.fasta (real Flye/NextDenovo)
#    2. assembly_dedup stage runs purge_dups end-to-end
#    3. purge_dups shell script generation is correct
#    4. purged_assembly.fasta is produced and valid FASTA
#    5. purged_candidate.json bundle is correct
#    6. Artifacts published to new layout
#    7. assembly_dedup → assembly_review handoff
#    8. Unit tests pass
#
#  NOTE: This is the longest-running test (assembly can take hours).
#  For quick validation, subsample your reads first:
#    seqkit sample -p 0.1 long_reads.fq.gz > subsampled.fq.gz
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 04: Assembly + Assembly Dedup"

# ---- Unit tests ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    cd "${PROJECT_ROOT}"
    info "Running unit tests: test_assembly.py"
    python3 -m pytest tests/test_assembly.py -v --tb=short 2>&1 && \
        pass "unit tests: test_assembly.py" || fail "unit tests: test_assembly.py"
    info "Running unit tests: test_assembly_dedup.py"
    python3 -m pytest tests/test_assembly_dedup.py -v --tb=short 2>&1 && \
        pass "unit tests: test_assembly_dedup.py" || fail "unit tests: test_assembly_dedup.py"
    cd "${SCRIPT_DIR}"
fi

# ---- Schema check: assembly_dedup in RequestableStage ----
cd "${PROJECT_ROOT}"
python3 -c "
from app.schemas.run_config import RequestableStage
import typing
literals = typing.get_args(RequestableStage) if hasattr(typing, 'get_args') else []
assert 'assembly_dedup' in str(literals) or 'assembly_dedup' in str(RequestableStage), \
    f'assembly_dedup not in RequestableStage: {literals}'
print(f'RequestableStage values include assembly_dedup')
" 2>&1 && pass "assembly_dedup in RequestableStage" || fail "assembly_dedup missing from RequestableStage"

# ---- Schema check: assembly_dedup in DomainStage ----
python3 -c "
from app.schemas.state import DomainStage
import typing
vals = typing.get_args(DomainStage) if hasattr(typing, 'get_args') else []
assert 'assembly_dedup' in str(vals) or 'assembly_dedup' in str(DomainStage), \
    f'assembly_dedup not in DomainStage: {vals}'
print(f'DomainStage includes assembly_dedup')
" 2>&1 && pass "assembly_dedup in DomainStage" || fail "assembly_dedup missing from DomainStage"

# ---- Tool preflight ----
# Do this before creating a session or launching an expensive assembler.  A
# completed draft is not useful to this integration test unless purge_dups can
# also run, so missing tools are a fast, explicit test failure.
case "${ASSEMBLY_METHOD}" in
    flye|nextdenovo) ;;
    *)
        fail "unsupported ASSEMBLY_METHOD: ${ASSEMBLY_METHOD} (expected flye or nextdenovo)"
        summary "Test 04: Assembly + Assembly Dedup"
        exit 1
        ;;
esac

if [[ -f "${PROJECT_ROOT}/config/envs.local.yaml" ]]; then
    info "Preflighting with config/envs.local.yaml"
else
    info "Preflighting with config/envs.yaml"
fi
ASSEMBLER_METHOD="assembly.candidate.${ASSEMBLY_METHOD}"
if ! preflight_configured_methods \
    read_qc read_qc.long_read_stats.seqkit_stats \
    assembly "$ASSEMBLER_METHOD" \
    assembly_dedup assembly_dedup.purge_dups
then
    info "Fix the displayed environment target in config/envs.yaml, then install its missing tools."
    summary "Test 04: Assembly + Assembly Dedup (missing tools)"
    exit 1
fi

# ---- Prepare manifest ----
SID="hpc-test04-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test04_manifest.yaml"
mkdir -p "${TEST_DATA_ROOT}"

LR_PATHS=$(echo "$LONG_READS" | tr ' ' ',')
write_manifest "$MANIFEST" "$SID" "raw_long_reads:${LR_PATHS}:${PLATFORM}:single"

# ---- Write run-config enabling assembly + assembly_dedup ----
RC="${TEST_DATA_ROOT}/_test04_rc.json"
write_run_config_stages "$RC" "read_qc" "assembly"

# Note: assembly_dedup is not in LocalExecStage, it runs automatically
# when there is a materialized assembly and long reads.
# We don't add it to local_exec explicitly.

# ---- Run ----
cd "${PROJECT_ROOT}"
info "Running assembly + dedup session: ${SID}"
info "  Method: ${ASSEMBLY_METHOD}"
info "  This may take a while for real assembly..."
if GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1; then
    pass "assembly + dedup session completed without CLI error"
else
    fail "assembly + dedup session returned a CLI error"
fi

# ---- Verify assembly outputs ----
RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
ASM_ARTIFACT_DIR="${RUN_DIR}/artifacts/assembly/${SID}"
DEDUP_ARTIFACT_DIR="${RUN_DIR}/artifacts/assembly_dedup/${SID}"
DEDUP_WORK_DIR="${RUN_DIR}/work/assembly_dedup/${SID}"

# ---- Assembly stage outputs ----
info "Checking assembly outputs"
DRAFT_ASM="${ASM_ARTIFACT_DIR}/draft_assembly.fasta"
# The assembly might be under a different path structure, so search
if [[ ! -f "$DRAFT_ASM" ]]; then
    DRAFT_ASM=$(find "${RUN_DIR}/artifacts/assembly" -name "*.fasta" -o -name "*.fa" 2>/dev/null | head -1 || true)
fi

if [[ -n "$DRAFT_ASM" && -f "$DRAFT_ASM" ]]; then
    pass "draft assembly produced: $(basename "$DRAFT_ASM")"
    SEQ_COUNT=$(count_fasta_seqs "$DRAFT_ASM")
    TOTAL_BP=$(fasta_total_bp "$DRAFT_ASM")
    info "  Contigs: ${SEQ_COUNT}, Total: ${TOTAL_BP} bp"
    if (( SEQ_COUNT > 0 )); then
        pass "assembly has ${SEQ_COUNT} contigs"
    else
        fail "assembly has 0 contigs"
    fi
else
    fail "draft assembly FASTA not found"
    # Try to continue with dedup checks anyway
fi

# The storage refactor changed method scripts from cp to replay-safe publish.
ASM_SCRIPT=$(find "${RUN_DIR}/work/assembly/${SID}" -name "*.sh" -type f 2>/dev/null | head -1 || true)
if [[ -n "$ASM_SCRIPT" ]]; then
    pass "assembly method script generated: $(basename "$ASM_SCRIPT")"
    assert_file_contains "$ASM_SCRIPT" "published artifact conflicts" "assembly script rejects conflicting replay output"
else
    fail "assembly method script not found"
fi

# ---- Verify assembly candidate bundle ----
CANDIDATE_BUNDLE=$(find "${RUN_DIR}/artifacts/assembly" -name "*candidate*.json" 2>/dev/null | head -1 || true)
if [[ -n "$CANDIDATE_BUNDLE" && -f "$CANDIDATE_BUNDLE" ]]; then
    pass "assembly candidate bundle: $(basename "$CANDIDATE_BUNDLE")"
    cd "${PROJECT_ROOT}"
    python3 -c "
import json
d = json.load(open('${CANDIDATE_BUNDLE}'))
assert 'candidate_id' in d, 'missing candidate_id'
assert 'method_name' in d, 'missing method_name'
assert 'status' in d, 'missing status'
print(f'  candidate_id: {d[\"candidate_id\"]}')
print(f'  method: {d[\"method_name\"]}, status: {d[\"status\"]}')
outputs = d.get('expected_outputs', {})
fasta = outputs.get('draft_assembly_fasta', '')
print(f'  draft_assembly_fasta: {fasta}')
" 2>&1 && pass "candidate bundle structure valid" || fail "candidate bundle structure invalid"
    cd "${SCRIPT_DIR}"
else
    warn "assembly candidate bundle not found (may be at different path)"
fi

# ---- Assembly dedup outputs ----
info "Checking assembly_dedup outputs"

# purge_dups shell script
DEDUP_SCRIPT="${DEDUP_WORK_DIR}/run_purge_dups.sh"
if [[ -f "$DEDUP_SCRIPT" ]]; then
    pass "purge_dups shell script generated"
    # Verify script content
    assert_file_contains "$DEDUP_SCRIPT" "minimap2"    "script: minimap2 alignment"
    assert_file_contains "$DEDUP_SCRIPT" "samtools"    "script: samtools sort"
    assert_file_contains "$DEDUP_SCRIPT" "pbcstat"     "script: pbcstat"
    assert_file_contains "$DEDUP_SCRIPT" "calcuts"     "script: calcuts"
    assert_file_contains "$DEDUP_SCRIPT" "purge_dups"  "script: purge_dups"
    assert_file_contains "$DEDUP_SCRIPT" "get_seqs"    "script: get_seqs"
    assert_file_contains "$DEDUP_SCRIPT" "set -euo"    "script: strict mode"

    # Check FOFN handling
    assert_file_contains "$DEDUP_SCRIPT" "fofn\|mapfile" "script: FOFN-aware read loading"
else
    # dedup may have been skipped
    info "purge_dups script not found — checking if dedup was skipped"
fi

# Core output files
PURGED_ASM="${DEDUP_ARTIFACT_DIR}/purged_assembly.fasta"
READ_BAM="${DEDUP_ARTIFACT_DIR}/reads_to_raw_assembly.bam"
BASE_COV="${DEDUP_ARTIFACT_DIR}/PB.base.cov"
PB_STAT="${DEDUP_ARTIFACT_DIR}/PB.stat"
CUTOFFS="${DEDUP_ARTIFACT_DIR}/cutoffs"
SELF_PAF="${DEDUP_ARTIFACT_DIR}/self.paf.gz"
DUPS_BED="${DEDUP_ARTIFACT_DIR}/dups.bed"

assert_file_nonempty "$READ_BAM"    "minimap2 alignment BAM"
assert_file_nonempty "$BASE_COV"    "pbcstat base coverage"
assert_file_nonempty "$PB_STAT"     "pbcstat statistics"
assert_file_nonempty "$CUTOFFS"     "calcuts cutoffs"
assert_file_nonempty "$SELF_PAF"    "self-alignment PAF"
assert_file_nonempty "$DUPS_BED"    "purge_dups BED file"
assert_file_nonempty "$PURGED_ASM"  "purged assembly FASTA"

# ---- Validate purged assembly ----
if [[ -f "$PURGED_ASM" ]]; then
    PURGED_SEQS=$(count_fasta_seqs "$PURGED_ASM")
    PURGED_BP=$(fasta_total_bp "$PURGED_ASM")
    info "  Purged contigs: ${PURGED_SEQS}, Total: ${PURGED_BP} bp"
    if (( PURGED_SEQS > 0 )); then
        pass "purged assembly has ${PURGED_SEQS} contigs"
    else
        fail "purged assembly has 0 contigs"
    fi

    # Compare with original
    if [[ -f "$DRAFT_ASM" ]]; then
        ORIG_BP=$(fasta_total_bp "$DRAFT_ASM")
        if (( ORIG_BP > 0 )); then
            RATIO=$(python3 -c "print(f'{${PURGED_BP}/${ORIG_BP}*100:.1f}')")
            info "  Purged/original size ratio: ${RATIO}%"
            pass "purged assembly size comparison recorded"
        fi
    fi
fi

# ---- Purged candidate bundle ----
PURGED_BUNDLE="${DEDUP_ARTIFACT_DIR}/purged_candidate.json"
assert_file_nonempty "$PURGED_BUNDLE" "purged_candidate.json bundle"
if [[ -f "$PURGED_BUNDLE" ]]; then
    cd "${PROJECT_ROOT}"
    python3 -c "
import json
d = json.load(open('${PURGED_BUNDLE}'))
required = ['candidate_id', 'method_name', 'status', 'expected_outputs']
for k in required:
    assert k in d, f'missing field: {k}'
assert d['method_name'] == 'assembly_dedup.purge_dups', f'wrong method: {d[\"method_name\"]}'
assert d['status'] == 'executed', f'wrong status: {d[\"status\"]}'
outputs = d['expected_outputs']
assert 'draft_assembly_fasta' in outputs, 'missing draft_assembly_fasta in expected_outputs'
meta = d.get('metadata', {})
assert meta.get('cutoffs_strategy') == 'calcuts_auto', 'wrong cutoffs_strategy'
assert meta.get('get_seqs_exclude_haplotigs') == True, 'get_seqs_exclude_haplotigs should be True'
print(f'  candidate_id: {d[\"candidate_id\"]}')
print(f'  raw_candidate_ref: {meta.get(\"raw_candidate_ref\", \"N/A\")}')
print('purged_candidate.json: all fields valid')
" 2>&1 && pass "purged_candidate.json structure valid" || fail "purged_candidate.json structure invalid"
    cd "${SCRIPT_DIR}"
fi

# ---- Verify assembly_dedup → assembly_review handoff ----
info "Checking stage handoff"
cd "${PROJECT_ROOT}"
python3 -c "
import json, sys
# Look at the ledger to verify stage transitions
events_path = '${RUN_DIR}/ledger/events.jsonl'
try:
    events = [json.loads(line) for line in open(events_path)]
except:
    print('Could not read events')
    sys.exit(0)

# Check that assembly_dedup stage was entered
dedup_events = [e for e in events if 'assembly_dedup' in str(e)]
if dedup_events:
    print(f'Found {len(dedup_events)} assembly_dedup events in ledger')
else:
    print('No assembly_dedup events found (stage may have been skipped)')
" 2>&1

if [[ -f "${RUN_DIR}/ledger/events.jsonl" ]]; then
    assert_ledger_has_method_attempt "${RUN_DIR}/ledger/events.jsonl" "assembly.candidate.${ASSEMBLY_METHOD}"
    assert_ledger_has_method_attempt "${RUN_DIR}/ledger/events.jsonl" "assembly_dedup.purge_dups"
    assert_no_duplicate_method_attempts "${RUN_DIR}/ledger/events.jsonl" "assembly/dedup ledger has no duplicate attempts"
else
    fail "assembly/dedup ledger events.jsonl not found"
fi

# ---- Inspect ----
cd "${PROJECT_ROOT}"
GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --trace 2>&1 && \
    pass "inspect assembly+dedup session" || fail "inspect assembly+dedup session"

summary "Test 04: Assembly + Assembly Dedup"
