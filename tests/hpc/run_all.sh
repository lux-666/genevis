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
#    ./run_all.sh 0        # run the full suite starting at test_00
#
#  Each test_n*.sh can also be run independently:
#    ./test_04_assembly_dedup.sh
#
#  Tests share state via ${TEST_DATA_ROOT}/runs/:
#    test_04 produces an assembly → test_05 reviews it → test_06 scaffolds it
#
#  Output:
#    Per-test PASS/SKIP/FAIL summary printed to stdout. Exit 77 means either an
#    optional real-data fixture is absent or a durable human gate is waiting.
#    Full logs are kept under ${TEST_DATA_ROOT}/test_logs/run-all-*/.
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
if [[ ! "$START_STEP" =~ ^[0-9]+$ ]]; then
    echo "ERROR: start_step must be a non-negative integer (for example 0, 4, or 9)."
    exit 2
fi
RUN_ALL_ID="run-all-$(date +%Y%m%d-%H%M%S)"
RUN_ALL_LOG_DIR="${TEST_DATA_ROOT}/test_logs/${RUN_ALL_ID}"
mkdir -p "$RUN_ALL_LOG_DIR"
ENV_CONFIG="${PROJECT_ROOT}/config/envs.yaml"
[[ -f "${PROJECT_ROOT}/config/envs.local.yaml" ]] && ENV_CONFIG="${PROJECT_ROOT}/config/envs.local.yaml"

echo ""
echo "============================================================"
echo "  Gene Agent HPC Integration Tests"
echo "  Project: ${PROJECT_ROOT}"
echo "  Data:    ${TEST_DATA_ROOT}"
echo "  Envs:    ${ENV_CONFIG}"
echo "  Start:   Step ${START_STEP}"
echo "  Logs:    ${RUN_ALL_LOG_DIR}"
echo "  Time:    $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo ""

# ---- Pre-flight checks ----
info "Pre-flight checks"

# Project root
assert_dir_exists "${PROJECT_ROOT}" "project root"
assert_file_exists "${PROJECT_ROOT}/pyproject.toml" "pyproject.toml"

# Conda binary. Individual tests resolve the exact target from ENV_CONFIG for
# each method; a named CONDA_ENV in hpc_env.sh would be a conflicting map.
if command -v conda &>/dev/null; then
    pass "conda is on PATH"
else
    fail "conda is not on PATH; configured method environments cannot run"
    exit 1
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
    "7:test_07_t2t.sh:T2T Telomere Finishing"
    "9:test_09_e2e_pipeline.sh:End-to-End Pipeline Integration"
)

MASTER_PASS=0
MASTER_FAIL=0
MASTER_SKIP=0
RESULTS=()

for entry in "${TESTS[@]}"; do
    IFS=: read -r step script label <<< "$entry"

    if (( step < START_STEP )); then
        info "Skipping step ${step}: ${label} (before start step ${START_STEP})"
        continue
    fi

    echo ""
    LOG_FILE="${RUN_ALL_LOG_DIR}/test_$(printf '%02d' "$step")_${script%.sh}.log"

    info "Running step ${step}: ${label}"
    info "  Script: ${script}"
    info "  Log:    ${LOG_FILE}"

    START_TIME=$(date +%s)

    set +e
    bash "${SCRIPT_DIR}/${script}" 2>&1 | tee "$LOG_FILE"
    TEST_STATUS=${PIPESTATUS[0]}
    set -e
    if (( TEST_STATUS == 0 )); then
        END_TIME=$(date +%s)
        ELAPSED=$(( END_TIME - START_TIME ))
        printf "${GREEN}[MASTER]${NC} Step %d: %s — ${GREEN}PASS${NC} (%ds)\n" "$step" "$label" "$ELAPSED"
        RESULTS+=("PASS  Step ${step}: ${label} (${ELAPSED}s)")
        (( ++MASTER_PASS ))
    elif (( TEST_STATUS == 77 )); then
        END_TIME=$(date +%s)
        ELAPSED=$(( END_TIME - START_TIME ))
        printf "${YELLOW}[MASTER]${NC} Step %d: %s — ${YELLOW}SKIP${NC} (%ds)\n" "$step" "$label" "$ELAPSED"
        RESULTS+=("SKIP  Step ${step}: ${label} (${ELAPSED}s)")
        (( ++MASTER_SKIP ))
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
    elif [[ "$r" == SKIP* ]]; then
        printf "  ${YELLOW}%s${NC}\n" "$r"
    else
        printf "  ${RED}%s${NC}\n" "$r"
    fi
done
echo ""
printf "  Total: ${GREEN}%d passed${NC}, ${YELLOW}%d skipped${NC}, ${RED}%d failed${NC}\n" "$MASTER_PASS" "$MASTER_SKIP" "$MASTER_FAIL"
echo ""

if (( MASTER_FAIL > 0 )); then
    printf "  ${RED}${BOLD}OVERALL: FAIL${NC}\n"
    echo ""
    echo "  Check individual logs in: ${RUN_ALL_LOG_DIR}/"
    echo "  Session data in:          ${TEST_DATA_ROOT}/runs/"
    exit 1
else
    printf "  ${GREEN}${BOLD}OVERALL: PASS${NC}\n"
    echo ""
    echo "  All runnable tests passed. Session data in: ${TEST_DATA_ROOT}/runs/"
    if (( MASTER_SKIP > 0 )); then
        echo "  SKIP can mean optional data was absent or a durable human gate is awaiting TUI review."
        echo "  The per-test log prints the exact session ID and resume command."
    fi
    exit 0
fi
