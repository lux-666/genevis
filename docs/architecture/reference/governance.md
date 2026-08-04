# Architecture Governance

> **Status:** implemented policy; see
> [Architecture conformance](conformance.md) for gates that are not currently
> proven green.  
> **Read when:** changing architecture ownership, documentation, descriptors,
> review policy, or repository-wide gates.  
> **Comes from:** accepted project design and current engineering policy.  
> **Feeds into:** every architecture and implementation change.  
> **Related contracts:** [Descriptors](descriptors.md),
> [Run data](data-contract.md), [Stage contracts](stage-contracts.md).  
> **Code evidence:** `CLAUDE.md`, `pyproject.toml`, and the tests relevant to the
> changed boundary.

This document describes how a coordinating agent should steer Gene Agent work.
The coordinator is not expected to write every method implementation. The main
job is to preserve architecture, contracts, sequencing, review quality, and
handoff clarity.

## Coordinator Role

The coordinator owns these responsibilities:

- Keep `docs/archive/deep-research-report.md` as the biological design source.
- Keep `CLAUDE.md` as the engineering behavior source.
- Keep architecture documents current before large implementation work.
- Split large work into testable slices with explicit exit criteria.
- Make sure state, ledger, artifact, runner, review, and descriptor boundaries
  remain separate.

The coordinator should avoid these behaviors:

- Do not implement a heavy bioinformatics method before its descriptor, inputs,
  outputs, review rule, and artifact contract are clear.
- Do not copy `rna_agent` business terms such as `integration`, `annotation`,
  and `benchmark` into genome-specific code unless they are truly generic.
- Do not put FASTQ/FASTA contents, large reports, generated scripts, or command
  logs in LangGraph state.
- Do not make a top-level graph edge for every biological tool.
- Do not let an LLM write routing, checkpoint, ledger, or lifecycle core logic.

## Sources of Truth

Do not force design authority, current implementation state, biological
rationale, and coordination procedure into one linear ranking:

- Direct user instruction governs the current task.
- Accepted topic and reference nodes under `docs/architecture/` govern intended
  cross-cutting behavior; `docs/architecture/README.md` routes readers to the
  single owner for each boundary.
- `docs/architecture/reference/stage-contracts.md` and
  `docs/architecture/reference/descriptors.md` govern stable domain boundaries.
- Code and tests are the evidence for what is implemented now. They do not
  silently amend an accepted design; a mismatch is recorded as conformance
  debt.
- `CLAUDE.md` and this document govern engineering/coordination procedure.
- `docs/archive/deep-research-report.md` supplies biological rationale, not
  runtime storage or software-state facts.
- Git history retains superseded implementation and design records; the working
  tree keeps current contracts, accepted planned nodes, explicit conformance
  debt, and operational documentation.

## Change Control

For each non-trivial change:

1. State the assumption and success criteria.
2. Add or update a focused test before behavior code when applicable.
3. Keep edits scoped to the approved task.
4. Update the relevant document in the same change.
5. Run focused verification for the affected boundary and the full regression
   suite when behavior changes.

Documentation-only changes do not need full runner tests, but they should keep
the scaffold structure test passing when they add required documents or paths.

### Repository quality gates

The intended repository-wide gates are:

```bash
ruff format --check .
ruff check .
mypy app
pytest -q
```

Do not call a command a passing gate unless it returns zero for the revision in
question. Do not weaken configuration, add broad exclusions, or preserve a
historical error-count baseline to manufacture a pass. Until all four gates are
green and enforced, record that difference in
[Architecture conformance](conformance.md).

## Boundary Rules

### Graph State

`TopLevelState` stores only small control facts and references:

- session and thread IDs
- current stage
- input manifest reference
- asset registry reference
- stage plan reference
- compact interrupt request ref and hash
- bounded diagnostic route tail
- compact `stage_outputs` cursors, which may select the current parameter
  recommendation or execution attempt by ID/ref
- the current logical-resource table in `resource_refs`

Anything large or replay-sensitive belongs outside checkpointed state.

### Ledger

Ledger stores business execution facts:

- attempt ID
- method name
- immutable frozen-attempt ref when it is needed to prove the transition
- status
- error summary
- immutable artifact refs needed to prove a transition
- review event

Ledger is append-oriented. Do not treat it as a mutable cache.

### Asset registry and artifact index

The asset registry stores inventory identity and declaration facts for external
inputs. The artifact index stores identity and integrity metadata for immutable
run-owned artifacts. Between them they record:

- file paths
- file hashes
- file sizes
- artifact type
- stage and method provenance
- compact metadata

The graph state should point to the asset registry and indexed artifact outputs
rather than embedding either record body.

The accepted [run data contract](data-contract.md) governs run-relative paths,
exact fact ownership,
parameter-recommendation persistence, checkpoint budgets, report derivation,
and retention. No stage may introduce a conflicting local convention.

### Descriptor Layer

Descriptors express compatibility and defaults:

- package capability
- method inputs and outputs
- resource hints
- review rules
- human review triggers

Descriptors should be readable by domain experts. Avoid hiding method selection
rules inside Python when a descriptor or YAML policy is enough.

### Script generation and freezing

Stage nodes generate or template execution scripts and freeze them into a
`FrozenAttemptSpec` via the shared `execution/method_runner` helpers. Script
generation and freezing must not mutate:

- top-level graph routing
- state schema
- checkpoint policy
- ledger semantics
- lifecycle commands
- descriptor registry loader

The frozen spec is persisted before execution. The runner consumes that exact
spec (or its verified in-memory hydration), and resume reloads it by ref rather
than regenerating it.

## Review Gate Policy

Use human gates for decisions with biological or irreversible consequences:

- ambiguous asset classification
- explicit operator approval for aggressive long-read filtering (currently a
  frozen Read QC request flag, not a stage-local resume gate)
- assembly quality interpretation
- purge/keep decisions after BUSCO, depth, or Merqury review
- Hi-C contact map review
- accepting a new generated method
- overwriting stable method descriptors

Do not interrupt for ordinary local validation or deterministic file collection.

## Documentation Standard

Each current architecture or domain contract has one owning document. Update
that owner in place; do not add phase plans, implementation summaries, handoff
snapshots, acceptance logs, or duplicate design explanations to the working
tree. Keep operational guides executable and current, and retain superseded
material only in Git history.
