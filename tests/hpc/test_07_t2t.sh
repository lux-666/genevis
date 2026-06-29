#!/usr/bin/env bash
# ============================================================
#  Test 07 — T2T telomere finishing placeholder smoke test
# ============================================================
# This validates the executable contract and unit fixture.  A real-data run is
# deliberately opt-in: set T2T_ASSEMBLY and T2T_ONT_READS in hpc_env.sh once a
# suitable project sample or small on-site fixture is available.
set -euo pipefail
cd "$(dirname "$0")"
source common.sh

section "Test 07: T2T Telomere Finishing"

cd "${PROJECT_ROOT}"
if [[ "${RUN_UNIT_TESTS:-true}" == "true" ]]; then
    python3 -m pytest tests/test_t2t.py -v --tb=short 2>&1 && \
        pass "unit tests: test_t2t.py" || fail "unit tests: test_t2t.py"
fi

python3 -c "
from app.schemas.run_config import RunConfig
cfg = RunConfig.model_validate({'local_exec': ['t2t'], 't2t': {}})
assert cfg.t2t.mode == 'auto_patch'
assert cfg.t2t.thresholds.min_supporting_reads == 1
print('T2T RunConfig validation: PASS')
" 2>&1 && pass "T2T RunConfig schema validation" || fail "T2T RunConfig schema validation"

if [[ -n "${T2T_ASSEMBLY:-}" && -n "${T2T_ONT_READS:-}" ]]; then
    if [[ -f "${T2T_ASSEMBLY}" && -f "${T2T_ONT_READS}" ]]; then
        warn "Real T2T data supplied; execute the stage manually after selecting a session/data-dir policy."
        info "assembly=${T2T_ASSEMBLY}"
        info "ont_reads=${T2T_ONT_READS}"
    else
        fail "T2T_ASSEMBLY or T2T_ONT_READS is configured but missing"
    fi
else
    info "No real T2T fixture configured; placeholder smoke coverage complete."
fi
