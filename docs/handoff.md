# Agent Handoff

Current work snapshot for repository takeover. This file is intentionally short
and non-normative. Architecture decisions live in
[`architecture/README.md`](architecture/README.md); implementation history lives
in phase documents and Git.

**Updated:** 2026-07-20

**Current baseline:** `e74b96d` plus the uncommitted unified-resource refactor

## Read Order

1. `docs/architecture/README.md` for specification ownership and conformance.
2. `docs/architecture.md` for the system overview.
3. `docs/architecture/data-v2.md` for run data and checkpoint boundaries.
4. This file for the next local task.
5. `README.md` and `docs/stage_contracts.md` for current executable/domain scope.

## Current Status

- New sessions use the incompatible Data v2 run layout and run-relative,
  hash-verifiable artifact refs.
- Runtime `ExecutionResult` paths now terminate at the publication boundary;
  durable process results use the strict, path-free `ExecutionEvidence` body.
- The shared runner publishes and indexes stdout/stderr once, including failure.
- Assembly and AutoHiC parameter recommendation persist exact model requests,
  raw responses, parsed recommendations, approvals, and compact attempt refs.
- AutoHiC is the only Hi-C execution method; parent routing decides whether the
  stage is entered.
- The diagnostic route tail uses one shared limit of 16 entries.

Data v2 is accepted but only partially conformant. Its code-audited debt table
is the current list: [`architecture/data-v2.md`](architecture/data-v2.md#22-confirmed-conformance-debt).

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
review reruns remain outside this ownership rule. Assembly RNA-seq
recommendation signals now flow from the asset registry through
`AssemblyInputProfile`, not through a pseudo-stage output.

## Next Implementation Slice

The resource handoff, typed stage cursor, frozen-attempt, review persistence,
read-only storage audit, durable SQLite resume, and assembly-to-annotation E2E
paths are implemented. The next slice is based on the 2026-07-20 code audit:

1. remove direct writes to fixed `artifacts/` paths and enforce atomic publish-and-register;
2. unify the three resume transaction implementations and their failure events;
3. enforce whole-`TopLevelState`/active-interrupt budgets, then compact terminal checkpoints;
4. expose the existing storage audit as a CLI; enable deletion only after post-compaction retention-root tests;
5. add the missing raw-read-to-assembly entrypoint E2E and package/CI acceptance.

Detailed evidence and ordering live in
[`architecture/refactor/architecture-contracts.md`](architecture/refactor/architecture-contracts.md#10-后续路线).

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
