# Phase10 Implementation Summary

## Overview

Phase10 integrates parameter recommendation into the assembly stage, enabling the LLM to recommend L2 (heuristic-tunable) parameters based on input characteristics and prior execution failures. Parameter quality/review gating is handled by the `assembly_review` stage.

## Key Changes

### 1. Parameter Lifecycle Integration

**Modified Files:**
- `app/stages/assembly/method_application/hifiasm.py`
- `app/stages/assembly/method_application/hifiasm_ont.py`
- `app/stages/assembly/method_application/lifecycle.py`

**Changes:**
- `_parameters()` functions now get L0 parameters (read_type, threads) from `read_type_prior` and `{method}_parameters`, not from L2 `recommended_params`
- L2 parameters come from `parameter_attempts[].recommended_params` and are merged by command builder
- `run_method_attempt()` only updates `parameter_attempts` for methods that have them (hifiasm/hifiasm_ont), skipping Flye/NextDenovo

### 2. Default Parameters for Testing

**Modified Files:**
- `app/stages/assembly/decision.py`

**Changes:**
- `allow_model_unavailable_defaults: True` now returns empty L2 params (uses all catalog defaults)
- This allows tests to run without actual LLM calls

### 3. Test Compatibility

**Test Status:**
- **160 passed, 1 skipped**
- hifiasm/hifiasm_ont integration tests now run through the Phase10 recommendation flow
- All Phase10 parameter catalog tests passing
- All Phase4 Flye tests passing (backward compatible)

**Restored Integration Tests:**
- `test_local_hifiasm_execution_writes_draft_assembly_and_ledger`
- `test_local_hifiasm_execution_from_fofn_uses_ont_flag`
- `test_failed_local_hifiasm_records_failed_attempt`

See `docs/phases/phase10/test_compatibility_notes.md` for the resolved compatibility note.

## Parameter Architecture

### Level 0 (Executor Parameters)
- **Source**: `read_type_prior`, `{method}_parameters` (run config)
- **Examples**: threads, output_prefix, input_reads
- **Set by**: Input resolution, user config, executor

### Level 1 (Data-Derived Parameters)
- **Source**: Genome survey, input profile
- **Examples**: estimated_genome_size (for Flye)
- **Set by**: Genome survey stage, profile builder

### Level 2 (Heuristic Parameters)
- **Source**: LLM recommendation via `parameter_attempts[].recommended_params`
- **Examples**: kmer_length, minimizer_window, bloom_filter_bits
- **Set by**: Parameter recommendation node

### Parameter Flow
```
Input Resolution → L0 (read_type_prior)
                 ↓
User Config → L0 ({method}_parameters)
            ↓
Genome Survey → L1 (input_profile)
              ↓
LLM Recommendation → L2 (recommended_params)
                   ↓
Command Builder → Merged argv
```

## Graph Flow

### Phase10 Flow (Hifiasm/Hifiasm-ONT)
```
enter → resolve → infer_profile → select_route → build_candidates → select_primary
  → recommend_parameters → plan_candidate_attempts → {method}_subgraph
  → freeze_attempt → run_attempt → collect_artifacts → execution_retry_gate
  → (on execution failure) recommend_parameters (retry)
```

### Phase4 Flow (Flye/NextDenovo)
```
enter → resolve → infer_profile → select_route → build_candidates → select_primary
  → plan_candidate_attempts → {method}_subgraph
  → freeze_attempt → run_attempt → collect_artifacts
```

## Backward Compatibility

- **Flye and NextDenovo** continue to use Phase4 flow (no parameter recommendation)
- `{method}_parameters` still work for explicit parameter setting
- Tests for Phase4 methods all pass
- No breaking changes to external interfaces

## Files Modified

### Core Assembly
- `app/stages/assembly/method_application/hifiasm.py` - Updated parameter extraction
- `app/stages/assembly/method_application/hifiasm_ont.py` - Updated parameter extraction
- `app/stages/assembly/method_application/lifecycle.py` - Conditional parameter_attempts update
- `app/stages/assembly/decision.py` - Default parameter handling

### Tests
- `tests/test_assembly.py` - hifiasm/hifiasm_ont integration tests restored for Phase10
- `tests/test_phase10_parameter_catalog.py` - All tests passing

### Documentation
- `docs/phases/phase10/test_compatibility_notes.md` - Test migration guide
- `docs/phases/phase10/IMPLEMENTATION_SUMMARY.md` - This file

## Next Steps

### Short Term
1. ✅ All Phase10 tests passing
2. ✅ Backward compatibility with Phase4 methods maintained
3. ✅ Restored hifiasm/hifiasm_ont integration tests under Phase10 flow

### Medium Term
1. Migrate Flye to Phase10 (add L2 parameters to catalog)
2. Migrate NextDenovo to Phase10
3. Extend `assembly_review` parameter quality gate behavior as needed

### Long Term
1. Extend to other stages (scaffolding, annotation)
2. Multi-attempt visualization in UI
3. Parameter importance attribution

## Validation

Run all tests:
```bash
conda run -n gene_agent pytest tests/test_assembly.py tests/test_ask_plan.py \
  tests/test_phase10_parameter_catalog.py tests/test_registry_executable_invariant.py -v
```

Expected: **160 passed, 1 skipped**
