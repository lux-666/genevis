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
assets:
YAML
    for spec in "$@"; do
        IFS=: read -r atype paths plat lay <<< "$spec"
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
    format: fastq
    compression: gzip
YAML
    done
    cat >> "$out" <<YAML
target_assembly_level: chromosome
genome_size_estimate: ${GENOME_SIZE_ESTIMATE}
YAML
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
            'method': 'skip',
            'approved': False,
            'thresholds': {},
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
    rc['assembly'] = {
        'assembly_method': '${ASSEMBLY_METHOD}',
        'target_level': 'draft',
        'assembly_route': 'auto',
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
    # chrom_sizes_ref is required for execution; if not set, leave it out
    # (the pipeline will plan but may not execute)
    chrom_sizes = '${HIC_CHROM_SIZES:-}'
    if chrom_sizes:
        rc['hic_scaffolding']['juicer_3ddna_parameters']['chrom_sizes_ref'] = chrom_sizes

json.dump(rc, open(sys.argv[1], 'w'), indent=2)
" "$out"
}

# -------------------------------------------------------------------
#  gene-agent runner
# -------------------------------------------------------------------
GA() {
    # Run gene-agent with common flags.
    info "gene-agent $*"
    python3 -m app.entrypoints.run "$@"
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
