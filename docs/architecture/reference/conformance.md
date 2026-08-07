# Architecture Conformance

This page records known differences between accepted architecture and the
current working tree. It is not a compatibility contract and does not weaken
the owning reference specifications.

> **Status:** conformance-debt  
> **Read when:** a design claim and current code appear to disagree.  
> **Comes from:** architecture review and repository verification.  
> **Feeds into:** scoped implementation work that closes a listed gap.  
> **Related topics:** [Data and storage](../topics/data-and-storage.md),
> [Execution](../topics/execution.md),
> [Runtime orchestration](../topics/runtime-orchestration.md), and
> [Bootstrap](../topics/bootstrap.md).  
> **Code evidence:** each item below names its current evidence path.

## Open gaps

### New-run execution has not yet converged on one interactive boundary

The accepted runtime contract requires both structured files and a confirmed
TUI draft to join at `execute_frozen_session`, with a real TTY verified before
freeze and no public non-interactive new-run branch. The current structured
`run` still accepts `--non-interactive`, allows a missing TTY to freeze and
execute without the foreground review loop, and does not render stage progress
directly from the frozen plan and graph transitions.

- Current evidence: `_run`, `_foreground_review_available`,
  `execute_frozen_session`, and `_interactive_gate_loop` in
  `app/entrypoints/run.py`, plus `_freeze_and_execute` in
  `app/entrypoints/tui.py`.
- Contract owners: [Runtime orchestration](../topics/runtime-orchestration.md),
  [TUI and planning](../topics/tui-and-planning.md), and
  [Run data](data-contract.md).
- Closure condition: reject a missing TTY before freeze, remove the public
  non-interactive new-run path, retain direct structured-run freeze without a
  Plan Draft prompt, and make both producers use one attached executor that
  renders progress without publishing another progress store.

### Parameter approval has no countdown resolver

The accepted parameter contract applies the frozen review timeout only after a
schema-3 recommendation and immutable approval request exist. Expiry approves
that exact recommendation through the existing stage decision applier. The
current Assembly, AutoHiC, and T2T `parameter_approval` declarations have no
`timeout_applier`, so they remain mandatory-human gates regardless of a zero or
positive timeout.

- Current evidence: the three `parameter_approval` declarations in
  `app/review/gates.py`, the stage workflows under
  `app/stages/*/parameters/workflow.py`, and foreground timeout dispatch in
  `app/control/lifecycle.py`.
- Contract owners: [Review and resume](../topics/review-and-resume.md),
  [Parameter recommendation](parameter-recommendation.md), and
  [Governance](governance.md).
- Closure condition: register one shared parameter timeout applier for all
  three gates, bind automatic approval to the immutable request and
  recommendation evidence, preserve stage-specific validation, record the
  automatic decision audit, and return verified resolver failures to manual
  review in the same attached terminal.

### Repository quality gates are targets, not a current success claim

The repository intends these commands to be merge gates:

```bash
ruff format --check .
ruff check .
mypy app
pytest -q
```

The working tree must not be described as passing a gate unless that command
returns zero in the revision being described. Historical error counts are not
retained here because they become stale immediately.

- Policy owner: [Governance](governance.md).
- Current evidence: `pyproject.toml` plus the command output for the checked
  revision.
- Closure condition: all four commands return zero and CI or an equivalent
  enforced workflow runs them without exclusions that weaken the declared
  policy.

### Storage garbage collection is audit-only

The run data contract defines retention and garbage-collection safety, but the
implemented `storage-audit` command is read-only. It can project a cleanup
manifest; it does not delete unreachable immutable artifacts, compact the
artifact index after deletion, or reclaim retained method work directories.

- Current evidence: `app/storage/audit.py` and the `storage-audit` CLI path in
  `app/entrypoints/run.py`.
- Contract owner: [Run data](data-contract.md).
- Closure condition: implement and test an explicit safe apply path, or narrow
  the accepted contract to audit and cleanup-manifest generation only.

### Some reports still depend on live state projections

The run-data contract says reports are regenerable projections from
authoritative artifacts and Ledger events. The session summary satisfies that
boundary, but `write_operator_report` requires a caller-supplied compact result
for current stage, supervision, error, interrupt, and route tail. The
stage-owned `assembly_review.md` writer likewise renders from the live Assembly
Review cursor and an indexed packet; it neither embeds the shared source
watermark nor has a dedicated durable-record-only regeneration path. There is
no command or loader that reconstructs either exact projection solely from the
Ledger and indexed artifacts. Despite that, `_reclaimable_candidates` currently
labels every file under `reports/` as a derived report that can be regenerated.
The cleanup manifest is dry-run-only today, but that blanket classification is
not safe input for a future apply path.

- Current evidence: `app/reports/review_report.py` versus
  `app/reports/trace_writer.py`, plus the report helpers in
  `app/stages/assembly_review/nodes.py`, `_reclaimable_candidates` in
  `app/storage/audit.py`, `tests/test_run_trace.py`, and
  `tests/test_assembly_review.py`.
- Contract owner: [Run data](data-contract.md).
- Closure condition: implement and test durable-record reconstruction and
  equivalent watermarks for both projections, or explicitly narrow the owning
  contract so these live-result projections are not promised to be
  independently regenerable; in either case, derive cleanup eligibility from
  proven per-report regeneration support instead of directory membership.

### Run-level replay capability is not declared

The data contract defines `fully_materialized`,
`verified_external_dependencies`, and `audit_only` as the run-level execution
replay vocabulary. `SessionPaths` exposes `control/run.json`, but no production
writer creates that file and no current loader consumes a replay-capability
field. Frozen control inputs and per-attempt dependency records provide the
underlying evidence, but there is no single durable declaration to query.

- Current evidence: `app/storage/paths.py`, `app/storage/frozen_inputs.py`, and
  `app/execution/dependency_snapshot.py`; repository search finds no writer or
  reader of `run_metadata_path` outside its property definition.
- Contract owner: [Run data](data-contract.md).
- Closure condition: publish and validate a run metadata record whose replay
  enum is derived from the frozen dependency/materialization facts, or remove
  the enum and `run.json` target from the accepted contract and path API.

## Update rule

Every entry must identify the accepted owner, current code evidence, and an
objective closure condition. Delete an entry in the same change that closes it;
do not turn this page into a backlog, phase report, or historical status log.
