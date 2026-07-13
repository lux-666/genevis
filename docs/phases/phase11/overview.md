# Phase 11: Hifiasm Initial Evidence And Same-Run Retry

**Phase**: 11
**Status**: Complete; Tasks 2-4 and Task 6 are implemented, the 10-case MVP corpus is present, and regression verification passes
**Created**: 2026-07-07
**Updated**: 2026-07-13

## Goal

Use validated, source-grounded EvidenceCases for the initial hifiasm parameter
recommendation, then handle failures from that run through the typed parameter
attempt lifecycle. Initial evidence retrieval and same-run retry are different
concerns: retry does not create or query a second evidence database.

## Current Phase Boundary

The approved completion design splits Task 6 into two independent parts:

- **Task 6A — initial top-5 cutover.** Rank every eligible initial case with
  the Task 3 unified similarity score and retain the first five, or all eligible
  cases when fewer than five exist. Offline evaluation is
  observational and cannot enable, disable, or block production behavior.
- **Task 6B — same-run retry.** Build a retry from attempt 0's immutable
  profile/evidence/policy snapshot, the immediately preceding attempt, and its
  structured execution or review failure. Retry performs no database lookup,
  evidence retrieval, backend selection, or runtime-case promotion.

Both initial and retry recommendations pass automatic recommendation, catalog,
hard-rule, capability, and parameter validation before entering the shared
`parameter_approval` human gate. `approve_parameters` freezes the validated
candidate and routes it to execution. `reject_parameters` terminates without
execution; for retry it also appends no attempt and consumes no retry budget.

Execution and review retry remain owned by different stages and have independent
counters and limits. There is no failure classifier or repairability gate before
an execution retry: a recorded execution failure plus remaining execution
budget is sufficient to request a retry recommendation.

## Initial Evidence Boundary

A human curator supplies:

```text
structured EvidenceCase YAML
+ normalized methods.md
```

The project validates and consumes those inputs. It does not download or parse
papers. These approved paper cases are active production evidence: they are
ranked and the selected cases are projected into the parameter recommendation
prompt, so their content can affect the resulting recommendation. Only the
separate offline evaluation report is observational. The initial path is:

```text
manual initial case/Methods pair
  -> schema and source validation
  -> validation
  -> Task 3 similarity scoring
  -> fixed top-5 shortlist
  -> Methods-aware recommendation
  -> automatic deterministic validation
  -> parameter_approval human gate
  -> freeze and execute only after approval
```

Task 3 combines target-method match, read-type match, and missing-aware numeric
similarity into one ranking score. Method and read type are scoring attributes,
not hard filters. Ties use evidence coverage, numeric distance, clade, and case
ID. Task 6A adds no maximum distance, minimum coverage, case-count,
query-count, evaluation-result, recorded-recommendation, scientific-approval,
non-inferiority, or other activation threshold. The 10-query evaluation target
is not a gate. Task 6 retains a reduced MVP delivery target of 10 approved
hifiasm-family initial cases; the current corpus contains all 10, while corpus
size still does not gate the production path.

## Same-Run Retry Boundary

Retry reuses the actual attempt 0 contract, whether the initial path was legacy
or curated. It inherits attempt 0's immutable profile, selected initial cases,
Methods evidence, retrieval trace, catalog, hard rules, capabilities, policy,
and retry limits. It may additionally use only the previous attempt's frozen
parameters/command and that attempt's structured execution result or review
diagnosis.

Retry has none of the following:

- a retry EvidenceCase shard, corpus, query, retriever, or selector backend;
- a `hifiasm_retry` backend;
- cross-run or cross-sample historical retrieval;
- draft candidate `EvidenceCase` creation, retention, curation, or promotion;
- paper or policy re-retrieval during the run;
- a repairability label or pre-retry repairability decision;
- a combined execution/review retry budget.

## Explicit Non-Goals

Phase 11 does not introduce PDF download/OCR, GROBID, GraphRAG, LLM Wiki,
embeddings, a vector database, generic RRF, cross-tool parameter translation,
or evidence migration for Flye, NextDenovo, or hifiasm-ont. TUI implementation
and presentation are also out of scope; a later TUI may consume the generic
human-review interrupt contract.

## Task Index

- [Task 2: Hifiasm EvidenceCase And Methods Contract](task_02_hifiasm_evidence_case_contract.md) — implemented and verified for initial EvidenceCases and the v3 methods-only contract.
- [Task 3: Hifiasm EvidenceCase Scoring](task_03_hifiasm_numeric_case_retrieval.md) — implemented initial-only ranking; Task 6A fixes the production shortlist at top five.
- [Task 4: Hifiasm Methods-Aware Recommendation And Validation](task_04_hifiasm_methods_aware_recommendation.md) — implemented bounded prompt/response contract; initial and retry recommendations share post-validation parameter approval.
- [Task 5: Superseded Retry-Evidence Design](task_05_hifiasm_retry_evidence_and_curated_retention.md) — superseded; it creates no retained runtime contract.
- [Task 6: Hifiasm Initial Cutover And Same-Run Retry](task_06_hifiasm_hybrid_retrieval_evaluation_and_cutover.md) — split into Task 6A initial top-5 cutover and Task 6B same-run retry.

## Dependency Order

```text
[Phase10 Task14 typed parameter-attempt prerequisite]
  -> Task 2 initial EvidenceCase/source contract
      -> Task 3 deterministic initial numeric ranking
          -> Task 4 Methods-aware recommendation and validation
              -> Task 6A initial top-5 cutover
              -> Task 6B same-run execution/review retry
```

The old Task 5 retry database/retrieval/promotion design is not a dependency.

## Phase Completion Criteria

Phase 11 is complete when the implementation and regression suite confirm that:

- hifiasm initial retrieval preserves Task 3 scoring and fixed top-5 selection;
- approved paper-derived cases are loaded and consumed as production evidence,
  rather than treated as an observational-only evaluation corpus;
- observational evaluation has no activation or forward-progress effect;
- the MVP production EvidenceCase corpus contains 10 approved hifiasm-family
  initial cases, without making corpus size a production activation gate;
- retry uses only the current run's immutable attempt chain and structured
  failure facts;
- execution and review budgets remain independent;
- every valid actionable initial or retry parameter candidate waits at
  `parameter_approval`; `stop` and `expert_review` are not actionable and do
  not enter parameter approval;
- only approval can freeze and execute, while rejection terminates cleanly;
- no hifiasm retry shard/backend/retriever or candidate-promotion path remains;
- other methods retain their legacy descriptors and behavior.
