# Shared Biological Facts Contract

> **Status:** implemented.
> **Read when:** changing cross-stage biological facts, planner fact
> collection, genome-size resolution, or shared-parameter conflict rules.
> **Comes from:** operator-provided RunConfig, TUI planning, or the frozen
> input manifest.
> **Feeds into:** [Stage contracts](stage-contracts.md),
> [Run data](data-contract.md), [Resource handoff](resource-handoff.md), and
> [TUI and planning](../topics/tui-and-planning.md).
> **Code evidence:** `app/schemas/shared_parameters.py`,
> `app/schemas/run_config.py`, `app/domains/genome_size.py`,
> `app/entrypoints/ask_planner.py`, `app/stages/inventory/nodes.py`, and
> `app/stages/assembly/nodes.py`.

## Definition

`SharedParameters` owns cross-stage biological facts collected once during
planning and projected into the frozen run state. The frozen RunConfig is the
sole authority for these values at runtime; stage outputs may supply derived
measurements but never overwrite a frozen fact.

The schema rejects unknown fields. Every field is optional; a stage that
requires an absent fact must fail closed at its own boundary rather than
inventing a default.

```json
{
  "platform": "ont",
  "read_type": "ont_raw",
  "estimated_genome_size": 500000000,
  "species_facts": {
    "scientific_name": "Homo sapiens",
    "taxon_id": 9606,
    "clade": "mammal",
    "ploidy": 2,
    "expected_chromosome_count": 23,
    "telomere_motif": "TTAGGG"
  }
}
```

## Field semantics

### `platform`

One sequencing-platform identifier from the manifest schema. It is a
classification hint, not a method selection. When only one platform is
registered, `read_type` may be derived from it; when platforms are mixed or
unknown, `read_type` must be supplied explicitly or deferred to a runtime
recommendation gate.

### `read_type`

One normalized read type from the Assembly selection domain. It constrains
which Assembly methods are compatible and is the primary input to the deferred
route selector. A frozen value is never silently overridden by a platform
inference; conflict is a validation error.

### `estimated_genome_size`

A positive integer in base pairs. This is an operator-provided prior, not a
measurement. It exists so a caller who already knows the genome size can skip
the survey stage or provide a fallback when survey reads are unavailable.

Resolution priority is deterministic and single-tracked:

1. `shared_parameters.estimated_genome_size` projected by runtime
   reconciliation;
2. `inventory.genome_size_estimate` projected from the manifest;
3. `genome_survey.genome_size_prior` when `genome_size_source == "survey"`.

The current `RunConfigSharedParameters` intentionally excludes this field.
Source 1 is a runtime projection, not a frozen-file input.
An earlier source wins, and a later source never overwrites an earlier one.
If neither an explicit value nor a valid survey estimate exists when Genome
Survey needs one, it fails with `GENOME_SIZE_UNAVAILABLE`. If Assembly reaches
candidate planning without a resolved prior for a method that requires one,
that candidate is rejected with `genome_size_required`.

### `species_facts`

Caller-supplied biological identity used by parameter recommenders. All
sub-fields are optional except `scientific_name`. `telomere_motif` is
normalized to uppercase ACGT and requires at least two bases. This block is
consumed by T2T finishing; Assembly and Genome Survey do not read it.

## Planning and freeze validation

The planner does not ask for `estimated_genome_size`. A deferred local Assembly
must have at least one of these three supportable sources before RunConfig
construction:

- `read_type` is required when the platform cannot uniquely determine it and
  the Assembly route is `auto`;
- `input_manifest.genome_size_estimate` is an explicit operator prior; or
- every local Assembly occurrence has a prior locally executed Genome Survey
  occurrence with usable survey reads.

Without one of those sources, plan compilation fails with a validation error.
The same biological rule is revalidated against canonical frozen inputs before
a run can be committed.

When a prior Survey exists, planning exposes the fact as
`awaiting_genome_survey`; it is not collected as a mutable draft value.

## Runtime reconciliation

Inventory reconciles the frozen RunConfig facts with any runtime projection:

- a runtime value that conflicts with a configured RunConfig shared fact is
  an error;
- a manifest `genome_size_estimate` that conflicts with a runtime
  `estimated_genome_size` is an error;
- a configured RunConfig shared fact always wins over a manifest value when
  both exist and agree.

After reconciliation, `shared_parameters` is immutable for the remainder of
the run. Stage outputs may add derived facts (for example, a survey
measurement), but they never mutate this block.

## Conflict matrix

| Source | Priority | Conflict behavior |
|---|---|---|
| Configured shared fact | 1 (highest) | authoritative; runtime must match |
| Manifest | 2 | fills absence; conflicts with runtime are errors |
| Genome Survey | 3 (lowest) | used only when no explicit value exists |

## Related contracts

- Genome Survey input and skip semantics: [Stage contracts](stage-contracts.md).
- Scalar-observation evidence boundary: [Resource handoff](resource-handoff.md).
- Deferred Assembly method binding: [Run data](data-contract.md).
