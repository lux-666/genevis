#!/usr/bin/env bash
# Common utilities for HPC integration tests.
# Sourced by every test script.  Do NOT execute directly.
set -euo pipefail

# -------------------------------------------------------------------
#  Colour helpers
# -------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

pass()  { printf "${GREEN}[PASS]${NC} %s\n" "$*"; (( ++_PASS )); }
fail()  { printf "${RED}[FAIL]${NC} %s\n" "$*"; (( ++_FAIL )); }
warn()  { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
info()  { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
section() { printf "\n${BOLD}=== %s ===${NC}\n" "$*"; }

_PASS=0; _FAIL=0

summary() {
    local label="${1:-Test}"
    printf "\n${BOLD}--- %s Summary ---${NC}\n" "$label"
    printf "  Passed: ${GREEN}%d${NC}\n" "$_PASS"
    printf "  Failed: ${RED}%d${NC}\n" "$_FAIL"
    if (( _FAIL > 0 )); then
        printf "  ${RED}RESULT: FAIL${NC}\n"
        return 1
    else
        printf "  ${GREEN}RESULT: PASS${NC}\n"
        return 0
    fi
}

run_pytest() {
    # hpc_env.sh configures the real smoke commands below, but unit tests must
    # remain hermetic. In particular, an HPC-local method map must not replace
    # the repository defaults used by pytest fixtures, and unit tests must not
    # accidentally call the operator's live LLM endpoint.
    (
        unset GENE_AGENT_ENVS_CONFIG
        unset GENE_AGENT_LLM_API_KEY GENE_AGENT_LLM_BASE_URL GENE_AGENT_LLM_MODEL
        unset OPENAI_API_KEY OPENAI_BASE_URL
        python3 -m pytest "$@"
    )
}

# -------------------------------------------------------------------
#  File / directory assertions
# -------------------------------------------------------------------
assert_file_exists() {
    local f="$1" desc="${2:-$1}"
    if [[ -f "$f" ]]; then pass "file exists: $desc"
    else fail "file missing: $desc ($f)"; fi
}

assert_file_nonempty() {
    local f="$1" desc="${2:-$1}"
    if [[ -s "$f" ]]; then pass "file non-empty: $desc"
    else fail "file empty or missing: $desc ($f)"; fi
}

assert_dir_exists() {
    local d="$1" desc="${2:-$1}"
    if [[ -d "$d" ]]; then pass "dir exists: $desc"
    else fail "dir missing: $desc ($d)"; fi
}

assert_file_contains() {
    local f="$1" pattern="$2" desc="${3:-$f contains $pattern}"
    if grep -q "$pattern" "$f" 2>/dev/null; then pass "$desc"
    else fail "$desc"; fi
}

assert_json_field() {
    local f="$1" field="$2" desc="${3:-$f has .$field}"
    if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert $field" "$f" 2>/dev/null; then
        pass "$desc"
    else fail "$desc"; fi
}

assert_ledger_has_method_attempt() {
    # Require a successful attempt for one exact method, rather than merely
    # finding a stage name somewhere in the JSONL ledger.
    local events="$1" method_name="$2" desc="${3:-ledger succeeded: $2}"
    if python3 - "$events" "$method_name" <<'PY'
import json
import sys

events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
method_name = sys.argv[2]

def event_method(event):
    return event.get("method_name") or (event.get("summary") or {}).get("method_name")

if not any(
    event.get("event_type") == "attempt_succeeded"
    and event_method(event) == method_name
    for event in events
):
    print(f"No attempt_succeeded row found for method {method_name!r}.", file=sys.stderr)
    print("Attempt rows in ledger:", file=sys.stderr)
    for event in events:
        if str(event.get("event_type", "")).startswith("attempt_"):
            print(json.dumps(event, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)
PY
    then
        pass "$desc"
    else
        fail "$desc"
    fi
}

assert_no_duplicate_method_attempts() {
    # Replaying a blocked session must not re-run a completed method attempt.
    local events="$1" desc="${2:-no duplicate method attempts}"
    if python3 - "$events" <<'PY'
import collections
import json
import sys

counts = collections.Counter()
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip():
        continue
    event = json.loads(line)
    if event.get("event_type") != "attempt_started":
        continue
    summary = event.get("summary") or {}
    method_name = event.get("method_name") or summary.get("method_name")
    attempt_id = event.get("attempt_id") or event.get("subject_id")
    key = (event.get("stage"), method_name, attempt_id)
    counts[key] += 1
duplicates = {key: count for key, count in counts.items() if count > 1}
if duplicates:
    print(f"Duplicate attempt_started rows: {duplicates}", file=sys.stderr)
    raise SystemExit(1)
PY
    then
        pass "$desc"
    else
        fail "$desc"
    fi
}

preflight_configured_methods() {
    # Usage: preflight_configured_methods STAGE METHOD [STAGE METHOD ...]
    # Resolve exactly as the application does: method -> stage -> default in
    # config/envs.local.yaml when present, otherwise config/envs.yaml. Do not
    # introduce a second HPC-only environment map.
    local output status=0 line outcome method target detail
    if (( $# == 0 || $# % 2 != 0 )); then
        fail "preflight_configured_methods requires STAGE METHOD pairs"
        return 2
    fi
    if ! output=$(python3 - "$@" <<'PY'
import sys

from app.execution.env_resolver import EnvResolver, preflight

resolver = EnvResolver()
failed = False
for stage, method in zip(sys.argv[1::2], sys.argv[2::2]):
    spec = resolver.resolve(stage=stage, method_name=method)
    result = preflight(spec)
    if result.status == "ok":
        print(f"OK\t{method}\t{spec.display_target()}\t")
    else:
        failed = True
        print(f"FAIL\t{method}\t{result.env_target}\t{result.detail}")
raise SystemExit(1 if failed else 0)
PY
    ); then
        status=1
    fi

    while IFS=$'\t' read -r outcome method target detail; do
        [[ -z "$outcome" ]] && continue
        if [[ "$outcome" == "OK" ]]; then
            pass "configured method ready: ${method} (${target})"
        else
            fail "configured method blocked: ${method} (${target}) — ${detail}"
        fi
    done <<< "$output"
    return "$status"
}

run_configured_method_tool() {
    # Usage: run_configured_method_tool STAGE METHOD COMMAND [ARG ...]
    # Run a small helper command inside the exact method environment that the
    # application will use, rather than trusting the test runner's PATH.
    local stage="$1" method="$2"
    shift 2
    (
    cd "${PROJECT_ROOT}"
    python3 - "$stage" "$method" "$@" <<'PY'
import subprocess
import sys

from app.execution.env_resolver import EnvResolver

stage, method, *command = sys.argv[1:]
spec = EnvResolver().resolve(stage=stage, method_name=method)
result = subprocess.run([*spec.conda_run_prefix(), *command], check=False)
raise SystemExit(result.returncode)
PY
    )
}

latest_test04_assembly() {
    # Print the preferred assembly from the newest Test 04 session. Within a
    # session, use the purged assembly when present, otherwise Flye's draft.
    local -a runs
    local run candidate i
    runs=("${TEST_DATA_ROOT}"/runs/hpc-test04-*)
    for (( i=${#runs[@]} - 1; i >= 0; i-- )); do
        run="${runs[$i]}"
        [[ -d "$run" ]] || continue
        for candidate in "$run"/artifacts/assembly_dedup/*/purged_assembly.fasta; do
            if [[ -s "$candidate" ]]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
        for candidate in "$run"/artifacts/assembly/*/draft_assembly.fasta; do
            if [[ -s "$candidate" ]]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

latest_hpc_assembly() {
    # Prefer a materialized assembly from the newest smoke/E2E-style session.
    # This lets quick smoke runs reuse a known assembly instead of re-running
    # the slow assembly stage every time.
    local -a runs
    local run candidate i
    runs=("${TEST_DATA_ROOT}"/runs/hpc-test04-* "${TEST_DATA_ROOT}"/runs/hpc-test09-*)
    for (( i=${#runs[@]} - 1; i >= 0; i-- )); do
        run="${runs[$i]}"
        [[ -d "$run" ]] || continue
        for candidate in "$run"/artifacts/assembly_dedup/*/purged_assembly.fasta \
                         "$run"/artifacts/assembly/*/flye/draft_assembly.fasta \
                         "$run"/artifacts/assembly/*/draft_assembly.fasta; do
            if [[ -s "$candidate" ]] && grep -q '^>' "$candidate" 2>/dev/null; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

# -------------------------------------------------------------------
#  FASTA / FASTQ helpers
# -------------------------------------------------------------------
count_fasta_seqs() {
    grep -c '^>' "$1" 2>/dev/null || echo 0
}

fasta_total_bp() {
    awk '/^>/{next} {s+=length($0)} END{print s+0}' "$1" 2>/dev/null || echo 0
}

# -------------------------------------------------------------------
#  Manifest writers
# -------------------------------------------------------------------
# Usage: write_manifest OUTPUT_PATH SESSION_ID [asset_type:paths:platform:layout ...]
write_manifest() {
    local out="$1" session_id="$2"; shift 2
    cat > "$out" <<YAML
session_id: "${session_id}"
sample_id: "${SAMPLE_ID}"
user_metadata:
  species: "${SPECIES:-}"
assets:
YAML
    for spec in "$@"; do
        IFS=: read -r atype paths plat lay <<< "$spec"
        local format="fastq" compression="gzip"
        case "$atype" in
            assembly_fasta_registered|repeat_library|reference_proteins)
                format="fasta"; compression="none" ;;
            hic_alignment_bam)
                format="bam"; compression="none" ;;
            hic_merged_nodups)
                format="unknown"; compression="none" ;;
        esac
        cat >> "$out" <<YAML
  - asset_type: ${atype}
    paths:
YAML
        IFS=',' read -ra path_arr <<< "$paths"
        for p in "${path_arr[@]}"; do
            echo "      - ${p}" >> "$out"
        done
        cat >> "$out" <<YAML
    platform: ${plat}
    layout: ${lay}
    format: ${format}
    compression: ${compression}
YAML
    done
    cat >> "$out" <<YAML
target_assembly_level: chromosome
YAML
    # GenomeScope must run without an operator-provided size. Individual tests
    # can set MANIFEST_GENOME_SIZE_ESTIMATE="" deliberately.
    local manifest_size="${MANIFEST_GENOME_SIZE_ESTIMATE-${GENOME_SIZE_ESTIMATE:-}}"
    if [[ -n "$manifest_size" ]]; then
        echo "genome_size_estimate: ${manifest_size}" >> "$out"
    fi
}

# -------------------------------------------------------------------
#  Run-config writers
# -------------------------------------------------------------------
write_run_config_stages() {
    # Writes a run-config JSON enabling the given stages.
    # Usage: write_run_config_stages OUTPUT_PATH STAGE1 STAGE2 ...
    local out="$1"; shift
    local stages_json
    stages_json=$(printf ',"%s"' "$@" | cut -c2-)
    python3 -c "
import json, sys
stages = [$(printf '"%s",' "$@" | sed 's/,$//')]
rc = {
    'local_exec': stages,
    'requested_stages': stages,
}

# Shared parameters
genome_size_text = '''${GENOME_SIZE_ESTIMATE:-}'''.strip()
genome_size = int(genome_size_text) if genome_size_text else None
platform_to_read_type = {
    'cycloneseq': 'cyclone',
    'ont': 'ont_raw',
    'clr': 'pacbio_clr',
    'pacbio_hifi': 'pacbio_hifi',
}
rc['shared_parameters'] = {
    'read_type': platform_to_read_type.get('${PLATFORM}', '${PLATFORM}'),
}
if genome_size is not None:
    rc['shared_parameters']['estimated_genome_size'] = genome_size

# Read QC config
if 'read_qc' in stages:
    rc['read_qc'] = {
        'length_filter': {
            'method': '${READ_QC_LENGTH_FILTER_METHOD:-skip}',
            'approved': ${READ_QC_LENGTH_FILTER_APPROVED:-False},
            'thresholds': {'min_len': ${READ_QC_MIN_LEN:-1000}} if '${READ_QC_LENGTH_FILTER_METHOD:-skip}' == 'read_qc.length_filter.seqkit_seq' else {},
        }
    }

# Genome survey config
if 'genome_survey' in stages:
    rc['genome_survey'] = {
        'survey_parameters': {
            'kmer_size': 21,
            'hash_size': '1G',
            'threads': 4,
        }
    }

# Assembly config
if 'assembly' in stages:
    assembly_route = {
        'cycloneseq': 'cyclone',
        'pacbio_hifi': 'hifi',
    }.get('${PLATFORM}', 'ont')
    assembly_method = '${ASSEMBLY_METHOD}'
    rc['assembly'] = {
        'assembly_method': assembly_method,
        'target_level': 'draft',
        # This is an unattended integration test. An explicit route avoids the
        # interactive LLM/human route-selection gate before the assembler starts.
        'assembly_route': assembly_route,
    }
    if assembly_method == 'flye':
        rc['assembly']['flye_parameters'] = {
            'read_type': '${FLYE_READ_TYPE}',
            'threads': ${ASSEMBLY_THREADS},
        }
        if genome_size is not None:
            rc['assembly']['flye_parameters']['estimated_genome_size'] = genome_size
    elif assembly_method == 'nextdenovo':
        nextdenovo_read_type = {
            'cycloneseq': 'ont',
            'ont': 'ont',
            'clr': 'clr',
            'pacbio_hifi': 'hifi',
        }.get('${PLATFORM}', '${PLATFORM}')
        rc['assembly']['nextdenovo_parameters'] = {
            'read_type': nextdenovo_read_type,
            'threads': ${ASSEMBLY_THREADS},
        }
        if genome_size is not None:
            rc['assembly']['nextdenovo_parameters']['genome_size'] = genome_size
    elif assembly_method == 'hifiasm':
        rc['assembly']['hifiasm_parameters'] = {
            'read_type': 'hifi',
            'threads': ${ASSEMBLY_THREADS},
        }
    elif assembly_method == 'hifiasm_ont':
        rc['assembly']['hifiasm_ont_parameters'] = {
            'read_type': 'ont',
            'threads': ${ASSEMBLY_THREADS},
        }

# Assembly review config
if 'assembly_review' in stages:
    rc['assembly_review'] = {
        'quast': {'threads': 4},
        'busco': {
            'lineage_path': '${BUSCO_LINEAGE_PATH}',
            'lineage_name': '${BUSCO_LINEAGE_NAME}',
            'lineage_version': 'odb10',
            'lineage_source': 'local',
            'threads': ${ASSEMBLY_THREADS},
        },
    }

# Hi-C config
if 'hic_scaffolding' in stages:
    rc['hic_scaffolding'] = {
        'autohic_parameters': {
            'restriction_enzyme': '${HIC_RESTRICTION_ENZYME}',
            'threads': ${ASSEMBLY_THREADS},
        },
    }

json.dump(rc, open(sys.argv[1], 'w'), indent=2)
" "$out"
}

write_genome_survey_run_config() {
    # Deliberately omits shared_parameters.estimated_genome_size so this smoke
    # validates the measured genome-size path without an operator override.
    local out="$1"
    python3 - "$out" <<'PY'
import json
import sys

json.dump(
    {
        "local_exec": ["read_qc", "genome_survey"],
        "requested_stages": ["read_qc", "genome_survey"],
        "read_qc": {"length_filter": {"method": "skip", "approved": False, "thresholds": {}}},
        "genome_survey": {"survey_parameters": {"kmer_size": 21, "hash_size": "1G", "threads": 4}},
    },
    open(sys.argv[1], "w", encoding="utf-8"),
    indent=2,
)
PY
}

# -------------------------------------------------------------------
#  gene-agent runner
# -------------------------------------------------------------------
GA() {
    info "gene-agent $*"
    if [[ "${GENE_AGENT_HPC_INTERACTIVE:-false}" == "true" && -t 0 ]]; then
        python3 -m app.entrypoints.run "$@"
    else
        # Batch HPC tests must never consume the terminal at a human-review
        # gate. A blocked gate is reported and asserted by the owning script.
        python3 -m app.entrypoints.run "$@" </dev/null
    fi
}

resume_configured_parameter_gates() {
    # Resume any stage's parameter gate only when hpc_env.sh explicitly opts
    # into unattended approval. Production defaults remain human-reviewed.
    # Return 3 when a manual approval is required.
    local session_id="$1"
    local mode="${HPC_PARAMETER_APPROVAL_MODE:-manual}"
    local max_approvals="${HPC_MAX_PARAMETER_APPROVALS:-3}"
    local inspect_json decision_json gate stage supervision approval_count=0
    HPC_PARAMETER_APPROVALS_RESUMED=0

    case "$mode" in
        manual|approve) ;;
        *) fail "invalid HPC_PARAMETER_APPROVAL_MODE=${mode} (expected manual or approve)"; return 2 ;;
    esac
    [[ "$max_approvals" =~ ^[1-9][0-9]*$ ]] || {
        fail "HPC_MAX_PARAMETER_APPROVALS must be a positive integer"
        return 2
    }

    inspect_json="${TEST_DATA_ROOT}/_${session_id}_inspect.json"
    decision_json="${TEST_DATA_ROOT}/_${session_id}_parameter_decision.json"
    while true; do
        if ! python3 -m app.entrypoints.run inspect \
            --session-id "$session_id" --data-dir "$TEST_DATA_ROOT" --json \
            > "$inspect_json" 2>/dev/null; then
            fail "cannot inspect session while checking parameter approval: ${session_id}"
            return 1
        fi
        IFS=$'\t' read -r supervision gate stage < <(python3 - "$inspect_json" <<'PY'
import json
import sys

summary = json.load(open(sys.argv[1], encoding="utf-8"))
requirements = summary.get("decision_requirements") or {}
interrupt = summary.get("active_interrupt") or {}
print(
    summary.get("supervision_state") or "unknown",
    requirements.get("gate_id") or interrupt.get("gate_id") or "none",
    requirements.get("stage") or interrupt.get("stage") or summary.get("current_stage") or "unknown",
    sep="\t",
)
PY
)
        if [[ "$supervision" != "blocked_human" || "$gate" != "parameter_approval" ]]; then
            return 0
        fi

        if [[ "$mode" == "manual" ]]; then
            warn "session ${session_id} is waiting at ${stage} parameter_approval"
            info "Review it in the TUI, or generate a bound decision template:"
            info "  python3 -m app.entrypoints.run inspect --session-id ${session_id} --data-dir ${TEST_DATA_ROOT} --write-decision-template ${decision_json}"
            info "  python3 -m app.entrypoints.run resume --session-id ${session_id} --data-dir ${TEST_DATA_ROOT} --decision-file ${decision_json} --checkpointer ${CHECKPOINTER}"
            info "For an explicitly unattended smoke test, set HPC_PARAMETER_APPROVAL_MODE=approve in hpc_env.sh."
            return 3
        fi

        if (( approval_count >= max_approvals )); then
            fail "session exceeded HPC_MAX_PARAMETER_APPROVALS=${max_approvals}"
            return 1
        fi
        (( ++approval_count ))

        info "Generating bound decision template for parameter approval ${approval_count}/${max_approvals}"
        if ! python3 -m app.entrypoints.run inspect \
            --session-id "$session_id" --data-dir "$TEST_DATA_ROOT" \
            --write-decision-template "$decision_json" >/dev/null; then
            fail "could not generate parameter-approval decision template"
            return 1
        fi
        if ! python3 - "$decision_json" "$stage" <<'PY'
import json
import sys

path = sys.argv[1]
expected_stage = sys.argv[2]
decision = json.load(open(path, encoding="utf-8"))
assert decision.get("stage") == expected_stage, decision
assert decision.get("gate_id") == "parameter_approval", decision
assert decision.get("decision") == "approve_parameters", decision
metadata = decision.get("metadata") or {}
for key in ("attempt_id", "task", "previous_attempt_id", "recommendation_hash"):
    assert key in metadata, f"decision template missing bound metadata.{key}"
decision["notes"] = ["Explicit unattended HPC integration-test approval"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(decision, handle, ensure_ascii=False, indent=2)
PY
        then
            fail "parameter-approval template is not bound to the exact recommendation"
            return 1
        fi

        info "Resuming ${session_id} with explicit test-only parameter approval"
        if ! GA resume \
            --session-id "$session_id" \
            --data-dir "$TEST_DATA_ROOT" \
            --decision-file "$decision_json" \
            --checkpointer "$CHECKPOINTER" 2>&1; then
            fail "parameter approval resume failed"
            return 1
        fi
        (( ++HPC_PARAMETER_APPROVALS_RESUMED ))
    done
}

inspect_session_gate() {
    # Print: supervision_state<TAB>gate_id<TAB>stage. The inspect summary is
    # the durable source of truth after a graph invocation returns to its
    # caller; do not infer a gate from the CLI exit code alone.
    local session_id="$1"
    local inspect_json="${TEST_DATA_ROOT}/_${session_id}_inspect.json"
    python3 -m app.entrypoints.run inspect \
        --session-id "$session_id" --data-dir "$TEST_DATA_ROOT" --json \
        > "$inspect_json" 2>/dev/null || return 1
    python3 - "$inspect_json" <<'PY'
import json
import sys

summary = json.load(open(sys.argv[1], encoding="utf-8"))
requirements = summary.get("decision_requirements") or {}
interrupt = summary.get("active_interrupt") or {}
print(
    summary.get("supervision_state") or "unknown",
    requirements.get("gate_id") or interrupt.get("gate_id") or "none",
    requirements.get("stage") or interrupt.get("stage") or summary.get("current_stage") or "unknown",
    sep="\t",
)
PY
}

report_session_gate() {
    # Return 3 when a durable human gate is active. Callers map this to exit
    # 77: waiting for a person is an intentional incomplete smoke, not FAIL.
    local session_id="$1"
    local details supervision gate stage
    if ! details=$(inspect_session_gate "$session_id"); then
        fail "cannot inspect durable session: ${session_id}"
        return 1
    fi
    IFS=$'\t' read -r supervision gate stage <<< "$details"
    case "$supervision" in
        blocked_human)
            warn "session ${session_id} is waiting for human input"
            info "  stage: ${stage}"
            info "  gate:  ${gate}"
            info "Continue the same conversation/session with:"
            info "  gene-agent tui --resume-session ${session_id} --data-dir ${TEST_DATA_ROOT}"
            info "Non-interactive inspection/template flow:"
            info "  python3 -m app.entrypoints.run inspect --session-id ${session_id} --data-dir ${TEST_DATA_ROOT} --write-decision-template ${TEST_DATA_ROOT}/_${session_id}_${gate}_decision.json"
            info "Run directory: ${TEST_DATA_ROOT}/runs/${session_id}"
            return 3
            ;;
        failed_terminal)
            fail "session ${session_id} is durably failed at stage ${stage}"
            return 1
            ;;
        unknown|"")
            fail "session ${session_id} has unknown durable supervision state at stage ${stage}"
            return 1
            ;;
        idle|running_stage|reviewing|completed)
            return 0
            ;;
        *)
            fail "session ${session_id} has unsupported durable supervision state: ${supervision}"
            return 1
            ;;
    esac
}

latest_hic_scaffold() {
    local -a runs
    local run candidate i
    runs=("${TEST_DATA_ROOT}"/runs/hpc-test06-* "${TEST_DATA_ROOT}"/runs/hpc-test09-*)
    for (( i=${#runs[@]} - 1; i >= 0; i-- )); do
        run="${runs[$i]}"
        [[ -d "$run" ]] || continue
        for candidate in "$run"/artifacts/hic_scaffolding/*/*scaffolded*.fasta \
                         "$run"/artifacts/hic_scaffolding/*/*FINAL*.fasta; do
            if [[ -s "$candidate" ]] && grep -q '^>' "$candidate" 2>/dev/null; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

# -------------------------------------------------------------------
#  Load config
# -------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/hpc_env.sh" ]]; then
    source "${SCRIPT_DIR}/hpc_env.sh"
elif [[ -f "${SCRIPT_DIR}/hpc_env.sh.example" ]]; then
    echo "ERROR: ${SCRIPT_DIR}/hpc_env.sh not found."
    echo "  Copy the example and edit it:"
    echo "    cp hpc_env.sh.example hpc_env.sh && vim hpc_env.sh"
    exit 2
else
    echo "ERROR: Cannot find hpc_env.sh or hpc_env.sh.example"
    exit 2
fi

export GENE_AGENT_DATA_ROOT="${TEST_DATA_ROOT}"
export GENE_AGENT_CHECKPOINTER="${CHECKPOINTER}"

ENV_CONFIG="${PROJECT_ROOT}/config/envs.yaml"
[[ -f "${PROJECT_ROOT}/config/envs.local.yaml" ]] && ENV_CONFIG="${PROJECT_ROOT}/config/envs.local.yaml"
export GENE_AGENT_ENVS_CONFIG="$ENV_CONFIG"
