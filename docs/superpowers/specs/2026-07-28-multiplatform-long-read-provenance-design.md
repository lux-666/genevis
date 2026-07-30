# Multiplatform Long-Read Provenance and Single-Backbone Assembly Design

## Context

The current manifest and inventory path cannot safely represent PacBio HiFi
and ONT inputs when both are declared as `raw_long_reads`.

`validate_manifest()` stores inferred properties in a mapping keyed by
`asset_type`. A later entry of the same type replaces the earlier entry, and
`_build_asset_records()` then applies the surviving properties to every
matching manifest entry. Asset IDs are also constructed from
`session_id:asset_type:basename`, so files with the same basename in different
directories collide.

Fixing only that mapping is insufficient. The current read-QC handoff projects
all long reads into one `platform` value and replaces
`resource_refs["long_reads"]` without retaining the per-file platform relation.
Assembly then selects inputs from the first projected platform, while T2T later
reopens the registry independently. The same provenance can therefore be
correct at inventory and wrong after read-QC.

The Assembly stage also creates planned bundles for a primary and fallback
method even though it executes only the primary method. That makes an
unmaterialized fallback look like a peer assembly candidate. This change
instead models one selected Assembly attempt with one canonical primary FASTA
and zero or more method-native outputs in the existing executed candidate
bundle.

The current TUI intake has the same single-platform assumption: its typed draft
contains ONT reads but no HiFi reads, and its planner entry request hard-codes
ONT. The existing manifest, run-config, draft, and candidate-bundle structures
are otherwise sufficient. This design extends those structures in place rather
than adding a second input-selection or assembly-product protocol.

## Goals

- Preserve a stable identity and platform for every declared long-read file.
- Keep HiFi and ONT distinct through inventory, read-QC, resource handoff,
  Assembly input selection, and later T2T input resolution.
- Accept every file belonging to the selected read role. A platform split
  across several manifest entries or several files in one entry must not be
  reduced to the first path.
- Extend the existing TUI draft and planner so a user can declare HiFi, ONT,
  and paired multi-lane Hi-C inputs in natural language before the ordinary
  manifest/run-config freeze.
- Let the HiFi Hifiasm method consume multiple primary HiFi files, optional
  multiple ONT ultra-long files through `--ul`, and optional paired multi-lane
  Hi-C files through `--h1`/`--h2`.
- Keep `assembly.candidate.hifiasm_ont` as a separate ONT-primary method that
  fixes Hifiasm's `--ont` switch and does not reuse the HiFi hybrid-input
  contract.
- Retain Data v2 ownership, immutable-artifact, ArtifactRef, ledger, checkpoint,
  crash, and resume invariants.
- Publish one canonical Assembly FASTA for downstream stages.
- Retain adapter-declared method-native deliverables as indexed artifacts
  without exposing them as peer assemblies.
- Prevent planned but unexecuted fallback methods from appearing as materialized
  review candidates.
- Preserve Assembly Dedup's separate raw-versus-purged candidate relation; the
  single-backbone rule applies to assembler-origin products, not later derived
  review candidates.

## Non-goals

- No parallel or sequential execution of Hifiasm and
  NextDenovo/NextPolish.
- No candidate integration, quarTeT orchestration, or automatic assembler
  fallback.
- No automatic selection between Hifiasm's primary, alternate, or haplotype
  graphs. Only the adapter-declared canonical primary FASTA enters the
  downstream assembly resource.
- No use of `--ul`, `--h1`, or `--h2` in the separate
  `assembly.candidate.hifiasm_ont` method.
- No automatic use of method-native Hifiasm contigs by Hi-C scaffolding or T2T.
- No compatibility reader, migration, or dual write for earlier development
  sessions or checkpoints.

## Considered Approaches

### Patch only the manifest inference mapping

Key inference by path or manifest index and leave later stages unchanged.
This fixes the immediate overwrite but loses the relation again when read-QC
projects several inputs into one platform. It is rejected because it cannot
meet the end-to-end provenance goal.

### Extend the existing contracts in place

Make the asset registry authoritative for declared inputs, add an immutable
read-set manifest for derived read files, and make resource resolution load
those records into runtime-only typed members. Add HiFi facts beside the
existing ONT and Hi-C facts in `TuiSessionDraft`, add explicit Hifiasm input-use
booleans to the existing run config, add optional read-role bindings to the
existing parameter catalog, and extend the existing executed
`AssemblyCandidateBundle` with generic method-native outputs. Keep Assembly
single-method and single-primary-output. This is the selected approach.

### Add a new input-slot and assembly-product framework

Introduce new input IDs and run-config input bindings, a new top-level assembly
product manifest, or a general dynamic method-input DSL. These abstractions
overlap the current manifest, run config, parameter catalog, and candidate
bundle. They are unnecessary for the current single-backbone requirement and
are explicitly deferred.

## Architectural Boundaries

This feature does not create a second data or resource architecture. The
implementation must update the existing owning specifications together with
the code:

- `docs/architecture/data-v2.md` owns durable fact placement, ArtifactRefs,
  checkpoint bounds, ledger projections, publication order, and resume.
- `docs/architecture/resource-handoff.md` owns the `resource_refs` projection
  and resolver behavior.
- `docs/stage_contracts.md` owns the biological Assembly input/output contract.
- `docs/architecture/execution-lifecycle.md` and
  `docs/architecture/execution-evidence.md` continue to own the shared
  freeze/run/collect graph and canonical execution evidence. Execution
  Lifecycle gains only the external-input verification described below; this
  feature does not create a stage-local runner path.

The feature specification may describe those interactions but does not replace
their ownership.

## Durable Fact Ownership

| Fact | Authoritative owner | Checkpoint or ledger projection |
|---|---|---|
| User-declared input paths and metadata | frozen control manifest | control ref only |
| Per-file asset identity, platform, format, declaration provenance, external byte size, and SHA-256 | immutable asset-registry artifact | registry ArtifactRef, counts, IDs, platform set |
| Derived read file and its source asset | immutable read-set manifest | manifest ArtifactRef, member count, platform set |
| Run-owned artifact location, hash, size, and integrity | artifact index | `ArtifactRef` |
| Selected Assembly method and current parameter attempt | typed Assembly cursor and `ParameterAttempt` | bounded IDs/status/refs |
| Primary and method-native Assembly outputs | immutable executed candidate bundle plus artifact index | candidate-bundle ArtifactRef and primary resource path |
| Registration/materialization ordering | ledger | bounded counts, IDs, and ArtifactRefs |
| Current cross-stage read or assembly location | `resource_refs` | paths plus bounded provenance refs |

The ledger must not contain the asset table, read-set members, or Assembly
output table. Reports remain regenerable projections and cannot control
resolution or resume.

## Input Asset Model

### Asset types and platforms

`raw_long_reads` and `clean_long_reads` remain lifecycle-oriented asset types.
Platform is an independent dimension. The implementation must not add
`pacbio_hifi` or `ont` as asset types to solve this problem.

The accepted long-read platforms remain the manifest platform vocabulary,
including `pacbio_hifi`, `ont`, `clr`, `cycloneseq`, and `unknown`.

### Typed registry document

The inventory artifact is validated as one typed registry document containing
the session ID and an ordered list of `AssetRecord` values. Every file path
produces one record. The manifest structure remains
`InputManifest.assets: list[InputAssetSpec]`; it does not gain a second input-ID
namespace. Related chunks retain their manifest entry and path ordinals, while
explicitly paired Hi-C files retain typed pair metadata.

The registry covers frozen long-read declarations from both the input manifest
and supported run-config overrides. In this slice those overrides are
`assembly.hifi_reads_ref` and the entries in `t2t.read_inputs`. Their
declaration source and field name are retained in provenance; the former
supplies `platform=pacbio_hifi`, while every T2T entry declares one of
`cycloneseq`, `ont`, `clr`, or `pacbio_hifi`. A path already declared by the
manifest reuses the same asset identity and must have the exact same platform
metadata. Conflicting duplicate declarations fail frozen-input validation.

Every record retains:

- opaque stable `asset_id`;
- asset type and canonical external path;
- sample, platform, format, compression, and layout;
- external byte size, SHA-256, and verification timestamp;
- manifest entry and path ordinals;
- declared path, declaration source, and manifest/run-config field;
- platform-resolution source.

Within one frozen session, `asset_id` is deterministically derived from the
session identity and canonical declared file path. This makes replay stable and
prevents equal basenames in different directories from colliding. Declaring
the same canonical path more than once in one declaration source is rejected
rather than assigned two roles silently. A compatible manifest/run-config
declaration of the same path is the explicit cross-source exception: it reuses
the manifest-owned identity and records both declaration fields in provenance.

Declared read files remain verified external dependencies rather than
ArtifactRefs, because their bytes are not copied below the run root. Inventory
hashes every declared read file and records its size and SHA-256 before
publishing the registry. The registry is the immutable external-input
dependency manifest for those assets. A registered read asset cannot remain in
`pending` or `skipped` checksum state.

The run declares `verified_external_dependencies` as its execution-replay
capability once the registry is committed. The frozen attempt for a method that
directly consumes an external read records the selected asset IDs, sizes, and
digests. The shared run boundary verifies those dependencies immediately before
Runner invocation, including after resume; a mismatch fails without starting
the tool. Run-owned filtered read artifacts use their ArtifactRefs instead.

For `hic_reads_registered`, `paired_reads` remains the frozen manifest's source
of truth. Inventory creates one file-level `AssetRecord` per mate and adds
`pair_index` and `mate: R1 | R2` to its existing metadata projection. The
flattened `paths` list remains a bounded path projection in the frozen
manifest. Assembly and AutoHiC reconstruct lane lists from pair metadata, never
from file names or even/odd list positions. Duplicate mates, incomplete pairs,
or a repeated file in two pairs fail before registry publication.

### Inference report

Manifest validation returns a typed ordered inference list, not a mapping keyed
by `asset_type`. Each inference item binds one manifest entry ordinal to its
ordered path facts. Inventory verifies that the manifest entry, path, and
inference ordinals still match before building records.

Per-file format, compression, size, and existence facts are not taken from the
first file and copied to every member. Entry-level layout and platform may be
projected to a member only when the inference record names that source.

### Platform precedence

Platform resolution is:

1. explicit per-entry manifest platform;
2. a global shared platform only when the manifest contains exactly one
   long-read input group and that group has an unknown platform;
3. bounded filename/metadata inference;
4. `unknown`.

The global shared platform never mutates all `raw_long_reads` entries. When
several long-read groups exist, each group must have an explicit or
independently inferred platform. If any group remains unknown, frozen-input
validation fails with `mixed_long_read_platform_unresolved` and instructs the
operator to create a new run with explicit per-entry platforms. A frozen
manifest is never amended by an inventory review decision, and inventory must
not select the first platform.

## Existing TUI Draft and Freeze Extension

`TuiSessionDraft` remains the sole mutable pre-freeze authority.
`manifest_draft.yaml` remains its rebuildable projection, and the planner
continues to build the existing `RunConfig`. Natural-language parsing updates
typed draft facts; it never edits frozen YAML or JSON directly.

`AssetCollectionState` gains `hifi_reads: HifiReadsFact | None` beside the
existing `ont_reads` and `hic_reads` facts. Both long-read facts contain a
non-empty ordered `paths` list, so several sequencing chunks remain one
confirmed platform fact. The role vocabulary, deterministic parser, optional
LLM parser, replacement confirmation, answer history, rerun reuse, rendering,
and manifest materializer all gain the HiFi role. Existing
`HiCPairedReadsFact.pairs` continues to represent multiple lanes explicitly.

`WorkflowIntent` and the existing planner state gain only the typed intent
needed to project current run-config fields:

- optional requested Assembly method;
- explicit request to use ONT ultra-long reads in the HiFi Hifiasm method;
- explicit request to use Hi-C phasing in the HiFi Hifiasm method.

The parsers set those booleans only from explicit user language or a subsequent
planner answer. Merely declaring ONT or Hi-C files does not silently enable a
different Hifiasm mode. When Hifiasm and matching inputs are present, the
planner may recommend the options, but the existing final manifest/run-config
preview and confirmation remain the authorization boundary.

`materialize_manifest_draft()` emits one existing `InputAssetSpec` for the
confirmed HiFi paths, one for the confirmed ONT paths, and the existing
paired-read spec for Hi-C. Both long-read specs keep
`asset_type=raw_long_reads` and differ by `platform`. No new manifest document,
input-binding table, or user-visible input ID is introduced.

`HifiasmRunParameters` gains the executor-owned booleans
`use_ultralong_ont` and `use_hic_phasing`, both defaulting to false. The
run config does not duplicate manifest paths. Existing explicit path overrides
retain their current precedence and must resolve to verified registered
members. The freeze boundary cross-validates the generated run config against
the frozen manifest before publishing the existing atomic manifest, run-config,
dependency-snapshot, and commit-marker transaction.

`use_hic_phasing` consumes only complete pairs from the frozen
`InputManifest.paired_reads` declarations. The existing singular
`assembly.hic_reads_ref` is not pair authority: it cannot construct an R1/R2
pair or enable phasing by itself. If supplied under existing override
semantics, it must resolve inside exactly one registered paired-read
declaration and selects that declaration's complete pair set; the pair
declaration remains authoritative.

## Data v2 Registry Reference

Inventory publishes the registry under its existing run-local artifact
directory and stores its complete `ArtifactRef` in `asset_registry_ref`.
Consumers resolve and verify that ref through the storage layer before parsing
the typed registry. Direct `Path(state["asset_registry_ref"])` reads are
removed.

Data v2 has no string-path fallback for the old registry cursor. Current test
fixtures and current-session writers are updated to the typed ref in the same
change.

One shared registry loader owns:

- ArtifactRef validation and run-relative resolution;
- index membership and checksum verification;
- registry schema validation;
- selection by asset type, platform, asset ID, and canonical path;
- deterministic registry order.

Consumers may receive runtime `AssetRecord` objects, but checkpoint state and
`resource_refs` must not embed registry records.

## Resource Handoff

### Inventory-produced long reads

For inventory-owned long reads, `resource_refs["long_reads"]` continues to
carry the materialized external paths required by the current ResourceRef
contract. Its metadata is bounded to:

- the asset-registry ArtifactRef;
- ordered asset IDs corresponding to the paths;
- member count;
- deduplicated asset types, platforms, and formats.

The current `metadata["assets"]` copy is removed. Resolution verifies the
registry ref and reconstructs per-path runtime members from the ordered IDs.
A missing ID, duplicate ID, path mismatch, or platform mismatch fails closed.
For external assets it also verifies the current size against the registry;
the shared execution boundary performs the full SHA-256 verification before a
tool consumes the bytes.

### Inventory-produced Hi-C reads

`resource_refs["hic_reads"].paths` carries the materialized Hi-C paths required
by the same ResourceRef contract. Its metadata is bounded to:

- the asset-registry ArtifactRef;
- ordered asset IDs corresponding one-to-one with `paths`;
- pair count;
- deduplicated asset types, platforms, and formats.

It does not embed asset records or a second pair table. A shared verified
paired-read resolver loads the referenced registry records by ordered ID,
checks every ResourceRef path against its record, and returns runtime-only
ordered pairs containing manifest entry ordinal, `pair_index`, R1, and R2
members. It requires exactly one R1 and one R2 for each unique
`(manifest_entry_ordinal, pair_index)` key and rejects within-entry gaps,
duplicates, cross-entry pairs, path/ID mismatches, and unpaired members. Pair
ordering is manifest entry ordinal followed by `pair_index`, never ResourceRef
path position or file names.

Both HiFi Hifiasm phasing and Hi-C scaffolding consume this resolver. The
current `metadata["assets"]` shortcut and the Hi-C scaffolding fallback that
constructs assets from flattened paths are removed. This keeps
`resource_refs` as the only cross-stage file table while the registry remains
the authority for pair semantics.

When absent, `assembly.hic_reads_ref` leaves all registered pair groups
selected. When present, it is only a group-selection anchor: the path must
resolve to one member of exactly one frozen `InputAssetSpec.paired_reads`
declaration, and the resolver selects that declaration's complete pair set.
It never selects one mate independently, constructs a pair, or enables
`use_hic_phasing`.

### Read-QC-produced long reads

When read-QC does not transform the input bytes, it carries forward the same
registry-backed provenance metadata rather than replacing it with one platform.

When read-QC publishes filtered reads, it also publishes one immutable
`read_qc.read_set_manifest`. Each ordered member contains:

- output read ArtifactRef;
- source asset ID;
- source registry ArtifactRef;
- platform and format as bounded immutable projections;
- transformation method and attempt ID.

The manifest is the authoritative mapping from each derived read artifact to
its input asset. Artifact-index dependency metadata links the manifest to the
registry and output artifacts for reachability.

The read-QC ledger records the manifest and output refs plus only the member
count and platform set. Publication follows the Data v2 order: write immutable
outputs and manifest, register index rows, append the semantic event, then
return the compact state delta.

`resource_refs["long_reads"]` then contains the derived output paths and
bounded metadata with the read-set-manifest ArtifactRef, member count, source
asset IDs, and platform set. It does not copy manifest members.

### Runtime read resolution

A shared long-read resolver converts either registry-backed or
read-set-manifest-backed resource metadata into ordered runtime members:

- logical member ID;
- materialized path;
- platform and format;
- source asset ID;
- source artifact ref when the bytes are run-owned.

The runtime member list is not checkpointed. Stage freeze callbacks load it
again from verified immutable refs and bind the exact selected paths and
identities into the existing frozen attempt and parameter evidence.

The resolver retains the normative Resource Handoff precedence:

1. explicit run override;
2. current `resource_refs["long_reads"]`;
3. registry fallback.

An override changes selection, not provenance ownership. Every override path
must resolve to exactly one member in the verified asset registry or current
read-set manifest. Frozen run-config overrides are therefore registered during
inventory as described above. A runtime-only path that is absent from both
authoritative records fails with `unregistered_long_read_override`; the
resolver never synthesizes an asset record or bypasses external-input hash
verification.

## Assembly Input Selection

Assembly resolves the complete long-read member list and partitions it by
platform. It aggregates every matching member across every manifest entry in
stable registry/read-set order. The canonical resolved value for each method
slot is an ordered member list, and no method adapter selects `[0]`. Existing
single-path override fields may explicitly constrain that list, but they do not
replace registry/read-set provenance or implicitly discard sibling files.

The existing Assembly parameter catalog remains the method input-application
boundary. Its closed `ExecutorBindingKey` vocabulary gains:

- `assembly_ultralong_reads`;
- `assembly_hic_r1_reads`;
- `assembly_hic_r2_reads`.

The existing `assembly_input_reads` binding remains the primary-read slot.
This is an in-place extension of the current L0 executor bindings, not a new
general input-slot DSL.

### `assembly.candidate.hifiasm`

The HiFi Hifiasm method has these slots:

| Slot | Selection | Cardinality | CLI application |
|---|---|---:|---|
| primary | all selected `pacbio_hifi` members | one or more | positional, expanded |
| ultra-long | all selected ONT members when `use_ultralong_ont=true` | one or more when enabled | `--ul`, comma-joined |
| Hi-C R1 | every registered R1 ordered by manifest entry then `pair_index` when `use_hic_phasing=true` | one or more when enabled | `--h1`, comma-joined |
| Hi-C R2 | every registered R2 in the same pair-key order when `use_hic_phasing=true` | equal to R1 | `--h2`, comma-joined |

The Hifiasm parameter catalog adds optional L0 `path_list` parameters for the
three new bindings. The existing renderer already supports expanded and
comma-joined lists; no shell concatenation or temporary merged FASTQ is
introduced. A representative rendered command is:

```text
hifiasm -o PREFIX -t 32 \
  --ul ont-ul-1.fq.gz,ont-ul-2.fq.gz \
  --h1 lane1-R1.fq.gz,lane2-R1.fq.gz \
  --h2 lane1-R2.fq.gz,lane2-R2.fq.gz \
  hifi-1.fq.gz hifi-2.fq.gz
```

These applications follow the upstream Hifiasm option contract: positional
reads are the primary input, `--ul` is the ultra-long ONT companion input, and
`--h1`/`--h2` are the paired Hi-C inputs. The separate `--ont` primary mode is
not applied by this method.

The ONT files in this method are supplementary ultra-long inputs. They do not
replace or join the primary HiFi slot.

### `assembly.candidate.hifiasm_ont`

Hifiasm ONT remains a separate method and parameter catalog. It selects every
compatible ONT FASTQ member as its primary positional input and fixes the
`--ont` CLI switch. It does not set `--ul`, and this slice rejects
`use_ultralong_ont` or `use_hic_phasing` when `hifiasm_ont` is selected. In
particular, `--ont` must never be inferred merely because ONT supplementary
reads are present beside a HiFi Hifiasm attempt.

### Flye and NextDenovo

Flye keeps its existing `path_list` binding and passes every member of one
selected read type after the corresponding Flye flag. It must not mix HiFi,
ONT, CLR, or CycloneSEQ members in one primary slot. NextDenovo keeps its
existing behavior of writing every selected primary member to the attempt FOFN.

An explicit route whose required primary subset is empty fails before parameter
recommendation. Enabling an optional Hifiasm slot whose required subset is
empty also fails before freeze. A mixed-platform set is never collapsed to
`platforms[0]`. The parameter input snapshot and frozen attempt bind each slot's
ordered member identities and exact paths; resume loads those immutable records
rather than rerunning platform or mate inference.

## Read-QC Policy for Mixed Platforms

SeqKit statistics may run over all resolved members because it does not merge
or reclassify their bytes. Any length-filter request remains one ordered batch,
but output-to-input correspondence must be one-to-one and is recorded in the
read-set manifest.

The collector validates equal input/output cardinality and preserves member
order. A partial output set, duplicate output, or missing source identity fails
the attempt collection and does not update `resource_refs`.

This design does not introduce platform-specific QC thresholds or separate
per-platform QC graphs.

## Single-Backbone Assembly Product

### Planning and execution

Only the selected primary method enters parameter recommendation. After its
parameter attempt freezes, that method publishes one planned candidate bundle
and enters execution. The currently unused `fallback_method` selection and
cursor fields are removed rather than retained as a dormant second route.
Future method switching after a failed primary attempt requires a separate
design.

Checkpoint cursor schemas reject the removed `fallback_method` and former
plural/path-based candidate-ref fields at their read boundaries.

The existing `ParameterAttemptOwner` remains the sole checkpoint owner for the
selected parameterized method's frozen and execution refs.

### Planned and executed bundle lifecycle

`AssemblyCandidateBundle` remains one existing schema with status-specific
validation; no second product schema is introduced.

A `status="planned"` bundle is an immutable planning artifact for the selected
method. It contains the candidate/method identity, input member IDs grouped by
semantic role, parameter-attempt identity, and expected output roles. It has no
`primary_output`, no `native_outputs`, and is not published in
`resource_refs["assembly"]` or Assembly Review's materialized-candidate list.
The freeze step publishes and indexes it before Runner invocation and records
its ArtifactRef in the attempt evidence.

Collect never edits or replaces the planned artifact. After validating the
runner outputs, it publishes a new immutable `status="executed"` bundle with:

- the same candidate ID, method ID, ordered input-role identities, and
  parameter-attempt identity as the plan;
- the planned-bundle ArtifactRef as its `plan_ref`;
- a required `primary_output`;
- zero or more `native_outputs`.

Status-specific validation rejects output ArtifactRefs in a planned bundle,
rejects an executed bundle without `plan_ref` or `primary_output`, and rejects
any executed identity or input-role drift from its verified plan. The existing
`expected_outputs` planning field is advisory role declaration only; it is not
a file handoff and cannot satisfy executed output validation.

Publication order is: publish and index validated outputs and logs; publish and
index the executed bundle with dependencies on the plan and output refs; append
bounded artifact/attempt ledger events; then update stage cursors and
`resource_refs`. A crash before the final state update is reconciled from the
indexed executed bundle and ledger without mutating or regenerating the plan.
Only the executed bundle ref enters candidate handoff metadata.

### Executed candidate bundle

The executed Assembly candidate bundle is the authoritative product manifest
for one Assembly attempt. It contains:

- candidate and method IDs;
- selected input asset/member IDs grouped by semantic input role;
- the immutable planned-bundle ArtifactRef;
- `primary_output`, containing the canonical assembly ArtifactRef;
- `native_outputs`, containing zero or more generic
  `AssemblyNativeOutput` entries;
- canonical stdout/stderr refs or bounded run-relative projections allowed by
  Execution Evidence;
- parameter-attempt identity;
- status and bounded provenance scalars.

It does not contain absolute internal artifact paths, embedded execution
evidence, command bodies, or a second copy of the parameter recommendation.

`AssemblyNativeOutput` contains:

- a coarse `artifact_class`, such as `assembly_graph`,
  `alternate_contigs`, `haplotype_contigs`, `report`, `config`, `log`, or
  `other`;
- an adapter-owned stable `native_role`;
- a format/media-type value;
- its ArtifactRef.

The shared collector validates and registers these entries but does not know
tool filenames. Each existing method adapter declares the finite set of
deliverables worth retaining. Undeclared intermediates remain under `work/`
and are not preserved automatically. Adding a new assembler or a new native
deliverable changes its adapter declaration, not the candidate-bundle schema.

### Hifiasm output declaration

The Hifiasm adapter must always materialize and publish the canonical primary
FASTA. Missing or empty primary output is a collection failure.

Recognized GFA, alternate-contig, and haplotype outputs are one adapter-specific
implementation of the generic native-output contract. When present and
non-empty, collect publishes and indexes them using this deterministic mapping:

| File relative to the selected output prefix | Artifact class | Native role |
|---|---|---|
| `.bp.p_ctg.gfa` | `assembly_graph` | `hifiasm.primary_graph_bp` |
| `.p_ctg.gfa` | `assembly_graph` | `hifiasm.primary_graph_legacy` |
| `.bp.a_ctg.gfa` | `alternate_contigs` | `hifiasm.alternate_graph_bp` |
| `.a_ctg.gfa` | `alternate_contigs` | `hifiasm.alternate_graph_legacy` |
| `.hic.hap1.p_ctg.gfa` | `haplotype_contigs` | `hifiasm.hic_haplotype_1_graph` |
| `.hic.hap2.p_ctg.gfa` | `haplotype_contigs` | `hifiasm.hic_haplotype_2_graph` |
| `.hap1.p_ctg.gfa` | `haplotype_contigs` | `hifiasm.haplotype_1_graph` |
| `.hap2.p_ctg.gfa` | `haplotype_contigs` | `hifiasm.haplotype_2_graph` |

Every non-empty recognized file is retained under its distinct role. The
canonical primary FASTA is derived from `.bp.p_ctg.gfa` when present and falls
back to `.p_ctg.gfa`; no other native graph is eligible. Missing optional
outputs do not fail a primary assembly.

The Hifiasm ONT adapter applies the same filename/class mapping under its own
output prefix and uses the `hifiasm_ont.*` native-role namespace. Thus `--ont`
mode retains its GFAs without conflating them with standard Hifiasm attempts.

The native-output ArtifactRefs appear only in the executed candidate bundle and
artifact index. They do not enter `resource_refs["assembly"].paths`, Assembly
Review's candidate list, Hi-C scaffolding, or T2T automatically.

### Cross-stage assembly handoff

`resource_refs["assembly"]` contains exactly the canonical primary FASTA path
after Assembly collect. Its bounded metadata contains the executed
candidate-bundle ArtifactRef, selected method ID, and candidate ID.

Assembly Review loads and verifies the bundle and retains its existing human
decision boundary. Assembly emits exactly one assembler-origin candidate.
Assembly Dedup may subsequently preserve the raw candidate and add a purged
derivative candidate exactly as required by the normative stage contract; both
remain eligible for Assembly Review. Method-native products and planned
assembler fallbacks never enter that comparison. Later stages consume only the
assembly selected by review.

## T2T Input Resolution

T2T obtains the complete platform-preserving long-read selection from the
shared runtime resolver. It prefers the current read-set manifest when read-QC
produced derived reads and otherwise uses the registry.

Frozen `t2t.read_inputs` retain explicit-selection precedence. Each configured
source path binds to its registered source asset and, when read-QC transformed
that asset, to the corresponding derived ArtifactRef. If a supplied path was
not also in the input manifest, inventory registers it from the frozen run
config with its explicitly declared platform, size, and SHA-256 provenance. A
missing, conflicting, unknown-platform, or unregistered supplied path fails
closed.

T2T does not use method-native Hifiasm contigs in this slice. Telomeric reads
are grouped by canonical platform into `map-ont`, `map-hifi`, and `map-pb`
inputs, executed inside one frozen minimap2 attempt, and combined into one PAF.

## Checkpoint and Boundedness Rules

- `asset_registry_ref`, `read_set_manifest_ref`, and candidate-bundle refs are
  typed ArtifactRef JSON values.
- Registry assets and read-set members never appear in `stage_outputs`,
  `resource_refs`, `active_interrupt`, or ledger summaries.
- Ordered IDs and platform sets are bounded projections; existing top-level,
  stage-cursor, and resource-table byte limits remain unchanged.
- Full input profiles, output tables, commands, and execution results stay in
  immutable artifacts.
- Stage cursor schemas reject unknown compatibility fields and embedded
  artifact bodies.

The change must include a representative mixed-platform checkpoint-size test,
not only schema unit tests.

## Failure and Resume Semantics

- Duplicate canonical paths within one declaration source, conflicting
  cross-source declarations, generated asset-ID collisions, inference ordinal
  mismatches, and unresolved platforms in a multi-group manifest fail before
  registry publication. A compatible manifest/run-config declaration of the
  same path reuses one asset identity. Correcting a frozen manifest requires a
  new run.
- Missing, unindexed, checksum-invalid, or schema-invalid registry/read-set
  refs fail closed.
- An external read whose current size or SHA-256 differs from its registry
  record fails before Runner invocation, including after resume.
- Read-set member/path cardinality or identity mismatches fail collection
  before the resource table changes.
- Incomplete or duplicated Hi-C pairs, unequal ordered R1/R2 lane counts, and
  pair-index/mate mismatches fail before an Assembly attempt freezes.
- `use_ultralong_ont=true` without registered ONT members and
  `use_hic_phasing=true` without complete registered pairs fail before freeze.
  Both options are invalid for `assembly.candidate.hifiasm_ont`.
- Replaying a completed publication with the same path and hash is idempotent;
  different bytes at the same immutable path fail closed.
- A stale checkpoint after registry or read-set ledger publication is
  reconciled from the ledger and verified refs according to Data v2; inference
  and lineage are not recomputed from current code.
- Hifiasm primary-output failure stops collection. Missing optional native
  outputs does not.
- No failure path converts an unexecuted fallback method into a candidate.

## Compatibility and Rollout

This is a current Data v2 representation change. There is no reader for the old
string `asset_registry_ref`, embedded `resource_refs.metadata.assets`, or old
candidate-bundle output paths. Development fixtures and current-session
writers change atomically.

Implementation should proceed along existing boundaries:

1. TUI HiFi facts, existing manifest projection, and run-config option
   projection;
2. typed registry, ordered inference, stable IDs, Hi-C pair metadata,
   external-input hashes, and verified registry loader;
3. compact inventory ResourceRef projection;
4. read-set manifest publication and shared runtime read resolver;
5. shared pre-run verification of external input dependencies;
6. read-QC provenance-preserving commit;
7. Assembly method-specific multi-file selection and Hifiasm L0 bindings;
8. single-primary candidate publication and generic method-native outputs;
9. T2T platform-specific input selection;
10. owning architecture specifications, tests, and reports.

This ordering is implementation guidance only. Every merged change must keep
the current representation internally consistent; no intermediate dual write
is allowed.

## Testing

### Inventory and manifest

- Several `raw_long_reads` entries and several paths per entry retain distinct
  platform, format, metadata, asset IDs, and deterministic order.
- Two `raw_long_reads` entries, one HiFi and one ONT, cannot overwrite each
  other's inference.
- Equal basenames in different directories produce distinct asset IDs.
- Repeating the same frozen manifest produces the same IDs.
- Repeating the same canonical path within one declaration source is rejected;
  a compatible manifest/run-config alias reuses one identity, while conflicting
  cross-source metadata fails.
- Inference for a later same-type entry cannot change an earlier entry.
- A global shared platform does not overwrite an explicitly mixed manifest.
- Mixed entries with an unresolved platform fail frozen-input validation and
  require a new explicitly classified run.
- Frozen `assembly.hifi_reads_ref` and `t2t.read_inputs` become verified
  registry members; duplicate conflicts and unregistered runtime overrides
  fail closed.
- Registered external reads have ready size/SHA-256 evidence; changed bytes
  fail before execution, including resumed execution.
- The registry cursor is a verified ArtifactRef; corrupt registry bytes fail
  closed.
- Multi-lane `paired_reads` publish one file record per mate with matching
  `pair_index` and opposite `mate` values; incomplete, duplicated, or reordered
  pairs fail closed.
- `resource_refs["hic_reads"]` carries only ordered IDs, the registry ref, and
  bounded summary metadata; it does not embed asset or pair records.
- The paired-read resolver reconstructs complete ordered pairs from verified
  registry records and rejects path/ID mismatch, cross-entry mates, duplicate
  roles, or gaps without falling back to path parity or file names.

### TUI draft and freeze

- Natural language can confirm several HiFi paths, several ONT paths, and
  several explicit Hi-C pairs in one draft.
- `materialize_manifest_draft()` emits separate existing
  `raw_long_reads` entries for HiFi and ONT plus the existing paired Hi-C
  entry.
- Replacing confirmed HiFi uses the same explicit replacement boundary as
  ONT and Hi-C; rerun drafts revalidate and reuse every platform independently.
- Hifiasm input-use booleans are projected into the existing run config only
  from explicit intent/planner confirmation and appear in the final preview.
- Freeze rejects method/input contradictions before publishing any control
  file and preserves the existing idempotent atomic transaction.

### Resource handoff and read-QC

- Inventory ResourceRef metadata does not contain `assets`.
- Resolver reconstructs the correct path/platform relation from ordered asset
  IDs and the registry.
- No-filter read-QC preserves the registry-backed relation.
- Filtering multiple platforms publishes one output per source and a verified
  read-set manifest with correct lineage.
- Partial or reordered/mismatched filter output fails before resource commit.
- Resource and stage cursors stay within existing Data v2 limits.

### Assembly and T2T

- Flye receives every file of exactly one selected read type; its existing
  multi-file CLI expansion remains green.
- NextDenovo writes every selected primary member to its FOFN.
- HiFi Hifiasm receives every HiFi file as positional primary input.
- With `use_ultralong_ont=true`, HiFi Hifiasm receives every selected ONT file
  once through one comma-joined `--ul` option.
- With `use_hic_phasing=true`, HiFi Hifiasm receives every R1 lane through
  `--h1` and the matching ordered R2 lanes through `--h2`.
- HiFi Hifiasm never emits `--ont`.
- `hifiasm_ont` receives every compatible ONT FASTQ as primary input, fixes
  `--ont`, and emits none of `--ul`, `--h1`, or `--h2` in this slice.
- A missing requested primary or enabled supplementary input fails before
  parameter recommendation or attempt freeze.
- A missing requested platform fails before parameter recommendation.
- The approved parameter snapshot and frozen attempt retain ordered member
  identity grouped by input role.
- T2T resolves ONT, PacBio HiFi, CLR, and CycloneSEQ after both no-filter and
  filtered read-QC handoffs.
- Explicit `t2t.read_inputs` preserve order and exact platform provenance while
  selecting verified members with stable source identities.
- Unknown or conflicting platforms fail closed instead of being assigned a
  minimap2 preset.

### Assembly product

- Only the primary selected method produces a candidate bundle.
- Hifiasm collect publishes primary FASTA and every recognized optional output
  that exists.
- Missing optional output succeeds; missing primary output fails.
- A planned bundle is immutable, contains no output ArtifactRefs, and never
  enters the materialized candidate handoff.
- Collect publishes a distinct executed bundle whose `plan_ref`, candidate
  identity, method, inputs, and parameter attempt match the verified plan.
- A crash after executed-bundle indexing but before state update resumes from
  indexed bundle/ledger evidence without rewriting the planned bundle.
- `resource_refs["assembly"].paths` contains exactly one primary FASTA.
- Assembly emits one materialized assembler-origin candidate and no planned
  fallback.
- After Assembly Dedup, Assembly Review may see the raw and purged related
  candidates without seeing method-native Hifiasm products.
- Bundle and ledger payloads contain ArtifactRefs and bounded scalars, not
  absolute internal artifact paths or execution bodies.
- A non-Hifiasm adapter can declare a native output without changing the
  candidate-bundle schema or shared collector.

### Data v2 regressions

- Artifact index and ledger writes are replay-idempotent.
- Storage audit finds no dangling refs from registry, read-set, candidate, or
  method-native artifacts.
- Checkpoint-free retention reaches registry, read-set, selected outputs, and
  method-native outputs through indexed dependencies and ledger roots.
- Corrupting any authoritative referenced artifact fails replay/resume closed.
- Existing state, checkpoint compaction, execution lifecycle, and storage-audit
  suites remain green.

## Acceptance Criteria

A TUI draft or frozen manifest may declare several HiFi files, several ONT
files, and several explicit Hi-C pairs using the existing draft, manifest, and
run-config structures. After inventory and optional read-QC, every current
long-read and Hi-C path can be traced to its stable source asset, platform, and
pair role through verified immutable artifacts. A HiFi Hifiasm run consumes all
primary HiFi files and, only when explicitly enabled, passes all ONT
ultra-long files through `--ul` and all paired lanes through `--h1`/`--h2`.
The separate Hifiasm ONT method consumes ONT as primary input under `--ont`.
Both publish one canonical primary FASTA and retain adapter-declared
method-native outputs without turning them into peer candidates. A later T2T
run can resolve all eligible long-read members across supported platforms. The
checkpoint, resource table,
ledger, artifact index, crash ordering, external-dependency verification, and
resume behavior satisfy the existing Data v2 contracts without legacy fallback
or dual write. A later purge-dups step may add its raw/purged derived candidate
relation without reintroducing assembler fallbacks or native graphs as peer
candidates.
