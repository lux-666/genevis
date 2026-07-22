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
    run_pytest tests/test_assembly.py -v --tb=short 2>&1 && \
        pass "unit tests: test_assembly.py" || fail "unit tests: test_assembly.py"
    info "Running unit tests: test_assembly_dedup.py"
    run_pytest tests/test_assembly_dedup.py -v --tb=short 2>&1 && \
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
    flye|nextdenovo|hifiasm|hifiasm_ont) ;;
    *)
        fail "unsupported ASSEMBLY_METHOD: ${ASSEMBLY_METHOD} (expected hifiasm, hifiasm_ont, flye, or nextdenovo)"
        summary "Test 04: Assembly + Assembly Dedup"
        exit 1
        ;;
esac
case "${ASSEMBLY_METHOD}:${PLATFORM}" in
    hifiasm:pacbio_hifi|hifiasm_ont:ont|hifiasm_ont:cycloneseq|flye:*|nextdenovo:*) ;;
    hifiasm:*)
        fail "ASSEMBLY_METHOD=hifiasm expects PLATFORM=pacbio_hifi for this HPC test"
        summary "Test 04: Assembly + Assembly Dedup"
        exit 1
        ;;
    hifiasm_ont:*)
        fail "ASSEMBLY_METHOD=hifiasm_ont expects PLATFORM=ont or PLATFORM=cycloneseq for this HPC test"
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
RUN_EXITCODE=0
if GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1; then
    pass "assembly + dedup session completed without CLI error"
else
    RUN_EXITCODE=$?
    warn "initial assembly invocation returned ${RUN_EXITCODE}; inspecting durable gate state"
fi

PARAMETER_RESUME_STATUS=0
resume_configured_parameter_gates "$SID" || PARAMETER_RESUME_STATUS=$?
if (( PARAMETER_RESUME_STATUS == 3 )); then
    info "[SKIP] Test 04 paused at an intentional parameter approval gate."
    info "Resume the printed session, then rerun Test 04 assertions or continue with Test 05."
    exit 77
elif (( PARAMETER_RESUME_STATUS != 0 )); then
    fail "parameter-approval handling failed"
elif (( RUN_EXITCODE != 0 )); then
    if (( ${HPC_PARAMETER_APPROVALS_RESUMED:-0} > 0 )); then
        info "continuing after ${HPC_PARAMETER_APPROVALS_RESUMED} successful durable parameter approval(s)"
    else
        fail "assembly invocation failed with exit code ${RUN_EXITCODE} and no parameter gate was resumed"
        summary "Test 04: Assembly + Assembly Dedup (run failed)" || true
        exit 1
    fi
fi

GATE_STATUS=0
report_session_gate "$SID" || GATE_STATUS=$?
if (( GATE_STATUS == 3 )); then
    info "[SKIP] Test 04 is durably paused at a human gate."
    exit 77
elif (( GATE_STATUS != 0 )); then
    fail "could not determine the post-resume session state"
    summary "Test 04: Assembly + Assembly Dedup (durable state failed)" || true
    exit 1
fi

# ---- Verify assembly outputs ----
RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
ASM_ARTIFACT_DIR="${RUN_DIR}/artifacts/assembly"
DEDUP_ARTIFACT_DIR="${RUN_DIR}/artifacts/assembly_dedup"
DEDUP_WORK_DIR="${RUN_DIR}/work/assembly_dedup"

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
ASM_SCRIPT=$(find "${RUN_DIR}/work/assembly" -name "*.sh" -type f 2>/dev/null | head -1 || true)
if [[ -n "$ASM_SCRIPT" ]]; then
    pass "assembly method script generated: $(basename "$ASM_SCRIPT")"
    case "${ASSEMBLY_METHOD}" in
        flye|nextdenovo)
            assert_file_contains "$ASM_SCRIPT" "published artifact conflicts" "assembly script rejects conflicting replay output"
            ;;
        hifiasm|hifiasm_ont)
            assert_file_contains "$ASM_SCRIPT" "hifiasm" "hifiasm script invokes assembler"
            assert_file_contains "$ASM_SCRIPT" "awk '/^S/" "hifiasm script converts GFA segments to FASTA"
            ;;
    esac
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

# ---- Parameter recommendation and execution evidence ----
info "Checking assembly parameter recommendation and execution evidence"
cd "${PROJECT_ROOT}"
python3 - "${RUN_DIR}" "${SID}" "${ASSEMBLY_METHOD}" <<'PY' 2>&1 && \
    pass "assembly parameter and execution evidence valid" || fail "assembly parameter or execution evidence invalid"
import json
import sys
from pathlib import Path

from app.execution.evidence import load_execution_evidence, load_frozen_attempt
from app.graph.builder import build_graph
from app.schemas.artifacts import ParameterAttempt
from app.stages.assembly.parameters.storage import load_recommendation_bundle, read_json_artifact
from app.storage.artifacts import resolve_artifact_ref

run_dir = Path(sys.argv[1])
session_id = sys.argv[2]
method_slug = sys.argv[3]
method_id = f"assembly.candidate.{method_slug}"

checkpoint_path = run_dir / "checkpoints" / "checkpoints.sqlite"
assert checkpoint_path.is_file(), f"missing sqlite checkpoint: {checkpoint_path}"
graph = build_graph(backend="sqlite", data_dir=str(run_dir))
try:
    snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
    state = dict(snapshot.values or {})
finally:
    connection = getattr(getattr(graph, "checkpointer", None), "conn", None)
    if connection is not None:
        connection.close()

assert state.get("session_id") == session_id, state.get("session_id")
assembly_out = dict((state.get("stage_outputs") or {}).get("assembly") or {})
current_id = assembly_out.get("current_parameter_attempt_id")
assert type(current_id) is int and current_id >= 0, current_id
matching_attempts = [
    ParameterAttempt.model_validate(item)
    for item in assembly_out.get("parameter_attempts") or []
    if isinstance(item, dict)
    and item.get("method_id") == method_id
    and item.get("attempt_id") == current_id
]
assert len(matching_attempts) == 1, f"expected one current attempt for {method_id}, got {len(matching_attempts)}"
attempt = matching_attempts[0]
assert attempt.status == "executed", attempt
for field in (
    "recommendation_manifest_ref",
    "approval_decision_ref",
    "frozen_parameters_ref",
    "command_ref",
    "frozen_attempt_ref",
    "execution_ref",
):
    assert getattr(attempt, field) is not None, f"current ParameterAttempt missing {field}"

attempt_scope = f"{attempt.attempt_id:03d}"
assert attempt.recommendation_manifest_ref.path_ref == (
    f"artifacts/assembly/parameter_recommendations/{attempt_scope}/recommendation_manifest.json"
)
assert attempt.command_ref.path_ref == f"artifacts/assembly/attempts/{attempt_scope}/command.json"
assert attempt.frozen_parameters_ref.path_ref == f"artifacts/assembly/attempts/{attempt_scope}/frozen_parameters.json"

# Canonical loaders verify artifact-index identity, hashes, types, and all
# recommendation-manifest refs before returning the payloads.
bundle = load_recommendation_bundle(state, attempt.recommendation_manifest_ref)
manifest = bundle["manifest"]
recommendation_record = bundle["recommendation"]
assert manifest.get("method_id") == method_id, manifest
assert manifest.get("recommendation_id") == attempt.attempt_id, manifest
assert recommendation_record.get("method_id") == method_slug, recommendation_record
recommendation = recommendation_record.get("recommendation") or {}
recommended_parameters = recommendation.get("parameters")
assert isinstance(recommended_parameters, dict), recommendation

command = read_json_artifact(state, attempt.command_ref)
frozen_parameters = read_json_artifact(state, attempt.frozen_parameters_ref)
sources = command.get("parameter_sources")
assert isinstance(sources, dict), "command.json missing parameter_sources"
for group in ("l0_fixed", "l1_readonly", "l2_recommended"):
    assert isinstance(sources.get(group), dict), f"missing parameter source group: {group}"
assert sources["l2_recommended"] == recommended_parameters
assert frozen_parameters.get("parameter_sources") == sources

argv = command.get("argv")
assert isinstance(argv, list) and argv, "command.json missing argv"
assert command.get("command"), "command.json missing rendered command string"
if method_slug in {"hifiasm", "hifiasm_ont"}:
    assert argv[0] == "hifiasm", argv
    assert "-t" in argv, f"hifiasm argv missing -t threads flag: {argv}"
    assert "-o" in argv, f"hifiasm argv missing output prefix: {argv}"
    if method_slug == "hifiasm_ont":
        assert "--ont" in argv, f"hifiasm_ont argv missing --ont: {argv}"
    else:
        assert "--ont" not in argv, f"hifiasm HiFi argv should not contain --ont: {argv}"
elif method_slug == "nextdenovo":
    assert argv[0] == "nextDenovo", argv
    rendered = command.get("rendered_parameters")
    assert isinstance(rendered, dict) and rendered, "NextDenovo command.json missing rendered_parameters"
    run_cfg = command.get("run_cfg")
    assert run_cfg and Path(run_cfg).is_file(), f"NextDenovo run.cfg missing: {run_cfg}"
else:
    assert argv[0] == "flye", argv

approval_path = resolve_artifact_ref(run_dir, session_id, attempt.approval_decision_ref)
approval = json.loads(approval_path.read_text(encoding="utf-8"))
assert approval.get("stage") == "assembly", approval
assert approval.get("gate_id") == "parameter_approval", approval
assert (approval.get("decision") or {}).get("decision") == "approve_parameters", approval

frozen_attempt = load_frozen_attempt(
    state,
    attempt.frozen_attempt_ref,
    expected_stage="assembly",
    expected_method_name=method_id,
)
assert (frozen_attempt.metadata or {}).get("parameter_attempt_id") == attempt.attempt_id
execution = load_execution_evidence(
    state,
    attempt.execution_ref,
    expected_stage="assembly",
    expected_attempt_id=frozen_attempt.attempt_id,
    expected_method_name=method_id,
)
assert execution.success is True, execution
assert execution.return_code == 0, execution

assembly_dir = run_dir / "artifacts" / "assembly"

candidate_paths = sorted((assembly_dir / "candidates").glob("*.json"))
matching_candidates = []
for path in candidate_paths:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("method_name") == method_id and data.get("status") == "executed":
        matching_candidates.append((path, data))
assert matching_candidates, f"no executed candidate bundle for {method_id}"
candidate_path, _candidate = matching_candidates[-1]

events_path = run_dir / "ledger" / "events.jsonl"
assert events_path.is_file(), f"missing ledger events: {events_path}"
events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
assert any(
    event.get("event_type") == "attempt_succeeded" and event.get("method_name") == method_id
    for event in events
), f"ledger has no attempt_succeeded for {method_id}"

print(f"  recommendation_manifest: {attempt.recommendation_manifest_ref.path_ref}")
print(f"  command_json: {attempt.command_ref.path_ref}")
print(f"  approval_decision: {attempt.approval_decision_ref.path_ref}")
print(f"  execution_evidence: {attempt.execution_ref.path_ref}")
print(f"  explicit_l2_parameters: {sorted(recommended_parameters)}")
print(f"  argv_preview: {' '.join(argv[:12])}")
print(f"  candidate_bundle: {candidate_path}")
PY
cd "${SCRIPT_DIR}"

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
    assert_file_contains "$DEDUP_SCRIPT" "split_fa"    "script: split_fa"
    assert_file_contains "$DEDUP_SCRIPT" "purge_dups"  "script: purge_dups"
    assert_file_contains "$DEDUP_SCRIPT" "get_seqs"    "script: get_seqs"
    assert_file_contains "$DEDUP_SCRIPT" "get_seqs -e -p" "script: get_seqs writes named outputs"
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
READ_PAF="${DEDUP_ARTIFACT_DIR}/reads_to_raw_assembly.paf.gz"
SPLIT_ASM="${DEDUP_ARTIFACT_DIR}/raw_assembly.split.fasta"
BASE_COV="${DEDUP_ARTIFACT_DIR}/PB.base.cov"
PB_STAT="${DEDUP_ARTIFACT_DIR}/PB.stat"
CUTOFFS="${DEDUP_ARTIFACT_DIR}/cutoffs"
SELF_PAF="${DEDUP_ARTIFACT_DIR}/self.paf.gz"
DUPS_BED="${DEDUP_ARTIFACT_DIR}/dups.bed"

assert_file_nonempty "$READ_BAM"    "minimap2 alignment BAM"
assert_file_nonempty "$READ_PAF"    "minimap2 alignment PAF for pbcstat"
assert_file_nonempty "$SPLIT_ASM"   "split assembly for purge_dups"
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
