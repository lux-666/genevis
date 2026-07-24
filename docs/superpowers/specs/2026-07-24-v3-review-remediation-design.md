# V3 Parameter Review Remediation Design

## Goal

Close the correctness and merge-gate findings discovered after the canonical
parameter-recommendation v3 cutover:

1. preserve a manifest-only genome-size estimate across the runtime entry
   boundary;
2. make T2T parameter approval reject stale or inconsistent evidence with the
   same fail-closed guarantees already used by Assembly;
3. make the changed-file Ruff gate and strict mypy checks for the core v3
   modules pass; and
4. remove the explicitly identified orphaned files and compatibility helpers.

This is a focused remediation. It does not extract a shared three-stage
approval state machine or replace the stage storage modules with adapters.

## Manifest Genome-Size Normalization

`manifest.genome_size_estimate` becomes the only external configuration field
for an operator-provided genome-size prior. The run-config shared-parameter
schema no longer accepts `shared_parameters.estimated_genome_size`. Run config
continues to own execution choices and the remaining cross-stage hints, but it
does not duplicate sample/input metadata already owned by the manifest.

Interactive planning and `ask --set genome_size=...` may collect a value before
execution. That value is planning state, not a second runtime configuration
source. On confirmation, ask writes the value into the session-local
materialized manifest without modifying a user-authored source manifest. The
generated run config omits genome size.

Inventory is the single runtime normalization boundary. After loading and
validating the materialized manifest, it projects
`manifest.genome_size_estimate` into the internal canonical
`shared_parameters.estimated_genome_size`. Downstream code receives only this
canonical internal value. There is no two-source merge or precedence policy.

Runtime survey measurements remain in `stage_outputs.genome_survey` and are
never merged into provided shared parameters. They continue to represent the
independent measured/resolved fact path.

`build_canonical_facts` remains a pure adapter over canonical shared
parameters, Read-QC output, and genome-survey output. Assembly does not pass
inventory output into it and no state-aware facts wrapper is added. This keeps
the recommendation snapshot, L1 resolution, and Flye/NextDenovo freeze path on
one provided-fact source.

Tests must cover the production-shaped path from a manifest-only genome size
through entry-state construction, canonical facts, L1 resolution, and
Flye/NextDenovo parameter freezing without an executable genome-survey stage.
They must also prove that run config rejects the removed duplicate field, ask
materializes an explicitly collected genome size without mutating the source
manifest, and survey output remains independent.

## T2T Approval Validation

Before persisting a decision, T2T approval must validate all immutable
bindings:

- review-request identity and request hash;
- decision membership in the persisted request payload;
- cursor status, review-request identity, recommendation identity, task,
  method, and manifest reference;
- payload manifest reference and `recommendation_manifest_hash`;
- interrupt recommendation hash;
- manifest status, recommendation identity, task, method, predecessor fields,
  and equality with the cursor; and
- the current attempt-chain head, including rejection of an already resolved
  approval.

T2T currently supports only the initial recommendation. The validated chain
must therefore describe recommendation 0, `recommend_initial`, and no
predecessor. Approval creates a `ParameterAttempt` from that validated
manifest identity rather than independently hard-coding a second identity.

This change does not alter parameter sources. L2 remains the approved
recommendation artifact referenced by the manifest. L1 remains the immutable
canonical-facts snapshot referenced by the manifest. Freeze continues to load
the approved immutable chain and never re-reads live top-level state.

Targeted tests will mutate each binding independently and require a
`ResumeValidationError`. A second approval of the same gate must also fail
without writing a duplicate attempt.

## Quality Gate

Ruff will run over every tracked or untracked Python file changed by the v3
branch. E501, I001, and E402 findings will be fixed in code; lint configuration
will not be weakened to hide them.

Strict mypy will run with this reproducible scope:

```shell
mypy app/domains/parameter_recommendation \
  app/schemas/parameter_catalog.py \
  app/schemas/parameter_recommendation.py \
  app/storage/parameter_evidence.py
```

Actual type errors will be fixed with narrower types, explicit validation, or
small local refactors. The development dependency will include the PyYAML type
stubs needed for a meaningful strict check. Broad `Any`, blanket ignores, and
configuration exclusions are not acceptable substitutes.

Verification proceeds from focused genome-size and T2T approval tests to the
parameter-recommendation suites, then the full test suite, changed-file Ruff,
strict mypy, and `git diff --check`.

## Orphan Cleanup

The following identified orphans will be removed:

- the empty `package-lock.json` in a repository with no `package.json`;
- `PlanWarning.model_dump_json_compat`, which has no call sites; and
- the private Assembly `_genome_size_prior` compatibility helper.

The HPC shell test that imports `_genome_size_prior` will be migrated to the
public genome-size resolution contract. No storage-wrapper consolidation or
cross-stage approval-service extraction is part of this change.

## Error Handling and Compatibility

Invalid manifest genome-size values and run configs using the removed duplicate
field fail before recommendation. Stale, tampered, or already-resolved T2T
approval evidence fails closed before the review decision or attempt is
persisted. Existing valid Assembly, AutoHiC, and T2T v3 evidence remains
readable. No pre-v3 recommendation compatibility path is introduced.
