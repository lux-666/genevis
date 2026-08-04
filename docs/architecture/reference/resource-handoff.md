# Resource Handoff Contract

> **Status:** implemented.  
> **Read when:** a stage publishes or consumes a materialized biological file.  
> **Comes from:** [Data and storage](../topics/data-and-storage.md) and stage
> commit boundaries.  
> **Feeds into:** downstream stage validation and method freezing.  
> **Related contracts:** [Stage contracts](stage-contracts.md),
> [Run data](data-contract.md).  
> **Code evidence:** `app/stages/resource_specs.py`,
> `app/stages/resource_resolution.py`, `app/schemas/resource_ref.py`.

This specification owns cross-stage file handoff. Biological inputs, outputs,
and review triggers remain in [`stage-contracts.md`](stage-contracts.md);
this document defines how materialized resources move between stages.

## State ownership

`TopLevelState.resource_refs` is the only cross-stage file handoff table.
`stage_outputs` contains stage-private, checkpointable cursors and may hold
status, metrics, evidence refs, and recovery metadata. A downstream stage must
not scan another stage's output or guess a key name to locate an input file.

A persisted `ResourceRef` has this shape:

```json
{
  "resource_type": "assembly",
  "paths": ["/run/artifacts/assembly/draft.fasta"],
  "source_stage": "assembly",
  "metadata": {
    "candidate_bundle_ref": {
      "schema_version": 2,
      "artifact_id": "art_...",
      "path_ref": "artifacts/assembly/attempts/000/candidates/hifiasm_candidate.executed.json",
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
the resolver itself remains a non-throwing query for absence. Environment and
method preflight occur when a method attempt is frozen; there is no separate
stage-planning preflight authority.

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
| `t2t` | current adopted `assembly`, `long_reads` | `assembly` |
| `repeat_annotation` | `assembly` | `masked_assembly` |
| `gene_annotation` | `masked_assembly`; explicit fallback to `assembly` | `predicted_proteins` |
| `function_annotation` | `predicted_proteins` | no cross-stage path resource |

Runtime stage adapters enforce these direct dependencies. Genome Survey
already admits a short-read-only run and prefers short DNA reads when both
families are present. `resource_specs.py` owns only canonical resource
resolution; stages do not share a second declarative planning table.

Hi-C reads stop at `hic_scaffolding`. After human adoption, that stage replaces
the central `assembly` resource. T2T resolves this current assembly and the
long-read set; it does not consume `hic_reads` or a contact map directly.

Candidate bundles, review decisions, execution evidence, reports, and metrics
remain artifacts. They may be referenced from resource metadata but do not form
a second cross-stage file table.

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
