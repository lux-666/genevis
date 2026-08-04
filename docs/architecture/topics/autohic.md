# AutoHiC Hi-C Scaffolding

> **Status:** implemented as the only Hi-C scaffolding method
>
> **Read when:** following chromosome-scale scaffolding, AutoHiC parameter
> approval, execution, result adoption, or retry behavior.
>
> **Comes from:** parent-stage selection in
> [runtime orchestration](runtime-orchestration.md), current resources from
> [data and storage](data-and-storage.md), machine setup from
> [bootstrap](bootstrap.md), and schema-3
> [parameter recommendation](parameter-recommendation.md).
>
> **Feeds into:** the adopted `assembly` resource, downstream stages, durable
> execution/review evidence, and [review and resume](review-and-resume.md).
>
> **Contract dependencies:** [stage contracts](../reference/stage-contracts.md),
> [descriptors](../reference/descriptors.md),
> [parameter recommendation](../reference/parameter-recommendation.md),
> [method execution lifecycle](../reference/execution-lifecycle.md),
> [execution evidence](../reference/execution-evidence.md), and
> [resource handoff](../reference/resource-handoff.md).
>
> **Code evidence:** `app/stages/hic_scaffolding/builder.py`,
> `app/stages/hic_scaffolding/routing.py`,
> `app/stages/hic_scaffolding/nodes.py`,
> `app/stages/hic_scaffolding/parameters/workflow.py`,
> `app/stages/hic_scaffolding/method_application/autohic.py`,
> `app/stages/hic_scaffolding/method_application/support.py`, and
> `tests/test_hic_scaffolding.py`.

## Stage boundary

The parent graph decides whether `hic_scaffolding` is in the requested stage
set. If it is absent, the parent bypasses the stage. If it is present, the
compiled stage runs AutoHiC directly. There is no one-item method-selection
gate, stage-local skip branch, or no-op AutoHiC result.

The stable method ID `hic_scaffolding.candidate.autohic` identifies the
descriptor, catalog, recommendation, attempt, artifacts, and ledger events. It
does not imply that another Hi-C method can currently be selected.

At entry the stage resolves the current adopted assembly through the resource
resolver and resolves paired Hi-C members with their asset-registry identity.
It also requires the registered AutoHiC descriptor and local execution mode.
Missing or invalid selected-stage requirements produce the stage's explicit
requirements/failure outcome; they are not reinterpreted as “Hi-C was not
selected.” The stage cursor says `blocked_missing_requirements`, but top-level
supervision terminates that run rather than opening a resumable human-review
block.

## Implemented flow

```text
parent selects hic_scaffolding
  -> resolve adopted contig assembly + paired Hi-C reads
  -> build schema-3 AutoHiC recommendation
  -> block for parameter approval
  -> freeze exact AutoHiC config, script, dependencies, and outputs
  -> shared local run and execution-evidence persistence
  -> collect chromosome FASTA, contact map, assembly file, and HTML report
  -> block for a separate adoption review
  -> adopt candidate, keep prior assembly, request a fresh retry, or stop
```

Parameter approval and chromosome-assembly adoption are two different human
decisions. Approving a sparse mapping authorizes one executable attempt; it
does not adopt that attempt's biological output. The final gate sees the
published evidence and decides which assembly downstream stages should use.

## Adapter and execution boundary

The AutoHiC adapter loads only a verified approved schema-3 parameter chain.
It resolves executor values, canonical supplied facts, and approved model
overrides without merging a preceding retry mapping. It then materializes the
frozen parameter evidence, AutoHiC configuration, retained script, command
record, exact output paths, and typed dependencies for the shared lifecycle.

Machine-level AutoHiC, Juicer, 3D-DNA, Python, and model locations come from
the dependency selection prepared through [bootstrap](bootstrap.md) and frozen
for the run. The assembly, Hi-C read pairs, and any supplied restriction enzyme
are run-level facts. The adapter refuses to freeze when those boundaries cannot
produce a complete executable attempt.

Execution uses the production local subprocess Runner described in
[method execution](execution.md). The generated wrapper invokes the configured
AutoHiC entry point; Gene Agent does not reimplement Juicer, 3D-DNA, error
detection, or chromosome assignment inside the graph. There is no AutoHiC
remote/scheduler Runner.

On the normal wrapper-success path, collection publishes the chromosome-scale
FASTA and the `.hic`, `.assembly`, and HTML review products, together with
compact review/QC bundles and the canonical execution ref. The shared runner
already owns stdout, stderr, and process evidence; collection does not register
those logs again.

## Adoption and retries

Accepting the candidate verifies the published FASTA and replaces
`resource_refs["assembly"]` with the Hi-C-produced assembly plus provenance.
Keeping the prior assembly leaves the current resource unchanged. Requesting a
parameter retry seals the current attempt and returns to recommendation with
immutable execution and review evidence.

Execution-failure retry is controlled by explicit operator policy and a
bounded budget. Review-triggered retry has its own budget. Both create a fresh
schema-3 recommendation and attempt-specific workspace; neither patches the
previous mapping or overwrites its artifacts.

## Current limits

- AutoHiC is the only registered Hi-C method; alternative scaffolders are not
  an implemented selection path.
- The QC bundle explicitly records that automatic QC thresholds are not
  applied. Final biological adoption is a human decision, not an implemented
  auto-adopter.
- The generated wrapper requires all four expected products before reporting
  success, but the collection helper independently requires the FASTA and
  tolerates absent declared review artifacts. Required-output enforcement
  therefore relies on the wrapper-success boundary; do not claim that collect
  performs a second complete output check. This mismatch is tracked in
  [architecture conformance](../reference/conformance.md).
- Production execution is local to the CLI host. Scheduler submission,
  process reattachment, and remote orchestration are not implemented.
- Requirement failure, parameter rejection, exhausted retries, and execution
  failure remain distinct outcomes. None of them is a hidden skip branch.
