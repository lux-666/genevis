# Execution Evidence

**Status:** Accepted

**Accepted:** 2026-07-16

**Implementation:** Process-result boundary implemented

## Decision

Process execution has one durable representation. Runtime code may use local
paths while a process is running, but no checkpoint or durable execution record
may persist those paths.

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
  "attempt_id": "session:stage:attempt-000",
  "success": true,
  "return_code": 0,
  "status": "ok",
  "status_detail": "",
  "error": "",
  "stdout_ref": {
    "schema_version": 2,
    "artifact_id": "...",
    "artifact_type": "command.stdout",
    "path_ref": "artifacts/stage/logs/attempt-000.invocation.stdout.log",
    "sha256": "...",
    "size_bytes": 0
  },
  "stderr_ref": {
    "schema_version": 2,
    "artifact_id": "...",
    "artifact_type": "command.stderr",
    "path_ref": "artifacts/stage/logs/attempt-000.invocation.stderr.log",
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
- A stage may wrap the canonical result body in a stage-specific immutable
  record, but it may not change or extend the body schema.
- For model-recommended parameterized execution after human approval, current
  and historical execution-record refs live only on the corresponding
  `ParameterAttempt`; the stage cursor selects an attempt by ID and does not
  duplicate a method-specific execution cursor. Ordinary methods, ordered
  batches, and operator-directed Assembly reruns bind their frozen and execution
  refs through the shared typed `execution_attempts` cursor. Small immutable
  projections such as success, return code, or a bounded error may be used for
  routing and display; the execution record remains authoritative.
- Collect resolves refs only when it needs log bytes. It registers biological
  outputs, reports, and other stage-owned artifacts, but never registers the
  runner logs again.
- Candidate and review records may project the log `path_ref` for display, but
  an absolute or work-directory path is never durable evidence.

The frozen runner input is a separate pre-execution fact. Resume loads it from
its immutable ref; it must not regenerate a command from current catalogs,
configuration, or environment. The record contains the runner's paths,
environment projection, expected outputs, and Conda selection. Its script path
points into the retained run `work/` tree. If that file is manually removed,
resume fails closed; no script hash, duplicate script artifact, or command
regeneration is required. Data v2 owns that attempt boundary.

For model-recommended Assembly and AutoHiC execution, the same
`ParameterAttempt` owns both this `frozen_attempt_ref` and the later
`execution_ref`. Sealing an attempt binds both refs into its immutable manifest,
so advancing the current attempt during a retry cannot erase which runner input
produced an earlier result. This ownership rule does not include an
operator-directed Assembly review rerun, which uses the direct-attempt owner
defined by [`execution-lifecycle.md`](execution-lifecycle.md).

## Cutover

Data v2 has no legacy-session read or migration contract. Loaders therefore
fail closed on an old path-based result. They do not infer refs from paths, add
dual reads, or write both formats. A workflow that must preserve an old run does
so outside the v2 resume contract.

## Acceptance criteria

1. Every durable execution result validates as `ExecutionEvidence`.
2. No stage serializes `ExecutionResult.model_dump()` into checkpoint state.
3. No stage reads durable `stdout_path`, `stderr_path`, or `output_paths`.
4. Missing, mismatched, or checksum-invalid stdout/stderr refs fail closed.
5. Each command log has one artifact-index registration, owned by the shared
   runner path.
