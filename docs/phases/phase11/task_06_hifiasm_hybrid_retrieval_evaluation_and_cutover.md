# Task 6: Hifiasm Hybrid Retrieval Evaluation And Cutover

**Phase**: 11
**Status**: Designed; implementation pending
**Depends on**: Tasks 2-5
**Created**: 2026-07-10
**Updated**: 2026-07-10

## Goal

Build a reproducible offline evaluation for hifiasm evidence retrieval and
recommendation, compare it with the existing bucket/weight selector, and cut the
hifiasm production path over to the V2 EvidenceCase pipeline only after the new
contract is demonstrably safer and more useful.

## Evaluation Corpus

The first evaluation set is manually curated and version-controlled.

Recommended minimum:

- 20 approved hifiasm initial cases;
- 8 approved hifiasm retry cases;
- 10 initial query profiles;
- 6 retry query profiles;
- deliberate contrasts in genome size, coverage, read N50, heterozygosity,
  ploidy, and assembly goal;
- at least three weak/missing-evidence queries;
- at least three cases where keeping defaults is the expected answer.

Synthetic edge cases remain test fixtures and are not counted as scientific
production evidence.

## Golden Labels

For each query, a curator records:

```yaml
query_id: query_001
relevant_case_tiers:
  strong: [case_a, case_b]
  acceptable: [case_c]
  irrelevant: [case_x]
expected_effect_areas:
  - purging
expected_parameter_actions:
  purge_level:
    allowed_actions: [keep_default, set]
    allowed_values: [0, 1]
  kmer_length:
    allowed_actions: [keep_default]
forbidden_actions:
  - "Changing a parameter without cited evidence"
notes: "..."
```

Golden labels test acceptable decision boundaries rather than requiring one
magical exact parameter vector when the literature does not justify it.

## Retrieval Metrics

Measure:

- Recall@k;
- nDCG@k using strong/acceptable tiers;
- mean reciprocal rank;
- evidence coverage of selected cases;
- shortlist diversity;
- incompatible-case leakage rate;
- initial/retry cross-contamination rate.

## Numeric Robustness Tests

Include metamorphic tests:

- 29.9x and 30.1x coverage do not create a rank discontinuity;
- multiplying query and case values by the same factor preserves log-ratio
  distance;
- missing fields never improve evidence coverage;
- same Methods text with materially different numeric context changes ranking;
- identical numeric context with different Methods rationale remains available
  for Task4 to distinguish;
- very low heterozygosity does not produce division-by-zero or unstable ranks.

## Evidence And Recommendation Metrics

Measure:

- source-span citation validity;
- unsupported case/span citation rate;
- non-default parameter evidence rate;
- default-preservation rate under weak evidence;
- catalog/rule violation rate;
- expert pairwise preference between V1 and V2 outputs;
- abstention/stop correctness when retry is not evidence-supported.

The primary safety requirements are:

- zero invalid catalog values reaching freeze;
- zero accepted references to absent cases or spans;
- zero silent use of draft/rejected cases;
- zero automatic promotion of runtime draft cases.

## Baseline

Run the existing `structured_v1` bucket/weight selector against the same query
set and record:

- selected cases;
- trace and score;
- boundary discontinuities;
- expert preference;
- recommendation output under the same mocked or fixed decision backend where
  possible.

V2 does not need to win every ranking metric. It must demonstrate:

- no regression in hard compatibility;
- improved numeric continuity;
- explicit missingness/evidence coverage;
- source-grounded non-default recommendations;
- better expert preference or safer default preservation.

## Method-Scoped Cutover

After acceptance:

- `assembly.candidate.hifiasm` uses `hifiasm_evidence_v2` only;
- its prompt contains V2 evidence payloads only;
- hifiasm no longer reads bucket fields or `structured_v1` weights;
- V1 tests that assert hifiasm bucket scores are removed or rewritten as V2
  behavior tests;
- Flye, NextDenovo, and hifiasm-ont may remain on their existing selector until
  a later explicitly scoped task migrates them.

This is not a hidden compatibility fallback. Each method has one declared
selector backend.

## Documentation And Operator Visibility

Document:

- how a curator creates a case/body/Methods bundle;
- how to validate and approve it;
- how to run the offline evaluation;
- how to inspect retrieval and evidence artifacts for an attempt;
- what weak-evidence warnings mean;
- how a runtime draft case is manually promoted;
- why GraphRAG, LLM Wiki, embeddings, and cross-tool mapping are deferred.

## Tests And Commands

The implementation task should provide focused test commands for:

- EvidenceCase validation;
- numeric retrieval;
- Methods-aware prompt/response validation;
- retry retrieval and retention;
- golden-set evaluation;
- existing hifiasm integration and typed attempt lifecycle.

The full repository regression suite runs after focused tests pass.

## Acceptance Criteria

- [ ] A version-controlled hifiasm initial/retry evaluation corpus exists.
- [ ] V1 and V2 run against the same golden queries with recorded metrics.
- [ ] V2 has no bucket-boundary discontinuity and exposes missingness explicitly.
- [ ] All case-backed recommendations cite valid approved evidence; non-case
  recommendations name another allowed evidence basis.
- [ ] Weak evidence increases conservative/default-preserving behavior at the
  expected rate, while no-case queries still complete through L1/SKILL/default
  evidence bases.
- [ ] Hifiasm uses one V2 selector/prompt contract after cutover.
- [ ] Existing typed attempt, validation, freeze, execution, and retry behavior
  remains regression-covered.
- [ ] Deferred GraphRAG/LLM Wiki/vector/cross-tool work is not introduced by
  stealth.
