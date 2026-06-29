#!/usr/bin/env bash
# ============================================================
#  Quick E2E Pipeline Runner for HPC
# ============================================================
#  Simplified launcher for test_09 that handles environment setup
#  and provides clear feedback about configuration status.
#
#  Usage:
#    ./quick_run.sh              # Run full e2e test with current config
#    ./quick_run.sh --check      # Check configuration without running
#    ./quick_run.sh --setup      # Interactive setup wizard
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Color helpers
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()  { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}[OK]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
error() { printf "${RED}[ERROR]${NC} %s\n" "$*"; }
section() { printf "\n${BOLD}=== %s ===${NC}\n" "$*"; }

# ============================================================
# Configuration Check
# ============================================================
check_config() {
    section "Configuration Check"

    if [[ ! -f "${SCRIPT_DIR}/hpc_env.sh" ]]; then
        error "hpc_env.sh not found"
        info "Copy the example and configure it:"
        info "  cp hpc_env.sh.example hpc_env.sh"
        info "  vim hpc_env.sh"
        return 1
    fi

    ok "hpc_env.sh found"

    # Source the config
    source "${SCRIPT_DIR}/hpc_env.sh"

    # Check required variables
    local missing=0

    if [[ -z "${LONG_READS:-}" ]]; then
        error "LONG_READS not configured"
        ((missing++))
    elif [[ ! -f "$LONG_READS" ]]; then
        error "LONG_READS file not found: $LONG_READS"
        ((missing++))
    else
        ok "Long reads: $LONG_READS"
    fi

    if [[ -z "${GENOME_SIZE_ESTIMATE:-}" ]]; then
        warn "GENOME_SIZE_ESTIMATE not set"
    else
        ok "Genome size: $GENOME_SIZE_ESTIMATE bp"
    fi

    if [[ -z "${PLATFORM:-}" ]]; then
        warn "PLATFORM not set (default: ont)"
    else
        ok "Platform: $PLATFORM"
    fi

    if [[ -z "${BUSCO_LINEAGE_PATH:-}" ]] || [[ ! -d "$BUSCO_LINEAGE_PATH" ]]; then
        warn "BUSCO lineage not available: ${BUSCO_LINEAGE_PATH:-not set}"
    else
        ok "BUSCO lineage: $BUSCO_LINEAGE_NAME"
    fi

    # Check optional Hi-C data
    if [[ -n "${HIC_R1:-}" && -f "$HIC_R1" && -n "${HIC_R2:-}" && -f "$HIC_R2" ]]; then
        ok "Hi-C reads available: will execute real scaffolding"
        if [[ -z "${HIC_RESTRICTION_ENZYME:-}" ]]; then
            warn "HIC_RESTRICTION_ENZYME not set"
        else
            ok "  Restriction enzyme: $HIC_RESTRICTION_ENZYME"
        fi
        if [[ -z "${HIC_CHROMOSOME_COUNT:-}" ]]; then
            warn "HIC_CHROMOSOME_COUNT not set"
        else
            ok "  Expected chromosomes: $HIC_CHROMOSOME_COUNT"
        fi
    else
        warn "Hi-C reads not configured - will skip scaffolding stage"
        info "  Set HIC_R1 and HIC_R2 in hpc_env.sh to enable Hi-C + T2T"
    fi

    if (( missing > 0 )); then
        error "$missing required configuration items missing"
        return 1
    fi

    section "Configuration OK"
    return 0
}

# ============================================================
# Setup Wizard
# ============================================================
setup_wizard() {
    section "Interactive Setup Wizard"

    if [[ -f "${SCRIPT_DIR}/hpc_env.sh" ]]; then
        warn "hpc_env.sh already exists"
        read -p "Overwrite? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            info "Keeping existing configuration"
            return 0
        fi
    fi

    info "Copying example configuration..."
    cp "${SCRIPT_DIR}/hpc_env.sh.example" "${SCRIPT_DIR}/hpc_env.sh"

    ok "Created hpc_env.sh"
    info ""
    info "Now edit the configuration file:"
    info "  vim ${SCRIPT_DIR}/hpc_env.sh"
    info ""
    info "Required settings:"
    info "  - LONG_READS: path to ONT/PacBio reads (FASTQ/FASTA, gzipped ok)"
    info "  - GENOME_SIZE_ESTIMATE: estimated genome size in bp"
    info "  - BUSCO_LINEAGE_PATH: path to BUSCO lineage database"
    info ""
    info "Optional (for full pipeline):"
    info "  - HIC_R1, HIC_R2: Hi-C paired reads"
    info "  - HIC_RESTRICTION_ENZYME: enzyme used for Hi-C (e.g. DpnII)"
    info "  - HIC_CHROMOSOME_COUNT: expected number of chromosomes"
    info ""
    info "After editing, run:"
    info "  ./quick_run.sh --check   # Verify configuration"
    info "  ./quick_run.sh           # Run the full pipeline"
}

# ============================================================
# Main
# ============================================================
main() {
    case "${1:-}" in
        --check)
            check_config
            exit $?
            ;;
        --setup)
            setup_wizard
            exit 0
            ;;
        --help|-h)
            cat <<HELP
Quick E2E Pipeline Runner for HPC

Usage:
  ./quick_run.sh              Run full e2e test with current config
  ./quick_run.sh --check      Check configuration without running
  ./quick_run.sh --setup      Interactive setup wizard
  ./quick_run.sh --help       Show this help

Configuration file: ${SCRIPT_DIR}/hpc_env.sh
HELP
            exit 0
            ;;
        "")
            # Default: check config and run
            if ! check_config; then
                error "Configuration check failed"
                info "Run './quick_run.sh --setup' to create configuration"
                exit 1
            fi

            section "Running End-to-End Pipeline Test"
            info "Test script: test_09_e2e_pipeline.sh"
            info "This will run the full pipeline with real biological software"
            info ""
            read -p "Continue? (Y/n): " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Nn]$ ]]; then
                info "Cancelled"
                exit 0
            fi

            # Run the test
            exec "${SCRIPT_DIR}/test_09_e2e_pipeline.sh"
            ;;
        *)
            error "Unknown option: $1"
            info "Run './quick_run.sh --help' for usage"
            exit 1
            ;;
    esac
}

main "$@"
