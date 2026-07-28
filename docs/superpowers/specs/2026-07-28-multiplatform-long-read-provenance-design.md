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
and optional auxiliary Hifiasm outputs.

## Goals

- Preserve a stable identity and platform for every declared long-read file.
- Keep HiFi and ONT distinct through inventory, read-QC, resource handoff,
  Assembly input selection, and later T2T input resolution.
- Retain Data v2 ownership, immutable-artifact, ArtifactRef, ledger, checkpoint,
  crash, and resume invariants.
- Publish one canonical Assembly FASTA for downstream stages.
- Retain recognized Hifiasm graph, alternate-contig, and haplotype outputs as
  auxiliary artifacts without exposing them as peer assemblies.
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
- No use of ONT as Hifiasm `--ul` input in this slice.
- No Hifiasm `--h1`/`--h2` input binding or haplotype-selection workflow.
- No new HiFi collection flow in the TUI. Existing manifest-based intake is
  the supported mixed-platform entry path.
- No automatic use of auxiliary Hifiasm contigs by Hi-C scaffolding or T2T.
- No compatibility reader, migration, or dual write for earlier development
  sessions or checkpoints.

## Considered Approaches

### Patch only the manifest inference mapping

Key inference by path or manifest index and leave later stages unchanged.
This fixes the immediate overwrite but loses the relation again when read-QC
projects several inputs into one platform. It is rejected because it cannot
meet the end-to-end provenance goal.

### Add a bounded provenance spine

Make the asset registry authoritative for declared inputs, add an immutable
read-set manifest for derived read files, and make resource resolution load
those records into runtime-only typed members. Keep Assembly single-method and
single-primary-output. This is the selected approach.

### Build a general assembly-product and multi-attempt framework

Introduce a collection of independently executable assembly attempts, generic
candidate selection, integration, and recovery. This would address a future
dual-assembler workflow but is unnecessary for the current single-backbone
requirement and is explicitly deferred.

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
| Primary and auxiliary Assembly outputs | immutable executed candidate bundle plus artifact index | candidate-bundle ArtifactRef and primary resource path |
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
produces one record. A multi-path manifest entry also receives a stable
`input_group_id` so related chunks or explicitly paired inputs retain their
group relation.

The registry covers frozen long-read declarations from both the input manifest
and supported run-config overrides. In this slice those overrides are
`assembly.hifi_reads_ref` and `t2t.ont_read_refs`. Their declaration source and
field name are retained in provenance; the former supplies
`platform=pacbio_hifi` and the latter supplies `platform=ont`. A path already
declared by the manifest reuses the same asset identity and must have compatible
platform metadata. Conflicting duplicate declarations fail frozen-input
validation.

Every record retains:

- opaque stable `asset_id`;
- `input_group_id`;
- asset type and canonical external path;
- sample, platform, format, compression, and layout;
- external byte size, SHA-256, and verification timestamp;
- manifest entry and path ordinals;
- declared path, declaration source, and manifest/run-config field;
- platform-resolution source.

Within one frozen session, `asset_id` is deterministically derived from the
session identity and canonical declared file path. This makes replay stable and
prevents equal basenames in different directories from colliding. Declaring
the same canonical path more than once is rejected rather than assigned two
roles silently.

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
platform. The selected route determines the primary subset:

- Hifiasm uses every `pacbio_hifi` member in resource order.
- Hifiasm ONT uses every ONT member supported by its existing format contract.
- Flye and NextDenovo keep their current read-type compatibility rules but
  receive only members matching the selected read type.

ONT members are not consumed by the HiFi Hifiasm attempt in this slice. They
remain reachable through the current long-read resource and provenance
artifacts for later T2T use.

An explicit route whose required platform subset is empty fails before
parameter recommendation. A mixed-platform set is never collapsed to
`platforms[0]`. The parameter input snapshot and frozen attempt record the
selected member identities and exact paths; resume loads those immutable
records rather than rerunning platform inference.

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

Only the selected primary method produces a planned candidate bundle and enters
parameter recommendation/execution. The currently unused `fallback_method`
selection and cursor fields are removed rather than retained as a dormant
second route. Future method switching after a failed primary attempt requires a
separate design.

The existing `ParameterAttemptOwner` remains the sole checkpoint owner for the
selected parameterized method's frozen and execution refs.

### Executed candidate bundle

The executed Assembly candidate bundle is the authoritative product manifest
for one Assembly attempt. It contains:

- candidate and method IDs;
- selected input asset/member IDs;
- primary assembly ArtifactRef;
- optional auxiliary output entries with a role and ArtifactRef;
- canonical stdout/stderr refs or bounded run-relative projections allowed by
  Execution Evidence;
- parameter-attempt identity;
- status and bounded provenance scalars.

It does not contain absolute internal artifact paths, embedded execution
evidence, command bodies, or a second copy of the parameter recommendation.

### Hifiasm collection

The Hifiasm adapter must always materialize and publish the canonical primary
FASTA. Missing or empty primary output is a collection failure.

Recognized GFA, alternate-contig, and haplotype outputs are optional. When
present and non-empty, collect publishes and indexes them with explicit roles
using this deterministic filename-to-role mapping:

| File relative to the selected output prefix | Auxiliary role |
|---|---|
| `.bp.p_ctg.gfa` | `primary_graph_bp` |
| `.p_ctg.gfa` | `primary_graph_legacy` |
| `.bp.a_ctg.gfa` | `alternate_graph_bp` |
| `.a_ctg.gfa` | `alternate_graph_legacy` |
| `.hic.hap1.p_ctg.gfa` | `hic_haplotype_1_graph` |
| `.hic.hap2.p_ctg.gfa` | `hic_haplotype_2_graph` |
| `.hap1.p_ctg.gfa` | `haplotype_1_graph` |
| `.hap2.p_ctg.gfa` | `haplotype_2_graph` |

Every non-empty recognized file is retained under its distinct role. The
canonical primary FASTA is derived from `.bp.p_ctg.gfa` when present and falls
back to `.p_ctg.gfa`; no other auxiliary graph is eligible. Missing optional
outputs do not fail a primary assembly.

The auxiliary ArtifactRefs appear only in the executed candidate bundle and
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
remain eligible for Assembly Review. Auxiliary Hifiasm products and planned
assembler fallbacks never enter that comparison. Later stages consume only the
assembly selected by review.

## T2T Input Resolution

T2T obtains ONT reads from the shared runtime long-read resolver with an
explicit `platform="ont"` predicate. It prefers the current read-set manifest
when read-QC produced derived reads and otherwise uses the registry.

Frozen `t2t.ont_read_refs` retain their existing explicit-override precedence.
They select those exact ordered paths from the verified registry or read-set
manifest. If a supplied path was not also in the input manifest, inventory has
already registered it from the frozen run config with ONT platform, size, and
SHA-256 provenance. A missing, conflicting, or unregistered supplied path
fails closed.

T2T does not use auxiliary Hifiasm contigs in this slice. Missing ONT members
continues to follow the stage's existing bypass or failure policy; it must not
substitute HiFi because both share `raw_long_reads`.

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

- Duplicate canonical input paths, generated asset-ID collisions, inference
  ordinal mismatches, and unresolved platforms in a multi-group manifest fail
  before registry publication. Correcting a frozen manifest requires a new
  run.
- Missing, unindexed, checksum-invalid, or schema-invalid registry/read-set
  refs fail closed.
- An external read whose current size or SHA-256 differs from its registry
  record fails before Runner invocation, including after resume.
- Read-set member/path cardinality or identity mismatches fail collection
  before the resource table changes.
- Replaying a completed publication with the same path and hash is idempotent;
  different bytes at the same immutable path fail closed.
- A stale checkpoint after registry or read-set ledger publication is
  reconciled from the ledger and verified refs according to Data v2; inference
  and lineage are not recomputed from current code.
- Hifiasm primary-output failure stops collection. Missing optional auxiliary
  outputs does not.
- No failure path converts an unexecuted fallback method into a candidate.

## Compatibility and Rollout

This is a current Data v2 representation change. There is no reader for the old
string `asset_registry_ref`, embedded `resource_refs.metadata.assets`, or old
candidate-bundle output paths. Development fixtures and current-session
writers change atomically.

Implementation should proceed along existing boundaries:

1. typed registry, inference, stable IDs, external-input hashes, and verified
   registry loader;
2. compact inventory ResourceRef projection;
3. read-set manifest publication and shared runtime read resolver;
4. shared pre-run verification of external input dependencies;
5. read-QC provenance-preserving commit;
6. Assembly and T2T platform-specific input selection;
7. single-primary candidate publication and Hifiasm auxiliary artifacts;
8. owning architecture specifications, tests, and reports.

This ordering is implementation guidance only. Every merged change must keep
the current representation internally consistent; no intermediate dual write
is allowed.

## Testing

### Inventory and manifest

- Two `raw_long_reads` entries, one HiFi and one ONT, retain distinct platform,
  format, metadata, and asset IDs.
- Equal basenames in different directories produce distinct asset IDs.
- Repeating the same frozen manifest produces the same IDs.
- Declaring the same canonical path twice is rejected.
- Inference for a later same-type entry cannot change an earlier entry.
- A global shared platform does not overwrite an explicitly mixed manifest.
- Mixed entries with an unresolved platform fail frozen-input validation and
  require a new explicitly classified run.
- Frozen `assembly.hifi_reads_ref` and `t2t.ont_read_refs` become verified
  registry members; duplicate conflicts and unregistered runtime overrides
  fail closed.
- Registered external reads have ready size/SHA-256 evidence; changed bytes
  fail before execution, including resumed execution.
- The registry cursor is a verified ArtifactRef; corrupt registry bytes fail
  closed.

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

- A HiFi route selects all HiFi members and no ONT members.
- An ONT route selects all compatible ONT members and no HiFi members.
- A missing requested platform fails before parameter recommendation.
- The approved parameter snapshot and frozen attempt retain selected member
  identity.
- T2T resolves ONT after both no-filter and filtered read-QC handoffs.
- Explicit `t2t.ont_read_refs` preserve precedence while selecting verified
  members with stable source identities.
- T2T never substitutes HiFi for absent ONT.

### Assembly product

- Only the primary selected method produces a candidate bundle.
- Hifiasm collect publishes primary FASTA and every recognized optional output
  that exists.
- Missing optional output succeeds; missing primary output fails.
- `resource_refs["assembly"].paths` contains exactly one primary FASTA.
- Assembly emits one materialized assembler-origin candidate and no planned
  fallback.
- After Assembly Dedup, Assembly Review may see the raw and purged related
  candidates without seeing auxiliary Hifiasm products.
- Bundle and ledger payloads contain ArtifactRefs and bounded scalars, not
  absolute internal artifact paths or execution bodies.

### Data v2 regressions

- Artifact index and ledger writes are replay-idempotent.
- Storage audit finds no dangling refs from registry, read-set, candidate, or
  auxiliary artifacts.
- Checkpoint-free retention reaches registry, read-set, selected outputs, and
  auxiliary outputs through indexed dependencies and ledger roots.
- Corrupting any authoritative referenced artifact fails replay/resume closed.
- Existing state, checkpoint compaction, execution lifecycle, and storage-audit
  suites remain green.

## Acceptance Criteria

A frozen manifest may declare HiFi and ONT as separate `raw_long_reads`
entries. After inventory and optional read-QC, every current long-read path can
be traced to its stable source asset and platform through verified immutable
artifacts. A HiFi Assembly run consumes only HiFi, publishes one canonical
primary FASTA, and retains any recognized auxiliary Hifiasm outputs without
turning them into peer candidates. A later T2T run can still resolve only the
ONT members. The checkpoint, resource table, ledger, artifact index, crash
ordering, external-dependency verification, and resume behavior satisfy the
existing Data v2 contracts without legacy fallback or dual write. A later
purge-dups step may add its raw/purged derived candidate relation without
reintroducing assembler fallbacks or auxiliary graphs as peer candidates.
