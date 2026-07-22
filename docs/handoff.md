# Agent Handoff

Current work snapshot for repository takeover. This file is intentionally short
and non-normative. Architecture decisions live in
[`architecture/README.md`](architecture/README.md); superseded implementation
and design records live in Git history.

**Updated:** 2026-07-22

**Current baseline:** unified resource handoff, shared execution lifecycle,
atomic artifact publication, unified resume, whole-state budgets, terminal
checkpoint compaction, and read-only storage audit

## Read Order

1. `docs/architecture/README.md` for specification ownership and conformance.
2. `docs/architecture.md` for the system overview.
3. `docs/architecture/execution-lifecycle.md` for executable-method boundaries.
4. `docs/architecture/data-v2.md` for run data and checkpoint boundaries.
5. This file for the current implementation snapshot.
6. `README.md` and `docs/stage_contracts.md` for current executable/domain scope.

## Current Status

- New sessions use the incompatible Data v2 run layout and run-relative,
  hash-verifiable artifact refs.
- Runtime `ExecutionResult` paths now terminate at the publication boundary;
  durable process results use the strict, path-free `ExecutionEvidence` body.
- The shared runner publishes and indexes stdout/stderr once, including failure.
- Every executable production method uses the shared freeze/run/collect graph;
  direct and parameter attempts differ only in compact ref ownership.
- Assembly and AutoHiC parameter recommendation persist exact model requests,
  raw responses, parsed recommendations, approvals, and compact attempt refs.
- AutoHiC is the only Hi-C execution method; parent routing decides whether the
  stage is entered.
- The diagnostic route tail uses one shared limit of 16 entries.
- Durable producers now write below stage `work/` and cross the existing atomic
  publication boundary; already-published shell outputs use registration only.
- All three resume entrypoints use `app.control.resume_transaction`; entrypoints
  retain prompts, decision-file handling, and output formatting only.
- MemorySaver, SqliteSaver, and injected savers all pass through the same
  whole-state budget boundary.
- A terminal run retains one standalone checkpoint after ledger reconstruction
  and artifact-reachability verification; active, blocked, and failed
  verification paths keep their checkpoint history.
- `gene-agent storage-audit` provides read-only JSON audit and dry-run manifest
  output.

## Current Architecture Decision

Checkpoint state is a recovery cursor, not the owner of a frozen command.
Freezing happens before execution and produces an immutable record. The
checkpoint stores its verified ref; resume loads that same record and never
rebuilds a command from current catalogs, configuration, or environment.

The complete frozen runner input lives in one immutable attempt record. Its
script remains at the recorded path under the run's retained `work/` tree.
Resume validates that path and fails closed if an operator manually deleted it;
it does not copy/hash the script for that case and does not regenerate the
command. Sealed manifests remain reserved for retry/review chains that must bind
several independently published facts.

For model-recommended Assembly and AutoHiC execution after human parameter
approval, the compact `ParameterAttempt` is the sole relation owner for
approved/frozen parameters, the command record, the complete frozen runner
input, execution evidence, and review evidence. The stage cursor only selects
the current attempt and retains the ordered chain; method-specific
frozen/execution cursor keys have been removed. Operator-directed Assembly
review reruns remain outside this parameter-attempt ownership rule and use the
shared direct `execution_attempts` cursor. Assembly RNA-seq recommendation
signals now flow from the asset registry through `AssemblyInputProfile`, not
through a pseudo-stage output.

## Remaining Simplification Refactor

The remaining work is limited to reducing duplicate ownership and making the
existing code easier to change:

1. replace duplicate run-root plus `session_id` parameters with one typed run
   context and delete the old signatures;
2. split large modules along existing responsibility and side-effect
   boundaries, deleting moved implementations and duplicate helpers in the
   same change;
3. replace `ask_interactive` string-prefix and field-name inference with
   structured field metadata, then remove the old representation;
4. automate the existing full-suite, durable-resume, CWD and entrypoint checks
   as refactor guardrails without adding a test-only runtime architecture.

Storage audit usage:

```bash
gene-agent storage-audit --session-id <id> --data-dir <data-root> --format json
gene-agent storage-audit --session-id <id> --data-dir <data-root> --format json --dry-run-manifest
```

Detailed evidence and ordering live in
[`architecture/refactor/architecture-contracts.md`](architecture/refactor/architecture-contracts.md#10-剩余减负重构).

## Verification

Use the repository environment:

```bash
git diff --check
conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
```

Run focused tests for the affected stages before the full suite. Static checks
are scoped separately from the repository's historical full-project Ruff/mypy
baseline; do not report the whole project green unless those baselines have
actually been removed.

## Operational Boundary

Real bioinformatics execution happens from the server checkout after `git pull`.
In-agent SSH/remote execution is intentionally unsupported; see
`docs/deployment/mac_dev_server_pull.md`.

Stop and ask before changing stage order, removing a documented stage,
introducing a new top-level persistence backend, adding a heavy dependency,
performing destructive filesystem/Git operations, or running large real-data
tools.
