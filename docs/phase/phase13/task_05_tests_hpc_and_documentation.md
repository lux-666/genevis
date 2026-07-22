# Task 05: Close Phase 13 with Lifecycle, Replay, and HPC Coverage

**Phase**: 13
**Status**: Planned
**Depends on**: Tasks 01-04

## Goal

Prove the Phase 13 Assembly, Hi-C, and T2T parameter contract plus the completed
T2T/Hi-C execution behavior through focused tests, replay assertions, artifact
and ledger checks, and real-tool HPC smoke scripts. Update stable documentation
only after the implemented contracts pass.

## Current State

The focused suites for T2T, Hi-C scaffolding, and the shared execution
lifecycle currently pass, but they do not cover the Phase 13 contracts. The
T2T HPC smoke also reads `final_assembly_for_annotation` from the finishing
bundle while the current `T2TFinishingBundle` exposes `output_assembly_ref`.
The smoke checks the minimap2 PAF in work storage rather than requiring an
immutable registered PAF.

## Design

### Test layers

Use four complementary layers:

1. pure tests for DTOs, response-shape parsing, catalog projection, sequence
   junction calculations, and attempt-chain validation;
2. stage tests with fake model and fake executables for complete graph routing,
   approval resume, retry, collect, and commit;
3. shared lifecycle tests for freeze/run/collect recovery and collect failure;
4. skip-safe HPC smoke using real AutoHiC/minimap2 resources when configured.

Tests must assert durable facts and refs, not only final status strings.

### Sparse parameter contract coverage

Add cross-stage regression coverage for:

- `parameters: {}` is structurally valid and reaches human approval;
- a one-key response remains a one-key recommendation and approval payload;
- no absent-key/catalog-difference list is created;
- a retry mapping is not merged with its predecessor;
- a retry prompt includes the ordered sparse recommendations, effective
  execution evidence, and outcomes for all earlier attempts;
- retry-history code does not calculate a parameter patch or require any key to
  be retained, removed, or changed;
- an Assembly attempt `0` with two keys followed by attempt `1` with one key
  gives attempt `1` exactly that one L2 key and emits only that key;
- an explicitly returned L2 value remains L2 and is emitted even when it equals
  the documented executor/tool default;
- defaults used during freeze remain executor/tool-owned evidence;
- absent CLI keys are not emitted;
- every shape-valid initial or retry mapping reaches human approval;
- Assembly retry no longer accepts or routes on `retry_disposition`.

### T2T coverage

Add regression coverage for:

- multiple ONT files and source attribution;
- non-zero terminal distance and correct junction trimming;
- no-read work-to-immutable publication;
- PAF publication, artifact-index registration, and `attempt_succeeded`;
- candidate-to-attempt/execution provenance;
- mandatory parameter approval and unchanged execution handoff;
- T2T `parameter_approval` gate registration and resume dispatch;
- `ParameterAttemptOwner` freeze/run/collect replay;
- collect failure after process success;
- manual patch review after parameter approval;
- downstream assembly resource handoff.

### Hi-C coverage

Add regression coverage for:

- the expanded request/catalog surface;
- sparse initial and retry mappings, including `{}`;
- response-shape-only parsing and mandatory approval;
- initial attempt, execution-failure retry, and review-feedback retry;
- an attempt `2` prompt containing the bounded evidence for attempts `0` and
  `1`, including their execution/review outcomes;
- immutable predecessor seals and retry evidence hydration;
- replay after recommendation, approval, freeze, run, and collect;
- retry exhaustion without fallback;
- separation of the parameter approval gate and final adoption gate;
- accepted candidate and retained-prior-assembly outcomes.

### HPC smoke correction

Update `tests/hpc/test_07_t2t.sh` to consume the canonical bundle field
`output_assembly_ref`. Do not add a second compatibility field solely to make
the smoke pass.

When minimap2 executes, the smoke must assert:

- canonical execution evidence exists;
- the immutable PAF artifact exists and is indexed;
- the T2T method has started and succeeded ledger facts;
- the candidate table and bundle refer to the collected execution;
- the final assembly ref exists and is valid FASTA;
- the source assembly remains unchanged.

Update the Hi-C smoke to cover parameter approval and, where practical, a
bounded retry fixture without requiring an expensive second real AutoHiC run.
Real retry execution may remain an opt-in extended smoke if runtime cost is too
high, but the approval and artifact contracts must still be exercised.

### Documentation update

After implementation, update:

- `docs/architecture/parameter-recommenders.md`;
- `docs/architecture/execution-lifecycle.md`, including T2T's
  `ParameterAttemptOwner` conformance entry;
- `docs/stage_contracts.md`;
- `docs/user_guide/parameter_recommendation.md`;
- HPC quick-start documentation and current run-config examples.

Documentation must repeat the Phase 13 parameter boundary precisely: species
facts are supplied; `parameters` is an attempt-local sparse mapping; absent
catalog keys are neither listed nor individually reviewed; retry mappings are
not merged; every retry prompt contains bounded chronological recommendation
and execution evidence for all earlier attempts; application code does not
derive parameter deltas, require key changes, or make retry decisions from that
history; every shape-valid recommendation is human-reviewed; and only the
normal post-approval executability check may stop freeze.

## Verification Commands

At minimum, run the focused local suites:

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n gene_agent \
  python -m unittest \
  tests.test_t2t \
  tests.test_hic_scaffolding \
  tests.test_execution_lifecycle \
  tests.test_parameter_recommendation \
  tests.test_assembly_parameter_request \
  tests.test_parameter_attempt_schema \
  tests.test_phase11_parameter_approval -v
```

Then run the broader affected suite selected by changed modules. The real HPC
smokes remain environment-dependent and must report a clear skip/block reason
when AutoHiC, minimap2, models, or real data are unavailable.

## Acceptance Criteria

- [ ] Focused unit and integration suites pass.
- [ ] T2T lifecycle, provenance, multi-input, and junction regressions pass.
- [ ] Hi-C initial and retry attempt regressions pass.
- [ ] Parameter tests prove sparse mappings, no predecessor merge, shape-only
      parsing, and mandatory human approval.
- [ ] Assembly retry has no model-controlled approval bypass.
- [ ] Collect failures cannot produce successful durable output.
- [ ] T2T HPC smoke uses `output_assembly_ref`.
- [ ] HPC smoke requires an immutable registered PAF after real execution.
- [ ] Stable architecture, stage-contract, user-guide, and HPC docs reflect the
      implemented behavior.
- [ ] Any unavailable real-tool smoke is reported explicitly rather than
      represented as passing.
