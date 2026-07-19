#!/usr/bin/env bash
# ============================================================
#  Test 00 — Entrypoints & Cross-cutting: CLI, schema, unit tests
# ============================================================
#  What it tests:
#    1. Full unit test suite runs cleanly (catches regressions early)
#    2. CLI argument parsing for all subcommands
#    3. Schema cross-compatibility (run_config ↔ artifacts ↔ state)
#    4. ask_planner changes (genome size deferral, Hi-C method selection)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 00: Entrypoints & Cross-cutting"

# ---- Full unit test suite ----
cd "${PROJECT_ROOT}"
info "Running full unit test suite"
UNIT_LOG="${TEST_DATA_ROOT}/_unit_test_output.log"
mkdir -p "${TEST_DATA_ROOT}"

if run_pytest tests/ -v --tb=short -x 2>&1 | tee "$UNIT_LOG"; then
    TOTAL_PASS=$(grep -Ec '^tests/.*[[:space:]]PASSED([[:space:]]|$)' "$UNIT_LOG" 2>/dev/null || true)
    pass "full unit test suite: ${TOTAL_PASS} passed"
else
    # Count pytest's per-test progress records only. Summary lines such as
    # "1 failed" and the later "FAILED tests/..." section describe the same
    # failure and must not inflate the diagnostic count.
    TOTAL_PASS=$(grep -Ec '^tests/.*[[:space:]]PASSED([[:space:]]|$)' "$UNIT_LOG" 2>/dev/null || true)
    TOTAL_FAIL=$(grep -Ec '^tests/.*[[:space:]]FAILED([[:space:]]|$)' "$UNIT_LOG" 2>/dev/null || true)
    fail "unit tests: ${TOTAL_PASS} passed, ${TOTAL_FAIL} failed"
    info "Full log at: ${UNIT_LOG}"

    # Show the first few failures
    info "First failures:"
    grep "FAILED" "$UNIT_LOG" | head -5
fi

# ---- Critical test files ----
cd "${PROJECT_ROOT}"
for test_file in test_entrypoints.py test_run_config.py test_ask_plan.py test_ask_interactive.py test_durable_resume.py test_storage_paths.py; do
    if [[ -f "tests/${test_file}" ]]; then
        info "Running: ${test_file}"
        if run_pytest "tests/${test_file}" -v --tb=short 2>&1; then
            pass "unit: ${test_file}"
        else
            fail "unit: ${test_file}"
        fi
    fi
done

# ---- Schema cross-compatibility checks ----
cd "${PROJECT_ROOT}"
info "Verifying schema cross-compatibility"

python3 -c "
# Test that assembly_dedup is in all the right enums
from app.schemas.run_config import RequestableStage
from app.schemas.state import DomainStage
from app.workflow import STAGE_ORDER
import typing

# STAGE_ORDER
assert 'assembly_dedup' in STAGE_ORDER, 'assembly_dedup not in STAGE_ORDER'
print(f'STAGE_ORDER position: {STAGE_ORDER.index(\"assembly_dedup\")}')
assert STAGE_ORDER.index('assembly_dedup') == STAGE_ORDER.index('assembly') + 1, \
    'assembly_dedup should come right after assembly'

# RequestableStage
req_vals = str(RequestableStage)
assert 'assembly_dedup' in req_vals, f'assembly_dedup not in RequestableStage'

# DomainStage
dom_vals = str(DomainStage)
assert 'assembly_dedup' in dom_vals, f'assembly_dedup not in DomainStage'

print('Schema cross-compat: assembly_dedup in all enums ✓')
" 2>&1 && pass "assembly_dedup schema cross-compat" || fail "assembly_dedup schema cross-compat"

python3 -c "
# Verify genome_survey schema changes are consistent
from app.schemas.run_config import GenomeSurveyRunParameters
from app.schemas.artifacts import GenomeSurveyResult
import inspect, typing

# read_length gone from params
params = list(inspect.signature(GenomeSurveyRunParameters).parameters.keys())
assert 'read_length' not in params, f'read_length still in params: {params}'

# fallback_low_confidence gone from mode
mode_hints = typing.get_type_hints(GenomeSurveyResult)
mode_str = str(mode_hints.get('mode', ''))
assert 'fallback_low_confidence' not in mode_str, \
    f'fallback_low_confidence still in mode: {mode_str}'

print(f'GenomeSurveyRunParameters fields: {params}')
print(f'GenomeSurveyResult.mode: {mode_str}')
print('Genome survey schema changes: consistent ✓')
" 2>&1 && pass "genome_survey schema changes consistent" || fail "genome_survey schema inconsistency"

python3 -c "
# Verify Hi-C schemas are self-consistent
from app.schemas.run_config import (
    HiCScaffoldingRunConfig, AutoHiCRunParameters,
)
from app.schemas.artifacts import HiCScaffoldDecisionArtifact

# The stage is a fixed AutoHiC pipeline, so the run config has no method selector.
assert 'methods' not in HiCScaffoldingRunConfig.model_fields
assert 'method' not in HiCScaffoldingRunConfig.model_fields
assert 'error_model_ref' not in AutoHiCRunParameters.model_fields
assert 'chromosome_model_ref' not in AutoHiCRunParameters.model_fields

# Decision artifact should accept accept_candidate
import typing
dec_hints = typing.get_type_hints(HiCScaffoldDecisionArtifact)
dec_str = str(dec_hints.get('decision', ''))
assert 'accept_candidate' in dec_str, f'accept_candidate not in decision type: {dec_str}'

print('Hi-C schema cross-compat: all types consistent ✓')
" 2>&1 && pass "Hi-C schema cross-compat" || fail "Hi-C schema cross-compat"

# ---- CLI smoke: verify all subcommands parse ----
cd "${PROJECT_ROOT}"
info "CLI subcommand parsing smoke test"
for cmd in run ask inspect resume review; do
    if python3 -m app.entrypoints.run "$cmd" --help >/dev/null 2>&1; then
        pass "CLI: gene-agent ${cmd} --help"
    else
        fail "CLI: gene-agent ${cmd} --help"
    fi
done

# ---- Storage/replay contract is importable from every stage adapter ----
python3 -c "
from app.storage.publish import shell_publish_file, shell_publish_variable
from app.stages.assembly.method_application import flye, nextdenovo
from app.stages.hic_scaffolding.method_application import autohic

assert callable(shell_publish_file) and callable(shell_publish_variable)
for module in [flye, nextdenovo, autohic]:
    source = open(module.__file__, encoding='utf-8').read()
    assert 'shell_publish_' in source, module.__name__
print('all refactored assembly/Hi-C adapters use replay-safe publishing')
" 2>&1 && pass "replay-safe publishing wired into method adapters" || fail "replay-safe publishing wiring"

# ---- Descriptor registry: assembly_dedup registered ----
cd "${PROJECT_ROOT}"
python3 -c "
from pathlib import Path
from app.schemas.descriptors import DescriptorRegistry
reg = DescriptorRegistry.load(Path('descriptors/registry.yaml'))
assert 'assembly_dedup.purge_dups' in reg.methods, \
    f'assembly_dedup.purge_dups not in registry. Methods: {list(reg.methods.keys())}'
print('assembly_dedup.purge_dups registered in descriptors/registry.yaml ✓')
" 2>&1 && pass "descriptor registry: assembly_dedup" || fail "descriptor registry: assembly_dedup"

summary "Test 00: Entrypoints & Cross-cutting"
