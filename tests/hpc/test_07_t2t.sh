#!/usr/bin/env bash
# ============================================================
#  Test 07 — T2T telomere finishing real-data smoke
# ============================================================
# Runs a real local T2T stage when T2T_ASSEMBLY and T2T_ONT_READS are
# configured. With neither configured, exits 77 (explicit SKIP).
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 07: T2T Telomere Finishing"

# Explicit T2T inputs are an all-or-nothing pair. Invalid or incomplete
# operator input is a configuration error; only fully automatic discovery may
# turn missing inputs into an explicit SKIP.
if [[ -n "${T2T_ASSEMBLY:-}" || -n "${T2T_ONT_READS:-}" ]]; then
    if [[ -z "${T2T_ASSEMBLY:-}" || -z "${T2T_ONT_READS:-}" ]]; then
        fail "T2T_ASSEMBLY and T2T_ONT_READS must be configured together"
        summary "Test 07: T2T Telomere Finishing (incomplete explicit inputs)" || true
        exit 1
    fi
    if [[ ! -s "$T2T_ASSEMBLY" || ! -s "$T2T_ONT_READS" ]]; then
        [[ -s "$T2T_ASSEMBLY" ]] || fail "T2T_ASSEMBLY is missing or empty: ${T2T_ASSEMBLY}"
        [[ -s "$T2T_ONT_READS" ]] || fail "T2T_ONT_READS is missing or empty: ${T2T_ONT_READS}"
        summary "Test 07: T2T Telomere Finishing (invalid explicit inputs)" || true
        exit 1
    fi
else
    _auto_t2t_assembly=$(latest_hic_scaffold || true)
    [[ -n "$_auto_t2t_assembly" ]] || _auto_t2t_assembly=$(latest_hpc_assembly || true)
    _auto_t2t_reads=""
    if [[ "${PLATFORM:-}" == "ont" || "${PLATFORM:-}" == "cycloneseq" ]]; then
        # This smoke reuses a single configured file; the run-config contract
        # itself accepts an ordered list of ONT read files.
        read -r -a _t2t_read_paths <<< "${LONG_READS:-}"
        if (( ${#_t2t_read_paths[@]} == 1 )) && [[ -s "${_t2t_read_paths[0]}" ]]; then
            _auto_t2t_reads="${_t2t_read_paths[0]}"
        fi
    fi
    if [[ -n "$_auto_t2t_assembly" && -n "$_auto_t2t_reads" ]]; then
        T2T_ASSEMBLY="$_auto_t2t_assembly"
        T2T_ONT_READS="$_auto_t2t_reads"
        info "Reusing upstream assembly for T2T: ${T2T_ASSEMBLY}"
        info "Reusing the configured long-read input for T2T: ${T2T_ONT_READS}"
    else
        T2T_ASSEMBLY=""
        T2T_ONT_READS=""
    fi
fi

cd "${PROJECT_ROOT}"
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    run_pytest tests/test_t2t.py -v --tb=short 2>&1 && \
        pass "unit tests: test_t2t.py" || fail "unit tests: test_t2t.py"
fi

python3 -c "
from app.schemas.run_config import RunConfig
cfg = RunConfig.model_validate({'local_exec': ['t2t'], 't2t': {}})
assert cfg.t2t.mode == 'auto_patch'
assert cfg.t2t.thresholds.min_supporting_reads == 1
print('T2T RunConfig validation: PASS')
" 2>&1 && pass "T2T RunConfig schema validation" || fail "T2T RunConfig schema validation"
cd "${SCRIPT_DIR}"

if [[ -z "${T2T_ASSEMBLY:-}" && -z "${T2T_ONT_READS:-}" ]]; then
    if (( _FAIL > 0 )); then
        summary "Test 07: T2T Telomere Finishing (contract checks failed)" || true
        exit 1
    fi
    info "[SKIP] Real T2T smoke not configured. Set both T2T_ASSEMBLY and T2T_ONT_READS."
    exit 77
fi
assert_file_nonempty "$T2T_ASSEMBLY" "T2T input assembly"
assert_file_nonempty "$T2T_ONT_READS" "T2T ONT reads"
if ! python3 - "$T2T_ASSEMBLY" <<'PY'
import gzip
import sys
from pathlib import Path

path = Path(sys.argv[1])
opener = gzip.open if path.suffix == ".gz" else open
with opener(path, "rt", encoding="utf-8") as handle:
    assert handle.read(1) == ">"
PY
then
    fail "T2T_ASSEMBLY is not a FASTA file: ${T2T_ASSEMBLY}"
fi
if (( _FAIL > 0 )); then
    summary "Test 07: T2T Telomere Finishing (invalid inputs)" || true
    exit 1
fi

cd "${PROJECT_ROOT}"
if ! preflight_configured_methods t2t t2t.alignment.minimap2; then
    info "Configure t2t.alignment.minimap2 in config/envs.local.yaml before the real smoke."
    summary "Test 07: T2T Telomere Finishing (missing minimap2)" || true
    exit 1
fi

SID="hpc-test07-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test07_manifest.yaml"
RC="${TEST_DATA_ROOT}/_test07_rc.json"
mkdir -p "${TEST_DATA_ROOT}"
write_manifest "$MANIFEST" "$SID" \
    "assembly_fasta_registered:${T2T_ASSEMBLY}:unknown:unknown"

python3 - "$RC" "$T2T_ASSEMBLY" "$T2T_ONT_READS" <<'PY'
import json
import sys

path, assembly, reads = sys.argv[1:]
config = {
    "local_exec": ["t2t"],
    "requested_stages": ["t2t"],
    "t2t": {
        "mode": "auto_patch",
        "assembly_fasta_ref": assembly,
        "ont_read_refs": [reads],
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
PY
python3 - "$RC" <<'PY'
import sys
from pathlib import Path
from app.schemas.run_config import load_run_config

config = load_run_config(sys.argv[1])
assert config.t2t is not None
assert config.t2t.mode == "auto_patch"
assert config.t2t.assembly_fasta_ref
assert config.t2t.ont_read_refs == [str(Path(config.t2t.ont_read_refs[0]))]
print("real T2T run-config validation OK")
PY

INPUT_SHA=$(python3 - "$T2T_ASSEMBLY" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)

info "Running real T2T session: ${SID}"
RUN_EXITCODE=0
if GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1; then
    pass "T2T session invocation exited cleanly"
else
    RUN_EXITCODE=$?
    warn "T2T invocation returned ${RUN_EXITCODE}; inspecting durable state"
fi

PARAMETER_RESUME_STATUS=0
resume_configured_parameter_gates "$SID" || PARAMETER_RESUME_STATUS=$?
if (( PARAMETER_RESUME_STATUS == 3 )); then
    info "[SKIP] T2T session is intentionally paused at parameter_approval."
    exit 77
elif (( PARAMETER_RESUME_STATUS != 0 )); then
    fail "T2T parameter-approval handling failed"
elif (( RUN_EXITCODE != 0 )) && (( ${HPC_PARAMETER_APPROVALS_RESUMED:-0} == 0 )); then
    fail "T2T invocation failed with no durable parameter gate resumed"
fi

GATE_STATUS=0
report_session_gate "$SID" || GATE_STATUS=$?
if (( GATE_STATUS == 3 )); then
    info "[SKIP] T2T session is waiting for its stage-specific human decision."
    exit 77
elif (( GATE_STATUS != 0 )); then
    fail "could not determine the T2T durable session state"
elif (( RUN_EXITCODE != 0 )); then
    fail "T2T session invocation returned a CLI error outside a human gate"
fi

RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
OUT_DIR="${RUN_DIR}/artifacts/t2t/outputs/t2t_finishing"
WORK_DIR="${RUN_DIR}/work/t2t/t2t_finishing"
BUNDLE="${OUT_DIR}/t2t_finishing_bundle.json"
ASSEMBLY_SCAN="${OUT_DIR}/assembly_telomere_scan.tsv"
READS_SCAN="${OUT_DIR}/reads_telomere_scan.tsv"
TELOMERIC_READS="${OUT_DIR}/telomeric_reads.tsv"
CANDIDATES="${OUT_DIR}/telomere_extension_candidates.tsv"
STATUS_SUMMARY="${OUT_DIR}/telomere_status_summary.tsv"
REPORT="${OUT_DIR}/t2t_finishing_report.md"

assert_dir_exists "$OUT_DIR" "T2T artifact directory"
assert_file_nonempty "$ASSEMBLY_SCAN" "assembly telomere scan"
assert_file_nonempty "$READS_SCAN" "ONT reads telomere scan"
assert_file_nonempty "$TELOMERIC_READS" "telomeric reads evidence"
assert_file_nonempty "$CANDIDATES" "extension candidate table"
assert_file_nonempty "$STATUS_SUMMARY" "telomere status summary"
assert_file_nonempty "$REPORT" "T2T finishing report"
assert_file_nonempty "$BUNDLE" "T2T finishing bundle"

AFTER_SHA=$(python3 - "$T2T_ASSEMBLY" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)
if [[ "$INPUT_SHA" == "$AFTER_SHA" ]]; then
    pass "T2T preserved the source assembly byte-for-byte"
else
    fail "T2T mutated the source assembly"
fi

python3 - "$BUNDLE" "$T2T_ASSEMBLY" "$T2T_ONT_READS" "$OUT_DIR" <<'PY' 2>&1 && \
    pass "T2T finishing bundle contract valid" || fail "T2T finishing bundle contract invalid"
import json
import sys
import gzip
from pathlib import Path

bundle_path, assembly, reads, out_dir = sys.argv[1:]
bundle = json.load(open(bundle_path, encoding="utf-8"))
assert bundle["status"] in {"completed", "completed_with_warnings"}, bundle
assert bundle["mode"] == "auto_patch", bundle
assert bundle["input_assembly_ref"] == assembly, bundle
assert bundle["ont_read_refs"] == [reads], bundle
assert bundle.get("metadata", {}).get("execution_enabled") is True, bundle
final_ref = Path(bundle["output_assembly_ref"])
assert final_ref.is_file(), final_ref
opener = gzip.open if final_ref.suffix == ".gz" else open
with opener(final_ref, "rt", encoding="utf-8") as handle:
    assert handle.read(1) == ">", final_ref
patched_ref = bundle.get("patched_assembly_ref")
if patched_ref:
    assert Path(patched_ref).is_file(), patched_ref
    assert final_ref == Path(patched_ref), (final_ref, patched_ref)
    assert (Path(out_dir) / "patch_records.tsv").is_file()
else:
    assert final_ref == Path(assembly), (final_ref, assembly)
required_artifacts = {
    "assembly_telomere_scan.tsv",
    "reads_telomere_scan.tsv",
    "telomeric_reads.tsv",
    "telomere_extension_candidates.tsv",
    "telomere_status_summary.tsv",
    "t2t_finishing_report.md",
}
assert required_artifacts <= set(bundle.get("artifact_refs") or {}), bundle.get("artifact_refs")
print(f"status={bundle['status']} final={final_ref}")
PY

READ_ROWS=$(awk 'END {print NR-1}' "$READS_SCAN")
if (( READ_ROWS > 0 )); then
    pass "T2T scanned ${READ_ROWS} real ONT reads"
else
    fail "T2T reads scan contains no input records"
fi

TELOMERIC_ROWS=$(awk 'END {print NR-1}' "$TELOMERIC_READS")
EVENTS="${RUN_DIR}/ledger/events.jsonl"
ARTIFACTS_INDEX="${RUN_DIR}/artifacts/index.jsonl"
assert_file_nonempty "$ARTIFACTS_INDEX" "T2T artifact index"
if (( TELOMERIC_ROWS > 0 )); then
    assert_file_contains "${WORK_DIR}/run_minimap2_t2t.sh" "minimap2 -x map-ont" "T2T minimap2 command generated"
    PAF_ARTIFACT=$(python3 - "$ARTIFACTS_INDEX" "$RUN_DIR" <<'PY'
import json
import sys
from pathlib import Path

index_path = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
matches = [
    json.loads(line)
    for line in index_path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
matches = [
    row for row in matches
    if row.get("stage") == "t2t"
    and row.get("artifact_type") == "t2t.read_alignment_paf"
]
assert len(matches) == 1, matches
print(run_dir / matches[0]["path_ref"])
PY
    )
    assert_file_nonempty "$PAF_ARTIFACT" "immutable registered T2T minimap2 PAF"
    assert_file_contains "$CANDIDATES" '"artifact_type":"t2t.read_alignment_paf"' "T2T candidates bind the immutable PAF"
    assert_file_contains "$CANDIDATES" '"artifact_type":"t2t.execution_result"' "T2T candidates bind execution evidence"
    if [[ -f "$EVENTS" ]]; then
        assert_ledger_has_method_attempt "$EVENTS" "t2t.alignment.minimap2"
    else
        fail "T2T ledger events.jsonl not found"
    fi
else
    info "No terminal-telomeric reads detected; minimap2 branch correctly skipped after real read scan."
fi

assert_file_contains "$ARTIFACTS_INDEX" '"stage":"t2t"' "T2T artifacts registered"

GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --trace 2>&1 && \
    pass "inspect real T2T session" || fail "inspect real T2T session"

summary "Test 07: T2T Telomere Finishing"
