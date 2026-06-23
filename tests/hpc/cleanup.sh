#!/usr/bin/env bash
# ============================================================
#  Cleanup: remove all HPC test session data
# ============================================================
#  Usage:
#    ./cleanup.sh           # remove test data, keep scripts
#    ./cleanup.sh --dry-run # show what would be removed
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source hpc_env.sh 2>/dev/null || source hpc_env.sh.example

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

echo "Test data root: ${TEST_DATA_ROOT}"

if [[ ! -d "${TEST_DATA_ROOT}" ]]; then
    echo "Nothing to clean (directory doesn't exist)."
    exit 0
fi

# Count what we'd remove
SESSION_COUNT=$(find "${TEST_DATA_ROOT}/runs" -maxdepth 1 -name "hpc-test*" -type d 2>/dev/null | wc -l)
TEMP_FILES=$(find "${TEST_DATA_ROOT}" -maxdepth 1 -name "_test*" 2>/dev/null | wc -l)

echo "Sessions to remove: ${SESSION_COUNT}"
echo "Temp files to remove: ${TEMP_FILES}"

if [[ "$DRY_RUN" == "true" ]]; then
    echo ""
    echo "Dry run — nothing removed. Run without --dry-run to clean."
    find "${TEST_DATA_ROOT}/runs" -maxdepth 1 -name "hpc-test*" -type d 2>/dev/null
    find "${TEST_DATA_ROOT}" -maxdepth 1 -name "_test*" 2>/dev/null
    exit 0
fi

read -p "Remove all test data? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Remove test sessions
    find "${TEST_DATA_ROOT}/runs" -maxdepth 1 -name "hpc-test*" -type d -exec rm -rf {} + 2>/dev/null || true
    # Remove temp manifests and configs
    find "${TEST_DATA_ROOT}" -maxdepth 1 -name "_test*" -exec rm -f {} + 2>/dev/null || true
    # Remove logs
    find "${TEST_DATA_ROOT}" -maxdepth 1 -name "_unit_test*" -exec rm -f {} + 2>/dev/null || true
    echo "Cleaned."
else
    echo "Aborted."
fi
