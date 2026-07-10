# Task 3: Hifiasm Continuous Numeric Case Retrieval

**Phase**: 11
**Status**: MVP implemented; attempt-scoped retrieval artifact split pending
**Depends on**: Task 2 EvidenceCase contract
**Created**: 2026-07-10
**Updated**: 2026-07-10

## Goal

Replace bucket-based hifiasm case scoring with deterministic, continuous,
missing-aware retrieval over approved HiFi EvidenceCases.

This task decides which cases are numerically and operationally plausible. It
does not ask an LLM to recommend parameters and does not score final assembly
quality.

## Implemented MVP

- canonical profile-to-query unit conversion for genome size, coverage, read
  N50, heterozygosity, and ploidy;
- approved/case-type/read-type/tool/target/goal hard filters;
- continuous log-ratio distance with interval and near-zero heterozygosity
  handling;
- separate numeric distance and evidence coverage;
- missing-value trace, stable ranking, weak-evidence warning, and deterministic
  no-case fallback metadata;
- method-scoped dispatch so non-hifiasm methods keep V1 behavior.

## Scope

In scope:

- canonical hifiasm retrieval query;
- hard compatibility filters;
- continuous numeric distance;
- interval and uncertainty handling;
- explicit evidence coverage;
- deterministic ranking and feature-level trace;
- conservative behavior when the query or case is incomplete.

Out of scope:

- semantic embeddings;
- LLM Methods interpretation;
- parameter recommendation;
- cross-tool cases;
- multi-objective assembly outcome selection.

## Query Contract

The retriever receives a normalized query rather than reading arbitrary profile
paths directly:

```yaml
query_id: <session>:parameter_attempt_<n>
method_id: assembly.candidate.hifiasm
task: recommend_initial
read_type: hifi
assembly_goal: primary
numeric_features:
  genome_size_mb:
    value: 1200
    source_ref: data/artifacts/genome_survey/.../result.json
  coverage:
    value: 31.4
    source_ref: data/artifacts/read_qc/.../result.json
  read_n50_kb:
    value: 18.7
    source_ref: data/artifacts/read_qc/.../result.json
  heterozygosity:
    value: 0.011
    source_ref: data/artifacts/genome_survey/.../result.json
  ploidy:
    value: 2
    source_ref: manifest
categorical_context:
  clade: vertebrate
```

The profile adapter owns field-path precedence and unit conversion. The
retriever sees one canonical unit per feature.

## Hard Filters

For the first hifiasm release, an eligible case must satisfy:

- `curation.status == approved`;
- `case_type == initial` for `recommend_initial`;
- `applicability.read_type == hifi`;
- target method includes `assembly.candidate.hifiasm`;
- source tool is hifiasm;
- assembly goal is compatible when both query and case declare it.

Clade is not a hard filter. It is a low-priority tie-break/explanation field and
must not override severe numeric differences.

## Continuous Distance

Positive scale features use log-ratio distance:

```text
genome_size_mb
coverage
read_n50_kb
```

For query value `q`, case value `c`, and configured tolerance scale `s_i`:

```text
d_i = abs(log(q / c)) / s_i
```

Heterozygosity uses an epsilon-stabilized log ratio:

```text
d_het = abs(log((q + epsilon) / (c + epsilon))) / s_het
```

Ploidy is categorical/discrete for the MVP:

- exact match: distance 0;
- mismatch: recorded as an incompatibility penalty or exclusion according to
  the assembly-goal policy;
- missing: no similarity credit.

Large component distances are Huberized or clipped using configuration so one
outlier cannot erase the complete trace from other features.

## Interval Values

An EvidenceCase may report an interval:

```yaml
coverage:
  value: null
  uncertainty:
    kind: interval
    min: 28
    max: 34
```

Distance is zero when the query value falls inside the interval. Otherwise it is
computed to the nearest interval boundary using the feature's normal distance
function.

## Missingness And Evidence Coverage

A feature contributes to distance only when both query and case have a usable
value.

```text
D_numeric = sum(w_i * present_i * robust(d_i)) /
            sum(w_i * present_i)
```

Evidence coverage is reported separately:

```text
C_evidence = sum(w_i * present_i) / sum(w_i)
```

Missing data must never improve similarity. A result can therefore be close on
known values but weak overall:

```yaml
numeric_distance: 0.18
numeric_similarity_display: 0.84
evidence_coverage: 0.40
weak_evidence: true
```

`numeric_similarity_display = exp(-D_numeric)` may be shown for operators, but
ranking uses distance and never treats the display value as assembly quality.

## Scales And Weights

The first release uses configuration-backed domain tolerances, not learned
standard deviations from the small seed corpus.

Initial weights should be equal among present numeric features. Biological
importance should first be expressed as:

- hard compatibility;
- required evidence coverage;
- task-specific features;
- explicit uncertainty.

It should not be hidden in another arbitrary `+8/+2` score table.

Later, after a labeled retrieval set exists, scales or rank weights may be
calibrated without changing the query/result interfaces.

## Result Contract

```yaml
retrieval_backend: hifiasm_numeric_v2
query_ref: data/artifacts/assembly/<session>/parameter_attempt_<n>/retrieval_query.json
eligible_case_count: 14
candidate_case_ids:
  - case_a
  - case_b
warning: null
trace:
  - case_id: case_a
    decision: candidate
    hard_filter_reasons: []
    numeric_distance: 0.18
    evidence_coverage: 0.80
    weak_evidence: false
    rank_numeric: 1
    feature_distances:
      genome_size_mb:
        query: 1200
        case: 1180
        distance: 0.02
        status: compared
      heterozygosity:
        query: 0.011
        case: null
        distance: null
        status: missing_case_value
  - case_id: case_x
    decision: excluded
    hard_filter_reasons: [read_type_mismatch]
```

The trace is an attempt-scoped artifact. Compact parameter-attempt state stores
only its ref, selected IDs, backend version, and warning summary.

## Candidate Budget

The numeric retriever may return a configurable shortlist, initially sized for
the Methods-aware prompt budget rather than for a scientific threshold.

Recommended first defaults:

- retain up to 12 numeric candidates;
- Task 4 may include at most 8 cases in the final LLM evidence payload;
- when fewer cases pass, keep all eligible cases and emit a warning;
- when evidence coverage is weak, Task 4 must prefer catalog defaults.

## Proposed Code Boundaries

```text
app/domains/assembly/retrieval_query.py
    Normalizes full/prompt profiles into canonical hifiasm query features.

app/domains/assembly/numeric_case_retriever.py
    Applies hard filters, numeric distances, ranking, and trace generation.

app/domains/assembly/case_selector.py
    Remains a thin method-aware dispatch boundary while hifiasm moves to V2.
```

The numeric retriever must be pure with respect to LLM calls and filesystem
writes. Artifact writing belongs to the assembly stage orchestration layer.

## Tests

Required deterministic tests:

- 29.9x and 30.1x coverage rank nearly identically;
- doubling a value produces the same raw log-ratio distance regardless of base
  magnitude;
- missing values reduce evidence coverage but never add similarity credit;
- interval containment and nearest-boundary behavior;
- heterozygosity values near zero remain finite;
- hard filters precede numeric ranking;
- clade cannot overcome major numeric mismatch;
- stable tie-breaking by case ID;
- result trace explains every included, missing, and excluded feature;
- no `coverage_bucket`, `genome_size_bucket`, or bucket-boundary dependency in
  hifiasm V2 retrieval.

## Acceptance Criteria

- [ ] Hifiasm initial retrieval uses continuous numeric distances rather than
  bucket matches.
- [ ] Every candidate has feature-level distance and missingness trace.
- [ ] Similarity and evidence coverage are separate values.
- [ ] Hard compatibility filters are deterministic and run before ranking.
- [ ] Weak evidence produces an explicit warning instead of false confidence.
- [ ] Retrieval artifacts are replayable and do not contain full source
  documents.
- [ ] The implementation performs no LLM, embedding, GraphRAG, or quality-score
  operation.
