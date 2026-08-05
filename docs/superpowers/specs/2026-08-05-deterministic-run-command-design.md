# Deterministic Run Command Design

**Date:** 2026-08-05
**Status:** approved in conversation; implementation requires specification
review and the normal implementation-planning gate.

## Objective

Restore a single public, deterministic, non-TUI workflow-start command for real
paper-data runs:

```bash
gene-agent run \
  --manifest manifest.yaml \
  --run-config run_config.json \
  --data-dir data \
  [--non-interactive]
```

The command must consume operator-authored structured inputs, validate the
manifest and run config together before workflow execution, freeze the complete
run input contract, and then reuse the existing frozen-session executor. It must
not invoke conversational intake, task-planning LLMs, or the legacy unfrozen
direct-run path.

This change also removes obsolete direct-run compatibility code once repository
reference checks prove that its remaining tests have been migrated to the
frozen-input boundary.

## Accepted product decisions

1. The public command is named only `gene-agent run`. There is no `launch`
   alias and the removed public `ask` command remains removed.
2. `--manifest`, `--run-config`, and `--data-dir` are required.
3. The manifest is the sole owner of `session_id`. The CLI has no
   `--session-id` option.
4. A run is always durable and uses the SQLite checkpointer. The public command
   does not expose a memory-checkpointer option.
5. Without `--non-interactive`, a human gate opens the existing interactive
   review loop. With `--non-interactive`, the run persists the blocked
   checkpoint, prints the existing `inspect`/`review`/`resume` guidance, and
   exits without prompting.
6. A safely persisted `blocked_human` result returns exit code `0`. A terminal
   execution or validation failure returns exit code `1`. Argument-parser errors
   retain argparse's exit code `2`.
7. The command bypasses TUI intake and whole-run planning only. Existing
   stage-owned parameter recommendation, method execution, evidence, and human
   review contracts remain unchanged.
8. Machine configuration continues to resolve through the existing active
   `envs.yaml` configuration. The selected dependency closure is frozen into
   `dependency_manifest.json` before graph execution.

## Public CLI contract

The parser adds `run` to the public command set:

```text
gene-agent run --manifest PATH --run-config PATH --data-dir PATH
               [--non-interactive]
```

No other run-specific switches are added. In particular, the command does not
accept CLI stage overrides, CLI method overrides, `--local-exec`,
`--session-id`, `--checkpointer`, or an alternate environment-config argument.
The structured files remain the only biological and execution-plan inputs.

`--data-dir` is the data-layout root, not an input-data directory. For manifest
session `paper-001`, the run-owned output root is:

```text
<data-dir>/runs/paper-001/
```

Manifest asset paths and filesystem paths declared by the run config may be
absolute or relative to their respective source files. The existing freeze
loaders canonicalize them once before publication.

## Deterministic run-config requirements

The existing strict `RunConfig` schema remains the field and type authority.
The public `run` boundary adds one presence rule: the source JSON must explicitly
contain both of these top-level fields, even when either list is empty:

```json
{
  "requested_stages": [],
  "local_exec": []
}
```

`requested_stages` is the exact initial biological stage plan. `local_exec` is
the exact set of stages authorized to start local tools. Requiring both keys
prevents a missing field from silently becoming full-pipeline routing or
no-process execution. The public boundary checks Pydantic's explicitly supplied
field set; it does not duplicate the full run-config schema.

The run-config examples used for operator and server workflows must be updated
to include both fields and remain schema-valid.

## Preflight and freeze flow

The command uses one fail-closed flow:

```text
parse CLI paths
  -> load and canonicalize manifest
  -> load and canonicalize run config
  -> require explicit requested_stages and local_exec
  -> obtain session_id from the validated manifest
  -> validate manifest/run-config declarations together
  -> validate local assembly method, read type, and genome-size prerequisites
  -> derive and validate dependency stage scope
  -> capture the dependency snapshot
  -> publish input_manifest.yaml, run_config.json, dependency_manifest.json
  -> publish frozen_inputs.json last
  -> execute_frozen_session(session_id, data_dir)
```

No graph invocation or local biological method may begin until every preceding
step succeeds. Invalid or missing biological input files fail during manifest
validation. Invalid JSON, unknown fields, invalid stage names, incompatible
cross-file declarations, missing local-execution parameters, invalid session
identity, and dependency-snapshot failures all fail before executor invocation.

The implementation must extract or promote reusable no-write loader/validator
components from `app.storage.frozen_inputs`; the CLI must not introduce a second
manifest parser or a second run-config schema. At minimum, the same component
must own:

- source-relative path canonicalization;
- `InputManifest` and `RunConfig` model validation;
- manifest/run-config declaration reconciliation;
- the pre-publication assertion that `manifest.session_id` equals the
  `SessionPaths.session_id` being frozen.

`freeze_session_inputs` remains the create-only transaction owner. It may retain
its source-based wrapper for the TUI, but the public `run` path must be able to
reuse its prepared, validated models without weakening the transaction's own
defensive validation.

The dependency snapshot is captured before immutable control publication.
Publication continues to write `frozen_inputs.json` last so a partial write is
never treated as executable authority.

### Dependency-snapshot scope

The public `run` command does not infer or add biological stages. Its dependency
stage scope is exactly the explicit `requested_stages` list, in the supplied
order. `local_exec` must be a subset of that list. Inventory is always part of
parent-graph startup but is not added to the dependency scope because it owns no
executable method.

A file-authored run config does not carry the TUI Plan Draft's generic
`selected_methods` map. The public command therefore freezes a conservative
method-environment superset for each requested stage rather than guessing one
method selection: all configured method overrides whose descriptor-owned stage
is requested, plus the stage environment, default environment, declared
packages, and registered internal helper methods used by that requested stage.
The existing dependency-snapshot closure logic remains the owner of that
expansion. Stage-specific run-config fields still determine runtime method
selection where the current stage contract permits an operator choice.

No upstream biological stage is silently inserted to satisfy a requested
stage. If the manifest and frozen resources cannot satisfy a requested stage,
preflight rejects a statically provable contradiction and the existing stage
contract otherwise blocks or fails with its normal missing-requirement evidence.
This keeps the run plan deterministic while freezing enough machine closure to
avoid a later environment lookup against mutable live configuration.

## Execution and gate behavior

After a successful freeze, `run` calls the existing
`execute_frozen_session(session_id, data_dir, checkpointer="sqlite",
interactive_gate=...)` boundary.

- Default mode passes `interactive_gate=True` and uses the existing interactive
  decision loop.
- `--non-interactive` passes `interactive_gate=False`. A human gate remains
  `blocked_human`, the SQLite checkpoint and immutable review request remain
  durable, and the command prints how to continue with existing commands.
- Repeating `run` with the same session and byte-equivalent canonical inputs is
  idempotent and continues an existing checkpoint where supported.
- Repeating it with different manifest/run-config content, a different
  dependency scope, a partial freeze, or tampered frozen files fails closed and
  never overwrites the existing run.
- The executor continues to take only session identity and data root. It does
  not accept arbitrary alternate frozen file paths.

## Legacy cleanup boundary

The restored public command must not revive the old unfrozen executor semantics.
Implementation must perform a repository-wide reference audit and remove the
following once their focused tests have been migrated:

- `_run_session` branches that accept arbitrary source manifest/run-config
  paths, generate or reconcile CLI session IDs, merge CLI `local_exec`, or call
  mutable `control/manifest.yaml` materialization;
- `_peek_manifest_session_id`, whose deliberate error swallowing is unsuitable
  for the new validated public boundary;
- `app.schemas.entry.EntryRequest` if the frozen executor no longer references
  it;
- `app.storage.materialization` if the final reference audit confirms that it
  has no remaining production or test contract after its useful relative-path
  cases are migrated to frozen-input tests;
- tests and documentation whose only purpose is to assert that public `run` was
  removed or that unfrozen direct execution remains supported.

The graph-execution and state-seeding body currently inside `_run_session`
remains live. It should be retained as a frozen-only internal component, renamed
or reshaped so its signature cannot express the deleted unfrozen behavior.

The cleanup must retain:

- `execute_frozen_session`, execution locking, checkpoint recovery, and frozen
  hash verification;
- `generate_session_id`, because the TUI still uses it for new and rerun drafts;
- `app.entrypoints.ask_planner`, because it remains TUI planning code rather
  than a public `ask` command;
- `SessionPaths.manifest_path` or other broadly used test/stage fixture paths
  unless a separate complete reference audit proves their removal belongs to
  this change. No repository-wide storage-layout refactor is authorized.

## Error reporting

All public-run failures use concise `error:` messages on stderr and return `1`.
Messages must identify the failing boundary without dumping secrets or entire
input documents. Required categories are:

- manifest missing, unreadable, malformed, or schema-invalid;
- manifest missing or invalid `session_id`;
- run config missing, unreadable, malformed JSON, schema-invalid, or missing
  explicit `requested_stages`/`local_exec`;
- cross-file input declaration failure;
- local method/read-type/genome-size prerequisite failure;
- dependency snapshot or package configuration failure;
- frozen-input conflict, tampering, partial publication, or duplicate active
  executor;
- graph terminal failure.

An input validation error must not call `execute_frozen_session`, create Ledger
events, or start a local method. Creating the v2 data-layout/run directories as
part of guarded freeze setup is not considered workflow execution, but the
pre-publication manifest/session identity assertion must prevent immutable files
from being written under the wrong run ID.

## Tests

Focused tests must cover:

1. top-level help advertises `run`; `ask` remains rejected;
2. parser acceptance of exactly the approved arguments and rejection of the
   removed `--session-id` and unsupported extra run switches;
3. manifest-owned session identity and output placement below
   `<data-dir>/runs/<session_id>`;
4. manifest and run-config parsing occurs before freeze/executor invocation;
5. missing explicit `requested_stages` or `local_exec` fails before publication;
6. dependency scope equals `requested_stages`, rejects `local_exec` stages not
   in that list, adds no implicit biological stage, and snapshots every
   configured method/internal-helper environment owned by a requested stage;
7. missing asset paths and cross-file declaration conflicts fail before graph
   invocation;
8. manifest/session mismatch is rejected before immutable publication;
9. a valid run publishes all four frozen-control files and calls the executor
   with SQLite;
10. default mode passes `interactive_gate=True`;
11. `--non-interactive` passes `interactive_gate=False`, persists a
    `blocked_human` checkpoint, returns `0`, and prints explicit `inspect`,
    `review`, and `resume` guidance;
12. repeated identical invocation is idempotent; changed or tampered inputs fail
    closed;
13. relative manifest and run-config paths retain their current
    source-relative canonicalization through the frozen-input component;
14. no repository references remain to deleted legacy symbols or modules;
15. TUI freeze/start/resume tests continue to pass, proving the shared freeze
    component was not forked or weakened.

Focused entrypoint, frozen-input, TUI, configuration-example, and affected
materialization tests run first. The final implementation acceptance then runs
the repository's declared formatting, lint, typing, and test gates without
weakening configuration or hiding unrelated baseline failures.

## Documentation changes

The same implementation change updates every current product-boundary claim:

- root `README.md`: present `run` as the deterministic structured-input entry
  and TUI as an optional conversational entry;
- `docs/architecture/topics/system-map.md`: list both supported new-run
  boundaries;
- `docs/architecture/topics/runtime-orchestration.md`: document parser dispatch,
  public preflight/freeze, and the shared frozen executor;
- `docs/architecture/topics/tui-and-planning.md`: retain the TUI flow without
  claiming it is the only new-run path;
- `docs/architecture/reference/data-contract.md`: remove any claim that
  `manifest.yaml` materialization remains an execution authority if the legacy
  materialization module is deleted;
- `docs/architecture/langgraph-implementation-map.md`: add the public-run
  preflight/freeze boundary and verification evidence;
- deployment/operator pages and `docs/examples/`: show executable deterministic
  commands and complete run configs with explicit stage fields.

Historical explanations of why `run` was previously removed are not retained in
current architecture pages; Git history owns superseded product decisions.

## Non-goals

This change does not:

- add `launch`, restore public `ask`, or add a batch scheduler;
- make a single JSON replace the biological input manifest;
- introduce CLI overrides for stages, methods, parameters, or local execution;
- disable existing stage-level LLM recommendations or human review;
- add remote execution, SSH, Slurm, or another Runner implementation;
- migrate or overwrite historical sessions;
- redesign the run directory, artifact registry, Ledger, checkpoint, or method
  attempt lifecycle;
- remove TUI support.

## Acceptance criteria

The change is accepted only when:

- the exact approved command starts a valid frozen session without entering TUI;
- both source files are fully validated together before graph execution;
- the run uses manifest-owned identity, required data root, SQLite durability,
  and the approved interactive/noninteractive gate behavior;
- public run-configs cannot omit `requested_stages` or `local_exec`;
- all immutable frozen inputs and the dependency snapshot are verified before
  execution;
- the old unfrozen public/direct-run compatibility path and its now-unreferenced
  helpers are removed, while live TUI and frozen-executor components remain;
- focused tests pass with completed exit evidence;
- changed files match this specification, documentation is synchronized, and no
  unrelated refactor or dependency is introduced.
