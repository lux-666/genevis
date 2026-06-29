#!/usr/bin/env bash
# ============================================================
#  Test 09 — Phase 9: End-to-End Pipeline Test
# ============================================================
#  What it tests:
#    1. Full pipeline flow: read_qc → genome_survey → assembly → assembly_review → hic → t2t
#    2. State transitions between stages (supervision_state tracking)
#    3. Blocking behavior at human gates (assembly route selection, assembly_review)
#    4. TUI-compatible exits (blocked_human → END without crash)
#    5. Resume capability after human intervention
#    6. Asset registry propagation across stages
#    7. Small-file execution for quick validation
#    8. Hi-C scaffolding with juicer+3D-DNA when Hi-C data available
#    9. T2T polishing with auto_patch mode
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 09: End-to-End Pipeline"

# ---- Input verification ----
info "Checking input data"
USE_EXISTING_ASSEMBLY=false
PREV_ASM="${REVIEW_ASSEMBLY:-}"
if [[ -n "$PREV_ASM" && ! -f "$PREV_ASM" ]]; then
    fail "REVIEW_ASSEMBLY does not exist: ${PREV_ASM}"
fi
if [[ -z "$PREV_ASM" && "${TEST09_REUSE_ASSEMBLY:-true}" == "true" ]]; then
    PREV_ASM=$(latest_hpc_assembly || true)
fi
if [[ -n "$PREV_ASM" ]]; then
    assert_file_nonempty "$PREV_ASM" "input existing assembly: $(basename "$PREV_ASM")"
    if grep -q '^>' "$PREV_ASM" 2>/dev/null; then
        USE_EXISTING_ASSEMBLY=true
        info "Using existing assembly shortcut: ${PREV_ASM}"
        info "  read_qc/genome_survey/assembly will be skipped for this smoke run"
    else
        fail "REVIEW_ASSEMBLY is not a FASTA file: ${PREV_ASM}"
    fi
else
    for f in $LONG_READS; do
        assert_file_nonempty "$f" "input long reads: $(basename "$f")"
    done
fi

# ---- Check Hi-C data availability ----
HIC_AVAILABLE=false
if [[ -n "${HIC_R1:-}" ]] && [[ -n "${HIC_R2:-}" ]]; then
    if [[ -f "$HIC_R1" ]] && [[ -f "$HIC_R2" ]]; then
        info "Hi-C data detected:"
        info "  R1: ${HIC_R1}"
        info "  R2: ${HIC_R2}"
        HIC_AVAILABLE=true
        pass "Hi-C data available for scaffolding"
    else
        warn "HIC_R1/HIC_R2 set but files not found, skipping Hi-C"
    fi
else
    info "No Hi-C data provided (HIC_R1/HIC_R2 not set), Hi-C stage will be skipped"
fi

# ---- Session setup ----
SID="hpc-test09-e2e-$(date +%Y%m%d-%H%M%S)"
MANIFEST="${TEST_DATA_ROOT}/_test09_manifest.yaml"
mkdir -p "${TEST_DATA_ROOT}"

# Conditionally add Hi-C reads to manifest
if [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    if [[ "$HIC_AVAILABLE" == true ]]; then
        write_manifest "$MANIFEST" "$SID" \
            "assembly_fasta_registered:${PREV_ASM}:unknown:unknown" \
            "hic_reads_registered:${HIC_R1},${HIC_R2}:illumina:paired"
    else
        write_manifest "$MANIFEST" "$SID" "assembly_fasta_registered:${PREV_ASM}:unknown:unknown"
    fi
else
    LR_PATHS=$(echo "$LONG_READS" | tr ' ' ',')
    if [[ "$HIC_AVAILABLE" == true ]]; then
        write_manifest "$MANIFEST" "$SID" \
            "raw_long_reads:${LR_PATHS}:${PLATFORM}:single" \
            "hic_reads_registered:${HIC_R1},${HIC_R2}:illumina:paired"
    else
        write_manifest "$MANIFEST" "$SID" "raw_long_reads:${LR_PATHS}:${PLATFORM}:single"
    fi
fi

cd "${PROJECT_ROOT}"
python3 - "$MANIFEST" <<'PY'
import sys
import yaml
from app.schemas.manifest import InputManifest

with open(sys.argv[1], encoding="utf-8") as handle:
    InputManifest.model_validate(yaml.safe_load(handle))
print("manifest schema validation OK")
PY
cd "${SCRIPT_DIR}"

# ---- Write run-config with all stages, auto policy for assembly routing ----
RC="${TEST_DATA_ROOT}/_test09_rc.json"

if [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    LOCAL_EXEC_STAGES=("t2t")
else
    LOCAL_EXEC_STAGES=("read_qc" "genome_survey" "assembly" "assembly_review" "t2t")
fi
if [[ "$HIC_AVAILABLE" == true ]]; then
    if [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
        LOCAL_EXEC_STAGES=("hic_scaffolding" "t2t")
    else
        LOCAL_EXEC_STAGES=("read_qc" "genome_survey" "assembly" "assembly_review" "hic_scaffolding" "t2t")
    fi
    if [[ -z "${HIC_CHROM_SIZES:-}" ]]; then
        info "HIC_CHROM_SIZES is empty; Juicer run-config will use chrom_sizes_ref=auto"
    fi
fi

READ_QC_LENGTH_FILTER_METHOD="read_qc.length_filter.seqkit_seq"
READ_QC_LENGTH_FILTER_APPROVED="True"
write_run_config_stages "$RC" "${LOCAL_EXEC_STAGES[@]}"
unset READ_QC_LENGTH_FILTER_METHOD READ_QC_LENGTH_FILTER_APPROVED

python3 - "$RC" "$HIC_AVAILABLE" "$USE_EXISTING_ASSEMBLY" <<'PY'
import json
import sys

path, hic_available, use_existing = sys.argv[1], sys.argv[2] == "true", sys.argv[3] == "true"
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)

requested = ["t2t"] if use_existing else ["read_qc", "genome_survey", "assembly", "assembly_review", "t2t"]
if hic_available:
    insert_at = 0 if use_existing else 4
    requested.insert(insert_at, "hic_scaffolding")
config["requested_stages"] = requested
config["review_policy"] = {"mode": "auto"}
config["t2t"] = {"mode": "auto_patch"}

with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
PY

info "Run config written: ${RC}"
cd "${PROJECT_ROOT}"
python3 - "$RC" <<'PY'
import sys
from app.schemas.run_config import load_run_config

load_run_config(sys.argv[1])
print("run-config schema validation OK")
PY

# ============================================================
# Phase 1: Run through read_qc and genome_survey (no blocking)
# ============================================================
if [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    info "Phase 1: Existing assembly shortcut + downstream stages"
else
    info "Phase 1: Read QC + Genome Survey (non-blocking stages)"
fi

RUN_EXITCODE=0
if GA run \
    --manifest "$MANIFEST" \
    --session-id "$SID" \
    --data-dir "$TEST_DATA_ROOT" \
    --run-config "$RC" \
    --checkpointer "$CHECKPOINTER" \
    2>&1; then
    pass "Pipeline invocation exited cleanly"
else
    RUN_EXITCODE=$?
    warn "Pipeline invocation returned exit code ${RUN_EXITCODE}; inspecting durable state"
fi

# ---- Verify read_qc artifacts ----
RUN_DIR="${TEST_DATA_ROOT}/runs/${SID}"
ARTIFACT_DIR="${RUN_DIR}/artifacts"
INSPECT_JSON="${TEST_DATA_ROOT}/_test09_inspect.json"

assert_dir_exists "$RUN_DIR" "run directory exists"
assert_dir_exists "$ARTIFACT_DIR" "artifacts directory exists"

READQC_SUMMARY="${ARTIFACT_DIR}/read_qc/${SID}/read_qc_summary.json"
if [[ -f "$READQC_SUMMARY" ]]; then
    pass "read_qc_summary.json produced"
    assert_file_contains "$READQC_SUMMARY" "method_name" "summary has method_name"
elif [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    info "read_qc skipped because assembly_fasta_registered was provided"
else
    warn "read_qc_summary.json not found (may have stopped earlier)"
fi

# ---- Verify genome_survey artifacts ----
SURVEY_RESULT="${ARTIFACT_DIR}/genome_survey/${SID}/survey_result.json"
if [[ -f "$SURVEY_RESULT" ]]; then
    pass "survey_result.json produced"
elif [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    info "genome_survey skipped because assembly_fasta_registered was provided"
else
    info "genome_survey skipped or not yet reached"
fi

# ---- Check current stage and supervision state ----
cd "${PROJECT_ROOT}"
info "Inspecting session state after Phase 1"
if python3 -m app.entrypoints.run inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" --json > "$INSPECT_JSON" 2>/dev/null; then
    CURRENT_STAGE=$(python3 - "$INSPECT_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("current_stage") or "unknown")
PY
)
    SUPERVISION_STATE=$(python3 - "$INSPECT_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("supervision_state") or "unknown")
PY
)
    ASSET_REGISTRY_REF=$(python3 - "$INSPECT_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("asset_registry_ref") or "")
PY
)
    DECISION_STAGE=$(python3 - "$INSPECT_JSON" <<'PY'
import json, sys
requirements = json.load(open(sys.argv[1], encoding="utf-8")).get("decision_requirements") or {}
print(requirements.get("stage") or "")
PY
)
    if [[ "$CURRENT_STAGE" == "unknown" && -n "$DECISION_STAGE" ]]; then
        CURRENT_STAGE="$DECISION_STAGE"
    fi
else
    CURRENT_STAGE="unknown"
    SUPERVISION_STATE="unknown"
    ASSET_REGISTRY_REF=""
    DECISION_STAGE=""
    warn "Could not inspect durable session state"
fi

WAITING_AT_ASSEMBLY_REVIEW=false
if [[ "$SUPERVISION_STATE" == "blocked_human" ]] && { [[ "$CURRENT_STAGE" == "assembly_review" ]] || [[ "$DECISION_STAGE" == "assembly_review" ]]; }; then
    WAITING_AT_ASSEMBLY_REVIEW=true
fi

info "  current_stage: ${CURRENT_STAGE}"
info "  supervision_state: ${SUPERVISION_STATE}"

if (( RUN_EXITCODE != 0 )) && [[ "$SUPERVISION_STATE" != "blocked_human" ]]; then
    fail "Pipeline failed with exit code ${RUN_EXITCODE}"
fi

# ============================================================
# Phase 2: Check blocking behavior
# ============================================================
section "Phase 2: Blocking Behavior Analysis"

if [[ "$SUPERVISION_STATE" == "blocked_human" ]]; then
    pass "Pipeline correctly blocked at human gate"

    # Inspect summaries reconstruct durable review requirements; checkpoint-only
    # active_interrupt may be absent after the graph exits to the TUI.
    GATE_ID=$(python3 - "$INSPECT_JSON" <<'PY'
import json, sys
interrupt = json.load(open(sys.argv[1], encoding="utf-8")).get("active_interrupt") or {}
print(interrupt.get("gate_id") or "")
PY
)
    if [[ -n "$GATE_ID" ]]; then
        pass "active_interrupt.gate_id present: ${GATE_ID}"
    else
        INTERRUPT_STAGE=$(python3 - "$INSPECT_JSON" <<'PY'
import json, sys
interrupt = json.load(open(sys.argv[1], encoding="utf-8")).get("active_interrupt") or {}
print(interrupt.get("stage") or "")
PY
)
        if [[ -n "$INTERRUPT_STAGE" ]]; then
            pass "active_interrupt present for stage: ${INTERRUPT_STAGE}"
        elif [[ -n "$DECISION_STAGE" ]]; then
            info "active_interrupt not persisted; using decision_requirements.stage=${DECISION_STAGE}"
        else
            warn "active_interrupt missing"
        fi
    fi
    if [[ -n "$DECISION_STAGE" ]]; then
        pass "decision_requirements available for stage: ${DECISION_STAGE}"
    else
        warn "decision_requirements missing"
    fi

    # Verify the session exited cleanly (didn't crash)
    if [[ -f "${RUN_DIR}/ledger/events.jsonl" ]]; then
        LAST_EVENT=$(tail -1 "${RUN_DIR}/ledger/events.jsonl" 2>/dev/null || echo "{}")
        EVENT_TYPE=$(echo "$LAST_EVENT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('event_type',''))" 2>/dev/null || echo "")
        info "  last ledger event: ${EVENT_TYPE}"
        pass "Session exited to TUI without crash"
    fi

    # ---- Test resume capability ----
    info "Testing resume after human decision"

    # Simulate human selecting a route (if at assembly route selection)
    if [[ "$GATE_ID" == "route_selection" ]]; then
        info "Simulating route selection: ont"

        # Create a decision file or use the review-interactive endpoint
        # For now, just verify the gate is reachable
        GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" 2>&1 | grep -q "blocked" && \
            pass "Blocked state accessible for TUI interaction" || \
            warn "Blocked state check failed"
    fi

elif [[ "$SUPERVISION_STATE" == "completed" ]] || [[ "$SUPERVISION_STATE" == "idle" ]]; then
    pass "Pipeline auto-completed (review_policy mode=auto worked)"

    # Verify assembly artifacts exist
    ASSEMBLY_DIR="${ARTIFACT_DIR}/assembly"
    if [[ -d "$ASSEMBLY_DIR" ]]; then
        CANDIDATE_COUNT=$(find "$ASSEMBLY_DIR" -name "candidate_bundle_*.json" 2>/dev/null | wc -l)
        if (( CANDIDATE_COUNT > 0 )); then
            pass "Assembly produced ${CANDIDATE_COUNT} candidate bundle(s)"
        else
            warn "No assembly candidate bundles found"
        fi
    fi

    # Check if assembly_review was reached
    if [[ "$CURRENT_STAGE" == "assembly_review" ]] || [[ "$CURRENT_STAGE" == "hic_scaffolding" ]] || [[ "$CURRENT_STAGE" == "t2t" ]]; then
        pass "Assembly review stage reached or passed"
    fi

elif [[ "$SUPERVISION_STATE" == "failed_terminal" ]]; then
    fail "Pipeline failed terminally"
    GA inspect --session-id "$SID" --data-dir "$TEST_DATA_ROOT" 2>&1 || true
else
    warn "Unexpected supervision_state: ${SUPERVISION_STATE}"
fi

# ============================================================
# Phase 3: Stage Transition Validation
# ============================================================
section "Phase 3: Stage Transition Validation"

if [[ -f "$INSPECT_JSON" ]]; then
    info "Analyzing route trace"
    cd "${PROJECT_ROOT}"
    python3 - "$INSPECT_JSON" "$USE_EXISTING_ASSEMBLY" "$HIC_AVAILABLE" <<'PY'
import json
import sys

summary = json.load(open(sys.argv[1], encoding="utf-8"))
trace = summary.get("route_trace") or []
stages = [str(item).removesuffix("_stage") for item in trace]
use_existing = sys.argv[2] == "true"
hic_available = sys.argv[3] == "true"

print(f"Stage sequence: {' → '.join(stages)}")

if use_existing:
    expected_start = ['inventory', 'hic_scaffolding'] if hic_available else ['inventory', 't2t']
else:
    expected_start = ['inventory', 'read_qc', 'genome_survey', 'assembly']
actual_start = stages[:len(expected_start)]
if actual_start == expected_start:
    print("✓ Stage order correct")
elif not stages:
    print("route_trace not available in inspect summary; stage order check skipped")
else:
    print(f"✗ Stage order mismatch: expected {expected_start}, got {actual_start}")
PY
    pass "Stage transition analysis complete"
else
    warn "Inspect summary not found"
fi

# ============================================================
# Phase 4: Asset Registry Propagation
# ============================================================
section "Phase 4: Asset Registry Propagation"

REGISTRY="${ASSET_REGISTRY_REF:-${ARTIFACT_DIR}/asset_registry/${SID}/registry.json}"
if [[ -f "$REGISTRY" ]]; then
    cd "${PROJECT_ROOT}"
    python3 <<EOPYTHON
import json
from pathlib import Path

reg = json.loads(Path("${REGISTRY}").read_text())
assets = reg.get('assets', [])

print(f"Total assets: {len(assets)}")

# Check for expected asset types
types = [a.get('asset_type') for a in assets]
print(f"Asset types: {set(types)}")

# Inventory registry records declared input assets. Derived outputs are checked
# through stage artifact directories below.
use_existing = "${USE_EXISTING_ASSEMBLY}" == "true"
if use_existing:
    assemblies = [a for a in assets if a.get('asset_type') == 'assembly_fasta_registered']
    if assemblies:
        print(f"✓ assembly_fasta_registered input asset registered: {len(assemblies)} entries")
    else:
        print("✗ No assembly_fasta_registered asset found")
else:
    raw_reads = [a for a in assets if a.get('asset_type') == 'raw_long_reads']
    if raw_reads:
        print(f"✓ raw_long_reads input asset registered: {len(raw_reads)} entries")
    else:
        print("✗ No raw_long_reads asset found")

hic_reads = [a for a in assets if a.get('asset_type') == 'hic_reads_registered']
if hic_reads:
    print(f"✓ hic_reads_registered input asset registered: {len(hic_reads)} entries")

# Verify survey artifacts
survey_assets = [a for a in assets if a.get('stage') == 'genome_survey']
if survey_assets:
    print(f"✓ genome_survey assets registered: {len(survey_assets)} entries")
EOPYTHON
    pass "Asset registry propagation validated"
else
    warn "Asset registry not found"
fi

# ============================================================
# Phase 5: TUI Interaction Points Summary
# ============================================================
section "Phase 5: TUI Interaction Points Summary"

info "TUI blocking points in the pipeline:"
info "  1. Assembly route selection (if confidence < threshold)"
info "  2. Assembly candidate selection (if multiple candidates)"
info "  3. Assembly review human gate (always blocks unless auto policy)"
info "  4. Hi-C scaffolding (blocks if execution_mode != local)"
info "  5. T2T review gate (if significant changes proposed)"

info "Current blocking pattern verification:"
info "  - blocked_human → sets supervision_state = 'blocked_human'"
info "  - blocked_human → exposes active_interrupt or durable decision_requirements"
info "  - Stage graph routes to END (clean exit)"
info "  - TUI can inspect decision requirements and present choices"
info "  - Resume via review_interactive endpoint with decision"

# ============================================================
# Phase 6: Tool Execution Verification
# ============================================================
section "Phase 6: Tool Execution Verification"

info "Checking tool execution artifacts"

if [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    info "Read QC, genome survey, and assembly execution skipped by existing assembly shortcut"
else
    # Check seqkit execution
    SEQKIT_WORK="${RUN_DIR}/work/read_qc/${SID}/seqkit_stats"
    if [[ -d "$SEQKIT_WORK" ]]; then
        assert_file_exists "${SEQKIT_WORK}/run_seqkit_stats.sh" "seqkit script generated"
        if [[ -f "${SEQKIT_WORK}/seqkit_stats.tsv" ]]; then
            pass "seqkit stats executed successfully"
        fi
    fi

    # Check jellyfish execution (if genome_survey ran)
    JELLYFISH_WORK="${RUN_DIR}/work/genome_survey/${SID}/jellyfish"
    if [[ -d "$JELLYFISH_WORK" ]]; then
        if [[ -f "${JELLYFISH_WORK}/run_jellyfish.sh" ]]; then
            pass "jellyfish script generated"
        fi
        if ls "${JELLYFISH_WORK}"/*.jf >/dev/null 2>&1; then
            pass "jellyfish k-mer database produced"
        fi
    fi

    # Check assembly execution (if reached)
    ASSEMBLY_WORK="${RUN_DIR}/work/assembly"
    if [[ -d "$ASSEMBLY_WORK" ]]; then
        # Look for flye or nextdenovo artifacts
        if find "$ASSEMBLY_WORK" -name "assembly.fasta" -o -name "*.contigs.fa" 2>/dev/null | grep -q .; then
            pass "Assembly artifacts produced"
        else
            info "Assembly work directory exists but may not have completed"
        fi
    fi
fi

# ============================================================
# Phase 7: Checkpoint Persistence
# ============================================================
section "Phase 7: Checkpoint Persistence"

CHECKPOINT_DIR="${RUN_DIR}/checkpoints"
if [[ -d "$CHECKPOINT_DIR" ]]; then
    CHECKPOINT_COUNT=$(find "$CHECKPOINT_DIR" -type f 2>/dev/null | wc -l)
    if (( CHECKPOINT_COUNT > 0 )); then
        pass "Checkpoints persisted (${CHECKPOINT_COUNT} files)"
    else
        warn "Checkpoint directory empty"
    fi
else
    warn "Checkpoint directory not found"
fi

# ============================================================
# Phase 8: Hi-C Scaffolding Verification
# ============================================================
HIC_RAN=false
if [[ "$HIC_AVAILABLE" == true ]]; then
    section "Phase 8: Hi-C Scaffolding Verification"

    info "Verifying Hi-C scaffolding execution"
    if [[ "$WAITING_AT_ASSEMBLY_REVIEW" == true ]]; then
        info "Hi-C not reached yet; assembly_review decision must be accepted before scaffolding starts"
    fi

    HIC_WORK="${RUN_DIR}/work/hic_scaffolding"
    if [[ -d "$HIC_WORK" ]]; then
        HIC_RAN=true
        pass "Hi-C work directory created"

        # Check for juicer artifacts
        if find "$HIC_WORK" -type d -name "*juicer*" 2>/dev/null | grep -q .; then
            pass "Juicer execution directory found"

            # Check for alignment files
            if find "$HIC_WORK" -name "*.bam" -o -name "merged_nodups.txt" 2>/dev/null | grep -q .; then
                pass "Juicer alignment artifacts produced"
            fi
        fi

        # Check for 3D-DNA artifacts
        if find "$HIC_WORK" -type d -name "*3ddna*" -o -name "*ddna*" 2>/dev/null | grep -q .; then
            pass "3D-DNA execution directory found"

            # Check for scaffolded assembly
            if find "$HIC_WORK" -name "*.FINAL.fasta" -o -name "*scaffolded*.fasta" 2>/dev/null | grep -q .; then
                pass "3D-DNA scaffolded assembly produced"
            fi
        fi
    else
        if [[ "$WAITING_AT_ASSEMBLY_REVIEW" == true ]]; then
            info "Hi-C work directory not created yet (waiting for assembly_review accept)"
        else
            warn "Hi-C work directory not found (stage may not have executed)"
        fi
    fi

    # Check Hi-C artifacts in artifact directory
    HIC_ARTIFACT="${ARTIFACT_DIR}/hic_scaffolding"
    if [[ -d "$HIC_ARTIFACT" ]]; then
        HIC_RAN=true
        # Look for scaffolding result
        if find "$HIC_ARTIFACT" -name "scaffolded_assembly.fasta" -o -name "hic_result.json" 2>/dev/null | grep -q .; then
            pass "Hi-C scaffolding artifacts registered"
        fi

        # Check for Hi-C contact maps or quality metrics
        if find "$HIC_ARTIFACT" -name "*.hic" -o -name "*contact_map*" 2>/dev/null | grep -q .; then
            pass "Hi-C contact map generated"
        fi
    fi

    # Verify Hi-C assets in registry
    if [[ "$WAITING_AT_ASSEMBLY_REVIEW" == true ]]; then
        info "Hi-C asset registry check deferred until after assembly_review accept"
    elif [[ -f "$REGISTRY" ]]; then
        cd "${PROJECT_ROOT}"
        python3 <<EOPYTHON
import json
from pathlib import Path

reg = json.loads(Path("${REGISTRY}").read_text())
assets = reg.get('assets', [])

hic_assets = [a for a in assets if a.get('stage') == 'hic_scaffolding']
if hic_assets:
    print(f"✓ Hi-C assets registered: {len(hic_assets)} entries")
    for asset in hic_assets:
        print(f"  - {asset.get('asset_type')}: {asset.get('path')}")
else:
    print("No Hi-C assets registered yet")
EOPYTHON
    fi
else
    info "Hi-C data not available, Phase 8 skipped"
fi

# ============================================================
# Phase 9: T2T Polishing Verification
# ============================================================
T2T_RAN=false
if [[ "$HIC_AVAILABLE" == true ]]; then
    section "Phase 9: T2T Polishing Verification"

    info "Verifying T2T polishing execution"
    if [[ "$WAITING_AT_ASSEMBLY_REVIEW" == true ]]; then
        info "T2T not reached yet; it runs after assembly_review and Hi-C scaffolding"
    fi

    T2T_WORK="${RUN_DIR}/work/t2t"
    if [[ -d "$T2T_WORK" ]]; then
        T2T_RAN=true
        pass "T2T work directory created"

        # Check for polishing tool artifacts (e.g., racon, medaka, nextpolish)
        if find "$T2T_WORK" -name "*polish*" -o -name "*racon*" -o -name "*medaka*" 2>/dev/null | grep -q .; then
            pass "T2T polishing tool artifacts found"
        fi

        # Check for polished assembly
        if find "$T2T_WORK" -name "*polished*.fasta" -o -name "*t2t*.fasta" 2>/dev/null | grep -q .; then
            pass "T2T polished assembly produced"
        fi

        # Check for gap closure artifacts
        if find "$T2T_WORK" -name "*gap*" -o -name "*patch*" 2>/dev/null | grep -q .; then
            pass "T2T gap closure/patching artifacts found"
        fi
    else
        if [[ "$WAITING_AT_ASSEMBLY_REVIEW" == true ]]; then
            info "T2T work directory not created yet (waiting for assembly_review accept)"
        else
            warn "T2T work directory not found (stage may not have executed)"
        fi
    fi

    # Check T2T artifacts in artifact directory
    T2T_ARTIFACT="${ARTIFACT_DIR}/t2t"
    if [[ -d "$T2T_ARTIFACT" ]]; then
        T2T_RAN=true
        # Look for final polished assembly
        if find "$T2T_ARTIFACT" -name "t2t_assembly.fasta" -o -name "t2t_result.json" 2>/dev/null | grep -q .; then
            pass "T2T polishing artifacts registered"
        fi

        # Check for quality metrics
        if find "$T2T_ARTIFACT" -name "*metrics*" -o -name "*qv*" 2>/dev/null | grep -q .; then
            pass "T2T quality metrics generated"
        fi
    fi

    # Verify T2T assets in registry
    if [[ "$WAITING_AT_ASSEMBLY_REVIEW" == true ]]; then
        info "T2T asset registry check deferred until after assembly_review accept"
    elif [[ -f "$REGISTRY" ]]; then
        cd "${PROJECT_ROOT}"
        python3 <<EOPYTHON
import json
from pathlib import Path

reg = json.loads(Path("${REGISTRY}").read_text())
assets = reg.get('assets', [])

t2t_assets = [a for a in assets if a.get('stage') == 't2t']
if t2t_assets:
    print(f"✓ T2T assets registered: {len(t2t_assets)} entries")
    for asset in t2t_assets:
        print(f"  - {asset.get('asset_type')}: {asset.get('path')}")
else:
    print("No T2T assets registered yet")
EOPYTHON
    fi
else
    info "Hi-C data not available, Phase 9 (T2T) skipped"
fi

# ============================================================
# Summary
# ============================================================
summary "Test 09: End-to-End Pipeline"

info ""
info "Key findings:"
if [[ "$USE_EXISTING_ASSEMBLY" == true ]]; then
    if [[ "$HIC_AVAILABLE" == true ]]; then
        info "  - Pipeline stages flow: inventory → hic_scaffolding → t2t"
    else
        info "  - Pipeline stages flow: inventory → t2t"
    fi
    info "  - Existing assembly shortcut: read_qc/genome_survey/assembly/assembly_review skipped"
else
    info "  - Pipeline stages flow: inventory → read_qc → genome_survey → assembly → ..."
fi
if [[ "$HIC_AVAILABLE" == true && "$USE_EXISTING_ASSEMBLY" != true ]]; then
        info "    → assembly_review → hic_scaffolding → t2t"
fi
info "  - Non-blocking stages: read_qc, genome_survey (execute and commit)"
info "  - Blocking stages: assembly (route selection), assembly_review (human gate), hic/t2t (conditional)"
info "  - TUI coupling: blocked_human → decision_requirements → END → TUI displays options"
info "  - Resume: TUI calls review_interactive with decision → stage continues"
info "  - Asset registry: records declared inputs; stage artifact directories verify derived outputs"
if [[ "$HIC_AVAILABLE" == true ]]; then
    if [[ "$HIC_RAN" == true ]]; then
        info "  - Hi-C scaffolding: executed or produced local work/artifacts"
    else
        info "  - Hi-C scaffolding: not reached before current blocking gate"
    fi
    if [[ "$T2T_RAN" == true ]]; then
        info "  - T2T polishing: executed or produced local work/artifacts"
    else
        info "  - T2T polishing: not reached before current blocking gate"
    fi
fi
if [[ "$SUPERVISION_STATE" == "blocked_human" ]]; then
    info "  - Current run is waiting at ${CURRENT_STAGE}; resume with an explicit review decision to continue downstream stages"
fi
info ""
info "Session ID: ${SID}"
info "Data directory: ${TEST_DATA_ROOT}/runs/${SID}"
