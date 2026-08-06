# Frozen Occurrence Integrity Design

## Status

Approved by the user on 2026-08-06. This specification covers the integrity
hardening work for frozen control inputs, executable attempts, Ledger facts,
cross-stage resources, and trace reconstruction. It does not change workflow
ordering or add biological capabilities.

## Problem

The repository has adopted a frozen, occurrence-addressed workflow model, but
five execution and audit boundaries still admit a second authority or lose the
current occurrence identity:

1. `execute_frozen_session` validates `run_config.json` once and projects its
   workflow occurrences and stage seeds, while Inventory later reloads the same
   file without checking the frozen marker. A mutation between those reads can
   make routing use the old config while declarations, asset registration, and
   shared parameters use a new config.
2. `run_method_attempts` can recover or execute a frozen attempt before proving
   that its `workflow_step_id` is the active occurrence. A stale same-stage
   cursor can therefore start a process and write `attempt_started` before a
   later persistence boundary rejects it.
3. Several stage-runtime Ledger facts are not occurrence-bound. Repeated stage
   names cannot be reliably distinguished, and default idempotency can collapse
   equivalent facts from different occurrences.
4. `ResourceRef` permits a stage-produced resource with no producing
   `workflow_step_id`, despite the documented handoff contract.
5. Run-trace reconstruction reads `run_config.json` without validating the
   frozen marker and may report a plan different from the one that executed.

These are one architectural class of defect: an occurrence-bearing durable fact
must be derived from one verified authority and must be rejected before any
side effect when its occurrence identity is absent or stale.

## Goals

- Make the verified `FrozenInputBundle` the sole runtime authority for Inventory
  control inputs.
- Prevent Inventory from reopening the frozen manifest or RunConfig through an
  unchecked path.
- Reject a frozen attempt belonging to another workflow occurrence before
  recovery lookup, dependency verification, Runner invocation, or attempt
  lifecycle events.
- Require every stage-runtime Ledger fact to carry an explicit producing or
  active `workflow_step_id`.
- Require persisted stage-produced resources to identify their producing
  occurrence.
- Make trace reconstruction use a freshly verified frozen bundle or durable
  occurrence-bearing fallback facts.
- Preserve the small, JSON-only checkpoint contract.

## Non-Goals

- No dynamic workflow insertion, successor table, or route-policy change.
- No frozen-input marker schema change or fourth frozen control file.
- No embedding of the complete RunConfig, manifest, or canonical declarations
  in checkpoint state.
- No compatibility reader or migration for malformed pre-release checkpoints.
- No attempt to provide process-level exactly-once execution after machine
  failure.
- No general stage-plugin registry refactor.

## Verified Frozen Input Context

`validate_frozen_inputs` already canonicalizes the frozen manifest and RunConfig
and computes `CanonicalInputDeclarations`. It must retain that result on the
immutable `FrozenInputBundle` instead of discarding it after validation.

The runtime dependency flow becomes:

```text
validate_frozen_inputs
  -> FrozenInputBundle(manifest, run_config, declarations, marker, paths)
  -> build_graph(..., verified_inputs=bundle)
  -> build_inventory_stage(verified_inputs=bundle)
  -> Inventory validate/register nodes consume that in-memory verified context
```

The bundle is executor-owned runtime context captured by graph-node closures. It
is not a LangGraph channel and is never serialized into a checkpoint. On a new
process or resume command, the production entry boundary validates the frozen
transaction again and constructs a new verified bundle before building a graph
that can execute nodes.

`build_graph` may retain an explicit no-bundle construction seam for tests and
read-only graph/checkpointer inspection, but an Inventory node reached without
verified inputs must fail closed with a clear configuration error. Production
run, review, and resume paths must never provide a disk-reload fallback.

Inventory uses:

- `bundle.manifest` as the canonical manifest object;
- `bundle.run_config` as the canonical configuration object;
- `bundle.declarations` for the merged manifest/RunConfig read declarations;
- `bundle.paths` for run-owned refs, including the RunConfig provenance ref.

Both `validate_inputs` and `register_assets` consume the same declaration
projection. The existing `_load_inventory_run_config` fallback is removed from
production code rather than retained as a compatibility path. Shared-parameter
reconciliation still verifies that the small runtime projection does not
conflict with the bundle's RunConfig and manifest facts.

## Executable Attempt Occurrence Gate

The shared run kernel owns the current-occurrence check because generic evidence
loaders may legitimately inspect historical attempts. `run_method_attempts`
must validate every pending owner-loaded attempt against:

- the active workflow step returned by `current_workflow_step_id`;
- the lifecycle's expected stage;
- the attempt metadata's non-empty `workflow_step_id`.

The required order is:

```text
validate lifecycle stage status
  -> load and validate owner cursor
  -> load frozen attempt through verified ref
  -> require active occurrence identity match
  -> search for recoverable execution evidence
  -> verify live input dependencies
  -> invoke Runner
  -> persist execution result
  -> advance owner cursor
```

A mismatch returns the lifecycle's existing bounded failure update. It must not
search for recovery evidence, call the Runner, publish execution logs, or append
`attempt_started`, `attempt_succeeded`, or `attempt_failed` for the stale
attempt.

`load_frozen_attempt` continues to validate the internal agreement between the
artifact index, record payload, attempt body, session, stage, method, and
run-owned paths. It does not implicitly reinterpret every load as a request to
execute the current occurrence.

## Occurrence-Bound Ledger Facts

The raw `record_event` API remains available for run-level facts such as session
creation, terminal outcome, and checkpoint compaction. Stage-runtime callers
must use an occurrence-requiring boundary whose signature requires:

- `stage: str`;
- `workflow_step_id: str`;
- a bounded payload;
- an occurrence-aware subject or idempotency identity.

This may be implemented as `record_occurrence_event` plus the existing typed
Ledger helpers. The central writer must still reject an empty step ID for all
known occurrence event types so direct misuse also fails closed.

The following events are migrated:

| Event | Occurrence source | Idempotency subject |
| --- | --- | --- |
| `env_preflight_blocked` | active stage state passed into environment dispatch | step ID plus method/environment identity |
| `candidate_comparison_generated` | current `assembly_review` occurrence | step ID plus comparison artifact identity |
| `registered_assembly_candidate_bound` | current Inventory occurrence | step ID plus registered asset/candidate identity |
| `automatic_review_failed` | active interrupt or verified blocked summary | step ID plus gate/request identity |

Environment dispatch currently receives stage output and run identity but not
the occurrence. Its public method-resolution boundary must therefore receive a
required `workflow_step_id`, with each stage adapter deriving it from the
current state before preflight. The same occurrence is used in both the Ledger
envelope and idempotency key.

`session_terminal` may retain a diagnostic stage without a workflow step because
it is a run-level outcome rather than an occurrence fact. That exception must
stay explicit rather than weakening stage-runtime validation generally.

## ResourceRef Invariant

`ResourceRef` enforces a paired provenance invariant:

```text
source_stage is None  <=> workflow_step_id is None
source_stage is set   <=> workflow_step_id is a non-empty string
```

The constructor also rejects an empty `source_stage`. `from_dict` and
`validate_resource_refs` inherit the invariant automatically.

Manifest/external resources use both fields as `None`. Stage-produced resources
use the canonical producing stage and exact workflow step. The registered
assembly created by Inventory changes its `source_stage` from the descriptive
`inventory_registered_assembly` label to canonical `inventory`; the more
specific source description remains metadata.

The schema does not import `DomainStage` from `state.py`, avoiding an import
cycle. Canonical current-stage equality remains enforced at the production
writer `update_resource_ref`, while the ResourceRef schema enforces the
presence/absence relationship for every persisted record.

There is no legacy upgrade path. A checkpoint containing a stage-produced
resource without a step ID is outside the accepted v2 contract and fails
validation.

## Verified Trace Plan

Trace generation is a separate command and cannot reuse the executor's old
in-memory bundle. `_workflow_steps_for_trace` must call
`validate_frozen_inputs(paths)` and derive steps only from the returned
`bundle.run_config`.

If marker, hash, schema, or biological validation fails, trace generation must
not parse the changed RunConfig directly. It falls back to occurrence-bearing
Ledger and artifact-index facts. The fallback may report gaps when durable facts
are insufficient; it must not invent order from `STAGE_ORDER`.

## Error Handling

- Missing verified Inventory context is a pre-side-effect failure.
- Frozen-input integrity failure stops graph construction or trace RunConfig
  projection and preserves the original error.
- Attempt occurrence mismatch becomes a bounded lifecycle failure before any
  recovery or process side effect.
- A stage-runtime Ledger call without a step ID raises before append.
- A malformed ResourceRef fails whole-state validation before checkpoint write.
- Trace fallback reports durable evidence gaps instead of accepting tampered
  control files.

## Documentation Changes

The implementation change updates:

- `docs/architecture/topics/runtime-orchestration.md` for verified runtime
  context injection;
- `docs/architecture/reference/execution-lifecycle.md` for the pre-recovery
  occurrence gate;
- `docs/architecture/reference/data-contract.md` for occurrence-bound stage
  events and verified trace authority;
- `docs/architecture/reference/resource-handoff.md` for the paired ResourceRef
  invariant;
- `docs/architecture/langgraph-implementation-map.md` evidence and status rows;
- `docs/architecture/reference/conformance.md`, adding gaps before implementation
  if needed and deleting them in the same change that proves closure.

The architecture must not continue to label these boundaries `debt: none`
without focused tests proving the corrected behavior.

## Verification

Focused tests must prove:

- mutating `run_config.json` after the executor validates it cannot change
  Inventory declarations, assets, shared parameters, or registry provenance;
- Inventory validate and register nodes consume the same bundle projection;
- production run and resume graph construction validate and inject frozen inputs;
- a stale same-stage attempt cannot trigger recovery lookup, Runner invocation,
  log publication, or attempt lifecycle events;
- all four formerly unbound events carry the exact active step ID;
- repeated same-stage environment failures produce distinct occurrence facts and
  are not idempotently folded together;
- raw stage-runtime Ledger calls without a step ID fail closed;
- stage-produced ResourceRefs without a step ID, and step IDs without a source
  stage, fail schema and whole-state validation;
- manifest resources with both provenance fields absent remain valid;
- trace uses a valid frozen bundle and never trusts a hash-invalid RunConfig;
- trace durable fallback preserves occurrence IDs and reports missing evidence
  without using registry order.

After focused tests, run formatting, lint, typing, and the complete test suite.
A gate is reported as passing only after the command exits zero with its expected
assertions.

## Acceptance Criteria

1. Inventory has no production code path that reopens an unchecked frozen
   RunConfig or manifest.
2. A stale occurrence cannot reach execution recovery or process launch.
3. Every stage-runtime Ledger fact is attributable to one workflow occurrence.
4. Every stage-produced ResourceRef identifies one producing occurrence.
5. Trace plan reconstruction accepts only a verified frozen RunConfig.
6. The checkpoint remains compact and contains no embedded frozen-control body.
