# Method Execution Lifecycle Contract

> **Status:** accepted and implemented.
> **Read when:** whole-run readiness, exact freeze/run/collect routing, attempt
> ownership, recovery, or failure policy matters.
> **Comes from:** [Execution](../topics/execution.md).  
> **Feeds into:** stage collection, review, and durable evidence.  
> **Related contracts:** [Run data](data-contract.md),
> [Execution evidence](execution-evidence.md),
> [Parameter recommendation](parameter-recommendation.md).  
> **Code evidence:** `app/entrypoints/run.py`,
> `app/storage/frozen_inputs.py`, `app/execution/run_readiness.py`,
> `app/execution/lifecycle.py`,
> `app/execution/method_outcomes.py`,
> `app/execution/attempt_owners.py`, `app/control/occurrence_settlement.py`.

## Scope

This specification owns the pre-freeze run-specific readiness boundary and the
shared execution lifecycle for every executable method. It defines graph
boundaries, attempt ownership, recovery routing, process-failure behavior, and
the division between the execution kernel and stage adapters.

The [run data contract](data-contract.md) owns durable fact placement and
reference rules. [Execution Evidence](execution-evidence.md) owns the canonical
process-result body. [Parameter Recommendation](parameter-recommendation.md) owns model
request, response, parsing, and approval behavior. This specification connects
those contracts without replacing them.

## Whole-run Readiness Boundary

After the real-TTY check and before publishing any frozen session input, both
structured run and TUI compute the canonical effective local-method closure.
Readiness resolves every fixed local method through the active machine mapping,
requires a non-empty tool/package probe declaration, verifies its environment,
and probes its declared tools. The same selected closure is then captured in
the whole-run dependency snapshot. A readiness failure creates no session.

Assembly remains the deferred-binding exception: whole-run readiness captures
its configured candidate dependency scope but does not pretend that one
candidate is already selected. The Assembly stage selects from that frozen
scope after upstream evidence exists and preflights the selected method before
freezing its attempt.

Whether an LLM endpoint is mandatory is owned by the selected workflow and
[recommendation policy](recommendation-policy.md#readiness-and-evidence). When
required, readiness invokes the configured endpoint and accepts only the exact
parsed JSON object `{"ready": true}`. Empty output, free text, a JSON array, or
an object with a different shape fails the probe. This is a structured-output
capability check, not merely an authentication or network-connectivity check.

This boundary is read-only. Bootstrap's broader configured-catalog validation
and a method's later frozen-dependency verification remain distinct checks.

## Canonical Flow

Every executable method uses the same execution and settlement boundaries:

```text
fixed-stage frozen method roster
  -> (Assembly: upstream evidence -> method recommendation/confirmation)
  -> (parameter recommendation and approval, where applicable)
  -> method-attempt freeze
  -> run
  -> collect
  -> settle method outcome
  -> stage-owned review and commit
```

`freeze -> run -> collect -> settle` is implemented by
`app.execution.lifecycle.build_method_graph`. Parameter recommendation is an
optional preparation capability, not a second execution track.

This lifecycle produces a typed **method outcome** for shared settlement and,
when a Runner starts, an **attempt terminal**. Settlement persists the outcome
as the method's one durable **method disposition**. Neither fact is a
workflow-occurrence outcome. Only the parent settlement boundary records
`stage_completed | stage_skipped | stage_failed` after the stage returns an
explicit `workflow_step_outcome` and every delivery claim verifies. The exact
orthogonal fact domains are owned by
[`data-contract.md`](data-contract.md#45-ledger-is-a-causal-event-stream).

Method disposition is not a duplicate attempt summary. An occurrence may select
a planner-only method or deliberately skip local execution and therefore own no
attempt; a retrying method may own several attempt terminals but only one final
disposition. The occurrence outcome answers whether the stage delivered, not
what happened to each selected method.

For fixed-method stages, selection is already frozen in the occurrence's
`selected_method_names`. Assembly is the single deferred-binding exception:
the roster may be omitted at whole-run freeze, and the Assembly stage must
recommend and validate `assembly_selection` from upstream QC/survey evidence
before binding `primary_method`. Runtime must not treat a displayed candidate
as selected or replace a fixed roster after recovery. The dynamic method must
be resolved from the frozen dependency snapshot, pass stage-time preflight, and
be checked at settlement against `assembly_selection.primary_method`; its
method disposition remains a required durable fact. This distinction makes
`not_started` meaningful without forcing Assembly to guess a method before its
evidence exists.

Method authority is therefore single-tracked. Intake text, planner hints,
legacy drafts, and displayed candidate lists are not method selections; only a
durable `assembly_selection`, confirmed through its required gate, binds the
Assembly method. Any planning-era tool hint is discarded before draft build and
freeze rather than merged into execution state.

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
attempt and the ran status enters collect. Every exit passes through the shared
settlement node. A pure, lifecycle-specific outcome policy reads only the typed
durable cursor/evidence produced by the preceding boundary and returns either a
validated final `MethodOutcome` or no outcome while a retry/review continuation
remains. It never guesses from an unregistered status string. Parent-stage
routing owns subsequent failure, review, retry, and commit decisions.

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
execution evidence, create stage-local lifecycle factories, or write method
dispositions directly. Freeze, run, collect, and the shared no-attempt planning
adapter leave a validated typed cursor/evidence boundary from which the
registered outcome policy returns a `MethodOutcome`. That outcome binds the
exact workflow occurrence, stage, canonical method name, disposition, bounded
reason, and immutable evidence refs. It is an internal settlement value, not a
new `TopLevelState` or `workflow_step_outcome` field.

The shared method-settlement node is the sole disposition writer:

- a final pre-attempt prerequisite failure returns `not_started`;
- a selected executable method deliberately not invoked returns `skipped`;
- a selected planner-only pass-through returns `completed` after its no-process
  contract is applied;
- successful collection of all required work returns `completed`;
- an execution or collection failure returns `failed` only after the method's
  retry/review policy has no continuing path.

Individual failed attempts during a permitted retry remain attempt facts and do
not prematurely settle the method. Ordered batches settle only after the whole
method collection boundary decides its final result. Exact replay returns the
existing disposition; a different result conflicts. Planner/no-attempt paths
use the same settlement node immediately after their explicit decision; stage
modules supply only a pure outcome policy and never call the Ledger writer.
Before writing, shared settlement rejects any method outside the effective
roster. For fixed stages this is the frozen roster; for deferred Assembly it is
the single validated `primary_method` selected by the stage. Before a stage
terminal, shared occurrence settlement requires every effective-roster method,
and no other method, to have exactly one durable disposition. Human blocking
and retry continuation do not run this terminal coverage check.

The successful collect adapter also supplies the evidence required for
`attempt_succeeded`; the run or collection failure boundary supplies
`attempt_failed` evidence only when an attempt actually existed. Attempt facts
remain causally before the final method disposition. Parent settlement alone
owns the occurrence terminal.

## Run Boundary

Before any recovery lookup, dependency check, or process starts, the shared run
node validates the lifecycle status and owner cursor, then requires every
pending frozen attempt to name the exact active `workflow_step_id` for the
lifecycle stage. A missing or stale occurrence identity becomes the lifecycle's
bounded failure update. It does not search for recoverable execution evidence,
invoke the Runner, publish logs, or append attempt lifecycle events. Generic
artifact loaders may inspect historical attempts, but execution may not
reinterpret a historical attempt as the active occurrence.

Only after that gate does the run node load the next frozen attempt through its
verified ref. Stage, method, session, and attempt identities must match. The
working directory must resolve below the
current run's `work/steps/<workflow_step_id>/<stage>/` tree; the declared absolute script path must be
lexically below that working directory and resolve somewhere inside the run;
every expected output must resolve inside the run. The retained script must
exist and match its frozen SHA-256. The Runner executes that exact declared path
with the frozen working directory as cwd. Production local dispatch is direct
argv execution: either `<interpreter> <script_path>` or a `conda run` wrapper
followed by that pair, always with `shell=False`. A non-empty legacy
`FrozenAttemptSpec.env_setup` is unsupported and must fail before either
dispatch path; environment overrides use the typed `env` mapping or a dedicated
frozen setup script instead.

After the occurrence gate and before checking live inputs or invoking the
Runner, the run node searches the
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
own attempt-succeeded facts after successful materialization. An expected
collection failure may record `attempt_failed` if the attempt has not already
terminated and returns a failed stage cursor. It must not write `stage_failed`;
the parent consumes the stage's explicit failed `workflow_step_outcome`.

The Ledger append boundary permits exactly one of `attempt_succeeded` and
`attempt_failed` per immutable attempt ID under its exclusive append lock.
Checking for an earlier success in application code is only an optimization;
it is not the invariant. Exact replay returns the existing terminal, while a
different outcome, identity binding, summary, or ref set conflicts. Legacy
conflicting rows are conservatively projected as failed with an integrity
diagnostic, never by last-event overwrite.

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
9. method disposition, attempt terminal, and workflow-occurrence outcome remain
   distinct facts; no cursor status, attempt result, route marker, or cursor
   advance synthesizes another domain's terminal.
10. the effective occurrence roster and durable disposition set are equal before
    any occurrence terminal is appended; deferred Assembly derives that roster
    only from its validated stage-owned selection.

Parameterized retries use a new attempt ID and an attempt-specific work/output
directory. A completed predecessor remains immutable; a later attempt cannot
reuse its mutable result directory or overwrite its published outputs.
