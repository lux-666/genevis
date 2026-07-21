# Execution Lifecycle Unification Design

Status: Accepted

Date: 2026-07-21

## 1. Context

Gene Agent already has stable Data v2 session ownership, checkpoint budgets,
durable resume, terminal compaction, artifact publication, and execution
evidence primitives. Those boundaries are not the target of this refactor.

The remaining execution path is not single-track. Assembly, Hi-C, read-QC,
genome survey, and assembly review each have a stage-local lifecycle module,
while T2T, repeat annotation, gene annotation, function annotation, dedup, and
review reruns implement parts of freeze, run, evidence persistence, and collect
directly. The implementations share the same intended lifecycle but differ in
state keys, error handling, evidence attachment, and graph wiring.

Assembly and Hi-C also prove an important future requirement: parameter
recommendation may be present for a method, but it is not mandatory for every
method or stage. A lifecycle split between "recommended" and "ordinary"
execution would preserve the current duplication instead of removing it.

## 2. Goals

1. Provide one execution lifecycle for every executable method.
2. Keep parameter recommendation as an optional method capability.
3. Keep freeze, run, and collect as separate graph nodes and checkpoint
   boundaries.
4. Make success and failure produce the same canonical durable execution
   evidence contract.
5. Let each stage own parameter consumption, command construction, biological
   artifact collection, review rules, and commit behavior.
6. Delete migrated lifecycle implementations, state-key conventions, adapters,
   and compatibility tests in the same change that replaces them.
7. Reduce code paths and failure semantics rather than merely moving code into
   new files.

## 3. Non-goals

- Redesigning session paths, Data v2, checkpoint storage, the control-layer
  resume transaction, or terminal checkpoint compaction. A review decision may
  schedule execution for the existing `update_state -> invoke` transaction;
  command execution must not remain inside the decision applier.
- Preserving old session or checkpoint compatibility when a removed execution
  cursor is no longer valid.
- Combining artifact publication, registration, and recovery. They have
  distinct write semantics and remain separate operations.
- Moving stage-specific parameter recommenders into the execution layer.
- Unifying biological collection, review, or commit logic.
- Creating a universal stage graph or a plugin framework for every stage
  decision.
- Splitting large modules without deleting duplicated behavior.

## 4. Architectural Decision

The canonical executable-method flow is:

```text
method selection
  -> optional stage-owned recommendation
  -> optional stage-owned approval
  -> freeze
  -> run
  -> collect
  -> stage-owned review and commit
```

`freeze -> run -> collect` is one shared lifecycle. Parameter recommendation
changes how an attempt is prepared and which cursor owns its durable refs; it
does not change the runner or create another lifecycle.

The shared implementation lives under `app/execution/`, not under Assembly or
Hi-C. Assembly and Hi-C become the first parameterized consumers of that
stage-neutral implementation.

## 5. Component Boundaries

### 5.1 Method execution specification

A small immutable `MethodExecutionLifecycle` specification describes only
execution-level facts:

- canonical stage ID;
- canonical method ID;
- frozen, executed, and failed status values;
- runner options such as output cleanup policy;
- execution failure policy (`fail_fast` or `collect_all`);
- an attempt owner;
- stage callbacks for preparing attempts and collecting results.

It must not contain parameter catalogs, prompt rendering, biological output
schemas, review policy, or stage routing rules.

### 5.2 Shared graph factory

One factory builds the method subgraph with exactly three durable boundaries:

```text
START -> freeze -> run -> collect -> END
```

The shared freeze node calls the stage adapter to prepare one or more complete
`FrozenAttemptSpec` values, persists each value, and attaches the resulting
refs through the attempt owner. Routing after freeze and run is based on a
typed lifecycle result, not on method-specific string-key inference. A failed
or blocked transition exits the method subgraph without calling later nodes.

The factory does not replace parent-stage graphs. A stage may still have
planning, recommendation, approval, review, retry, and commit nodes around the
method subgraph.

### 5.3 Attempt owner

The execution kernel uses one narrow attempt-owner interface:

```text
record_frozen_attempts(state, lifecycle, refs) -> state delta
load_frozen_attempts(state, lifecycle) -> list[FrozenAttemptSpec]
record_execution(state, lifecycle, attempt_id, evidence_ref, succeeded) -> state delta
load_executions(state, lifecycle) -> list[ExecutionEvidence]
```

There are two ownership strategies because their durable facts are genuinely
different:

- `DirectAttemptOwner` stores an ordered list of compact typed execution
  cursors for methods without parameter recommendation. Ordinary methods use a
  one-item list; batch evaluators such as QUAST and BUSCO use one cursor per
  candidate.
- `ParameterAttemptOwner` stores frozen-attempt and execution refs on the
  current `ParameterAttempt` and therefore requires exactly one prepared
  attempt for the selected parameter attempt.

These are not separate execution tracks. Both enter the same run node and use
the same identity checks, Runner, evidence construction, error handling, and
graph transitions. The owner only decides where the compact refs live and how
they are attached to their authoritative cursor.

The shared freeze node persists the canonical frozen-attempt artifact before
calling `record_frozen_attempts`. The shared run node persists the canonical
stage execution-result artifact before calling `record_execution`. Attempt
owners bind those refs; they do not create another stage-specific execution
wrapper. Existing Assembly and Hi-C parameter-execution wrapper artifact types
become obsolete after their last reader is migrated.

No frozen-attempt or execution ref may be duplicated between a stage cursor
and a `ParameterAttempt`. For parameterized methods, `ParameterAttempt` remains
the sole relation owner.

### 5.4 Stage adapter

Each executable method supplies two domain callbacks:

- `prepare`: consume already selected inputs and, when applicable, already
  approved parameters; construct one or more complete `FrozenAttemptSpec`
  values plus a bounded stage delta. The shared freeze node persists and
  attaches them; stage code does not write frozen-attempt cursor keys.
- `collect`: load validated execution evidence through the configured owner,
  publish or register expected biological outputs, and return a stage delta.

A parameterized stage continues to own recommendation details. Assembly may
consume assembly-specific profiles and retry evidence; Hi-C may consume
restriction-enzyme recommendations; a future stage may define a different
recommender. The lifecycle only requires that freeze receives an approved,
durably bound parameter attempt.

## 6. Data Flow

### 6.1 Method without parameter recommendation

1. The stage selects a method and resolves its inputs.
2. The method prepare callback creates one or more `FrozenAttemptSpec` values.
3. The shared freeze node persists them and `DirectAttemptOwner` writes an
   ordered cursor for each frozen ref.
4. The shared run node loads and validates the frozen attempt.
5. The shared run node calls `run_frozen_attempt`.
6. Runtime `ExecutionResult` is converted to canonical `ExecutionEvidence`.
7. `DirectAttemptOwner` appends each execution ref to its matching cursor.
8. The method collect callback loads that evidence and publishes outputs.

### 6.2 Method with parameter recommendation

1. The stage-specific recommender persists profile, request, raw response,
   parsed recommendation, and approval evidence.
2. The stage creates or advances the current `ParameterAttempt`.
3. The method prepare callback consumes the approved parameters and returns
   one `FrozenAttemptSpec`.
4. The shared freeze node persists it and `ParameterAttemptOwner` appends the
   frozen ref to the current attempt.
5. The same shared run node executes and builds canonical evidence.
6. `ParameterAttemptOwner` appends the execution ref and advances attempt
   status.
7. The method collect callback loads evidence through the owner and publishes
   outputs.
8. Stage-specific review may append review evidence or create a new retry
   recommendation after execution.

Recommendation and approval never run inside the shared execution node.

## 7. Validation and Failure Semantics

Before execution, the shared run node fails closed unless all of these match:

- state session ID and frozen-attempt session ID;
- lifecycle stage and frozen-attempt stage;
- lifecycle method ID and frozen-attempt method ID;
- attempt-owner cursor and frozen-attempt identity;
- retained working directory and script path.

Every started process must produce durable stdout and stderr refs. Both success
and failure persist canonical `ExecutionEvidence`. A process failure records
the evidence first. Under `fail_fast` it then returns a bounded failed lifecycle
delta. Under `collect_all` it marks only the matching attempt cursor failed,
continues the ordered batch, and keeps the lifecycle routable to collect. An
evidence write failure is an execution-boundary failure and must not be
reported as a successful run.

The shared run node accepts an ordered attempt list. `fail_fast` stops after the
first failed attempt and marks the lifecycle failed. `collect_all` runs every
prepared attempt, persists every result, and lets collect translate individual
failures into domain signals. Assembly Review metric batches use `collect_all`;
normal executable methods use `fail_fast`. Attempt and cursor order must match,
so resume cannot associate evidence with a different candidate.

`collect_all` applies only to process outcomes. Identity errors, cursor errors,
or failure to persist canonical evidence abort the lifecycle immediately,
because continuing would create an unauditable partial batch.

Collect must validate the execution ref and expected attempt ID before reading
outputs. Missing required outputs fail the method and record a causal ledger
event. Optional outputs may be absent only when the stage adapter declares
them optional.

The shared layer must not catch stage programming errors broadly and convert
them into success or planning status. Expected validation, storage, and runner
errors receive explicit bounded failure results; unexpected exceptions remain
visible to the graph failure boundary.

## 8. State Contract

Parameterized methods retain the existing bounded `ParameterAttempt` chain.
The lifecycle does not copy recommendation bodies, commands, execution bodies,
logs, or biological outputs into checkpoint state.

Non-parameterized methods replace method-specific keys such as
`*_frozen_attempt_ref`, `*_execution_result`, and plural variants with one
typed direct-attempt cursor list. Each cursor contains only attempt identity,
method identity, status, and immutable frozen/execution artifact refs. A
single-command method and a candidate batch therefore use the same schema
without singular/plural key conventions.

No compatibility reader, dual write, legacy-key fallback, or checkpoint
migration is added. Once a stage is migrated, its old cursor keys are rejected
or absent, and its old implementation is deleted.

## 9. Migration Strategy

Migration is incremental across stages but single-track within each migrated
stage.

1. Add the shared lifecycle, attempt-owner contract, direct cursor schema, and
   contract tests.
2. Migrate Assembly and Hi-C together to prove that the shared parameter owner
   covers both existing parameterized consumers. Delete their duplicated graph
   factory and run/evidence code in the same change.
3. Migrate one simple non-parameterized method to prove the direct owner. Use
   the result to finalize the direct cursor rather than designing speculative
   hooks.
4. Migrate the existing read-QC and genome-survey lifecycle modules, then
   migrate Assembly Review with the direct owner in `collect_all` mode. QUAST
   and BUSCO prepare one attempt per candidate; candidate iteration belongs to
   the shared ordered run kernel, while signal construction remains in the
   collect adapter. Delete each old lifecycle module as its last consumer
   moves.
5. Migrate manual execution paths in dedup, repeat annotation, function
   annotation, T2T, and gene annotation.
6. Move operator-directed Assembly Flye rerun out of the review decision
   applier. The applier validates and persists the decision, records a compact
   rerun request, clears the interrupt, and returns a running-stage delta. The
   existing resume transaction checkpoints that delta and invokes the graph;
   Assembly Review routing then enters the shared direct lifecycle before
   reevaluation. The rerun attempt retains canonical stage ID `assembly`;
   `assembly_review` owns only the rerun request, decision evidence, and route
   back to evaluation. This preserves the control-layer transaction while
   adding the normal freeze/run/collect checkpoint boundaries.
7. Remove obsolete state keys, routing helpers, mirror exports, compatibility
   tests, and dead artifact types.
8. Update architecture conformance documents only after all runtime consumers
   use the shared lifecycle.

No temporary adapter becomes a public interface. A migration slice is complete
only when its old path is deleted and repository searches show no remaining
callers.

## 10. Testing

The shared lifecycle contract test matrix covers both attempt owners:

- successful freeze, run, evidence persistence, and collect;
- process failure with durable stdout, stderr, and execution evidence;
- frozen-attempt session, stage, method, and attempt mismatch;
- missing or corrupted frozen-attempt evidence;
- evidence persistence failure;
- resume after freeze without rebuilding the command;
- resume after run without re-executing the command;
- missing required output during collect;
- direct-owner cursor validation;
- ordered multi-candidate execution and cursor/evidence association;
- `collect_all` behavior with mixed successful and failed candidates;
- parameter-attempt append-once and sealed-attempt validation;
- review rerun decision checkpointed before the shared lifecycle executes;
- resume after rerun freeze and run without duplicate execution;
- injected `FakeRunner` without adding a test execution mode.

Each migrated stage retains focused tests for command construction, parameter
consumption, biological output collection, review behavior, and routing. The
existing durable SQLite resume and entrypoint E2E suites remain refactor
guardrails.

## 11. Acceptance Criteria

The refactor is complete when:

1. Every executable method reaches one shared run implementation, including
   single attempts, Assembly Review candidate batches, and operator-directed
   Assembly reruns.
2. Assembly and Hi-C parameterized execution use the same attempt-owner and
   execution-evidence path.
3. A method can opt into recommendation without introducing a new lifecycle.
4. Stages without recommendation do not create fake recommendation records.
5. Stage-specific lifecycle modules and manual run/evidence implementations are
   deleted.
6. Old attempt cursor keys, fallbacks, adapters, and compatibility tests are
   deleted.
7. Session, checkpoint, artifact publication, and resume contracts remain
   unchanged except for the intentional removal of old stage cursor shapes.
8. Focused lifecycle tests, all affected stage tests, durable SQLite resume,
   and the full unit suite pass.
