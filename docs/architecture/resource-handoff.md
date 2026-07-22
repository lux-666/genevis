# Resource Handoff

This specification owns cross-stage file handoff. Biological inputs, outputs,
and review triggers remain in [`../stage_contracts.md`](../stage_contracts.md);
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
  "metadata": {"candidate_bundle_refs": ["..."]}
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

1. explicit run override;
2. `state.resource_refs[logical_resource]`;
3. initial asset-registry fallback;
4. `found=False`.

The resolver does not scan `stage_outputs`. Missing required resources are
handled by stage preflight using the corresponding `ResourceSpec`; the resolver
itself remains a non-throwing query for absence.

When inventory contains clean and raw assets for one logical resource,
`ResourceSpec.asset_types` determines the preferred type. Once a later stage
publishes a central resource ref, resolution must not silently fall back to an
older inventory asset.

Stages publish with `update_resource_ref(...)`, which returns a new validated
table and never mutates the supplied state.

## Logical resources

| Stage | Requires | Produces or updates |
|---|---|---|
| `inventory` | — | `long_reads`, `short_reads`, `hic_reads`, `assembly`, according to the manifest |
| `read_qc` | `long_reads` | `long_reads` |
| `genome_survey` | `short_reads` or `long_reads` | scalar measurement evidence only |
| `assembly` | `long_reads` | `assembly` |
| `assembly_dedup` | `assembly`, `long_reads` | `assembly` |
| `assembly_review` | `assembly` | `assembly` |
| `hic_scaffolding` | `assembly`; `hic_reads` when the stage is entered | `assembly` |
| `t2t` | `assembly`; optional `long_reads` | `assembly` |
| `repeat_annotation` | `assembly` | `masked_assembly` |
| `gene_annotation` | `masked_assembly`; explicit fallback to `assembly` | `predicted_proteins` |
| `function_annotation` | `predicted_proteins` | no cross-stage path resource |

Candidate bundles, review decisions, execution evidence, reports, and metrics
remain artifacts. They may be referenced from resource metadata but do not form
a second cross-stage file table.

## FOFN boundary

The canonical long-read handoff is
`resource_refs["long_reads"].paths: list[str]`.

FOFN files are tool-adapter or stage-internal artifacts:

- Read QC may generate a FOFN for audit or a tool invocation.
- An adapter such as NextDenovo may create its required FOFN in its own work
  directory from the canonical path list.
- Downstream stages do not consume `*_fofn_ref`, `assembly_input_ref`, or other
  stage-output handoff keys.
