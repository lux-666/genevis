# Phase10 Test Compatibility Notes

## Status

The hifiasm/hifiasm_ont integration tests that were temporarily skipped during
the Phase10 parameter-lifecycle migration have been restored.

Current targeted validation:

```text
160 passed, 1 skipped, 15 subtests passed
```

The remaining skip is the binary-backed hifiasm runtime help verification, which
skips when `HIFIASM_BIN` or `hifiasm` on `PATH` is unavailable.

## Restored Tests

- `test_local_hifiasm_execution_writes_draft_assembly_and_ledger`
- `test_local_hifiasm_execution_from_fofn_uses_ont_flag`
- `test_failed_local_hifiasm_records_failed_attempt`

These tests now let the assembly graph run through the Phase10 flow:

```text
enter -> resolve -> infer_profile -> select_route -> build_candidates
  -> select_primary -> recommend_parameters -> plan_candidate_attempts
  -> hifiasm/hifiasm_ont subgraph
```

## Compatibility Fixes

- `ParameterAttempt.method_id` uses the registry method id, such as
  `assembly.candidate.hifiasm_ont`, so recommender output and freeze validation
  share the same contract.
- The catalog id remains in `catalog_ref`, for example
  `descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml`.
- hifiasm/hifiasm_ont freeze nodes validate the current `ParameterAttempt`
  before consuming `recommended_params`.
- The restored integration tests assert `command_ref`, executed/failed attempt
  status, ledger events, and ONT `--ont` argv/script behavior.

## Test Coverage Status

| Test Category | Status |
|---------------|--------|
| Phase4 Flye/NextDenovo tests | Passing |
| hifiasm/hifiasm_ont integration tests | Restored and passing |
| Phase10 parameter catalog tests | Passing |
| Planner registry cleanup tests | Passing |
