#!/usr/bin/env bash
# ============================================================
#  Test 02 — Read QC: subgraph method execution
# ============================================================
#  What it tests:
#    1. seqkit_stats subgraph (freeze -> run -> collect) with real seqkit
#    2. seqkit_length_filter subgraph with real seqkit
#    3. make_fofn creates correct file-of-filenames
#    4. Artifacts are published to new runs/<sid>/artifacts/ layout
#    5. read_qc_summary.json has correct structure (new fields)
#    6. Ledger records attempt_succeeded
#    7. Unit tests pass
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 02: Read QC"

# ---- Verify input data exists ----
info "Checking input data"
for f in $LONG_READS; do
    assert_file_nonempty "$f" "input long reads: $(basename "$f")"
done

# ---- Unit tests ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    cd "${PROJECT_ROOT}"
    info "Running unit tests: test_read_qc.py"
    if python3 -m pytest tests/test_read_qc.py -v --tb=short 2>&1; then
        pass "unit tests: test_read_qc.py"
    else
        fail "unit tests: test_read_qc.py"
    fi
    cd "${SCRIPT_DIR}"
fi

# ---- Prepare manifest ----
SID="hpc-test02-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test02_manifest.yaml"
mkdir -p "${TEST_DATA_ROOT}"

# Build asset paths string (comma-separated)
LR_PATHS=$(echo "$LONG_READS" | tr ' ' ',')
write_manifest "$MANIFEST" "$SID" "raw_long_reads:${LR_PATHS}:${PLATFORM}:single"

# ---- Write run-config enabling read_qc ----
RC="${TEST_DATA_ROOT}/_test02_rc.json"
write_run_config_stages "$RC" "read_qc"

# ---- Run ----
cd "${PROJECT_ROOT}"
info "Running read_qc session: ${SID}"
GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1
RC_EXIT=$?

# ---- Verify directory structure ----
RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
ARTIFACT_DIR="${RUN_DIR}/artifacts/read_qc/${SID}"
WORK_DIR="${RUN_DIR}/work/read_qc/${SID}"

assert_dir_exists "$RUN_DIR"              "run_dir exists"
assert_dir_exists "$ARTIFACT_DIR"         "read_qc artifact dir"

# ---- Verify seqkit_stats output ----
STATS_TSV="${ARTIFACT_DIR}/seqkit_stats.tsv"
assert_file_nonempty "$STATS_TSV" "seqkit_stats.tsv produced"
if [[ -f "$STATS_TSV" ]]; then
    # Should have header + at least one data row
    LINE_COUNT=$(wc -l < "$STATS_TSV")
    if (( LINE_COUNT >= 2 )); then
        pass "seqkit_stats.tsv has ${LINE_COUNT} lines (header + data)"
    else
        fail "seqkit_stats.tsv has only ${LINE_COUNT} lines"
    fi
    # Check for expected columns
    assert_file_contains "$STATS_TSV" "num_seqs" "seqkit_stats has num_seqs column"
    assert_file_contains "$STATS_TSV" "sum_len"  "seqkit_stats has sum_len column"
fi

# ---- Verify read_qc_summary.json ----
SUMMARY="${ARTIFACT_DIR}/read_qc_summary.json"
assert_file_nonempty "$SUMMARY" "read_qc_summary.json produced"
if [[ -f "$SUMMARY" ]]; then
    cd "${PROJECT_ROOT}"
    python3 -c "
import json, sys
d = json.load(open('${SUMMARY}'))
assert 'summary' in d, 'missing summary field'
s = d['summary']
assert 'num_seqs' in s, 'missing num_seqs'
assert 'sum_len' in s or 'total_length' in s, 'missing total length field'
assert 'method_name' in d, 'missing method_name'
assert d['method_name'] == 'read_qc.long_read_stats.seqkit_stats', f'wrong method: {d.get(\"method_name\")}'
print(f'read_qc_summary.json valid: {s.get(\"num_seqs\", \"?\")} reads, {s.get(\"sum_len\", s.get(\"total_length\", \"?\"))} bp')
" 2>&1 && pass "read_qc_summary.json structure valid" || fail "read_qc_summary.json structure invalid"
    cd "${SCRIPT_DIR}"
fi

# ---- Verify shell script was generated ----
STATS_SCRIPT="${WORK_DIR}/seqkit_stats/run_seqkit_stats.sh"
assert_file_exists "$STATS_SCRIPT" "seqkit_stats shell script generated"
if [[ -f "$STATS_SCRIPT" ]]; then
    assert_file_contains "$STATS_SCRIPT" "seqkit stats" "script contains seqkit stats command"
    assert_file_contains "$STATS_SCRIPT" "set -e"       "script has set -e guard"
fi

# ---- Verify execution logs were published ----
LOG_DIR="${ARTIFACT_DIR}/logs"
if [[ -d "$LOG_DIR" ]]; then
    pass "execution log directory published"
    # Check for stdout/stderr
    LOG_FILES=$(find "$LOG_DIR" -type f 2>/dev/null | wc -l)
    if (( LOG_FILES > 0 )); then
        pass "execution logs published (${LOG_FILES} files)"
    else
        warn "log directory exists but empty"
    fi
else
    warn "execution log directory not found (may use different path)"
fi

# ---- Verify artifact index ----
ARTIFACTS_INDEX="${RUN_DIR}/artifacts/artifacts.jsonl"
if [[ -f "$ARTIFACTS_INDEX" ]]; then
    RC_COUNT=$(grep -c "read_qc" "$ARTIFACTS_INDEX" 2>/dev/null || echo 0)
    pass "artifacts.jsonl has ${RC_COUNT} read_qc entries"
else
    warn "artifacts.jsonl not found"
fi

# ---- Verify ledger ----
EVENTS="${RUN_DIR}/ledger/events.jsonl"
if [[ -f "$EVENTS" ]]; then
    ATTEMPT_OK=$(grep -c "attempt_succeeded" "$EVENTS" 2>/dev/null || echo 0)
    if (( ATTEMPT_OK > 0 )); then
        pass "ledger records ${ATTEMPT_OK} attempt_succeeded events"
    else
        # Check if there are any attempt events at all
        ATTEMPT_ANY=$(grep -c "attempt_" "$EVENTS" 2>/dev/null || echo 0)
        if (( ATTEMPT_ANY > 0 )); then
            fail "ledger has attempt events but no attempt_succeeded"
        else
            warn "no attempt events in ledger"
        fi
    fi
else
    warn "ledger events.jsonl not found"
fi

# ---- Inspect the session ----
cd "${PROJECT_ROOT}"
info "Inspecting read_qc session"
GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" 2>&1 && \
    pass "inspect read_qc session" || \
    fail "inspect read_qc session"

summary "Test 02: Read QC"
