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
    if python3 - "$events" "$method_name" <<'PY' 2>/dev/null
import json
import sys

events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
assert any(
    event.get("event_type") == "attempt_succeeded"
    and event.get("method_name") == sys.argv[2]
    for event in events
)
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
    if python3 - "$events" <<'PY' 2>/dev/null
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
    key = (event.get("stage"), event.get("method_name"), event.get("attempt_id"))
    counts[key] += 1
duplicates = {key: count for key, count in counts.items() if count > 1}
assert not duplicates, duplicates
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
    local manifest_size="${MANIFEST_GENOME_SIZE_ESTIMATE-${GENOME_SIZE_ESTIMATE}}"
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
rc = {'local_exec': stages}

# Shared parameters
rc['shared_parameters'] = {
    'read_type': '${PLATFORM}_raw' if '${PLATFORM}' == 'ont' else '${PLATFORM}',
    'estimated_genome_size': ${GENOME_SIZE_ESTIMATE},
}

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
    rc['assembly'] = {
        'assembly_method': '${ASSEMBLY_METHOD}',
        'target_level': 'draft',
        # This is an unattended integration test. An explicit route avoids
        # the interactive LLM/human route-selection gate before Flye starts.
        'assembly_route': assembly_route,
    }
    if '${ASSEMBLY_METHOD}' == 'flye':
        rc['assembly']['flye_parameters'] = {
            'read_type': '${FLYE_READ_TYPE}',
            'estimated_genome_size': ${GENOME_SIZE_ESTIMATE},
            'threads': ${ASSEMBLY_THREADS},
        }
    elif '${ASSEMBLY_METHOD}' == 'nextdenovo':
        rc['assembly']['nextdenovo_parameters'] = {
            'read_type': '${PLATFORM}',
            'genome_size': ${GENOME_SIZE_ESTIMATE},
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
        'methods': ['juicer_3ddna_juicebox'],
        'input_mode': 'raw_reads',
        'chromosome_count': ${HIC_CHROMOSOME_COUNT},
        'juicer_3ddna_parameters': {
            'restriction_enzyme': '${HIC_RESTRICTION_ENZYME}',
            'threads': ${ASSEMBLY_THREADS},
        },
    }
    juicer_scripts_dir = '${HIC_JUICER_SCRIPTS_DIR:-}'
    if juicer_scripts_dir:
        rc['hic_scaffolding']['juicer_3ddna_parameters']['juicer_scripts_dir'] = juicer_scripts_dir
    # chrom_sizes_ref is required for execution; if not set, leave it out
    # (the pipeline will plan but may not execute)
    chrom_sizes = '${HIC_CHROM_SIZES:-}'
    if chrom_sizes:
        rc['hic_scaffolding']['juicer_3ddna_parameters']['chrom_sizes_ref'] = chrom_sizes

json.dump(rc, open(sys.argv[1], 'w'), indent=2)
" "$out"
}

write_genome_survey_run_config() {
    # Deliberately omits shared_parameters.estimated_genome_size: that value
    # sends the stage down its explicit-prior skip route.
    local out="$1"
    python3 - "$out" <<'PY'
import json
import sys

json.dump(
    {
        "local_exec": ["read_qc", "genome_survey"],
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
    # HPC tests must never consume the terminal at a human-review gate.
    # A blocked gate is reported and asserted by the owning script instead.
    info "gene-agent $*"
    python3 -m app.entrypoints.run "$@" </dev/null
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
