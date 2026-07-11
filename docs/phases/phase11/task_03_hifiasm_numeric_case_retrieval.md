# Task 3: Hifiasm Continuous Numeric Case Retrieval

**Phase**: 11
**Status**: Complete; production cutover pending Task 6
**Depends on**: Task 2 EvidenceCase contract
**Created**: 2026-07-10
**Updated**: 2026-07-11

## Goal

Implement the deterministic, continuous, missing-aware hifiasm initial-case
retrieval backend that replaces bucket scoring after the Task 6 production
cutover.

This task decides which cases are numerically and operationally plausible. It
does not ask an LLM to recommend parameters and does not score final assembly
quality.

## Implemented Contract

- canonical profile-to-query unit conversion for genome size, coverage, read
  N50, heterozygosity, and ploidy;
- approved/case-type/read-type/tool/target/goal hard filters;
- continuous log-ratio distance with interval and near-zero heterozygosity
  handling;
- separate numeric distance and evidence coverage;
- missing-value trace, stable ranking, weak-evidence warning, and deterministic
  no-case fallback metadata;
- configuration-backed tolerance scales, feature weights, distance clipping,
  epsilon, and candidate budget under `strategies.yaml:hifiasm_numeric`;
- an explicit, default-disabled `production_cutover_enabled` gate owned by Task
  6; enabling it selects the numeric backend only for `recommend_initial`;
- explicit rejection of non-initial queries by both the query adapter and the
  numeric retriever;
- retry dispatch remains on `structured_default` with
  `retry_pending_task_5` metadata until Task 5 implements its separate query and
  failure-family contract;
- attempt-scoped `retrieval_query.json` and `retrieval_trace.json` artifacts,
  with compact refs/hashes, backend, query ID, candidate IDs, eligible count,
  and warning summary handed to the prompt and typed parameter attempt;
- method-scoped dispatch so non-hifiasm methods keep their current behavior.

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

## Dispatch And Release Gates

Task 3 does not reinterpret every non-initial task as retry. The dispatch
contract names all supported tasks explicitly:

| Task | `production_cutover_enabled` | Backend in Task 3 |
|---|---:|---|
| `recommend_initial` | `false` or absent | `structured_default` |
| `recommend_initial` | `true` | `hifiasm_numeric` |
| `retry_after_execution_failure` | either | `structured_default` pending Task 5 |
| `retry_after_review_failure` | either | `structured_default` pending Task 5 |
| any other hifiasm task | either | rejected as unsupported |

The production knowledge pack keeps the cutover flag `false`. Task 6 owns
changing it only after the curated corpus and golden evaluation pass. Flye,
NextDenovo, hifiasm-ont, and other methods do not consult this gate and retain
their existing selector behavior.

Before Task 6, `structured_default` is a compatibility backend for legacy
hifiasm seed records only. Its input corpus is explicitly partitioned before
selection: every `schema_version: 2` EvidenceCase is excluded regardless of
curation status, schema validity, or whether it also carries legacy-looking
`features`/`params` fields. The compatibility path does not validate those v2
records and cannot expose them in legacy selection results or trace metadata.
Only an explicitly enabled numeric initial backend may validate and consume the
curated v2 corpus. Retry remains legacy-only until Task 5 supplies its own
curated retrieval contract.

The gate is the only backend switch. There is no second corpus-size switch:
after an explicit cutover, an empty validated initial corpus remains on
`hifiasm_numeric` and returns its normal no-case metadata and conservative
fallback guidance. It does not silently fall back to legacy seed cases.

Configuration also fails safely:

- a missing or false cutover flag cannot activate numeric retrieval;
- a non-boolean cutover flag is rejected;
- when cutover is active for `recommend_initial`, invalid numeric configuration
  is rejected before corpus loading;
- retry never validates, queries, or ranks curated Task 3 cases before Task 5;
- default-off initial and pre-Task-5 retry compatibility selection receive only
  non-v2 legacy records, so draft, rejected, or malformed v2 records cannot be
  reinterpreted as legacy evidence.

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
    source_ref: prompt_profile.genome_survey.estimated_genome_size
  coverage:
    value: 31.4
    source_ref: prompt_profile.read_qc.effective_coverage_1kb
  read_n50_kb:
    value: 18.7
    source_ref: prompt_profile.read_qc.read_n50
  heterozygosity:
    value: 0.011
    source_ref: prompt_profile.genome_survey.heterozygosity
  ploidy:
    value: 2
    source_ref: prompt_profile.sample.ploidy
categorical_context:
  clade: vertebrate
```

The profile adapter owns field-path precedence and unit conversion. The
retriever sees one canonical unit per feature.

## Hard Filters

For the first hifiasm release, an eligible case must satisfy:

- `curation.status == approved`;
- `retrieval_eligible == true` from Task 2 source validation;
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
ranking never treats the display value as assembly quality. Eligible cases are
ordered lexicographically by evidence coverage descending, numeric distance
ascending, clade tie-break, and case ID. This prevents a one-field exact match
from displacing a complete near-match while preserving continuous distance
ordering among candidates with comparable coverage.

## Scales And Weights

The implementation uses configuration-backed domain tolerances, not learned
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

The prompt payload and `ParameterAttempt` retain compact replay metadata:

```yaml
retrieval_backend: hifiasm_numeric
query_id: <session>:parameter_attempt_<n>
query_ref: data/artifacts/assembly/<session>/parameter_attempt_<n>/retrieval_query.json
query_hash: <sha256>
trace_ref: data/artifacts/assembly/<session>/parameter_attempt_<n>/retrieval_trace.json
trace_hash: <sha256>
eligible_case_count: 14
candidate_case_ids:
  - case_a
  - case_b
warning: null
```

The trace artifact contains the replayable ranking details:

```yaml
retrieval_backend: hifiasm_numeric
query_id: <session>:parameter_attempt_<n>
selected_case_ids: [case_a, case_b]
eligible_case_count: 14
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
    feature_distances:
      coverage:
        query: 31.4
        case: 60
        distance: null
        status: not_compared_hard_filter
```

Both artifacts are attempt-scoped. They contain normalized numeric inputs,
ranking decisions, refs, hashes, IDs, and warning summaries, but not complete
body/Methods documents. The retriever remains filesystem-pure; the assembly
prompt orchestration owns artifact materialization.

### Validation-owned ranked selection envelope

Task 3 does not hand Task 4 a collection of independently trusted IDs and
dictionaries. After ranking, it mints a private validation-owned selection
envelope that binds the complete ordered transition result:

- retrieval backend;
- ordered `candidate_case_ids`, `selected_case_ids`, and
  `evidence_case_ids`;
- selected count and evidence-availability state;
- eligible/total case counts, conservative warning, and normalized query;
- every selected case's Task 3-owned ranked identity and semantic snapshot;
- each case's complete `numeric_match`, including `rank_numeric` in production
  retrieval output;
- the full ranking trace and its candidate order.

Envelope creation verifies that candidate IDs, selected IDs, ranked-case
order, candidate trace order, and per-case numeric metadata agree. Controlled
`copy.copy()` and `copy.deepcopy()` preserve the in-process attestation, while
plain-dict conversion or semantic mutation loses or invalidates it. The
envelope is an internal transition proof, not a new public EvidenceCase type,
and it is removed at Task 4's canonical prompt boundary.

Task 4 must validate this envelope before consuming caller-visible shortlist
IDs, ordering, or retrieval metadata. Its caller must provide exactly the first
`min(8, candidate_count)` attested ranked cases before Task 4 applies span,
character, or serialized-byte budgets; an empty, shortened, exchanged, or
reordered prefix fails rather than being reclassified as a budget omission.
Consequently, swapping cases and IDs, changing candidate/evidence order,
replacing a ranked case's numeric metadata, changing trace distances, replacing
the query/warning/counts, or appending an unattested candidate fails closed.
Retrieval query/trace artifacts are built only from a projected view of this
registered envelope, after the corresponding caller-visible fields compare
equal; no retrieval artifact is written before that check succeeds. This is an
in-process integrity boundary, not a claim of protection against arbitrary
same-process Python reflection or code execution.

## Candidate Budget

The numeric retriever may return a configurable shortlist, initially sized for
the Methods-aware prompt budget rather than for a scientific threshold.

Recommended first defaults:

- retain up to 12 numeric candidates;
- Task 4 may include at most 8 cases in the final LLM evidence payload;
- when fewer cases pass, keep all eligible cases and emit a warning;
- when evidence coverage is weak, Task 4 must prefer catalog defaults.

## Code Boundaries

```text
app/domains/assembly/retrieval_query.py
    Normalizes full/prompt profiles into canonical hifiasm query features.

app/domains/assembly/numeric_case_retriever.py
    Applies hard filters, numeric distances, ranking, and trace generation.

app/domains/assembly/case_selector.py
    Remains a thin method-aware dispatch boundary for hifiasm evidence retrieval.
```

The numeric retriever must be pure with respect to LLM calls and filesystem
writes. Artifact writing belongs to the assembly stage orchestration layer.

## Tests

Implemented deterministic coverage:

- 29.9x and 30.1x coverage rank nearly identically;
- doubling a value produces the same raw log-ratio distance regardless of base
  magnitude;
- missing values reduce evidence coverage but never add similarity credit;
- a one-field exact match cannot displace a complete near-match at a bounded
  candidate budget;
- interval containment and nearest-boundary behavior;
- heterozygosity values near zero remain finite;
- hard filters precede numeric ranking;
- clade cannot overcome major numeric mismatch;
- stable tie-breaking by case ID;
- result trace explains every included, missing, and excluded feature;
- ranked output binds ordered candidate/selected/evidence IDs, ranked case
  semantics, each `numeric_match`, and trace rank in one validation-owned
  selection envelope;
- reordered cases/IDs, altered numeric metadata, appended unattested
  candidates, and plain-dict ranked entries fail before Task 4 budgeting;
- legitimate ranked output and its copy/deepcopy forms remain valid, including
  the empty no-case selection;
- configured scales and weights change distance deterministically;
- non-finite profile values are treated as missing and non-finite retrieval
  configuration is rejected;
- query precedence, units, query ID, and source paths are recorded;
- the configured 12-case candidate budget is not capped by the legacy
  selector's 8-case prompt limit;
- fewer eligible cases than the configured candidate budget produce an
  explicit conservative warning;
- any selected weak-coverage case produces an explicit warning;
- retrieval query/trace artifacts are attempt-scoped, indexed, and handed to
  `ParameterAttempt` by ref and hash;
- no `coverage_bucket`, `genome_size_bucket`, or bucket-boundary dependency in
  hifiasm evidence retrieval;
- query construction and the numeric retriever both reject retry and unknown
  tasks instead of treating every non-initial task as retry;
- the production cutover gate defaults closed, active invalid configuration
  fails before corpus loading, and an explicitly cut-over empty corpus produces
  numeric no-case output without a hidden legacy fallback;
- mixed corpora cannot leak draft, rejected, or malformed v2 records carrying
  legacy-looking fields into either initial or retry `structured_default`
  selection;
- both named retry tasks stay on `structured_default` pending Task 5;
- the Phase10 parameter-attempt integration explicitly enables cutover through
  a temporary knowledge root when asserting numeric query/trace artifacts, and
  separately verifies that the production default writes no numeric artifacts;
- non-hifiasm selector tests remain unchanged and green.

Focused verification command:

```bash
conda run -n gene_agent python -m pytest \
  tests/test_assembly_case_selector.py \
  tests/test_hifiasm_numeric_retrieval.py \
  tests/test_phase10_parameter_catalog.py -q
```

Verified on 2026-07-10: `97 passed, 1 skipped, 2 subtests passed`.

## Acceptance Criteria

- [x] Hifiasm initial retrieval uses continuous numeric distances rather than
  bucket matches.
- [x] Every candidate has feature-level distance and missingness trace.
- [x] Similarity and evidence coverage are separate values.
- [x] Hard compatibility filters are deterministic and run before ranking.
- [x] Weak evidence produces an explicit warning instead of false confidence.
- [x] Retrieval artifacts are replayable and do not contain full source
  documents.
- [x] The implementation performs no LLM, embedding, GraphRAG, or quality-score
  operation.
- [x] Task 3 numeric query construction and ranking serve only
  `recommend_initial`; retry integration remains pending Task 5.
- [x] Production numeric cutover is explicit, defaults disabled, and remains
  pending Task 6 evaluation.
- [x] Empty-corpus and invalid-active-configuration behavior fail safely without
  silently selecting a different backend.
- [x] The pre-cutover compatibility backend excludes every v2 EvidenceCase
  before legacy scoring, including malformed records with legacy-looking
  fields.
