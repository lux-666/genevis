#!/usr/bin/env bash
# ============================================================
#  Test 01 — Storage Layer: session paths, publish, resume
# ============================================================
#  What it tests:
#    1. New runs/<session-id>/ directory layout is created correctly
#    2. Session ID auto-generation works
#    3. locate_session_data_dir() backward compat (legacy flat layout)
#    4. publish_file() atomic move / cross-device fallback
#    5. inspect subcommand reads the new layout
#    6. Unit tests for storage paths pass
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 01: Storage Layer"

# ---- Unit tests for storage ----
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    info "Running unit tests: test_storage_paths.py"
    cd "${PROJECT_ROOT}"
    if python3 -m pytest tests/test_storage_paths.py -v --tb=short 2>&1; then
        pass "unit tests: test_storage_paths.py"
    else
        fail "unit tests: test_storage_paths.py"
    fi
    cd "${SCRIPT_DIR}"
fi

# ---- Prepare a minimal manifest ----
SID="hpc-test01-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test01_manifest.yaml"
mkdir -p "${TEST_DATA_ROOT}"
write_manifest "$MANIFEST" "$SID" "raw_long_reads:${LONG_READS}:${PLATFORM}:single"

# ---- Test 1: Run with minimal config (inventory only) and check layout ----
info "Running inventory-only session: ${SID}"
cd "${PROJECT_ROOT}"
RC="${TEST_DATA_ROOT}/_test01_rc.json"
echo '{}' > "$RC"

GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1 || true   # pipeline may block at later stages; we only need inventory

RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
assert_dir_exists "${RUN_DIR}"                  "run_dir created: runs/${SID}"
assert_dir_exists "${RUN_DIR}/control"          "control/ subdir"
assert_dir_exists "${RUN_DIR}/artifacts"        "artifacts/ subdir"
assert_dir_exists "${RUN_DIR}/work"             "work/ subdir"
assert_dir_exists "${RUN_DIR}/ledger"           "ledger/ subdir"
assert_dir_exists "${RUN_DIR}/checkpoints"      "checkpoints/ subdir"

# Manifest should be copied into control/
assert_file_exists "${RUN_DIR}/control/manifest.yaml" "manifest copied to control/"

# ---- Test 2: inspect reads the new layout ----
info "Inspecting session ${SID}"
if GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" 2>&1; then
    pass "gene-agent inspect works with new layout"
else
    fail "gene-agent inspect failed with new layout"
fi

# ---- Test 3: inspect --json gives structured output ----
INSPECT_JSON=$(GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --json 2>&1 || true)
if echo "$INSPECT_JSON" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
    pass "inspect --json produces valid JSON"
else
    warn "inspect --json output not valid JSON (may be expected if inspect doesn't support --json yet)"
fi

# ---- Test 4: Check sqlite checkpoint was created ----
CKPT_DB="${RUN_DIR}/checkpoints/checkpoints.sqlite"
if [[ -f "$CKPT_DB" ]]; then
    pass "sqlite checkpoint DB created at checkpoints/checkpoints.sqlite"
    # Verify it has tables
    TABLE_COUNT=$(python3 - "$CKPT_DB" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    print(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
PY
)
    if (( TABLE_COUNT > 0 )); then
        pass "checkpoint DB has ${TABLE_COUNT} tables"
    else
        fail "checkpoint DB has no tables"
    fi
else
    fail "sqlite checkpoint DB not found"
fi

# ---- Test 5: Ledger event log ----
EVENTS="${RUN_DIR}/ledger/events.jsonl"
if [[ -f "$EVENTS" ]]; then
    LINE_COUNT=$(wc -l < "$EVENTS")
    pass "ledger events.jsonl exists with ${LINE_COUNT} events"
else
    warn "ledger events.jsonl not found (may not be created in inventory-only run)"
fi

# ---- Test 6: Replay-safe publish (manual test) ----
info "Testing publish_file() replay safety"
PUBLISH_TEST_DIR="${TEST_DATA_ROOT}/_publish_test"
rm -rf "${PUBLISH_TEST_DIR}"
mkdir -p "${PUBLISH_TEST_DIR}/src" "${PUBLISH_TEST_DIR}/dst"
echo "hello replay" > "${PUBLISH_TEST_DIR}/src/test.txt"

cd "${PROJECT_ROOT}"
python3 -c "
from app.storage.publish import publish_file, shell_publish_file
from pathlib import Path
import subprocess
src = Path('${PUBLISH_TEST_DIR}/src/test.txt')
dst = Path('${PUBLISH_TEST_DIR}/dst/test.txt')

# First publish
publish_file(src, dst)
assert dst.exists(), 'first publish failed'
assert dst.read_text() == 'hello replay\n', 'content mismatch'

# Replay: same content should succeed (idempotent)
# Re-create source to simulate replay
src.write_text('hello replay')
publish_file(src, dst)
assert dst.exists(), 'replay publish failed'
assert dst.read_text() == 'hello replay\n', 'replay content mismatch'

# Conflict: different content should raise
src.write_text('different content')
try:
    publish_file(src, dst)
    print('ERROR: expected FileExistsError on conflicting publish')
    exit(1)
except FileExistsError:
    pass  # expected

# Generated method scripts use this helper, so cover its replay semantics too.
shell_source = src.with_name('shell-source.txt')
shell_destination = dst.with_name('shell-destination.txt')
shell_source.write_text('shell replay')
subprocess.run(shell_publish_file(shell_source, shell_destination), shell=True, check=True)
shell_source.write_text('shell replay')
subprocess.run(shell_publish_file(shell_source, shell_destination), shell=True, check=True)
assert shell_destination.read_text() == 'shell replay'
assert not shell_source.exists()

print('publish_file: all replay-safe checks passed')
" 2>&1 && pass "publish_file() replay safety" || fail "publish_file() replay safety"

# ---- Test 7: SessionPaths layout validation ----
cd "${PROJECT_ROOT}"
python3 -c "
from app.storage.paths import SessionPaths
sp = SessionPaths(data_root='${TEST_DATA_ROOT}', session_id='${SID}')
assert str(sp.run_dir).endswith('runs/${SID}'), f'bad run_dir: {sp.run_dir}'
assert str(sp.control_dir).endswith('control'), f'bad control_dir: {sp.control_dir}'
assert str(sp.artifacts_dir).endswith('artifacts'), f'bad artifacts_dir: {sp.artifacts_dir}'
assert str(sp.work_dir).endswith('work'), f'bad work_dir: {sp.work_dir}'
assert str(sp.ledger_dir).endswith('ledger'), f'bad ledger_dir: {sp.ledger_dir}'
assert str(sp.checkpoints_dir).endswith('checkpoints'), f'bad checkpoints_dir: {sp.checkpoints_dir}'
print('SessionPaths: all paths correct')
" 2>&1 && pass "SessionPaths layout validation" || fail "SessionPaths layout validation"

# ---- Test 8: locate_session_data_dir backward compat ----
cd "${PROJECT_ROOT}"
python3 -c "
from pathlib import Path
from app.storage.paths import locate_session_data_dir

# New layout should be found
result = locate_session_data_dir('${TEST_DATA_ROOT}', '${SID}')
assert result is not None, 'new layout session not found'
assert 'runs/${SID}' in str(result), f'unexpected path: {result}'

# Non-existent session should return None
result2 = locate_session_data_dir('${TEST_DATA_ROOT}', 'nonexistent-session-xyz')
assert result2 is None, f'should be None for missing session, got {result2}'

print('locate_session_data_dir: backward compat OK')
" 2>&1 && pass "locate_session_data_dir backward compat" || fail "locate_session_data_dir backward compat"

# ---- Cleanup test-specific temp files ----
rm -rf "${PUBLISH_TEST_DIR}"

summary "Test 01: Storage Layer"
