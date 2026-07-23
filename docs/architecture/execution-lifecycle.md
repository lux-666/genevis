# Execution Lifecycle

## Scope

This specification owns the shared execution lifecycle for every executable
method. It defines graph boundaries, attempt ownership, recovery routing,
process-failure behavior, and the division between the execution kernel and
stage adapters.

Data v2 owns durable fact placement and reference rules. Execution Evidence
owns the canonical process-result body. Parameter Recommenders owns model
request, response, parsing, and approval behavior. This specification connects
those contracts without replacing them.

## Canonical Flow

Every executable method uses the same three execution boundaries:

```text
method selection
  -> optional stage-owned recommendation and approval
  -> freeze
  -> run
  -> collect
  -> stage-owned review and commit
```

`freeze -> run -> collect` is implemented by
`app.execution.lifecycle.build_method_graph`. Parameter recommendation is an
optional preparation capability, not a second execution track.

At method-subgraph entry, the current stage status determines the recovery
boundary:

- the lifecycle's `frozen_status` enters `run` without rebuilding the command;
- the lifecycle's `ran_status` enters `collect` without executing again;
- every other status enters the stage-owned freeze callback.

After freeze and run, any status other than the lifecycle's expected next
status exits the method subgraph. Parent-stage routing owns subsequent failure,
review, retry, and commit decisions.

## Lifecycle Specification

`MethodExecutionLifecycle` contains only execution-level configuration:

- canonical `stage` and `method_name` identities;
- a diagnostic `name`;
- `frozen_status` and `ran_status` recovery markers;
- one attempt owner;
- expected-output cleanup policy;
- process-failure policy;
- an optional bounded failure kind.

The implemented process-failure policies are:

- `"fail"`: persist the failed process evidence and stop the lifecycle;
- `"collect"`: run the ordered attempt batch and let collect translate
  individual process outcomes into domain signals.

`"collect"` applies only to ordinary process outcomes. Identity, cursor,
publication, ledger, or evidence-persistence failures stop the lifecycle
immediately because continuing would make the batch unauditable.

The lifecycle specification does not contain parameter catalogs, prompt
rendering, biological schemas, review rules, or parent-stage routing.

## Attempt Ownership

Both ownership strategies enter the same run kernel.

### Direct attempts

`DirectAttemptOwner` stores a non-empty ordered `execution_attempts` cursor
list on the stage output. Each typed cursor owns:

- attempt and method identity;
- `frozen` / `executed` / `execution_failed` status;
- the immutable frozen-attempt ref;
- the canonical execution-evidence ref after execution.

One-command methods use a one-item list. Ordered evaluators such as QUAST and
BUSCO use one item per candidate. Execution and collection preserve cursor
order; each cursor's method, attempt identity, status, and refs are validated
when loaded.

### Parameter attempts

`ParameterAttemptOwner` binds exactly one frozen attempt and execution ref to
the current approved `ParameterAttempt`. The frozen attempt must carry an
explicit `parameter_attempt_id` matching the current cursor. The lifecycle
validates the selected attempt only; append, retry, sealing, and timeline code
own complete-chain validation.

No method-specific frozen or execution key duplicates refs already owned by a
`ParameterAttempt`.

## Stage Adapter Boundary

A method adapter owns two callbacks:

- **freeze callback:** validate selected stage inputs and approved parameters,
  write the method-specific script, build complete `FrozenAttemptSpec` values,
  and call `freeze_method_attempts` to persist and bind them;
- **collect callback:** load verified executions through
  `load_method_executions`, validate required biological outputs, publish or
  register them, record domain ledger facts, and return the next stage delta.

Adapters do not invoke a Runner, publish command logs, persist canonical
execution evidence, or create stage-local lifecycle factories.

## Run Boundary

Before any process starts, the shared run node validates the owner cursor and
loads each frozen attempt through its verified ref. Stage, method, session,
and attempt identities must match. The retained script path and working
directory must each still exist; the current loader does not impose a separate
path-containment relation between them.

`run_frozen_attempt` owns Runner invocation, stdout/stderr publication,
artifact-index registration, output cleanup policy, and attempt-started or
attempt-failed ledger facts. The Runner result attempt ID is checked before any
result-derived publication or persistence.

Every process outcome is converted to canonical `ExecutionEvidence` and
persisted before its owner cursor advances. Expected infrastructure failures
become bounded failed stage deltas. Unexpected programming errors remain
visible to the graph failure boundary.

## Collect Boundary

The shared graph wraps every collect callback with one expected-failure
boundary. Filesystem, layout, ledger-format, ledger-conflict, and verified
artifact-loading failures represented by the boundary's declared error types
produce a bounded `failed` stage cursor. The boundary records causal ledger
events on a best-effort basis; a broken ledger cannot suppress the failed state
delta. Malformed typed cursors or evidence bodies may raise an ordinary
`ValueError`, and ordinary callback `ValueError` and other programming errors
are not converted into normal workflow outcomes.

Collect adapters decide which biological outputs are required or optional and
own attempt-succeeded facts after successful materialization.

## Operator-Directed Assembly Rerun

An Assembly Review decision may schedule a Flye rerun, but the decision applier
does not execute it. It persists the operator parameters as a compact rerun
request, clears the interrupt, and returns a running-stage update. The existing
resume transaction checkpoints that update before invoking the graph.

The rerun then uses the ordinary direct-attempt lifecycle under canonical stage
`assembly`:

```text
flye_rerun_planned
  -> freeze / execution_attempts[frozen]
  -> run / execution_attempts[executed|execution_failed]
  -> collect / candidate bundle
  -> assembly review reevaluation
```

The `execution_attempts` cursor is the recovery relation for the frozen and
execution refs. The review decision owns the operator parameters; the candidate
bundle is the collected domain result and may project those refs for audit. No
parameter-recommendation record is synthesized for this path.

## Conformance

There is no legacy cursor reader, dual write, checkpoint migration, or
stage-local lifecycle compatibility layer.

The current implementation requires:

1. every executable production method reaches `build_method_graph` and the
   shared run kernel;
2. Assembly, AutoHiC, and T2T parameterized methods use
   `ParameterAttemptOwner`;
3. ordinary, batch, and operator-rerun methods use `DirectAttemptOwner`;
4. process success and failure persist canonical execution evidence;
5. resume after freeze does not rebuild a command, and resume after run does
   not execute again;
6. production code has no method-specific duplicate cursor key, wrapper
   artifact, or stage-local lifecycle implementation.

Parameterized retries use a new attempt ID and an attempt-specific work/output
directory. A completed predecessor remains immutable; a later attempt cannot
reuse its mutable result directory or overwrite its published outputs.
