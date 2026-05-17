#!/usr/bin/env bash
# Shared helpers for server smoke scripts. Source this from a script:
#
#   source "$(dirname "$0")/lib/server_smoke_common.sh"
#
# Provides: ORCHESTRATOR_ENV (default), require, abs_path, ensure_conda.
# Does NOT enable set -euo pipefail; callers are expected to set their own.

ORCHESTRATOR_ENV="${ORCHESTRATOR_ENV:-gene_agent}"

require() {
  local name="$1" value="$2"
  if [[ -z "${value}" ]]; then
    printf 'error: %s is required\n' "${name}" >&2
    return 1
  fi
}

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)  printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}

ensure_conda() {
  if ! command -v conda >/dev/null 2>&1; then
    printf 'error: conda not found on PATH\n' >&2
    return 1
  fi
}

ensure_file() {
  local label="$1" path="$2"
  if [[ ! -f "${path}" ]]; then
    printf 'error: %s not found: %s\n' "${label}" "${path}" >&2
    return 1
  fi
}
