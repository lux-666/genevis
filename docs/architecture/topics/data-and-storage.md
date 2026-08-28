# Data and Storage

> **Status:** accepted, with explicit conformance debt noted below
>
> **Read when:** deciding where a fact belongs, following a file between
> stages, or debugging publication, resume, audit, and reporting.
>
> **Comes from:** [runtime orchestration](runtime-orchestration.md),
> [method execution](execution.md), inventory, and human review.
>
> **Feeds into:** [parameter recommendation](parameter-recommendation.md),
> [AutoHiC](autohic.md), downstream stage resolution,
> [review and resume](review-and-resume.md), and operator reports.
>
> **Contract dependencies:** [run data](../reference/data-contract.md),
> [resource handoff](../reference/resource-handoff.md),
> [execution evidence](../reference/execution-evidence.md), and
> [architecture conformance](../reference/conformance.md).
>
> **Code evidence:** `app/schemas/state.py`, `app/schemas/resource_ref.py`,
> `app/schemas/assets.py`, `app/storage/asset_registry.py`,
> `app/storage/artifacts.py`, `app/storage/ledger.py`,
> `app/control/occurrence_settlement.py`, `app/graph/checkpointer.py`,
> `app/graph/checkpoint_compaction.py`,
> `app/reports/review_report.py`, and `app/reports/trace_writer.py`.

## One run, several owners

`runs/<session_id>/` is the logical ownership boundary for a workflow run.
Within it, different stores answer different questions. They are not
interchangeable copies of one database.

| Layer | Authoritative for | Not authoritative for |
|---|---|---|
| `resource_refs` | The current logical cross-stage routing projection, such as the adopted `assembly` or current `long_reads` | Immutable proof that a named occurrence delivered it, input-member provenance, execution history, or large evidence bodies |
| Asset registry | Identity, declaration provenance, platform/format, path, and verification facts for inventory-registered external input members | Which derived output is currently adopted by later stages |
| Immutable artifacts | Exact evidence and produced file bytes for recommendations, reviews, commands, executions, and biological outputs | Causal ordering by themselves |
| Artifact index | Run-local artifact location, type, hash, size, producer identity, and compact dependency metadata | Business status, review decisions, model reasoning, or workflow routing |
| Ledger | Idempotent causal events; occurrence/method/attempt terminal singletons; immutable assembly-delivery facts; shared review status; and the one canonical session terminal, with refs to evidence | Evidence bodies, asset registries, result tables, or the latest resume cursor |
| Checkpoint | The latest bounded control frontier, latest `workflow_step_outcome` settlement handoff, and compact refs needed to continue | Audit history, durable terminal/delivery truth, reports, logs, commands, or model payloads |
| Reports | Operator/read-model projections; session summaries are regenerable from durable records, while current-status and Assembly Review gaps are tracked below | Workflow control, artifact identity, causal events, review status, or resume |

This ownership split is the core rule. A small immutable projection may appear
outside its owner for routing or display, but the projection does not become a
second authority. Exact placement, reference, size, retention, and crash rules
belong to the [run-data contract](../reference/data-contract.md).

## Progress is direct consumption, not another store

Interactive execution introduces no durable progress projection. Attached and
detached surfaces call the same pure reducer. Its exact inputs, occurrence
counting, conflict handling, and current-step rules are owned by the
[run-data contract](../reference/data-contract.md#410-one-workflow-progress-reducer).
Checkpoint availability, execution marker/lock, cursor, Ledger tail, and route
trace remain diagnostic state rather than alternate progress owners. A caller
may aggregate several read-only inspections outside the runs, but no such batch
projection becomes workflow truth.

## Cross-stage handoff

`TopLevelState.resource_refs` is the only table for passing file resources
between stages. A stage publishes a logical resource; a downstream stage
resolves that logical name. Stage-private cursors remain in `stage_outputs` and
must not become a hidden second handoff protocol.

Resolution first uses the current `resource_refs` entry and then the asset
registry according to the resource specification. It never scans another
stage's output keys. The registry fallback is why original manifest inputs can
seed a logical resource, while later adoption can replace that resource
without rewriting the original input provenance. The exact resolver priority,
logical resource set, and FOFN boundary live in the
[resource-handoff contract](../reference/resource-handoff.md).

For assembly, the replaced current table is paired with an immutable
occurrence-bound `resource_delivered` Ledger fact. That fact binds the producer
occurrence to the exact executed-candidate and primary-output refs, including a
pass-through occurrence that reuses older bytes. A downstream Review requires
the completed producer terminal, delivery fact, and current projection to
agree. Read QC likewise retains exact source-member/read-set lineage in its
current handoff; the detailed payload rules remain owned by the resource and
stage contracts.

The asset registry itself is an immutable artifact. Checkpoint state carries
only its verified `ArtifactRef`; consumers load the document, resolve ordered
member IDs, and recheck member paths, sizes, and ready checksums. A
`ResourceRef` may project those member IDs and compact provenance, but it does
not embed the registry.

## Publication and recovery

A normal durable transition moves in one direction:

```text
work file or in-memory record
  -> immutable run artifact
  -> idempotent artifact-index registration
  -> method/review semantic ledger event
  -> compact checkpoint update
  -> optional report projection
```

Occurrence completion adds a shared settlement suffix, used by the controller
and terminal recovery: validate the explicit `workflow_step_outcome`,
append/replay each assembly `resource_delivered` fact, append/replay the one
stage terminal, then render attached progress. A delivery without its completed
terminal is a recoverable crash prefix, not a consumable handoff.

The artifact index makes an `ArtifactRef` locatable and integrity-checkable.
The ledger makes the transition causally reconstructible. The checkpoint tells
LangGraph where to resume. A report makes the same facts convenient for a
human. Replaying a publisher with the same immutable path and bytes is
idempotent; conflicting bytes fail closed.

Checkpoints are validated at the central write boundary for JSON shape, known
stage names, size budgets, and accidental embedding of artifact or frozen
attempt bodies. Most stages add typed cursor adapters, but the central boundary
does not yet impose one universal schema on every stage-local cursor. Do not
claim that stronger invariant.

Reports run behind `ProjectionBoundary`: a normal projection failure is retained
as a bounded operational warning and cannot alter review status, checkpoint,
Ledger terminal authority, or resume outcome. Deleting a report must not alter
resume or audit truth. Session-summary views are regenerated from the Ledger
and indexed artifacts. The current-status writer still consumes the live compact
result supplied at a blocked or failed boundary. The stage-owned
`assembly_review.md` projection is generated from live stage state and its
indexed review packet and does not currently embed the standard watermark. Both
regeneration limitations are recorded in
[architecture conformance](../reference/conformance.md).

## Current limits and debt

- `storage-audit` is implemented as a read-only audit and can emit a dry-run
  cleanup manifest. The deletion executor for unreachable immutable artifacts
  and post-deletion artifact-index compaction are not implemented.
- The cleanup projection currently marks every file under `reports/` as
  regenerable. That is broader than the implemented regeneration paths, so it
  must not become an apply list without per-report eligibility checks.
- `work/` is retained because frozen attempts may still refer to scripts there;
  it is outside automatic cleanup.
- Reports are implemented projections, but a general automatic garbage
  collector is planned/debt, not a current product capability.
- `reports/current_status.json` is not currently reconstructed by a dedicated
  Ledger/index-only path; its writer requires a supplied compact runtime result.
- `reports/assembly_review.md` has no dedicated durable-record-only regeneration
  command and does not embed the shared source watermark.
