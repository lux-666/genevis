# Method Execution Lifecycle Contract

> **Status:** implemented.  
> **Read when:** exact freeze/run/collect routing, attempt ownership, recovery,
> or failure policy matters.  
> **Comes from:** [Execution](../topics/execution.md).  
> **Feeds into:** stage collection, review, and durable evidence.  
> **Related contracts:** [Run data](data-contract.md),
> [Execution evidence](execution-evidence.md),
> [Parameter recommendation](parameter-recommendation.md).  
> **Code evidence:** `app/execution/lifecycle.py`,
> `app/execution/attempt_owners.py`.

## Scope

This specification owns the shared execution lifecycle for every executable
method. It defines graph boundaries, attempt ownership, recovery routing,
process-failure behavior, and the division between the execution kernel and
stage adapters.

The [run data contract](data-contract.md) owns durable fact placement and
reference rules. [Execution Evidence](execution-evidence.md) owns the canonical
process-result body. [Parameter Recommendation](parameter-recommendation.md) owns model
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

One `run` transition advances at most one frozen attempt. If an ordered direct
batch still has pending cursors, the method graph keeps the frozen stage status,
checkpoints the completed prefix, and routes back to `run`. Collection begins
only after every owned attempt has durable execution evidence.

At method-subgraph entry, the current stage status determines the recovery
boundary:

- the lifecycle's `frozen_status` enters `run` without rebuilding the command;
- the lifecycle's `ran_status` enters `collect` without executing again;
- every other status enters the stage-owned freeze callback.

After freeze and each run transition, a frozen status loops to the next pending
attempt, the ran status enters collect, and any other status exits the method
subgraph. Parent-stage routing owns subsequent failure, review, retry, and commit
decisions.

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
list on the stage output. Stage-local adapters validate the cursor shape and
own:

- attempt and method identity;
- `frozen` / `executed` / `execution_failed` status;
- the immutable frozen-attempt ref;
- the canonical execution-evidence ref after execution.

One-command methods use a one-item list. Ordered evaluators such as QUAST and
BUSCO use one item per candidate. Execution and collection preserve cursor
order; each cursor's method, attempt identity, status, and refs are validated
when loaded. During execution, a completed prefix and frozen suffix are valid;
one transition advances only the first frozen cursor.

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
loads the next frozen attempt through its verified ref. Stage, method, session,
and attempt identities must match. The working directory must resolve below the
current run's `work/steps/<workflow_step_id>/<stage>/` tree; the declared absolute script path must be
lexically below that working directory and resolve somewhere inside the run;
every expected output must resolve inside the run. The retained script must
exist and match its frozen SHA-256. The Runner executes that exact declared path
with the frozen working directory as cwd.

Before checking live inputs or invoking the Runner, the run node searches the
artifact index for one execution record with the same stage, method, attempt ID,
and exact frozen-attempt ref. A unique verified match advances the owner cursor
without rerunning. A mismatched or ambiguous record fails closed.

Each frozen attempt also declares typed `input_dependencies`. Registry-owned
external assets freeze their asset ID, canonical path, size, and SHA-256;
run-owned inputs freeze their complete `ArtifactRef` plus the exact command
path. Explicit unregistered files freeze path, size, and SHA-256, while
external directories freeze a deterministic tree hash, file count, and total
byte count. The shared run node verifies every dependency immediately before
invoking the Runner, including after resume. Missing, re-registered,
path-drifted, or byte-drifted input fails without starting the process.

`run_frozen_attempt` owns Runner invocation, stdout/stderr publication,
artifact-index registration, output cleanup policy, and attempt-started or
attempt-failed ledger facts. The Runner result attempt ID is checked before any
result-derived publication or persistence.

Every process outcome is converted to canonical `ExecutionEvidence` and
persisted in an outer record that binds the exact frozen-attempt ref and lifecycle
boundary disposition before its owner cursor advances. Expected infrastructure
failures become bounded failed stage deltas. Unexpected programming errors
remain visible to the graph failure boundary.

This prevents replay once canonical execution evidence is committed. It does
not provide process-level exactly-once behavior if the machine fails after a
process exits but before that evidence is published; local subprocess execution
has no scheduler identity that can be queried or reattached.

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

## Conformance

There is no legacy cursor reader, dual write, checkpoint migration, or
stage-local lifecycle compatibility layer.

The current implementation requires the following for methods using the shared
lifecycle. The central checkpoint still supplies the common JSON/size boundary;
these guarantees do not imply that every stage-local path projection is an
`ArtifactRef` or that every cursor key is universally typed at that boundary:

1. every executable production method reaches `build_method_graph` and the
   shared run kernel;
2. Assembly, AutoHiC, and T2T parameterized methods use
   `ParameterAttemptOwner`;
3. ordinary and batch methods use `DirectAttemptOwner`;
4. process success and failure persist canonical execution evidence;
5. resume after freeze does not rebuild a command, and committed execution
   evidence is recovered before any rerun;
6. every command input is bound through a typed frozen input dependency;
7. ordered batches checkpoint after each attempt and retain a completed prefix;
8. production code has no method-specific duplicate cursor key, wrapper
   artifact, or stage-local lifecycle implementation.

Parameterized retries use a new attempt ID and an attempt-specific work/output
directory. A completed predecessor remains immutable; a later attempt cannot
reuse its mutable result directory or overwrite its published outputs.
