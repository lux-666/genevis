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

This does not invalidate data v2's artifact/ref ownership model. The incorrect
architecture is the parameter-specific profile and catalog layer built on top
of it: parallel dictionary contracts and a singular `ParameterSpec.level`
cannot express the actual source semantics. Data v2 remains the persistence
envelope; the v3 recommendation artifacts defined here are new payload
contracts inside that envelope.

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

The replacement declaration separates the parameter's execution destination
from its permitted value sources:

```yaml
catalog_id: autohic
method_id: hic_scaffolding.candidate.autohic

parameters:
  restriction_enzyme:
    value_type: string
    application:
      kind: config_key
      key: restriction_enzyme
    required_for_execution: true
    layers:
      l1:
        fact: hic_provided_restriction_enzyme
      l2:
        adjustable: true
    description: Restriction enzyme used to prepare the selected Hi-C library.
```

An ordinary hifiasm L1 binding is:

```yaml
hg_size:
  value_type: string
  application:
    kind: cli_option
    flag: --hg-size
    order: 40
  layers:
    l1:
      fact: effective_genome_size_bp
      transform: hifiasm_hg_size
  description: Effective haploid genome size.
```

An L0 runtime input uses a registered binding rather than a state path:

```yaml
input_reads:
  value_type: path_list
  application:
    kind: positional
    position: 0
  layers:
    l0:
      kind: runtime
      binding: assembly_input_reads
      required: true
```

`level`, `source`, `source_priority`, top-level `default`, and top-level
`fixed_value` are removed. The ambiguous `cli: null` convention is also
removed. Source-specific fixed or materialized default values live inside
`layers.l0`. L2 has no default field.

### Catalog schema

The catalog schema contains these concepts:

```text
MethodParameterCatalog
├── catalog_id
├── method_id
├── tool
└── parameters

ParameterSpec
├── value_type and descriptive constraints
├── application: ParameterApplication
├── required_for_execution: bool
├── layers
│   ├── l0: ExecutorSource | null
│   ├── l1: FactSource | null
│   └── l2: ModelSource | null
└── description / notes
```

`catalog_id` is the short descriptor-directory identity such as `hifiasm` or
`autohic`. `method_id` is always the stable execution identity used by
selection, snapshots, requests, attempts, and ledger records, such as
`assembly.candidate.hifiasm` or `hic_scaffolding.candidate.autohic`. Catalog
loaders resolve a descriptor's `parameter_catalog` to `catalog_id` and then
require the loaded stable `method_id` to equal the selected method. No request
or persisted attempt uses a short catalog ID as its method identity.

`ExecutorSource` is a discriminated union:

| `kind` | Required fields | Meaning |
|---|---|---|
| `fixed` | `value` | Route-owned value that cannot coexist with L1 or L2 |
| `runtime` | `binding`, `required`, optional `materialized_default` | Value supplied through a registered typed execution binding; the default is used only when the binding is absent |
| `materialized_default` | `value` | Wrapper-owned concrete default with no runtime binding |

- A runtime `binding` is an `ExecutorBindingKey` enum member registered by the
  owning stage, never a dotted state path.
- `runtime.required=true` and `materialized_default` are mutually exclusive.
  A required runtime binding must be present.
- `fixed` cannot coexist with L1 or L2 because a value that may be overridden
  is not fixed.
- `FactSource.fact` is a `CanonicalFactKey` enum.
- `FactSource.transform` is a small validated transform enum such as
  `identity` or `hifiasm_hg_size`, not an import path or free-form expression.
- `ModelSource.adjustable` must be `true` when L2 exists.
- A parameter must declare at least one layer.
- L0 `runtime` or `materialized_default`, L1, and L2 may coexist. Their
  precedence is the single global rule below; catalogs do not declare a second
  `source_priority`.
- `required_for_execution` belongs to `ParameterSpec`, not to an individual
  source. It means that some effective value must exist after all declared
  layers have been considered. Thus AutoHiC enzyme may be satisfied by either
  L1 or L2.

`ParameterApplication` is a discriminated union that replaces every meaning
previously hidden behind `cli` and `cli: null`:

| `kind` | Required fields | Consumer |
|---|---|---|
| `cli_option` | `flag`, `order`, optional `join` | emits `flag value`; `join` controls scalar, comma-joined, or repeated values |
| `cli_switch` | `flag`, `order` | emits the flag only for boolean `true` |
| `selector` | none | non-emitting execution selector referenced by one `choice_option` |
| `choice_option` | `selector`, `flags`, `order`, optional `join` | maps the selector value to a flag and emits that flag immediately followed by this parameter's own value(s) |
| `positional` | `position` | emits one scalar or expands a path list at the declared position |
| `config_key` | `key` | writes one key in a stage-owned typed config |
| `stage_value` | `key` | supplies a typed internal execution value such as a T2T threshold |

CLI option/switch/choice-option applications are emitted in ascending `order`,
followed by positionals in ascending `position`. A `selector` is not emitted
independently; catalog validation requires exactly one `choice_option` to
reference it, and that choice option emits its flag and values as one adjacent
render unit. Order/position collisions and dangling or multiply referenced
selectors fail catalog validation. A stage renderer accepts only the
application kinds it supports and fails closed on any other kind.

Catalog entries must describe execution destinations. Old `cli: null` entries
that only exposed recommendation context, such as Flye/NextDenovo `read_n50`
and `estimated_coverage`, are deleted from catalogs; those values already live
in canonical facts. They are not migrated to a `context_only` application.

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

This fixed precedence is applied only when a parameter declares the
corresponding layers; there is no per-parameter precedence field. After source
selection, every `required_for_execution` parameter must have an effective
value. An L2 key that is unknown or not L2-adjustable fails at the post-approval
executability boundary; the persisted recommendation is not rewritten.

The result is a typed, provenance-aware structure:

```text
ResolvedParameterSources
├── l0_executor
├── l1_facts
├── l2_approved
├── effective_parameters
├── effective_source_by_parameter
└── applications
```

Command/configuration rendering joins each effective value to its validated
`application` and visits every parameter once. It does not concatenate three
source groups, so a dual-source parameter cannot be emitted twice. Assembly
CLI rendering consumes CLI options, switches, selectors/choice options, and
positionals; NextDenovo and AutoHiC configuration writers consume
`config_key`; T2T execution consumes `stage_value`.

The frozen-attempt artifact persists all six views. The approved sparse L2
mapping remains byte-for-byte distinguishable from facts and defaults.

### Complete migrated examples

An Assembly CLI catalog declares both rendering and sourcing explicitly:

```yaml
catalog_id: flye
method_id: assembly.candidate.flye
tool: flye
parameters:
  read_type:
    value_type: string
    application: {kind: selector}
    required_for_execution: true
    layers:
      l0:
        kind: runtime
        binding: assembly_method_read_type
        required: true
  input_reads:
    value_type: path_list
    application:
      kind: choice_option
      selector: read_type
      order: 10
      join: expand
      flags:
        nano-raw: --nano-raw
        nano-hq: --nano-hq
        pacbio-raw: --pacbio-raw
        pacbio-hifi: --pacbio-hifi
    required_for_execution: true
    layers:
      l0:
        kind: runtime
        binding: assembly_input_reads
        required: true
  genome_size:
    value_type: int
    application: {kind: cli_option, flag: --genome-size, order: 30}
    required_for_execution: true
    layers:
      l1: {fact: effective_genome_size_bp, transform: identity}
  min_overlap:
    value_type: int
    application: {kind: cli_option, flag: --min-overlap, order: 80}
    layers:
      l2: {adjustable: true}
```

AutoHiC's dual-source config value and an executor-default/L2 value are:

```yaml
catalog_id: autohic
method_id: hic_scaffolding.candidate.autohic
tool: AutoHiC
parameters:
  restriction_enzyme:
    value_type: string
    application: {kind: config_key, key: restriction_enzyme}
    required_for_execution: true
    layers:
      l1: {fact: hic_provided_restriction_enzyme, transform: identity}
      l2: {adjustable: true}
  number_of_edit_rounds:
    value_type: int
    application: {kind: config_key, key: number_of_edit_rounds}
    required_for_execution: true
    layers:
      l0: {kind: materialized_default, value: 2}
      l2: {adjustable: true}
```

A T2T internal threshold has no fake CLI:

```yaml
catalog_id: minimap2
method_id: t2t.alignment.minimap2
tool: minimap2
parameters:
  min_mapq:
    value_type: int
    application: {kind: stage_value, key: min_mapq}
    required_for_execution: true
    layers:
      l0: {kind: materialized_default, value: 20}
      l2: {adjustable: true}
```

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
    └── producer: run_config | read_qc | genome_survey | assembly | hic | t2t |
                  parameter_recommendation
```

Examples include:

- genome size in `bp`;
- read N50 in `bp`;
- total bases in `bp`;
- sequencing coverage in `x`;
- heterozygosity and repeat fraction in `fraction`;
- provided-versus-measured genome-size delta in `relative_change`;
- expected chromosome count and ploidy in `count`.

`fraction` values are stored as `[0, 1]`, never sometimes percentages and
sometimes fractions. `relative_change` is a separate unit with a finite value
greater than `-1` and no upper bound; it represents
`(provided - measured) / measured`. Counts are positive integers where
biologically required. Numeric values reject booleans, NaN, and infinity.

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
│   ├── provided_vs_measured_relative_change
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
Each stage owns one strict context model. `stage_context` is a Pydantic
discriminated union on the field `stage`.

| Context | Exact fields |
|---|---|
| `AssemblyRecommendationContext` | `stage: Literal["assembly"]`; `assembly_route: Literal["hifi", "ont", "cyclone"]`; `read_type: NormalizedReadType`; `target_level: Literal["draft", "near_t2t", "strict_t2t"]`; `retry: RecommendationRetryContext \| None` |
| `AutoHiCRecommendationContext` | `stage: Literal["hic_scaffolding"]`; `library_protocols: tuple[NonEmptyStr, ...]`; `provided_restriction_enzyme: NonEmptyStr \| None`; `retry: RecommendationRetryContext \| None` |
| `T2TRecommendationContext` | `stage: Literal["t2t"]`; `resolved_telomere_motif: DnaMotif`; `motif_source: Literal["provided_species_fact", "llm_species_resolution"]`; `ont_asset_count: PositiveInt` |

`library_protocols` is deterministically deduplicated, sorted, and limited to
64 values of at most 120 characters. The T2T threshold workflow has no retry in
this scope and therefore has no retry field.

The AutoHiC enzyme is available through the
`hic_provided_restriction_enzyme` L1 fact key. The typed context remains its
stage-owned source; the common facts model does not gain a Hi-C-only field.

Execution paths, run IDs, artifact refs, and retry-policy values are excluded
from all three contexts.

### Retry evidence

`RecommendationRetryContext` is present only for
`retry_after_execution_failure` or `retry_after_review_failure` and contains:

```text
RecommendationRetryContext
├── trigger: ExecutionFailureTrigger | ReviewFailureTrigger
└── attempt_history: tuple[ParameterAttemptEvidenceV3, ...]
```

The history contains exactly every sealed predecessor in ascending
`attempt_id`; its length must equal the current recommendation ID. It is
bounded by the operator-authorized attempt chain rather than silently
truncated. Each item has this strict surface:

| Field | Type and bound |
|---|---|
| `attempt_id` | non-negative integer |
| `task` | `ParameterAttemptTask` |
| `status` | `ParameterAttemptStatus` |
| `recommended_parameters` | exact prior sparse JSON mapping |
| `approved_parameters` | exact approved sparse JSON mapping |
| `effective_parameters` | effective non-locator values from the frozen record |
| `effective_sources` | parameter name to `Literal["l0", "l1", "l2"]` |
| `execution` | optional `ParameterExecutionEvidence` |
| `review` | optional stage-specific review evidence |

`ParameterExecutionEvidence` contains `status`, optional `return_code`,
`failure_class` (120 characters), `status_detail` (500 characters), up to 16
finite numeric resource metrics, and an optional 4,000-character `stderr_tail`.
Only the latest failed execution may include the tail. It never contains
stdout/stderr refs, commands, paths, environment variables, or credentials.

Assembly review evidence contains the existing typed failure mode and these
optional finite metrics only: assembly size, assembly-size ratio, contig count,
N50, N90, largest contig, BUSCO complete/single/duplicate/fragmented/missing,
and the bounded scalar QUAST metrics selected by the review adapter. AutoHiC
review evidence contains the decision
`accept_candidate | keep_prior_assembly | request_parameter_retry` and at most
20 reviewer notes of 500 characters each. Unknown review fields do not cross
the model boundary.

The trigger duplicates only the current reason for requesting the retry:
execution status/return code/failure class/detail for an execution retry, or
the typed stage review evidence for a review retry.

### Snapshot

Every parameter-recommendation point builds and persists:

```text
RecommendationInputSnapshotV3
├── schema_version: 3
├── snapshot_kind: parameter_recommendation
├── method_id
├── task
├── facts: CanonicalRecommendationFacts
├── stage_context: discriminated stage-context union
└── dependency_refs: RecommendationDependencyRefs
```

`method_id` is the stable execution ID and must equal both the selected method
and `catalog.method_id`. Short catalog IDs are rejected.

`RecommendationDependencyRefs` has this exact audit-only surface:

```text
RecommendationDependencyRefs
├── fact_source_refs:
│   └── dict[read_qc_summary | genome_survey_result, ArtifactRef]
├── stage_input_refs: tuple[ArtifactRef, ...]
├── initial_input_snapshot_ref: ArtifactRef | null
├── prior_attempt_manifest_refs: tuple[ArtifactRef, ...]
└── motif_resolution_ref: ArtifactRef | null
```

- `initial_input_snapshot_ref` and `prior_attempt_manifest_refs` are empty on an
  initial attempt and required on a retry.
- `prior_attempt_manifest_refs` has one entry per history item in identical
  order.
- `motif_resolution_ref` is required only for the T2T threshold snapshot.
- `stage_input_refs` is deterministically role-sorted and limited to 1,024
  canonical refs; a larger input set must be represented by its indexed
  manifest ref rather than expanded into the snapshot.
- Missing optional upstream artifact roles are omitted rather than represented
  by path strings.
- All refs are canonical `ArtifactRef` records; bare paths and logical asset
  IDs are not accepted.

`dependency_refs` binds the snapshot to source artifacts for replay and audit.
It is never projected into a model DTO or prompt.

The snapshot is stage- and attempt-local. There is no long-lived global facts
snapshot reused after upstream state changes. “One facts model” means one
schema and adapter, not one stale object for the entire run.

Assembly retries load the initial v3 snapshot through
`initial_input_snapshot_ref`, copy its canonical facts byte-for-byte, and reuse
the same immutable Assembly literature-basis ref. AutoHiC retries likewise load
and copy the initial canonical facts, but AutoHiC has no literature selection
or literature basis to reselect. Only typed retry context and its dependency
refs change. A retry does not reread mutable upstream dictionaries or
recalculate sample facts.

### Separate T2T motif snapshot

Motif resolution is not a parameter recommendation and has no method catalog.
It therefore uses a separate strict contract rather than pretending to be a
member of the parameter stage-context union:

```text
T2TMotifInputSnapshotV3
├── schema_version: 3
├── snapshot_kind: t2t_motif_resolution
├── resolver_id: t2t.telomere_motif_resolver
├── facts:
│   └── species: CanonicalSpeciesFacts
└── dependency_refs: RecommendationDependencyRefs
```

For this snapshot, retry refs and `motif_resolution_ref` must be absent;
available canonical upstream fact refs may remain in `fact_source_refs`.
There is no `method_id`, `task`, parameter catalog, or parameter stage context.
Identity validation compares `resolver_id` to the fixed resolver constant.

`T2TMotifRequestV3` projects only `schema_version: 3`, the unchanged
motif-resolution contract, `resolver_id`, and canonical species facts. If a
supplied canonical motif makes resolution deterministic, the input snapshot
and resolution manifest are still persisted but no model
request/response/parse records are fabricated.

## Facts Adapter and Data Flow

`app/domains/parameter_recommendation/facts.py` owns:

- reading the authoritative species facts from shared parameters;
- projecting the current Read-QC summary mapping and `GenomeSurveyResult`
  estimate mappings into strict adapter-local source models;
- unit normalization;
- effective genome-size selection;
- raw-coverage derivation;
- conflict detection and canonical fact construction.

Read-QC and genome survey do not currently publish fully typed nested
quantitative contracts: Read-QC publishes a summary mapping, and
`GenomeSurveyResult.estimates`, `measured_estimates`, and
`provided_overrides` are `dict[str, Any]`. Changing those stage output schemas
is outside this refactor. The facts adapter is an anti-corruption boundary: it
allowlists the currently authoritative producer keys, validates them
immediately into private strict `ReadQCRecommendationSource` and
`GenomeSurveyRecommendationSource` models, and only then constructs canonical
facts. Malformed present values fail; absent optional values remain absent.
No downstream recommendation code receives the source mappings.

Stage `context.py` modules read only their stage-owned typed output and build
their strict stage context. They do not reimplement common fact extraction.

The required initial-attempt flow is:

```text
state and current upstream outputs
  -> canonical facts adapter
  -> strict adapter-local source models
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

The fact projection is normative:

| Request | `facts` projection | Additional fields |
|---|---|---|
| Assembly | species, reads, genome, sequencing, and asset presence, excluding `None` | `AssemblyRecommendationContext`, method guidance, literature selection, and literature cases |
| AutoHiC | canonical species facts only | `AutoHiCRecommendationContext` |
| T2T threshold | canonical species facts only | `T2TRecommendationContext` |
| T2T motif | canonical species facts only | no parameter catalog; this uses the separate motif-resolution contract |

Every projection reuses the canonical nested models; no request declares a
second species or quantity schema. Assembly retry evidence is carried only by
its context and reuses the initial literature basis. AutoHiC retains only its
bounded stage facts and attempt history and has no literature fields. T2T
retains its resolved motif and ONT asset count in its context.

`adjustable_parameters` is keyed only by catalog entries with `layers.l2`.
Each value contains `value_type`, allowed values, min/max, description, and
notes; it excludes all L0/L1 declarations, application bindings, defaults, and
stable execution metadata.

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

If canonical `species.supplied_telomere_motif` is present, the resolver
normalizes it and persists a deterministic resolution with source
`provided_species_fact` without invoking the model. Otherwise,
`species.scientific_name` is required and the resolver sends canonical species
facts to the model, persisting the exact request, response, parse result, and
source `llm_species_resolution`. Every later T2T consumer reads only the typed
resolved motif.

The schema-2 motif-resolution manifest has
`resolution_mode: provided_species_fact | llm_species_resolution`.
`input_snapshot_ref` and `resolution_ref` are always required. The three model
refs are forbidden in provided mode and required in LLM mode. Resume validates
the v3 motif snapshot and fixed `resolver_id` before applying those conditional
manifest rules.

The old `t2t.telomere_motif` run-config field,
`configured_telomere_motif`, `legacy_motif_hint`, raw `telomere_motif`
checkpoint fallback, and sources `legacy_t2t_config` /
`legacy_t2t_checkpoint` are deleted. They are not accepted at the resolver
boundary under a new name. A run must supply `species_facts` and either its
optional canonical motif or enough species identity for model resolution.

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
  top-level `fixed_value`, `cli`, and every overloaded `cli: null`;
- catalog-only context placeholders such as Flye/NextDenovo `read_n50` and
  `estimated_coverage`;
- `descriptors/knowledge_packs/assembly/parameter_recommender/rules/profile_boundaries.yaml`
  and its registry entry;
- `T2TRunConfig.telomere_motif`, `configured_telomere_motif`,
  `legacy_motif_hint`, raw checkpoint `telomere_motif` fallback, and their
  compatibility-source labels;
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
- a catalog has an unsupported application, duplicate render order/position,
  `fixed` plus another source layer, or an invalid runtime/default combination;
- an old input snapshot or catalog schema is supplied.

Missing optional facts remain `None`. The adapter does not guess a value, scan
old aliases, infer species from filenames, or manufacture a coverage estimate
from a non-authoritative field.

Recommendation parsing continues to preserve shape-valid unknown L2 names for
human review. After approval, source resolution fails explicitly if a key is
not cataloged as L2 or a value cannot be rendered. It never repairs the
recommendation.

## Persistence and Replay

The hard cutover changes these persisted contracts:

| Record | New file/key/type rule | Schema |
|---|---|---|
| recommendation input | `input_snapshot.json`; manifest key `input_snapshot_ref`; exact constants/types listed below | 3 |
| basis snapshot | existing basis filename/type; contains the new catalog snapshot | 3 |
| model request record | existing filename/type; owns exact rendered messages | 3 |
| recommendation manifest | requires `input_snapshot_ref`, `basis_snapshot_ref`, `model_request_ref`, `model_response_ref`, `response_parse_ref`, and `recommendation_ref` | 3 |
| recommendation cursor | same ownership, but validates only a v3 manifest chain | 3 |
| `ParameterAttempt` checkpoint cursor | same refs/ownership | 3 |
| attempt manifest | same filename/type and seal semantics | 3 |
| frozen parameter-source record | persists the six `ResolvedParameterSources` views | 3 |

The exact new input constants and artifact types are:

| Constant | Artifact type |
|---|---|
| `ASSEMBLY_PARAMETER_INPUT_SNAPSHOT` | `assembly.parameter_recommendation.input_snapshot` |
| `HIC_PARAMETER_INPUT_SNAPSHOT` | `hic_scaffolding.parameter_recommendation.input_snapshot` |
| `T2T_PARAMETER_INPUT_SNAPSHOT` | `t2t.parameter_recommendation.input_snapshot` |
| `T2T_MOTIF_INPUT_SNAPSHOT` | `t2t.telomere_motif.input_snapshot` |

The old `*_PARAMETER_INPUT_PROFILE` / `T2T_MOTIF_INPUT_PROFILE` constants and
artifact types, `input_profile.json` / `motif_input_profile.json`, and
`input_profile_ref` manifest keys are deleted and replaced by the table above,
`input_snapshot.json` / `motif_input_snapshot.json`, and
`input_snapshot_ref`. The recommendation record,
raw model-response record, response-parse record, review request/decision, and
execution records retain their current schemas because their bodies do not
change. A v3 manifest may reference only records produced and hash-bound by its
own v3 chain; retaining an unchanged leaf schema does not make a pre-v3 chain
resumable.

T2T motif persistence uses `motif_input_snapshot.json` with a new
`.input_snapshot` artifact type and schema 3. In LLM mode it also uses a
model-request record schema 3. The motif-resolution manifest is schema 2 with
the conditional refs defined above; its response, parse, and resolution leaf
records retain their current bodies.

Resume/hydration performs this ordered gate before reading a recommendation or
calling a model:

1. validate `ParameterRecommendationCursor` / `ParameterAttempt` as schema 3;
2. load a schema-3 recommendation manifest of the expected stage artifact type;
3. require and hash-validate its schema-3 input snapshot, basis, and
   model-request refs;
4. validate `snapshot.method_id == catalog.method_id == attempt.method_id` and
   the stage-context discriminator;
5. validate every remaining manifest ref and hash using the existing immutable
   artifact rules.

Any failure reports `unsupported_parameter_recommendation_schema` or the
specific integrity mismatch and stops. The implementation never falls back to
an old key, reconstructs a missing snapshot from current state, or mixes a v2
cursor/manifest with v3 artifacts.

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
- catalogs reject ambiguous applications, duplicate CLI order/positions, and
  short method IDs in persisted contracts;
- AutoHiC `restriction_enzyme` resolves from L1 when L2 omits it and from L2
  when approved L2 supplies it;
- one dual-source parameter is rendered exactly once;
- selector/choice options preserve flag-value adjacency, and positional
  inputs, CLI options/switches, config keys, and T2T stage values each reach
  only their declared renderer;
- Flye/hifiasm genome-size bindings resolve from the same canonical fact;
- request DTOs reject dependency refs, sessions, locators, hashes, and unknown
  fields;
- pre-v3 cursors, attempts, manifests, input profiles, and old catalog schemas
  fail closed;
- motif snapshots validate the fixed resolver identity without a parameter
  catalog, and provided-motif manifests contain no fabricated model refs;
- old T2T motif run-config and checkpoint fields are rejected rather than
  recovered.

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
- Assembly retries reuse the original canonical facts and Assembly literature
  basis without reselecting papers;
- AutoHiC retries reuse the original canonical facts, change only typed retry
  context, and never create or load a literature basis;
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
