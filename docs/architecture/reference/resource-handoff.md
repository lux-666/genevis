# Resource Handoff Contract

> **Status:** accepted; remaining cross-cutting gaps are listed in
> [Architecture conformance](conformance.md).
> **Read when:** a stage publishes or consumes a materialized biological file.  
> **Comes from:** [Data and storage](../topics/data-and-storage.md) and stage
> commit boundaries.  
> **Feeds into:** downstream stage validation and method freezing.  
> **Related contracts:** [Stage contracts](stage-contracts.md),
> [Run data](data-contract.md).  
> **Code evidence:** `app/stages/resource_specs.py`,
> `app/stages/resource_resolution.py`, `app/schemas/resource_ref.py`,
> `app/control/occurrence_settlement.py`.

This specification owns cross-stage file handoff. Biological inputs, outputs,
and review triggers remain in [`stage-contracts.md`](stage-contracts.md);
this document defines how materialized resources move between stages.

## State ownership

`TopLevelState.resource_refs` is the only cross-stage file handoff table.
`stage_outputs` contains stage-private, checkpointable cursors and may hold
status, metrics, evidence refs, and recovery metadata. A downstream stage must
not scan another stage's output or guess a key name to locate an input file.
It is a current-occurrence scratch projection, not a history table: when a
same-named stage occurrence is entered again, its slot is replaced from the
frozen stage seed. Prior occurrence history remains in step-scoped artifacts,
review records, execution evidence, and ledger events.

A persisted `ResourceRef` has this shape:

```json
{
  "resource_type": "assembly",
  "paths": ["/run/artifacts/steps/step-004-assembly/assembly/draft.fasta"],
  "source_stage": "assembly",
  "workflow_step_id": "step-004-assembly",
  "metadata": {
    "candidate_bundle_ref": {
      "schema_version": 2,
      "artifact_id": "art_...",
      "path_ref": "artifacts/steps/step-004-assembly/assembly/attempts/000/candidates/hifiasm_candidate.executed.json",
      "sha256": "...",
      "size_bytes": 1234,
      "artifact_type": "draft_assembly.candidate"
    },
    "selected_method": "assembly.candidate.hifiasm",
    "candidate_id": "session:assembly.candidate.hifiasm:parameter_attempt_0"
  }
}
```

Current constraints:

- the table key and `resource_type` must match;
- `paths` contains at least one non-empty string;
- `source_stage is None` if and only if `workflow_step_id is None`;
- a present `source_stage` and `workflow_step_id` are each non-empty strings;
- ordinary manifest/external resources leave both provenance fields absent,
  while a stage-produced resource records its canonical producing stage and
  exact workflow occurrence. A registered assembly is deliberately upgraded by
  Inventory into an executed candidate, so its central `assembly` handoff has
  `source_stage="inventory"` and
  `workflow_step_id="step-000-inventory"`;
- the complete table is JSON-only and at most 128 KiB;
- reducers validate and replace the complete table;
- stage nodes return a delta and do not mutate input state;
- large evidence, logs, commands, and metric tables live in artifacts or the
  ledger, with only compact refs in metadata.

Scalar observations such as genome size are parameter or measurement evidence,
not path resources.

## Resolution

All stages resolve inputs through
`app.stages.resource_resolution.resolve_stage_resource` in this order:

1. `state.resource_refs[logical_resource]`;
2. the asset registry, using the `ResourceSpec` asset types and priorities;
3. `found=False`.

There is no separate explicit run-override layer in the resolver. A stage may
still consume a run-config field at its own adapter boundary, but that is not a
fourth cross-stage resource source.

The resolver does not scan `stage_outputs`. The calling stage owns the decision
to fail, skip, or use an explicitly allowed fallback when a resource is absent;
the resolver itself remains a non-throwing query for absence. It does not own
environment/LLM readiness, method selection, or tool preflight and must not
re-read a live `envs.yaml` or infer method identity from a resource path. Those
checks belong to the
[execution lifecycle](execution-lifecycle.md#whole-run-readiness-boundary) and
the stage adapter that freezes the selected attempt.

When inventory contains clean and raw assets for one logical resource,
`ResourceSpec.asset_type_priority_groups` determines the preferred type. Once a
later stage publishes a central resource ref, resolution must not silently fall
back to an older inventory asset.

Stages publish with `update_resource_ref(...)`, which returns a new validated
table and never mutates the supplied state.

## Logical resources

| Stage | Requires | Produces or updates |
|---|---|---|
| `inventory` | — | `long_reads`, `short_reads`, `hic_reads`, `assembly`, according to the manifest |
| `read_qc` | `long_reads` | `long_reads` |
| `genome_survey` | for a full survey, at least one of `short_reads` or `long_reads`, preferring the `short_reads` family; each family's clean/raw priority is resolved before the stage boundary; an explicit genome size permits the no-read skip path | scalar measurement evidence only |
| `assembly` | `long_reads` | `assembly` |
| `assembly_dedup` | `assembly`, `long_reads` | `assembly` |
| `assembly_review` | `assembly` | `assembly` |
| `hic_scaffolding` | `assembly`, `hic_reads` | adopted chromosome-scale `assembly` |
| `gapcloser` | none: registered placeholder with no executable input contract | none |
| `t2t` | current adopted `assembly`, `long_reads` | `assembly` |
| `base_polish` | none: registered placeholder with no executable input contract | none |

Runtime stage adapters enforce these direct dependencies. Genome Survey
already admits a short-read-only run and prefers short DNA reads when both
families are present. `resource_specs.py` owns only canonical resource
resolution; stages do not share a second declarative planning table.

Hi-C reads stop at `hic_scaffolding`. After human adoption, that occurrence
replaces the central `assembly` resource. The current `gapcloser` placeholder
neither reads nor updates it; if a GapCloser occurrence is reached it stops
there. Any later T2T occurrence resolves the then-current assembly and the
long-read set; it does not consume `hic_reads` or a contact map directly. The
current `base_polish` placeholder likewise publishes no resource.

Candidate bundles, review decisions, execution evidence, reports, and metrics
remain artifacts. They may be referenced from resource metadata but do not form
a second cross-stage file table.

## Immutable assembly delivery

`resource_refs["assembly"]` is a replaceable current-routing projection. Its
`source_stage` and `workflow_step_id` labels are necessary for dispatch, but
they are not historical proof that the named occurrence delivered the
candidate relation. Assembly delivery history is owned by one immutable,
occurrence-bound `resource_delivered` Ledger fact written by the shared
settlement boundary before that occurrence's `stage_completed` terminal.

The current v1 delivery schema is deliberately assembly-specific:

```json
{
  "event_type": "resource_delivered",
  "stage": "assembly_dedup",
  "workflow_step_id": "step-004-assembly_dedup",
  "subject_id": "step-004-assembly_dedup:assembly",
  "summary": {
    "resource_type": "assembly",
    "delivery_mode": "reused"
  },
  "refs": [
    {"role": "candidate_bundle_ref", "artifact_id": "..."},
    {"role": "primary_output_ref", "artifact_id": "..."}
  ]
}
```

`delivery_mode="materialized"` means the occurrence created a new executed
candidate relation. `delivery_mode="reused"` means it deliberately delivered
an older verified candidate unchanged. Reuse is not absence of a delivery:
Dedup pass-through, retained Hi-C/T2T results, and an unchanged Review decision
each own a new delivery fact that points to the older candidate and primary
output. The physical producer recorded in those artifact-index rows may
therefore precede the delivery occurrence.

One completed occurrence may deliver `assembly` at most once. Exact replay of
the same event is idempotent; changing its stage/step binding, delivery mode,
candidate ref, or primary ref is a Ledger conflict. A completed occurrence that
claims an assembly handoff must have both this fact and a current
`ResourceRef` whose paths and metadata project the same primary output and
candidate. A delivery event without the matching completed occurrence is an
incomplete crash prefix and is not consumable.

Assembly Review validates all of the following before candidate loading:

1. its frozen `review_target.producer_step_id` names the expected earlier
   producer occurrence;
2. that occurrence has the canonical `stage_completed` terminal;
3. exactly one matching `resource_delivered` fact exists for `assembly`;
4. the current `ResourceRef` stage/step labels, candidate ref, primary ref, and
   path equal that immutable delivery; and
5. the executed candidate bundle and its embedded primary output verify.

Relabeling a central `ResourceRef` can no longer make an unrelated historical
candidate satisfy Review. There is no pre-release fallback that infers a
delivery fact from a mutable checkpoint or upgrades an old session.

The operator-facing `final_assembly` field is a detached read model of these
deliveries, not another resource record. Its terminal bounding and artifact
verification are owned by the
[run-data contract](data-contract.md#13-ledger-v2).

## Current assembly candidate lineage

Every central `assembly` resource that represents an executed candidate,
including an Inventory-wrapped registered assembly, carries a current executed
candidate bundle and its exact primary-output ref. When Assembly Dedup, an
accepted Hi-C adoption, an accepted T2T patch, or an Assembly Review corrective
action changes the assembly path, it publishes a new executed candidate in that
active occurrence. The previous current candidate and primary output remain as
`input_candidate_bundle_ref` and `input_primary_output_ref` lineage metadata.

The generic candidate wrapper accepts exactly these primary artifact types:
`assembly.draft_assembly`, `hic_scaffolding.chromosome_scaffolded_assembly`,
and `t2t.patched_assembly`. The wrapper itself remains
`draft_assembly.candidate`; no derived FASTA is copied under a draft-only type.
Assembly Review consumes the same current candidate contract after each of
those producers and has no Hi-C- or T2T-specific fallback path. An unchanged
Hi-C/T2T decision preserves the current paths and candidate relation while
republishing the central ResourceRef under the active producer occurrence, so
a later Review can verify the exact delivery handoff.

## Read QC handoff lineage

The accepted Read QC lineage never creates a second read-resource protocol.
An unfiltered completion republishes or retains `long_reads` with the exact ordered asset IDs,
asset-registry ref, member platforms/formats, and paths that were measured. A
successful length filter publishes one immutable read-set manifest that maps
each derived member to its source asset ID, source input ref, filter method, and
approved parameters. The central `long_reads` `ResourceRef` then contains the
ordered filtered paths plus that manifest ref and source-member identities.

Once Read QC publishes a central `long_reads` ref, downstream resolution must
not fall back to an older raw asset set. A filter that retains zero reads still
publishes its method evidence and zero-valued aggregate statistics, but it does
not replace the central resource with empty files; the Read QC occurrence fails
its delivery contract with `no_reads_retained`.

FOFNs, per-file statistics, and summary reports are artifacts or adapter-local
files. They are not peer handoff mechanisms. Exact measurement provenance is
owned by the Read QC section of
[`stage-contracts.md`](stage-contracts.md).

## Sequencing-member identity

Inventory registers every declared file independently. Two
`raw_long_reads` entries, or two files inside one entry, therefore retain
distinct opaque asset IDs even when they have the same basename. Platform and
format are per-member registry facts; `asset_type` is not an identity key.

Inventory-produced `long_reads` and `hic_reads` metadata contains the typed
asset-registry `ArtifactRef`, ordered asset IDs corresponding one-to-one with
`paths`, member/pair counts, and deduplicated platform/format sets. It never
embeds asset records. Consumers reconstruct and verify members through the
shared registry/read-set resolvers.

Hi-C pair authority is the manifest declaration projected into registry
`manifest_entry_ordinal`, `pair_index`, and `mate` facts. File names and flat
path position never create a pair.

## Assembly input roles and product handoff

Assembly partitions the complete ordered long-read member set by platform.
The HiFi Hifiasm method binds all selected HiFi members positionally, optional
ONT ultra-long members to one comma-joined `--ul`, and all declared Hi-C lanes
to comma-joined `--h1`/`--h2`. The separate Hifiasm-ONT method binds ONT as
primary positional input and fixes `--ont`; it does not reuse `--ul`.

Only one assembler-origin backbone is materialized. Its executed candidate
bundle references the immutable plan, canonical primary FASTA, and generic
adapter-declared native outputs. Native graphs, alternate contigs, reports,
logs, or configs remain inside that executed bundle and do not become peer
assembly candidates or downstream assembly paths.

New Assembly checkpoints hold singular typed
`planned_candidate_bundle_ref` and `executed_candidate_bundle_ref` values.
Former plural and absolute-path candidate cursor fields are rejected. Data v2
does not read or migrate legacy checkpoints, and no run writes those fields
into checkpoints or resource handoffs.

## FOFN boundary

The canonical long-read handoff is
`resource_refs["long_reads"].paths: list[str]`.

FOFN files are tool-adapter or stage-internal artifacts:

- Read QC may generate a FOFN for audit or a tool invocation.
- An adapter such as NextDenovo may create its required FOFN in its own work
  directory from the canonical path list.
- Downstream stages do not consume `*_fofn_ref`, `assembly_input_ref`, or other
  stage-output handoff keys.
