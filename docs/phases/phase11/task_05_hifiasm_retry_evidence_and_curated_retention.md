# Task 5: Hifiasm Retry Evidence And Curated Retention

**Phase**: 11
**Status**: Designed; implementation pending
**Depends on**: Task 4 Methods-aware recommendation; existing retry gate and assembly review
**Created**: 2026-07-10
**Updated**: 2026-07-10

## Goal

Apply the EvidenceCase and Methods-aware recommendation contract to hifiasm
execution/review retries, and emit curator-reviewable internal cases from real
runs without automatically contaminating the production evidence corpus.

## Retry Evidence Is A Separate Retrieval Space

Initial success cases answer:

> Which historical contexts and reported Methods are relevant before the first
> hifiasm run?

Retry cases answer:

> Given the previous parameters and this failure signature, which historical
> intervention is relevant now?

The two spaces must not be merged into one score.

## Retry Query Contract

```yaml
query_id: <session>:parameter_attempt_<n>
method_id: assembly.candidate.hifiasm
task: retry_after_review_failure
read_type: hifi
sample_context:
  genome_size_mb: 1200
  coverage: 31.4
  read_n50_kb: 18.7
  heterozygosity: 0.011
previous_attempt:
  attempt_id: 0
  parameters:
    purge_level: 2
    kmer_length: 51
  execution:
    return_code: 0
  review:
    assembly_size_ratio: 1.43
    contig_n50: 24000000
    busco_complete: 0.974
    busco_duplicate: 0.151
failure_signature:
  mode: under_purged
  confidence: high
```

Execution failures use:

- failure class;
- exit code/signal;
- bounded stderr/stdout tail refs;
- resource facts;
- previous rendered command and parameters.

Review failures use:

- diagnosed failure mode;
- previous parameters;
- assembly-size ratio with direction;
- BUSCO completeness and duplication;
- contiguity metrics;
- any other existing review-gate evidence.

## Constraint-First Outcome Interpretation

Retry retrieval does not normalize all assembly metrics into a universal quality
score.

The existing review gate first determines whether the attempt passed and emits
a failure signature such as:

- `under_purged`;
- `over_purged`;
- `fragmented`;
- `low_completeness`;
- execution/resource failure.

Retry retrieval then:

1. hard-filters to `case_type: retry` and compatible failure family;
2. compares sample context using the Task3 numeric functions;
3. compares the direction and magnitude of relevant review metrics;
4. compares previous parameter state and reported parameter change;
5. sends only plausible retry interventions and their Methods/internal-run
   evidence to Task4's assessment/recommendation contract.

A high N50 cannot compensate for a failed completeness or duplication
constraint during retrieval.

## Retry Recommendation Rules

A retry response must explain:

- which failure signal it is addressing;
- which previous parameter or parameter group is implicated;
- the proposed direction and value change;
- which retry case and source span support the intervention;
- why unchanged parameters remain unchanged;
- whether another parameter retry is meaningful.

Random exploration is prohibited. If no compatible evidence exists, the model
must choose one of:

- preserve defaults/previous safe values and request expert review;
- report that the failure is unlikely to be parameter-repairable;
- stop because retry budget is exhausted.

Existing hard-rule validation and retry limits remain authoritative.

## Internal Run Retention

After execution and review, the system may emit a draft candidate case artifact:

```text
data/artifacts/assembly/<session>/parameter_attempt_<n>/candidate_evidence_case.yaml
```

It includes:

- normalized sample context;
- exact method/version and command artifact ref;
- previous/current parameter changes;
- execution result;
- review metrics and failure signature;
- selected evidence case IDs;
- recommendation rationale artifact ref;
- source artifact hashes.

Runtime never writes directly into
`descriptors/knowledge_packs/assembly/parameter_recommender/cases/`.

A human curator may later prepare:

```text
sources/<new_case_id>/body.md
sources/<new_case_id>/methods.md
```

For an internal run:

- `body.md` is the normalized run and outcome report;
- `methods.md` is the actual command, parameter intervention, and curator-approved
  rationale/notes.

The curator then promotes the case to the descriptor pack with
`curation.status: approved`. Until that promotion and source validation, the
runtime draft is never retrieval-eligible.

## Attempt Trace

A retry attempt stores:

```yaml
task: retry_after_review_failure
previous_attempt_id: 0
failure_signature: under_purged
case_retrieval_backend: hifiasm_retry_v2
candidate_case_ids: []
selected_case_ids: []
retrieval_result_ref: ...
evidence_payload_ref: ...
candidate_evidence_case_ref: ...
```

Earlier attempts and evidence artifacts remain immutable.

## Tests

Required tests:

- initial recommendation never retrieves retry cases;
- retry retrieval requires a compatible failure family;
- under-purged and over-purged signatures cannot be treated as equivalent;
- metric direction is preserved for assembly-size ratio and BUSCO duplication;
- unsupported/random parameter changes fail evidence validation;
- non-parameter-repairable failures can stop without inventing a retry;
- draft candidate cases are written to attempt artifacts only;
- runtime candidate cases are not production retrieval inputs;
- curator promotion requires Task2 source and curation validation;
- retry budget and existing hard rules remain authoritative.

## Acceptance Criteria

- [ ] Hifiasm retry uses a retry-specific query and approved retry cases.
- [ ] Review metrics remain a failure signature/vector, not one universal quality
  score.
- [ ] Every retry parameter change identifies the failure signal and supporting
  evidence.
- [ ] Real runs emit immutable draft case artifacts suitable for manual
  curation.
- [ ] Runtime never auto-promotes evidence into the descriptor knowledge pack.
- [ ] Initial and retry evidence spaces remain separate and replayable.
