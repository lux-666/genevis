# Execution Evidence Contract

> **Status:** implemented.  
> **Read when:** runtime results, persisted process outcomes, logs, or result
> cursors must be distinguished exactly.  
> **Comes from:** [Execution](../topics/execution.md).  
> **Feeds into:** [Data and storage](../topics/data-and-storage.md), review, and
> recovery.  
> **Related contracts:** [Execution lifecycle](execution-lifecycle.md),
> [Run data](data-contract.md).  
> **Code evidence:** `app/schemas/execution.py`, `app/execution/evidence.py`,
> `app/storage/publish.py`.

## Contract

Process outcomes have one canonical durable representation. Runtime code and
stage cursors may use local/absolute paths while a process is running, but the
canonical execution-evidence body does not persist `ExecutionResult` path
fields. Frozen command inputs and some stage-local projections remain separate
runtime/dependency records.

```text
LocalRunner -> ExecutionResult (runtime paths)
            -> publish/index stdout and stderr once
            -> ExecutionEvidence (ArtifactRef only)
            -> immutable execution record
            -> checkpoint stores its ArtifactRef and optional bounded projections
```

`ExecutionResult` is a runtime value. Its `stdout_path`, `stderr_path`, and
`output_paths` fields exist only so the runner and immediate publisher can move
or inspect files. It is never serialized directly into durable state.

`ExecutionEvidence` is the canonical durable result body:

```json
{
  "attempt_id": "session:<workflow_step_id>:attempt-000",
  "success": true,
  "return_code": 0,
  "status": "ok",
  "status_detail": "",
  "error": "",
  "stdout_ref": {
    "schema_version": 2,
    "artifact_id": "...",
    "artifact_type": "command.stdout",
    "path_ref": "artifacts/steps/<workflow_step_id>/<stage>/attempts/attempt-000/invocation.stdout.log",
    "sha256": "...",
    "size_bytes": 0
  },
  "stderr_ref": {
    "schema_version": 2,
    "artifact_id": "...",
    "artifact_type": "command.stderr",
    "path_ref": "artifacts/steps/<workflow_step_id>/<stage>/attempts/attempt-000/invocation.stderr.log",
    "sha256": "...",
    "size_bytes": 0
  }
}
```

The model forbids unknown fields. In particular, `stdout_path`, `stderr_path`,
and `output_paths` are invalid durable fields rather than compatibility aliases.
The optional bounded failure summary is owned by `error`; stderr remains the
authoritative process output.

## Ownership and lifecycle

- The shared method runner publishes stdout and stderr into the run layout and
  registers both index records immediately after execution, including failure.
- The execution-evidence boundary converts those indexed logs to `ArtifactRef`
  values and validates both refs before writing or loading the immutable
  execution record.
- The outer execution record binds the canonical body to the exact
  `frozen_attempt_ref` and records whether the lifecycle encountered a boundary
  failure. These fields do not extend `ExecutionEvidence` itself.
- A stage may wrap the canonical result body in a stage-specific immutable
  record, but it may not change or extend the body schema.
- For model-recommended parameterized execution after human approval, current
  and historical execution-record refs live only on the corresponding
  `ParameterAttempt`; the stage cursor selects an attempt by ID and does not
  duplicate a method-specific execution cursor. Ordinary methods and ordered
  batches bind their frozen and execution refs through the shared typed
  `execution_attempts` cursor. Small immutable
  projections such as success, return code, or a bounded error may be used for
  routing and display; the execution record remains authoritative.
- Collect resolves refs only when it needs log bytes. It registers biological
  outputs, reports, and other stage-owned artifacts, but never registers the
  runner logs again.
- Candidate and review records may project the log `path_ref` for display. An
  absolute or work-directory path is not canonical execution evidence, although
  some stage-local checkpoint cursors still retain such runtime projections.

The frozen runner input is a separate pre-execution fact. Resume loads it from
its immutable ref; it must not regenerate a command from current catalogs,
configuration, or environment. The record contains the runner's paths,
environment projection, expected outputs, and Conda selection. Its script path
is an absolute retained path below the attempt work directory and binds a
SHA-256 digest. The working directory and expected outputs are constrained to
the current run; a run-local script symlink may resolve to an immutable artifact
inside that run. Missing or changed script bytes, path escape, or checksum drift
fails closed. Resume never regenerates the command from current configuration.

For model-recommended Assembly, AutoHiC, and T2T execution, the same
`ParameterAttempt` owns both this `frozen_attempt_ref` and the later
`execution_ref`. Sealing an attempt binds both refs into its immutable manifest,
so advancing the current attempt during a retry cannot erase which runner input
produced an earlier result.

## Compatibility Boundary

Data v2 has no legacy-session read or migration contract. Loaders therefore
fail closed on an old path-based result. They do not infer refs from paths, add
dual reads, or write both formats. A workflow that must preserve an old run does
so outside the v2 resume contract.

## Enforced Constraints

1. Every durable execution result validates as `ExecutionEvidence`.
2. No stage serializes `ExecutionResult.model_dump()` into checkpoint state.
3. No stage reads durable `stdout_path`, `stderr_path`, or `output_paths`.
4. Missing, mismatched, or checksum-invalid stdout/stderr refs fail closed.
5. Each command log has one artifact-index registration, owned by the shared
   runner path.
6. Each execution record binds exactly one frozen-attempt ref; recovery requires
   that exact dependency and rejects ambiguous records.
