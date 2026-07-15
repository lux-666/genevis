#!/usr/bin/env bash
# ============================================================
#  Test 05 — Assembly Review: subgraph refactor + related species
# ============================================================
#  What it tests:
#    1. QUAST subgraph (freeze -> run -> collect) with real quast.py
#    2. BUSCO subgraph with real busco
#    3. Related species sanity check (NCBI Metazoa reference)
#    4. Human gate correctly blocks for review
#    5. Review reports generated (JSON + Markdown)
#    6. gene-agent review interactive works
#    7. Durable decision-template generation and non-interactive review validation
#    8. Unit tests pass
#
#  Prerequisites:
#    - Test 04 must have produced a draft assembly, OR
#    - You have an existing assembly FASTA to use
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 05: Assembly Review"

# ---- Lightweight TUI/HPC smoke ----
cd "${PROJECT_ROOT}"
python3 -m app.entrypoints.run tui --help 2>&1 && \
    pass "tui entrypoint help" || fail "tui entrypoint help"
cd "${SCRIPT_DIR}"

# ---- Check for NCBI Metazoa reference data ----
info "Checking NCBI Metazoa reference data"
REF_DIR="${PROJECT_ROOT}/data/reference/ncbi_metazoa"
REFERENCE_READY=false
if [[ -f "${REF_DIR}/Metazoa.ncbi.tsv" && -f "${REF_DIR}/Metazoa.ncbi.lineage" \
    && -f "${REF_DIR}/species_alias.tsv" && -f "${REF_DIR}/reference_manifest.json" ]]; then
    assert_file_exists "${REF_DIR}/species_alias.tsv"     "species_alias.tsv"
    assert_file_exists "${REF_DIR}/reference_manifest.json" "reference_manifest.json"
    pass "NCBI Metazoa reference data is installed"
    REFERENCE_READY=true
elif [[ -n "${NCBI_REFERENCE_SOURCE_DIR:-}" && ! -d "${NCBI_REFERENCE_SOURCE_DIR}" ]]; then
    fail "NCBI_REFERENCE_SOURCE_DIR does not exist: ${NCBI_REFERENCE_SOURCE_DIR}"
    summary "Test 05: Assembly Review (invalid reference source)" || true
    exit 1
elif [[ -n "${NCBI_REFERENCE_SOURCE_DIR:-}" ]]; then
    info "Installing NCBI Metazoa reference from: ${NCBI_REFERENCE_SOURCE_DIR}"
    cd "${PROJECT_ROOT}"
    if python3 scripts/install_ncbi_metazoa_reference.py --source-dir "${NCBI_REFERENCE_SOURCE_DIR}" 2>&1; then
        pass "NCBI Metazoa reference installed and verified"
        REFERENCE_READY=true
    else
        fail "NCBI Metazoa reference install/verification failed"
    fi
    cd "${SCRIPT_DIR}"
else
    warn "NCBI Metazoa reference data is unavailable; set NCBI_REFERENCE_SOURCE_DIR to run related-species checks"
fi

# ---- Unit tests ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    cd "${PROJECT_ROOT}"
    info "Running unit tests: test_assembly_review.py"
    run_pytest tests/test_assembly_review.py -v --tb=short 2>&1 && \
        pass "unit tests: test_assembly_review.py" || fail "unit tests: test_assembly_review.py"

    if [[ "${REFERENCE_READY}" == "true" ]]; then
        info "Running unit tests: test_related_species_evidence.py"
        run_pytest tests/test_related_species_evidence.py -v --tb=short 2>&1 && \
            pass "unit tests: test_related_species_evidence.py" || fail "unit tests: test_related_species_evidence.py"
    else
        warn "Skipping test_related_species_evidence.py because the pinned NCBI snapshot is unavailable"
    fi

    info "Running unit tests: test_review_recommender.py"
    run_pytest tests/test_review_recommender.py -v --tb=short 2>&1 && \
        pass "unit tests: test_review_recommender.py" || fail "unit tests: test_review_recommender.py"
    cd "${SCRIPT_DIR}"
fi

# ---- Determine assembly to review ----
# Prefer an explicitly configured review assembly; otherwise use the preferred
# artifact from the newest Test 04 session, then any existing assembly artifact.
PREV_ASM="${REVIEW_ASSEMBLY:-}"
if [[ -n "$PREV_ASM" && ! -s "$PREV_ASM" ]]; then
    fail "REVIEW_ASSEMBLY is missing or empty: ${PREV_ASM}"
    summary "Test 05: Assembly Review (invalid REVIEW_ASSEMBLY)" || true
    exit 1
fi
if [[ -n "$PREV_ASM" ]] && ! grep -q '^>' "$PREV_ASM" 2>/dev/null; then
    fail "REVIEW_ASSEMBLY is not a FASTA file: ${PREV_ASM}"
    summary "Test 05: Assembly Review (invalid REVIEW_ASSEMBLY)" || true
    exit 1
fi
if [[ -z "$PREV_ASM" ]]; then
    PREV_ASM=$(latest_test04_assembly || true)
fi
# Fall back to another validated smoke/E2E assembly.
if [[ -z "$PREV_ASM" ]]; then
    PREV_ASM=$(latest_hpc_assembly || true)
fi

if [[ -z "$PREV_ASM" || ! -s "$PREV_ASM" ]] || ! grep -q '^>' "$PREV_ASM" 2>/dev/null; then
    info "[SKIP] No assembly FASTA is available yet."
    info "Test 04 may be waiting at a human gate; resume that session first, or set REVIEW_ASSEMBLY."
    exit 77
fi
info "Using assembly: ${PREV_ASM}"
SEQ_COUNT=$(count_fasta_seqs "$PREV_ASM")
info "  Contigs: ${SEQ_COUNT}"

# ---- Prepare review session ----
SID="hpc-test05-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${TEST_DATA_ROOT}"

# A fresh session cannot see Test 04's stage outputs.  Register only the
# existing assembly so inventory selects its existing-assembly shortcut and
# reaches assembly_review directly; adding raw reads would instead re-enter
# the assembly route-selection gate.
MANIFEST="${TEST_DATA_ROOT}/_test05_manifest.yaml"
write_manifest "$MANIFEST" "$SID" "assembly_fasta_registered:${PREV_ASM}:unknown:unknown"

# ---- Write run-config ----
RC="${TEST_DATA_ROOT}/_test05_rc.json"
write_run_config_stages "$RC" "assembly_review"

# ---- Run (expect it to block at human gate) ----
cd "${PROJECT_ROOT}"
info "Running assembly_review session: ${SID}"
info "  (Expected: block at human review gate)"
GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1 || true   # May exit non-zero due to human gate block

# ---- Verify outputs ----
RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
ARTIFACT_DIR="${RUN_DIR}/artifacts/assembly_review/${SID}"
WORK_DIR="${RUN_DIR}/work/assembly_review/${SID}"

assert_dir_exists "$ARTIFACT_DIR" "assembly_review artifact dir"

# ---- QUAST outputs ----
info "Checking QUAST subgraph outputs"
QUAST_DIR=$(find "$ARTIFACT_DIR" -name "quast*" -type d 2>/dev/null | head -1 || true)
if [[ -z "$QUAST_DIR" ]]; then
    QUAST_DIR=$(find "$WORK_DIR" -name "quast*" -type d 2>/dev/null | head -1 || true)
fi
if [[ -n "$QUAST_DIR" && -d "$QUAST_DIR" ]]; then
    pass "QUAST output directory found"
    # Check for typical QUAST outputs
    for f in "report.txt" "report.tsv" "transposed_report.tsv"; do
        if [[ -f "${QUAST_DIR}/${f}" ]]; then
            pass "QUAST output: ${f}"
        fi
    done
else
    fail "QUAST output directory not found"
fi

# QUAST shell script
QUAST_SCRIPT=$(find "$WORK_DIR" -name "*quast*.sh" 2>/dev/null | head -1 || true)
if [[ -n "$QUAST_SCRIPT" && -f "$QUAST_SCRIPT" ]]; then
    pass "QUAST shell script generated"
    assert_file_contains "$QUAST_SCRIPT" "quast" "script contains quast command"
else
    fail "QUAST shell script not found"
fi

# ---- BUSCO outputs ----
info "Checking BUSCO subgraph outputs"
BUSCO_DIR=$(find "$ARTIFACT_DIR" -name "busco*" -type d 2>/dev/null | head -1 || true)
if [[ -z "$BUSCO_DIR" ]]; then
    BUSCO_DIR=$(find "$WORK_DIR" -name "busco*" -type d 2>/dev/null | head -1 || true)
fi
if [[ -n "$BUSCO_DIR" && -d "$BUSCO_DIR" ]]; then
    pass "BUSCO output directory found"
    # Check for short_summary
    SUMMARY_FILE=$(find "$BUSCO_DIR" -name "short_summary*" -o -name "*summary*.txt" 2>/dev/null | head -1 || true)
    if [[ -n "$SUMMARY_FILE" ]]; then
        pass "BUSCO summary file: $(basename "$SUMMARY_FILE")"
    fi
else
    fail "BUSCO output directory not found"
fi

# ---- Eval bundle ----
EVAL_BUNDLE=$(find "$ARTIFACT_DIR" -name "assembly_eval_bundle*.json" 2>/dev/null | head -1 || true)
if [[ -n "$EVAL_BUNDLE" && -f "$EVAL_BUNDLE" ]]; then
    pass "assembly eval bundle produced"
    cd "${PROJECT_ROOT}"
    python3 -c "
import json
d = json.load(open('${EVAL_BUNDLE}'))
print(f'  Eval bundle keys: {list(d.keys())[:10]}')
signals = d.get('signals') or d.get('eval_signals') or {}
print(f'  Signals: {list(signals.keys()) if isinstance(signals, dict) else len(signals)} entries')
" 2>&1 && pass "eval bundle parseable" || warn "eval bundle parse error"
    cd "${SCRIPT_DIR}"
fi

# ---- Related species evidence pack ----
info "Checking related species sanity check"
REL_SPECIES=$(find "$ARTIFACT_DIR" -name "related_species_evidence*" 2>/dev/null | head -1 || true)
if [[ -n "$REL_SPECIES" && -f "$REL_SPECIES" ]]; then
    pass "related_species_evidence_pack produced"
    cd "${PROJECT_ROOT}"
    python3 -c "
import json
d = json.load(open('${REL_SPECIES}'))
print(f'  Evidence pack keys: {list(d.keys())[:10]}')
assert d.get('target', {}).get('input_name') == '${SPECIES}', d.get('target')
evidence = d.get('evidence') or d.get('related_species') or d.get('matches') or []
if isinstance(evidence, list):
    print(f'  Found {len(evidence)} related species entries')
    for e in evidence[:3]:
        if isinstance(e, dict):
            name = e.get('species_name') or e.get('name') or e.get('organism_name', '?')
            print(f'    - {name}')
elif isinstance(evidence, dict):
    print(f'  Evidence keys: {list(evidence.keys())[:5]}')
" 2>&1 && pass "related species evidence pack parseable" || warn "evidence pack parse issue"
    cd "${SCRIPT_DIR}"
else
    fail "related_species_evidence_pack not found"
fi

# ---- Review reports ----
info "Checking review reports"
REVIEW_JSON=$(find "$ARTIFACT_DIR" -name "assembly_review_report*.json" 2>/dev/null | head -1 || true)
REVIEW_MD=$(find "$ARTIFACT_DIR" -name "assembly_review_report*.md" 2>/dev/null | head -1 || true)
REVIEW_REQ=$(find "$ARTIFACT_DIR" -name "assembly_review_request*.json" 2>/dev/null | head -1 || true)

[[ -n "$REVIEW_JSON" ]] && assert_file_nonempty "$REVIEW_JSON" "review report JSON" || fail "review report JSON not found"
[[ -n "$REVIEW_MD" ]]   && assert_file_nonempty "$REVIEW_MD"   "review report Markdown" || fail "review report Markdown not found"
[[ -n "$REVIEW_REQ" ]]  && assert_file_nonempty "$REVIEW_REQ"  "review request JSON" || fail "review request JSON not found"

# ---- Human gate: verify pipeline blocked ----
info "Checking human gate block"
EVENTS="${RUN_DIR}/ledger/events.jsonl"
if [[ -f "$EVENTS" ]]; then
    BLOCKED=$(grep -c "blocked_human\|human_gate\|interrupt" "$EVENTS" 2>/dev/null || echo 0)
    if (( BLOCKED > 0 )); then
        pass "pipeline correctly blocked at human gate (${BLOCKED} events)"
    else
        fail "no blocked_human events found"
    fi
    assert_ledger_has_method_attempt "$EVENTS" "assembly_review.eval.quast"
    assert_ledger_has_method_attempt "$EVENTS" "assembly_review.eval.busco"
    assert_no_duplicate_method_attempts "$EVENTS" "assembly-review ledger has no duplicate attempts"
else
    fail "assembly-review ledger events.jsonl not found"
fi

# ---- Test inspect with --write-decision-template ----
info "Testing decision template generation"
cd "${PROJECT_ROOT}"
DECISION_TEMPLATE="${TEST_DATA_ROOT}/_test05_decision_template.json"
GA inspect \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --write-decision-template "$DECISION_TEMPLATE" \
    2>&1 || true

if [[ -f "$DECISION_TEMPLATE" ]]; then
    pass "decision template written"
    python3 -c "
import json
d = json.load(open('${DECISION_TEMPLATE}'))
print(f'  Template keys: {list(d.keys())}')
print(f'  Template content preview: {json.dumps(d, indent=2)[:500]}')
" 2>&1 && pass "decision template is valid JSON" || fail "decision template invalid JSON"
else
    warn "decision template not written (inspect may not support --write-decision-template for this state)"
fi

# ---- Test gene-agent review --dry-run ----
info "Testing review dry-run"
cd "${PROJECT_ROOT}"
if printf '1\n\n' | python3 -m app.entrypoints.run review \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --dry-run \
    2>&1; then
    pass "review --dry-run accepted a deterministic test decision"
else
    fail "review --dry-run failed for the blocked assembly_review gate"
fi

info "Manual TUI smoke step: run 'gene-agent tui --resume-session ${SID} --data-dir ${TEST_DATA_ROOT}', drive the blocked review gate, and record the operator decision."

# ---- Inspect with trace ----
cd "${PROJECT_ROOT}"
GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --trace 2>&1 && \
    pass "inspect with trace" || fail "inspect with trace"

summary "Test 05: Assembly Review"
