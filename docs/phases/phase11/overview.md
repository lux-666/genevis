# Phase 11: Assembly Parameter Attempts And Hifiasm Evidence Retrieval

**Phase**: 11
**Status**: Task 1 complete; Tasks 2-6 designed
**Created**: 2026-07-07
**Updated**: 2026-07-10

## Goal

Phase11 has two ordered goals:

1. carry the Phase10 typed parameter-attempt contract across executable assembly
   methods; and
2. replace the hifiasm recommender's production seed/bucket selection with a
   manually curated, source-grounded, numerically continuous EvidenceCase
   pipeline.

Task 1 completed the cross-method typed-attempt expansion for Flye and
NextDenovo. Tasks 2-6 deliberately return to PacBio HiFi hifiasm first so the
new evidence contract can be implemented and evaluated in one bounded method
before any broader rollout.

## Confirmed Evidence Boundary

PDF acquisition and document preparation happen outside this project for the
current phase. A human curator supplies:

```text
structured EvidenceCase YAML
+ matching normalized body.md
+ matching normalized methods.md
```

The project validates and consumes those inputs. It does not parse PDFs or run
GROBID.

The hifiasm-first pipeline is:

```text
manual case/body/Methods bundle
  -> schema and source validation
  -> hard compatibility filters
  -> continuous numeric shortlist
  -> Methods-aware LLM assessment and parameter recommendation
  -> deterministic catalog/rule validation
  -> typed freeze/run/review attempt
  -> retry-specific evidence retrieval when needed
  -> curator-reviewable draft case artifact
```

## Explicit Non-Goals

Phase11 Tasks 2-6 do not introduce:

- PDF download, OCR, GROBID, or automatic Methods extraction;
- GraphRAG, graph databases, or LLM Wiki;
- embedding services, vector databases, or a generic RRF stack;
- Flye/NextDenovo/hifiasm-ont evidence migration;
- cross-tool parameter translation;
- automatic promotion of runtime records into production evidence;
- one universal scalar assembly-quality score.

## Task 1 Completed Scope

- Flye and NextDenovo declare parameter catalogs.
- Both methods build catalog-driven parameter sources and attempt-scoped command
  artifacts.
- Shared execution lifecycle updates typed parameter attempts for success and
  failure.
- All executable assembly methods enter parameter recommendation before
  freeze/run.
- Model-unavailable behavior remains strict unless the explicit operator/test
  fallback is configured.
- Method execution failure routes through the retry gate.

## Hifiasm Evidence Design Principles

- Initial and retry cases are separate retrieval spaces.
- Numeric similarity and evidence coverage are separate values.
- Bucket matching is replaced by continuous, feature-level numeric distance.
- Only approved cases with verified source documents and exact source excerpts
  are eligible.
- The Methods-aware LLM sees only a bounded numeric shortlist.
- Every non-default hifiasm parameter change cites selected cases and source
  spans.
- Weak evidence preserves catalog defaults.
- Existing deterministic catalog/rule validation remains the execution gate.
- Real runs produce draft artifacts; humans decide whether to promote them.

## Task Index

- [Task 1: Flye and NextDenovo Typed Parameter Attempts](task_01_flye_nextdenovo_parameter_attempts.md) - Completed
- [Task 2: Hifiasm EvidenceCase And Manual Source Contract](task_02_hifiasm_evidence_case_contract.md) - Designed
- [Task 3: Hifiasm Continuous Numeric Case Retrieval](task_03_hifiasm_numeric_case_retrieval.md) - Designed
- [Task 4: Hifiasm Methods-Aware Evidence Recommendation](task_04_hifiasm_methods_aware_recommendation.md) - Designed
- [Task 5: Hifiasm Retry Evidence And Curated Retention](task_05_hifiasm_retry_evidence_and_curated_retention.md) - Designed
- [Task 6: Hifiasm Hybrid Retrieval Evaluation And Cutover](task_06_hifiasm_hybrid_retrieval_evaluation_and_cutover.md) - Designed

## Dependency Order

```text
Task 1
  -> Task 2 EvidenceCase/source contract
      -> Task 3 deterministic numeric retrieval
          -> Task 4 Methods-aware recommendation
              -> Task 5 retry evidence and curated retention
                  -> Task 6 evaluation and hifiasm cutover
```

Tasks are intentionally sequential because each later contract consumes
artifacts and invariants established by the earlier task.

## Phase Completion Criteria

Phase11 is complete when:

- Task1 typed parameter attempts remain green for all executable assembly
  methods;
- hifiasm production recommendation consumes only approved V2 EvidenceCases;
- hifiasm case ranking is continuous and missing-aware rather than bucket-based;
- non-default hifiasm values are traceable to case IDs and exact source spans;
- initial and retry evidence remain separate;
- runtime draft cases require manual promotion;
- the golden evaluation demonstrates safety and usefulness over the V1
  baseline;
- deferred graph/wiki/vector/cross-tool work remains outside the phase.
