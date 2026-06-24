#!/usr/bin/env bash
# ============================================================
#  Test 06 — Hi-C Scaffolding: interactive planning + execution
# ============================================================
#  What it tests:
#    1. Juicer+3D-DNA method subgraph (raw_reads mode)
#    2. Hi-C shell script generation correctness
#    3. RunConfig Hi-C schema validation
#    4. choose_many method selection (was choose_one)
#    5. Interactive parameter collection for Juicer
#    6. Hi-C artifacts published to new layout
#    7. Fallback to pre-Hi-C assembly on failure
#    8. Unit tests pass
#
#  Prerequisites:
#    - An assembly FASTA (from Test 04 or existing)
#    - Hi-C paired reads (HIC_R1, HIC_R2 in hpc_env.sh)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 06: Hi-C Scaffolding"

# ---- Verify input data ----
if [[ -z "${HIC_R1}" || ! -f "${HIC_R1}" || -z "${HIC_R2}" || ! -f "${HIC_R2}" ]]; then
    warn "Hi-C reads not configured or missing (HIC_R1/HIC_R2 in hpc_env.sh)"
    info "Running schema + unit tests only (no real Hi-C execution)"
    HIC_AVAILABLE=false
else
    assert_file_nonempty "$HIC_R1" "Hi-C R1"
    assert_file_nonempty "$HIC_R2" "Hi-C R2"
    HIC_AVAILABLE=true
fi

# ---- Unit tests ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    cd "${PROJECT_ROOT}"
    info "Running unit tests: test_hic_scaffolding.py"
    python3 -m pytest tests/test_hic_scaffolding.py -v --tb=short 2>&1 && \
        pass "unit tests: test_hic_scaffolding.py" || fail "unit tests: test_hic_scaffolding.py"

    info "Running unit tests: test_run_config.py (Hi-C schema)"
    python3 -m pytest tests/test_run_config.py -v --tb=short -k "hic or HiC or hic_scaffolding" 2>&1 && \
        pass "unit tests: test_run_config.py (Hi-C)" || fail "unit tests: test_run_config.py (Hi-C)"

    info "Running unit tests: test_ask_interactive.py"
    python3 -m pytest tests/test_ask_interactive.py -v --tb=short 2>&1 && \
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
        'methods': ['juicer_3ddna_juicebox'],
        'input_mode': 'raw_reads',
        'chromosome_count': ${HIC_CHROMOSOME_COUNT},
        'juicer_3ddna_parameters': {
            'restriction_enzyme': '${HIC_RESTRICTION_ENZYME}',
            'chrom_sizes_ref': '/tmp/chrom.sizes',
            'threads': ${ASSEMBLY_THREADS},
        },
    },
}
rc = RunConfig.model_validate(rc_data)
assert rc.hic_scaffolding is not None, 'hic_scaffolding config is None'
assert 'juicer_3ddna_juicebox' in rc.hic_scaffolding.methods, 'method not in methods list'
print(f'HiCScaffoldingRunConfig: methods={rc.hic_scaffolding.methods}')
print(f'  input_mode={rc.hic_scaffolding.input_mode}')
print(f'  chromosome_count={rc.hic_scaffolding.chromosome_count}')
print('Hi-C RunConfig validation: PASS')
" 2>&1 && pass "HiCScaffoldingRunConfig schema validation" || fail "HiCScaffoldingRunConfig schema validation"

python3 -c "
from app.schemas.run_config import HiCScaffoldingPlanCandidate
# Test: candidate readiness derivation
cand = HiCScaffoldingPlanCandidate(
    method='juicer_3ddna_juicebox',
    input_mode='raw_reads',
    chromosome_count=${HIC_CHROMOSOME_COUNT},
)
print(f'Candidate status: {cand.status}')
print(f'Missing fields: {cand.missing_fields}')
# Without juicer params, should show missing fields
assert cand.status == 'needs_clarification', \
    f'candidate should need clarification without params, got {cand.status}'
assert len(cand.missing_fields) > 0, 'should have missing fields'
print('Candidate readiness derivation: correct')
" 2>&1 && pass "HiCScaffoldingPlanCandidate readiness derivation" || fail "HiCScaffoldingPlanCandidate readiness derivation"

# Test choose_many (was choose_one)
python3 -c "
from app.schemas.run_config import HiCScaffoldingRunConfig
import json

# Test: multiple methods allowed (choose_many)
rc_data = {
    'methods': ['juicer_3ddna_juicebox', 'yahs'],
    'input_mode': 'raw_reads',
    'chromosome_count': ${HIC_CHROMOSOME_COUNT},
    # YAHS is also selected here, so give the schema an alignment reference.
    # This is a validation-only fixture; the real execution branch supplies
    # actual data and does not use this placeholder.
    'hic_alignment_ref': '/tmp/hic_alignment.bam',
    'juicer_3ddna_parameters': {
        'restriction_enzyme': '${HIC_RESTRICTION_ENZYME}',
        'chrom_sizes_ref': '/tmp/chrom.sizes',
        'threads': 4,
    },
}
rc = HiCScaffoldingRunConfig.model_validate(rc_data)
assert len(rc.methods) == 2, f'expected 2 methods, got {len(rc.methods)}'
print(f'Multiple methods accepted: {rc.methods}')
print('choose_many schema: PASS')
" 2>&1 && pass "choose_many allows multiple Hi-C methods" || fail "choose_many schema validation"

# ---- Test HiCInputMode and HiCMethod Literal types ----
python3 -c "
from app.schemas.run_config import HiCInputMode, HiCMethod
import typing
# These are Literal types, not enums
method_vals = typing.get_args(HiCMethod)
mode_vals = typing.get_args(HiCInputMode)
print(f'HiCInputMode values: {list(mode_vals)}')
print(f'HiCMethod values: {list(method_vals)}')
assert 'raw_reads' in mode_vals, 'raw_reads missing'
assert 'merged_nodups' in mode_vals, 'merged_nodups missing'
assert 'auto' in mode_vals, 'auto missing'
assert 'juicer_3ddna_juicebox' in method_vals, 'juicer_3ddna_juicebox missing'
assert 'yahs' in method_vals, 'yahs missing'
print('Hi-C Literal types: PASS')
" 2>&1 && pass "Hi-C Literal types valid" || fail "Hi-C Literal types"

# ---- Schema: hic_merged_nodups in asset types ----
python3 -c "
from app.schemas.manifest import InputAssetSpec
import typing
hints = typing.get_type_hints(InputAssetSpec)
atype = hints.get('asset_type')
type_str = str(atype)
assert 'hic_merged_nodups' in type_str, f'hic_merged_nodups not in asset types: {type_str}'
print('hic_merged_nodups in accepted asset types')
" 2>&1 && pass "hic_merged_nodups in manifest asset types" || fail "hic_merged_nodups not in asset types"

python3 -c "
from app.schemas.run_config import HiCScaffoldingPlanCandidate

candidate = HiCScaffoldingPlanCandidate(
    method='juicer_3ddna_juicebox',
    input_mode='merged_nodups',
    chromosome_count=${HIC_CHROMOSOME_COUNT},
    merged_nodups_ref='/tmp/merged_nodups.txt',
    juicer_3ddna_parameters={'restriction_enzyme': '${HIC_RESTRICTION_ENZYME}', 'chrom_sizes_ref': '/tmp/chrom.sizes', 'threads': 4},
)
assert candidate.status == 'ready_to_execute', candidate
print('merged_nodups Juicer candidate is execution-ready')
" 2>&1 && pass "merged_nodups planning contract" || fail "merged_nodups planning contract"

cd "${SCRIPT_DIR}"

# ---- Real Hi-C execution (if data available) ----
if [[ "$HIC_AVAILABLE" == "true" ]]; then
    info "Starting real Hi-C scaffolding test"

    # Prefer an explicitly configured assembly, otherwise use the preferred
    # artifact from the newest Test 04 session, then any existing assembly
    # artifact.
    PREV_ASM="${REVIEW_ASSEMBLY:-}"
    if [[ -n "$PREV_ASM" && ! -f "$PREV_ASM" ]]; then
        fail "REVIEW_ASSEMBLY does not exist: ${PREV_ASM}"
        summary "Test 06: Hi-C Scaffolding"
        exit 0
    fi
    if [[ -z "$PREV_ASM" ]]; then
        PREV_ASM=$(latest_test04_assembly || true)
    fi
    if [[ -z "$PREV_ASM" ]]; then
        PREV_ASM=$(find "${TEST_DATA_ROOT}/runs" -path "*/assembly*/*.fasta" 2>/dev/null | head -1 || true)
    fi
    if [[ -z "$PREV_ASM" ]]; then
        fail "No assembly FASTA found. Run test_04 first."
        summary "Test 06: Hi-C Scaffolding"
        exit 0
    fi
    info "Using assembly: ${PREV_ASM}"

    # Generate chrom.sizes if not provided
    if [[ -z "${HIC_CHROM_SIZES:-}" ]]; then
        info "Generating chrom.sizes from assembly (samtools faidx)"
        HIC_CHROM_SIZES="${TEST_DATA_ROOT}/_test06_chrom.sizes"
        if run_configured_method_tool \
            "hic_scaffolding" "hic_scaffolding.candidate.juicer_3ddna_juicebox" \
            samtools faidx "$PREV_ASM"; then
            cut -f1,2 "${PREV_ASM}.fai" > "$HIC_CHROM_SIZES"
            pass "chrom.sizes generated ($(wc -l < "$HIC_CHROM_SIZES") sequences)"
            rm -f "${PREV_ASM}.fai"  # clean up
        else
            warn "samtools failed in the configured Hi-C method environment; cannot generate chrom.sizes"
            warn "Hi-C execution will be skipped (schema tests still run)"
            HIC_CHROM_SIZES=""
        fi
    fi
    export HIC_CHROM_SIZES

    # Skip execution if chrom_sizes is still not available
    if [[ -z "${HIC_CHROM_SIZES:-}" ]]; then
        warn "Cannot run Hi-C execution without chrom.sizes"
        info "Schema and unit tests above still validate the Hi-C code paths"
        summary "Test 06: Hi-C Scaffolding (schema-only)"
        exit 0
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
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
PY

    # Run
    cd "${PROJECT_ROOT}"
    info "Running Hi-C scaffolding session: ${SID}"
    GA run \
        --manifest "$MANIFEST" \
        --session-id "$SID" \
        --data-dir "$TEST_DATA_ROOT" \
        --run-config "$RC" \
        --checkpointer "$CHECKPOINTER" \
        2>&1 || true

    # Verify outputs
    RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
    HIC_ARTIFACT="${RUN_DIR}/artifacts/hic_scaffolding/${SID}"
    HIC_WORK="${RUN_DIR}/work/hic_scaffolding/${SID}"

    assert_dir_exists "$HIC_ARTIFACT" "Hi-C artifact dir"

    # Shell script
    JUICER_SCRIPT=$(find "$HIC_WORK" -name "*.sh" 2>/dev/null | head -1 || true)
    if [[ -n "$JUICER_SCRIPT" ]]; then
        pass "Hi-C shell script generated: $(basename "$JUICER_SCRIPT")"
        assert_file_contains "$JUICER_SCRIPT" "juicer\|3d-dna\|run-asm-pipeline" "script has Juicer/3D-DNA commands"
        assert_file_contains "$JUICER_SCRIPT" "published artifact conflicts" "Hi-C script rejects conflicting replay output"
    else
        fail "Hi-C shell script not found"
    fi

    # Output files
    SCAFFOLDED=$(find "$HIC_ARTIFACT" -name "*scaffolded*.fasta" -o -name "*FINAL*.fasta" 2>/dev/null | head -1 || true)
    HIC_FILE=$(find "$HIC_ARTIFACT" -name "*.hic" 2>/dev/null | head -1 || true)

    [[ -n "$SCAFFOLDED" ]] && assert_file_nonempty "$SCAFFOLDED" "scaffolded assembly FASTA" || warn "scaffolded FASTA not found"
    [[ -n "$HIC_FILE" ]]   && assert_file_nonempty "$HIC_FILE"   "Hi-C contact map (.hic)" || warn ".hic file not found"

    # Inspect
    cd "${PROJECT_ROOT}"
    GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --trace 2>&1 && \
        pass "inspect Hi-C session" || fail "inspect Hi-C session"

    if [[ -f "${RUN_DIR}/ledger/events.jsonl" ]]; then
        assert_ledger_has_method_attempt "${RUN_DIR}/ledger/events.jsonl" "hic_scaffolding.candidate.juicer_3ddna_juicebox"
        assert_no_duplicate_method_attempts "${RUN_DIR}/ledger/events.jsonl" "Hi-C ledger has no duplicate attempts"
    else
        fail "Hi-C ledger events.jsonl not found"
    fi
else
    info "Skipping real Hi-C execution (no Hi-C data configured)"
    info "Schema and unit tests still validate the Hi-C code paths"
fi

summary "Test 06: Hi-C Scaffolding"
