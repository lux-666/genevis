# Method Execution

> **Status:** implemented
>
> **Read when:** tracing how one selected method becomes a durable process
> attempt, or diagnosing freeze, run, collect, and resume behavior.
>
> **Comes from:** [TUI and planning](tui-and-planning.md),
> [runtime orchestration](runtime-orchestration.md), and, when applicable,
> [parameter recommendation](parameter-recommendation.md).
>
> **Feeds into:** [data and storage](data-and-storage.md), stage collection,
> [review and resume](review-and-resume.md), and adapters such as
> [AutoHiC](autohic.md).
>
> **Contract dependencies:** [method execution lifecycle](../reference/execution-lifecycle.md),
> [execution evidence](../reference/execution-evidence.md),
> [run data](../reference/data-contract.md), and
> [resource handoff](../reference/resource-handoff.md).
>
> **Code evidence:** `app/storage/frozen_inputs.py`,
> `app/execution/dependency_snapshot.py`, `app/execution/lifecycle.py`,
> `app/execution/attempt_owners.py`, `app/execution/method_runner.py`,
> `app/execution/local_runner.py`, `app/execution/evidence.py`,
> `app/execution/__init__.py`, and
> `tests/test_execution_lifecycle.py`.

## Two freeze boundaries

Gene Agent freezes a run twice at different scopes. Treating these as one
operation breaks both provenance and resume semantics.

| Boundary | Happens when | Owns | Repeats? |
|---|---|---|---|
| Whole-run input freeze | The operator confirms the TUI plan, before the parent graph starts | The canonical input manifest, run configuration, and selected machine-dependency snapshot under `control/` | Once for the session; the same request is idempotent and a different request conflicts |
| Method-attempt freeze | A stage has selected a method and completed any required recommendation and approval | One exact retained script, working directory, environment projection, expected outputs, and typed input dependencies | Once per attempt; a run may contain many methods, batches, and retries |

The whole-run freeze prevents an executing session from following later edits
to the intake draft or `config/envs.yaml`. The method-attempt freeze prevents a
resume from rebuilding a command from later code, catalogs, configuration, or
environment state. A retry therefore creates a new method attempt; it does not
modify either the session control snapshot or an earlier frozen attempt.

Final confirmation does not freeze immediately. After the TTY check, a
read-only run-specific readiness pass probes the canonical effective local
method closure and any mandatory LLM endpoint. The same closure is then used by
the whole-run dependency snapshot, and a failure creates no session. Broader
machine-catalog readiness remains the responsibility of
[bootstrap](bootstrap.md); execution still verifies the frozen dependency
selection at the boundary where it is used.

## Shared method flow

Every production method adapter joins the same lifecycle after selection and
optional approval:

```text
stage adapter prepares exact attempt
  -> shared freeze persists it and binds an owner cursor
  -> shared run verifies identity, script, paths, and live input dependencies
  -> Runner starts at most one pending attempt
  -> shared code publishes logs and durable execution evidence
  -> stage adapter collects biological outputs
  -> stage routing reviews, retries, commits, or fails
```

The execution kernel owns process invocation, log publication, evidence
persistence, recovery lookup, and process-failure policy. The adapter owns
command construction before freeze and biological validation/publication after
run. It must not create a private runner path or another execution-evidence
schema. Exact state transitions and failure policies belong to the
[lifecycle contract](../reference/execution-lifecycle.md).

## The production Runner is local

`Runner` is a small dependency-injection protocol. Production calls omit the
test override and therefore instantiate `LocalRunner`, which executes the
frozen script with `subprocess.run` in its declared working directory. Conda
selection, when present, is expressed as `conda run`; otherwise the retained
shell script runs through the local shell path.

“Local” means local to the host running the `gene-agent` process. A real server
run is achieved by starting the same CLI in the server checkout. There is no
production SSH runner, scheduler submission backend, remote-agent transport,
or process reattachment implementation. `FakeRunner` is test support, not a
second production execution mode.

## Runtime result versus durable evidence

`ExecutionResult` and `ExecutionEvidence` are intentionally different types.

- `ExecutionResult` is the immediate process value. It carries local
  `stdout`, `stderr`, and expected-output paths so the shared publisher can
  inspect and move files.
- `ExecutionEvidence` is the path-free durable process body. It contains the
  outcome and verified artifact references for the two logs; the enclosing
  immutable execution record also binds the exact frozen-attempt reference.

Here “path-free” means no bare host-local `Path` fields. Each `ArtifactRef`
still contains its verified run-relative `path_ref`. Biological outputs remain
stage-owned artifacts, and expected-output paths remain part of the frozen
attempt rather than `ExecutionEvidence`.

The runtime model is never dumped into checkpoint state. The conversion occurs
only after stdout and stderr have been published and indexed. See the
[execution-evidence contract](../reference/execution-evidence.md) for the exact
schemas and integrity rules.

## Attempt ownership and resume

Ordinary one-shot methods and ordered batches use the direct attempt cursor.
Parameterized Assembly, AutoHiC, and T2T methods bind their frozen-attempt and
execution refs to the current `ParameterAttempt`. Both owners feed the same run
kernel; ownership changes where compact recovery refs live, not how a process
runs.

On entry, a frozen cursor resumes at run and a ran cursor resumes at collect.
Before starting a process, the run node searches the artifact index for a
unique execution record bound to the same method, attempt, and frozen input.
A verified record advances the cursor without rerunning; missing, ambiguous,
or mismatched evidence fails closed. Ordered direct batches advance one item
per graph transition and checkpoint their completed prefix.

This is replay protection after durable evidence is committed, not
process-level exactly-once execution. A machine failure after the subprocess
exits but before its execution record is committed cannot be reconciled with a
remote scheduler identity, because the implemented runner has no such
identity. That limitation must not be described as implemented reattachment.
