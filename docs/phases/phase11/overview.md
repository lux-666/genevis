# Phase 11: Hifiasm Evidence Retrieval And Methods-Aware Recommendation

**Phase**: 11
**Status**: Task 2 complete; Tasks 3-4 in progress; Tasks 5-6 designed
**Created**: 2026-07-07
**Updated**: 2026-07-10

## Goal

Replace the hifiasm recommender's production seed/bucket selection with a
manually curated, source-grounded, numerically continuous EvidenceCase
pipeline.

The cross-method typed-attempt expansion for Flye and NextDenovo belongs to
[Phase10 Task14](../phase10/task_14_flye_nextdenovo_parameter_attempts.md) and
is a completed prerequisite, not a Phase11 task. Task 2 now provides the
validated EvidenceCase input boundary consumed by Tasks 3-4. Those later tasks
remain incomplete until their own documented acceptance criteria are closed.
Tasks 5-6 remain later retry/evaluation work and must not start before Tasks
3-4 are complete.

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

## Completed Phase10 Prerequisite

[Phase10 Task14](../phase10/task_14_flye_nextdenovo_parameter_attempts.md)
provides the shared executable-method typed-attempt foundation:

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
- The existing L0/L1/L2 contract remains authoritative: cases and the LLM may
  explain L1 facts, but only cataloged L2 parameters may be recommended and L0
  remains fixed by execution/configuration code.
- Every recommendation records an auditable evidence basis. A case ID and exact
  source span are required only when the recommendation claims case evidence;
  L1 profile facts, SKILL heuristics, retry failures, and catalog defaults are
  also valid bases.
- Cases are optional evidence, not a recommendation prerequisite. No eligible
  case produces an explicit warning and a more conservative recommendation,
  while the existing L1 facts, SKILL guidance, and catalog defaults remain
  usable.
- Existing deterministic catalog/rule validation remains the execution gate.
- Real runs produce draft artifacts; humans decide whether to promote them.

## Task Index

- [Task 2: Hifiasm EvidenceCase And Manual Source Contract](task_02_hifiasm_evidence_case_contract.md) - Complete
- [Task 3: Hifiasm Continuous Numeric Case Retrieval](task_03_hifiasm_numeric_case_retrieval.md) - In progress; consumes validated Task 2 cases
- [Task 4: Hifiasm Methods-Aware Evidence Recommendation](task_04_hifiasm_methods_aware_recommendation.md) - In progress; begins after Task 3 acceptance
- [Task 5: Hifiasm Retry Evidence And Curated Retention](task_05_hifiasm_retry_evidence_and_curated_retention.md) - Designed
- [Task 6: Hifiasm Hybrid Retrieval Evaluation And Cutover](task_06_hifiasm_hybrid_retrieval_evaluation_and_cutover.md) - Designed

## Dependency Order

```text
[Phase10 Task14 typed parameter-attempt prerequisite]
  -> Task 2 EvidenceCase/source contract
      -> Task 3 deterministic numeric retrieval
          -> Task 4 Methods-aware recommendation
              -> Task 5 retry evidence and curated retention
                  -> Task 6 evaluation and hifiasm cutover
```

Tasks are intentionally sequential because each later contract consumes
artifacts and invariants established by the earlier task. Task 3 receives
normalized, retrieval-eligible cases and does not repeat manifest, filesystem,
checksum, quote, or provenance validation.

## Phase Completion Criteria

Phase11 is complete when:

- Phase10 Task14 typed parameter attempts remain green for all executable assembly
  methods;
- hifiasm production recommendation consumes only approved EvidenceCases;
- hifiasm case ranking is continuous and missing-aware rather than bucket-based;
- hifiasm recommendations preserve L0/L1/L2 permissions and record their
  evidence basis; case-backed claims are traceable to case IDs and exact source
  spans;
- no-case operation succeeds with an empty evidence set instead of blocking the
  parameter recommender;
- initial and retry evidence remain separate;
- runtime draft cases require manual promotion;
- the golden evaluation demonstrates safety and usefulness over the legacy
  baseline;
- deferred graph/wiki/vector/cross-tool work remains outside the phase.
