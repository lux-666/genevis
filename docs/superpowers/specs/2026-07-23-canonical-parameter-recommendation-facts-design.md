# Canonical Parameter-Recommendation Facts and Source Layers

## Goal

Replace the three stage-specific, dictionary-shaped recommendation profiles
with one typed canonical facts contract shared by Assembly, Hi-C, and T2T.
Stage-specific request DTOs remain separate, but they project the same facts
and use the same parameter-source semantics.

This is a hard cutover. The implementation will delete the old profile paths,
placeholder fields, compatibility aliases, and duplicate builders rather than
write both contracts or migrate old recommendation artifacts.

The immediate production defect is that Assembly writes
`genome_survey.effective_genome_size` and `read_qc.raw_coverage`, while
retrieval reads `genome_survey.estimated_genome_size` and the obsolete
`read_qc.effective_coverage_*` paths. A valid production state therefore loses
genome size and coverage at the retrieval boundary. The wider design problem is
that dictionary paths, model DTOs, retrieval, and command construction each
define their own version of the same facts.

## Decisions

1. `CanonicalRecommendationFacts` is the sole common biological and
   quantitative facts model for parameter recommendation.
2. `RecommendationInputSnapshotV3` combines those facts with one typed
   stage-owned context and audit-only dependency references.
3. Assembly, AutoHiC, T2T threshold selection, and T2T motif resolution keep
   distinct model-facing DTOs. They reuse canonical components; they do not
   share one universal biological DTO.
4. A central facts adapter is the only code that reads common upstream state
   field names or derives shared quantitative values.
5. Catalogs remain authoritative for L0/L1/L2 source permissions.
   Canonical facts do not decide which parameters belong to which layer.
6. The current singular `ParameterSpec.level` is replaced by explicit source
   layer declarations because a parameter may legitimately accept both an L1
   fact and an L2 override.
7. Retrieval and L1 resolution consume typed objects and validated symbolic
   fact keys. Arbitrary dotted paths are removed.
8. Model request schemas and persisted input snapshots are version-bumped.
   The response contract remains the existing sparse
   `{"parameters": ..., "summary": ...}` contract.
9. Old input profiles, old catalog fields, old artifacts, and old checkpoints
   fail closed. There is no migration reader, alias, fallback, or double write.

## Non-goals

- Changing method-selection behavior.
- Giving the model ownership of L0 executor mechanics or L1 measured/provided
  facts.
- Merging omitted L2 keys with catalog defaults or a previous attempt.
- Adding biological range validation between model parsing and human review.
- Replacing stage-owned execution renderers with one universal command builder.
- Changing `ParameterAttempt` ownership, approval binding, or immutable
  artifact-reference semantics from data v2.

## L0, L1, and L2 Ownership

### Meaning of the layers

The layers describe value provenance and override permission:

| Layer | Owner | Examples |
|---|---|---|
| L0 | route, executor, and run configuration | executable paths, input files, output directories, threads, fixed route switches, materialized wrapper defaults |
| L1 | caller-provided or pipeline-measured facts | effective genome size, read N50, coverage, supplied restriction enzyme |
| L2 | model recommendation followed by human approval | sparse tool-tuning parameters for the current attempt |

An implicit tool default is not copied into any recommendation. A wrapper
default that must be materialized is an L0 value and is recorded as such.

### Where layer placement is implemented

Layer placement is declarative and has one authority:

- method catalog YAML declares the allowed source layers for each parameter;
- `app/schemas/parameter_catalog.py` validates that declaration;
- `app/domains/parameter_recommendation/resolution.py` resolves the declared
  sources and records provenance;
- stage execution adapters render the resolved effective values into their
  command or configuration format;
- stage-specific model request builders project only parameters whose catalog
  declaration includes L2.

There must be no stage workflow condition such as “this field looks like L1”
and no request builder that independently maintains an adjustable-name list.

### Why singular `level` is removed

The current `ParameterSpec.level: Literal[0, 1, 2]` assumes that a parameter
can have exactly one source layer. AutoHiC already disproves that assumption:
`restriction_enzyme` may be supplied as an L1 library fact, and the approved
sparse L2 mapping may override it. The catalog labels it only as L2, while
runtime code contains a separate L1 fallback. That splits the contract across
two places.

The replacement declaration is source-oriented:

```yaml
restriction_enzyme:
  cli: null
  value_type: string
  layers:
    l1:
      fact: hic_provided_restriction_enzyme
      required_for_execution: true
    l2:
      adjustable: true
  description: Restriction enzyme used to prepare the selected Hi-C library.
```

An ordinary hifiasm L1 binding is:

```yaml
hg_size:
  cli: --hg-size
  value_type: string
  layers:
    l1:
      fact: effective_genome_size_bp
      transform: hifiasm_hg_size
  description: Effective haploid genome size.
```

An L0 runtime input uses a registered binding rather than a state path:

```yaml
input_reads:
  cli: null
  value_type: path_list
  layers:
    l0:
      binding: assembly_input_reads
```

`level`, `source`, `source_priority`, top-level `default`, and top-level
`fixed_value` are removed. Source-specific fixed or materialized default values
live inside `layers.l0`. L2 has no default field.

### Catalog schema

The catalog schema contains these concepts:

```text
ParameterSpec
├── cli
├── value_type and descriptive constraints
├── layers
│   ├── l0: ExecutorSource | null
│   ├── l1: FactSource | null
│   └── l2: ModelSource | null
└── description / notes
```

- `ExecutorSource.binding` is a registered, catalog-load-validated resolver ID,
  never a dotted state path.
- `ExecutorSource.fixed_value` and
  `ExecutorSource.materialized_default` are mutually exclusive.
- `FactSource.fact` is a `CanonicalFactKey` enum.
- `FactSource.transform` is a small validated transform enum such as
  `identity` or `hifiasm_hg_size`, not an import path or free-form expression.
- `ModelSource.adjustable` must be `true` when L2 exists.
- A parameter must declare at least one layer.
- A parameter may declare multiple layers when the effective-value precedence
  is intentional.

Catalog migration also corrects semantic inconsistencies. Biological values
such as Flye genome size are L1 facts even when they are required to construct
the command. Paths, threads, and route switches remain L0. The migration must
not infer `n_hap` from ploidy unless a separately reviewed typed derivation is
declared.

### Resolution and precedence

The shared resolver accepts:

- the selected `MethodParameterCatalog`;
- typed L0 values supplied by the stage execution adapter;
- the current `RecommendationInputSnapshotV3`;
- the exact approved sparse L2 mapping.

For each catalog parameter it records all declared candidate sources, then
chooses one effective value with this precedence:

```text
approved L2 value
  > resolved L1 fact
  > declared/materialized L0 value
  > implicit tool default (parameter remains absent)
```

This precedence is applied only when a parameter declares the corresponding
layers. An L2 key that is unknown or not L2-adjustable fails at the
post-approval executability boundary; the persisted recommendation is not
rewritten.

The result is a typed, provenance-aware structure:

```text
ResolvedParameterSources
├── l0_executor
├── l1_facts
├── l2_approved
├── effective_parameters
└── effective_source_by_parameter
```

Command/configuration rendering iterates `effective_parameters` once. It does
not concatenate three source groups, so a dual-source parameter cannot be
emitted twice.

The frozen-attempt artifact persists all five views. The approved sparse L2
mapping remains byte-for-byte distinguishable from facts and defaults.

## Canonical Facts Model

### Quantitative facts

Every quantitative fact is represented with an explicit unit and semantic
origin:

```text
QuantityFact[T]
├── value: finite int or float
├── unit: closed enum
└── origin
    ├── kind: provided | measured | derived | resolved
    └── producer: run_config | read_qc | genome_survey | assembly | hic | t2t
```

Examples include:

- genome size in `bp`;
- read N50 in `bp`;
- total bases in `bp`;
- sequencing coverage in `x`;
- heterozygosity and repeat fraction in `fraction`;
- expected chromosome count and ploidy in `count`.

Fractions are stored as `[0, 1]`, never sometimes percentages and sometimes
fractions. Counts are positive integers where biologically required. Numeric
values reject booleans, NaN, and infinity.

Artifact refs and source paths are not embedded in a `QuantityFact`. They live
in the audit-only dependency-ref section of the snapshot.

### Common facts

`CanonicalRecommendationFacts` is strict, frozen, and contains typed optional
groups:

```text
CanonicalRecommendationFacts
├── species
│   ├── scientific_name
│   ├── taxon_id
│   ├── clade
│   ├── ploidy
│   ├── expected_chromosome_count
│   └── supplied_telomere_motif
├── reads
│   ├── read_type
│   ├── read_count
│   ├── total_bases
│   ├── read_n50
│   ├── mean_read_length
│   ├── longest_read
│   ├── q20_fraction
│   └── mean_quality
├── genome
│   ├── effective_genome_size
│   ├── measured_genome_size
│   ├── provided_genome_size
│   ├── provided_vs_measured_delta_fraction
│   ├── heterozygosity
│   ├── repeat_fraction
│   └── estimate_confidence
├── sequencing
│   └── raw_coverage
└── asset_presence
    ├── has_hifi
    ├── has_ont
    ├── has_hic
    └── has_rnaseq
```

The exact Python model may split these groups into smaller named models, but it
must preserve this semantic surface and strict validation.

`raw_coverage` is derived exactly once as
`reads.total_bases / genome.effective_genome_size`. The derived fact records
origin `derived` and producer `parameter_recommendation`; no consumer repeats
the formula.

### Fact keys

`CanonicalFactKey` is a closed enum used by catalog L1 bindings. Its values are
semantic identifiers, not navigation instructions. Initial members include:

- `effective_genome_size_bp`;
- `read_n50_bp`;
- `raw_coverage_x`;
- `hic_provided_restriction_enzyme`;
- any other currently consumed L1 value that has an actual typed producer.

The enum-to-value mapping is exhaustive code over typed models. It does not
split strings, use reflection, or call a generic mapping-path helper.

Stage-specific facts such as the Hi-C enzyme may be exposed through this enum
from the typed stage context. Adding such a member requires adding both its
typed producer and resolver case.

## Stage Contexts and Input Snapshot

The shared facts model intentionally does not absorb every stage detail.
Each stage owns one strict context model:

### Assembly context

`AssemblyRecommendationContext` contains:

- selected assembly route and normalized read type;
- target assembly level;
- method-relevant asset availability not already represented in common facts;
- task-specific retry/review context;
- bounded chronological attempt history.

Execution paths, run IDs, and artifact refs are excluded.

### AutoHiC context

`AutoHiCRecommendationContext` contains:

- normalized Hi-C library protocols;
- explicitly provided restriction enzyme;
- task-specific bounded chronological attempt history.

The enzyme is also available through the
`hic_provided_restriction_enzyme` L1 fact key. The typed context remains its
stage-owned source; the common facts model does not gain a Hi-C-only field.

### T2T context

`T2TRecommendationContext` contains:

- resolved telomere motif and its semantic source;
- positive ONT asset count;
- bounded assembly/read facts required only by T2T threshold selection.

The T2T motif request has its own strict context containing canonical species
facts and the optional configured hint. The threshold context consumes only the
persisted resolved motif.

### Snapshot

Every recommendation point builds and persists:

```text
RecommendationInputSnapshotV3
├── schema_version: 3
├── method_id
├── task
├── facts: CanonicalRecommendationFacts
├── stage_context: discriminated stage-context union
└── dependency_refs: RecommendationDependencyRefs
```

`dependency_refs` binds the snapshot to source artifacts for replay and audit.
It is never projected into a model DTO or prompt.

The snapshot is stage- and attempt-local. There is no long-lived global facts
snapshot reused after upstream state changes. “One facts model” means one
schema and adapter, not one stale object for the entire run.

Retries reuse the initial attempt's canonical facts and selected literature
basis. Only attempt history and review/execution context change. A retry does
not reread mutable upstream dictionaries, recalculate the sample facts, or
reselect papers.

## Facts Adapter and Data Flow

`app/domains/parameter_recommendation/facts.py` owns:

- reading the authoritative species facts from shared parameters;
- reading current Read-QC and genome-survey typed producer contracts;
- unit normalization;
- effective genome-size selection;
- raw-coverage derivation;
- conflict detection and canonical fact construction.

Stage `context.py` modules read only their stage-owned typed output and build
their strict stage context. They do not reimplement common fact extraction.

The required initial-attempt flow is:

```text
state and typed upstream outputs
  -> canonical facts adapter
  -> stage context builder
  -> RecommendationInputSnapshotV3
  -> persist input snapshot
  -> typed retrieval query and immutable basis
  -> stage-specific model request DTO
  -> persist exact rendered messages
  -> invoke model with those same messages
```

The input snapshot is persisted before retrieval so a retrieval failure still
has inspectable input evidence.

## Retrieval

`app/domains/assembly/retrieval_query.py` changes from
`dict[str, Any]` plus dotted paths to a typed input:

```text
build_hifiasm_retrieval_query(
    facts: CanonicalRecommendationFacts,
    context: AssemblyRecommendationContext,
    method_id: str,
    task: ParameterAttemptTask,
) -> HifiasmRetrievalQuery
```

It reads:

- `facts.genome.effective_genome_size` and converts bp to Mb;
- `facts.sequencing.raw_coverage` directly;
- `facts.reads.read_n50` and converts bp to kb;
- typed heterozygosity, ploidy, clade, and read type.

The query retains source labels suitable for retrieval diagnostics, but those
labels are stable fact identifiers rather than source-object paths.

`profile_paths.py` is deleted. There is no fallback to old field names or old
coverage estimates.

## Model DTOs

The DTO classes remain under `app/llm/parameters/<stage>/request.py` because
they define provider-facing allowlists, not domain storage.

All parameter recommendation DTOs use:

- `schema_version: 3`;
- the unchanged `recommendation_contract:
  parameter_recommendation_v1`;
- `method_id`;
- `task`;
- `facts`, projected from `CanonicalRecommendationFacts`;
- one typed stage context;
- `adjustable_parameters`, selected from catalog entries declaring L2;
- stage-specific evidence and retry context where applicable.

Assembly retains literature selection, cases, and method guidance. AutoHiC
retains only its bounded stage facts and attempt history. T2T retains its
resolved motif and threshold-relevant context.

No DTO contains session identity, locators, artifact IDs, hashes, source paths,
dependency refs, credentials, or unrestricted registry metadata. DTOs are
strict and reject unknown fields.

The response stays:

```json
{
  "parameters": {"parameter_name": "explicit_value"},
  "summary": "Optional reviewer-facing explanation."
}
```

`parameters: {}` remains valid. Parsing remains shape-only. Omitted keys are
not filled, and retry responses are not merged with their predecessor.

## T2T Motif Resolution

The motif resolver uses the same canonical species facts projection as the
three parameter recommenders. It does not reread `shared_parameters` directly.

The optional configured motif hint is isolated in the T2T motif context. The
resolver persists the exact request, response, parse result, and resolved
source. Every later T2T consumer reads only the typed resolved motif.

The hard cutover removes direct reads of the old configured motif from
threshold selection and scanning consumers. The explicit configured-hint path
at the resolver boundary remains supported only if it is part of the new typed
T2T context; it is not treated as an old input-profile compatibility alias.

## File Ownership

The target layout is:

```text
app/schemas/
├── parameter_catalog.py
└── parameter_recommendation.py

app/domains/parameter_recommendation/
├── __init__.py
├── facts.py
└── resolution.py

app/domains/assembly/
└── retrieval_query.py

app/stages/assembly/parameters/
└── context.py

app/stages/hic_scaffolding/parameters/
└── context.py

app/stages/t2t/parameters/
└── context.py

app/llm/parameters/
├── assembly/request.py
├── hic/request.py
└── t2t/request.py
```

Ownership rules:

- `schemas` owns serializable cross-stage contracts and catalog syntax;
- `domains/parameter_recommendation` owns common fact derivation and source
  resolution;
- each stage owns its context construction and execution rendering;
- `llm` owns only model-facing allowlists and prompt rendering;
- storage adapters continue to own artifact publication.

`app/stages/assembly/command_builder.py` remains Assembly-owned for CLI
rendering, but it consumes `ResolvedParameterSources`. It no longer reads
`TopLevelState`, walks `source_priority`, or merges source groups itself.
AutoHiC and T2T freeze code use the same resolver and keep their stage-specific
config writers.

## Deleted Surface

The implementation deletes, rather than deprecates:

- `app/stages/assembly/parameters/profile.py`;
- `app/domains/assembly/profile_paths.py`;
- `app/domains/assembly/profile_evidence.py`;
- dictionary-style Hi-C and T2T input-profile builders;
- `_first_available_value`, `_lookup_path`, and arbitrary dotted
  `source_priority` handling in Assembly command construction;
- singular catalog `level`, `source`, `source_priority`, top-level `default`,
  and top-level `fixed_value`;
- `descriptors/knowledge_packs/assembly/parameter_recommender/rules/profile_boundaries.yaml`
  and its registry entry;
- fixtures and documentation containing old profile field names;
- stale retrieval-activation placeholder documentation;
- schema aliases, dual writes, and compatibility readers for pre-v3
  recommendation input artifacts.

Any still-needed retry evidence projection is rebuilt against typed retry
models. It is not retained by keeping `profile_evidence.py`.

## Validation and Failure Behavior

Construction fails before retrieval or model invocation when:

- a numeric fact is non-finite, has the wrong unit, or violates its typed
  domain;
- measured and provided values conflict in a way the existing authoritative
  producer contract does not resolve;
- a stage context discriminator does not match the selected method/stage;
- a catalog references an unknown L0 binding, fact key, or transform;
- a dual-layer declaration lacks an explicit valid precedence;
- an old input snapshot or catalog schema is supplied.

Missing optional facts remain `None`. The adapter does not guess a value, scan
old aliases, infer species from filenames, or manufacture a coverage estimate
from a non-authoritative field.

Recommendation parsing continues to preserve shape-valid unknown L2 names for
human review. After approval, source resolution fails explicitly if a key is
not cataloged as L2 or a value cannot be rendered. It never repairs the
recommendation.

## Persistence and Replay

The persisted input artifact contains the complete
`RecommendationInputSnapshotV3`, including audit-only dependency refs.
`model_request.json` continues to own the exact rendered messages sent to the
provider. Basis, response, parse, recommendation, approval, and frozen-attempt
records retain their existing ownership boundaries.

Old recommendation artifacts and checkpoints are unsupported by design. A
resume that points at a pre-v3 input snapshot reports a schema mismatch and
stops. The implementation does not silently rebuild it from current state,
because that would break replay identity.

## Verification

### Unit tests

- canonical fact construction from representative production Read-QC,
  genome-survey, and shared-parameter contracts;
- unit, finite-number, origin, fraction, and conflict validation;
- raw coverage is derived once from total bases and effective genome size;
- missing optional values remain absent;
- every `CanonicalFactKey` has an explicit typed resolver;
- catalog loading rejects old fields, dotted paths, unknown bindings, and
  invalid multi-layer declarations;
- AutoHiC `restriction_enzyme` resolves from L1 when L2 omits it and from L2
  when approved L2 supplies it;
- one dual-source parameter is rendered exactly once;
- Flye/hifiasm genome-size bindings resolve from the same canonical fact;
- request DTOs reject dependency refs, sessions, locators, hashes, and unknown
  fields;
- pre-v3 snapshots and old catalog schemas fail closed.

### Cross-stage contract tests

- Assembly, Hi-C, T2T threshold, and T2T motif requests project the same
  canonical species facts;
- the same upstream Read-QC/genome-survey state yields identical common facts
  at each stage point in time;
- each stage context accepts only its own discriminator and fields;
- each model request exposes exactly catalog-declared L2 choices;
- provided facts, L0 defaults, approved L2 values, and effective values remain
  separate in frozen evidence.

### Integration tests

- a production-shaped state reaches hifiasm retrieval with non-null genome
  size, coverage, read N50, heterozygosity, and ploidy when those producers
  supplied them;
- persisted input snapshot precedes retrieval and survives retrieval failure;
- initial recommendation persists the v3 snapshot, basis, exact request,
  response, parse, sparse recommendation, and approval request;
- Assembly and AutoHiC retries reuse the original canonical facts and selected
  literature basis, change only retry context, and do not reselect papers;
- T2T motif resolution feeds the typed resolved motif into threshold
  recommendation and all scan/patch consumers;
- empty sparse mappings still reach approval and use only L0/L1/default
  execution sources;
- invalid approved values fail at freeze without changing the recommendation.

### Removal and regression checks

- repository search finds no removed profile field names, dotted
  `source_priority`, compatibility aliases, or deleted modules outside
  explicitly historical design documents;
- all production paper packs load and validate;
- focused parameter-recommendation and command/freeze suites pass;
- the full test suite passes;
- changed-file lint and `git diff --check` pass.

## Production Readiness and Merge Gate

This branch is ready to merge only after the hard-cutover implementation,
removal checks, full suite, and a real-paper end-to-end trial all pass. The
trial must prove that a production-shaped state produces a populated typed
retrieval query, a v3 model request without audit fields, a sparse approved L2
mapping, and a provenance-separated frozen effective configuration.

Until that evidence exists, the branch remains separate from `main`.
