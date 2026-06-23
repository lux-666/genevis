#!/usr/bin/env bash
# ============================================================
#  Gene Agent — HPC Integration Test Runner
# ============================================================
#
#  Usage:
#    1. Copy hpc_env.sh.example → hpc_env.sh, fill in YOUR paths
#    2. chmod +x run_all.sh test_*.sh
#    3. ./run_all.sh [start_step]
#
#  Examples:
#    ./run_all.sh          # run all tests from the beginning
#    ./run_all.sh 3        # start from test_03 (skip 00-02)
#    ./run_all.sh 0        # only run test_00 (entrypoints + unit tests)
#
#  Each test_n*.sh can also be run independently:
#    ./test_04_assembly_dedup.sh
#
#  Tests share state via ${TEST_DATA_ROOT}/runs/:
#    test_04 produces an assembly → test_05 reviews it → test_06 scaffolds it
#
#  Output:
#    Per-test PASS/FAIL summary printed to stdout.
#    Full logs saved to ${TEST_DATA_ROOT}/_test_*.log
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load config
if [[ ! -f "hpc_env.sh" ]]; then
    echo "ERROR: hpc_env.sh not found."
    echo "  cp hpc_env.sh.example hpc_env.sh && vim hpc_env.sh"
    exit 2
fi
source hpc_env.sh
source common.sh

START_STEP="${1:-0}"

echo ""
echo "============================================================"
echo "  Gene Agent HPC Integration Tests"
echo "  Project: ${PROJECT_ROOT}"
echo "  Data:    ${TEST_DATA_ROOT}"
echo "  Conda:   ${CONDA_ENV}"
echo "  Start:   Step ${START_STEP}"
echo "  Time:    $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo ""

# ---- Pre-flight checks ----
info "Pre-flight checks"

# Project root
assert_dir_exists "${PROJECT_ROOT}" "project root"
assert_file_exists "${PROJECT_ROOT}/pyproject.toml" "pyproject.toml"

# Conda env
if command -v conda &>/dev/null; then
    if conda env list | grep -q "^${CONDA_ENV} "; then
        pass "conda env '${CONDA_ENV}' exists"
    else
        fail "conda env '${CONDA_ENV}' not found"
        echo "  Available envs:"
        conda env list
        exit 1
    fi
else
    warn "conda not on PATH — tools must be available via other means"
fi

# Input data
for f in $LONG_READS; do
    assert_file_nonempty "$f" "long reads: $(basename "$f")"
done

# Python / gene-agent
cd "${PROJECT_ROOT}"
if python3 -c "import app" 2>/dev/null; then
    pass "python3 can import app"
else
    fail "python3 cannot import app — is the project installed?"
    echo "  Try: pip install -e ."
    exit 1
fi

# Data root
mkdir -p "${TEST_DATA_ROOT}"
info "Test data root: ${TEST_DATA_ROOT}"

# ---- Test execution plan ----
TESTS=(
    "0:test_00_entrypoints.sh:Entrypoints & Cross-cutting"
    "1:test_01_storage.sh:Storage Layer"
    "2:test_02_read_qc.sh:Read QC"
    "3:test_03_genome_survey.sh:Genome Survey"
    "4:test_04_assembly_dedup.sh:Assembly + Assembly Dedup"
    "5:test_05_assembly_review.sh:Assembly Review"
    "6:test_06_hic.sh:Hi-C Scaffolding"
)

MASTER_PASS=0
MASTER_FAIL=0
RESULTS=()

for entry in "${TESTS[@]}"; do
    IFS=: read -r step script label <<< "$entry"

    if (( step < START_STEP )); then
        info "Skipping step ${step}: ${label} (before start step ${START_STEP})"
        continue
    fi

    echo ""
    LOG_FILE="${TEST_DATA_ROOT}/_test_$(printf '%02d' "$step")_${script%.sh}.log"

    info "Running step ${step}: ${label}"
    info "  Script: ${script}"
    info "  Log:    ${LOG_FILE}"

    START_TIME=$(date +%s)

    if bash "${SCRIPT_DIR}/${script}" 2>&1 | tee "$LOG_FILE"; then
        END_TIME=$(date +%s)
        ELAPSED=$(( END_TIME - START_TIME ))
        printf "${GREEN}[MASTER]${NC} Step %d: %s — ${GREEN}PASS${NC} (%ds)\n" "$step" "$label" "$ELAPSED"
        RESULTS+=("PASS  Step ${step}: ${label} (${ELAPSED}s)")
        (( ++MASTER_PASS ))
    else
        END_TIME=$(date +%s)
        ELAPSED=$(( END_TIME - START_TIME ))
        printf "${RED}[MASTER]${NC} Step %d: %s — ${RED}FAIL${NC} (%ds)\n" "$step" "$label" "$ELAPSED"
        RESULTS+=("FAIL  Step ${step}: ${label} (${ELAPSED}s)")
        (( ++MASTER_FAIL ))
        # Don't abort — continue with next test to maximize coverage
    fi
done

# ---- Master summary ----
echo ""
echo "============================================================"
echo "  MASTER SUMMARY"
echo "============================================================"
for r in "${RESULTS[@]}"; do
    if [[ "$r" == PASS* ]]; then
        printf "  ${GREEN}%s${NC}\n" "$r"
    else
        printf "  ${RED}%s${NC}\n" "$r"
    fi
done
echo ""
printf "  Total: ${GREEN}%d passed${NC}, ${RED}%d failed${NC}\n" "$MASTER_PASS" "$MASTER_FAIL"
echo ""

if (( MASTER_FAIL > 0 )); then
    printf "  ${RED}${BOLD}OVERALL: FAIL${NC}\n"
    echo ""
    echo "  Check individual logs in: ${TEST_DATA_ROOT}/_test_*.log"
    echo "  Session data in:          ${TEST_DATA_ROOT}/runs/"
    exit 1
else
    printf "  ${GREEN}${BOLD}OVERALL: PASS${NC}\n"
    echo ""
    echo "  All tests passed. Session data in: ${TEST_DATA_ROOT}/runs/"
    exit 0
fi
