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
> **Code evidence:** `pyproject.toml`, the implementation modules named by each
> contract, and the tests relevant to the changed boundary.

This document describes how a coordinating agent should steer Gene Agent work.
The coordinator is not expected to write every method implementation. The main
job is to preserve architecture, contracts, sequencing, review quality, and
handoff clarity.

## Coordinator Role

The coordinator owns these responsibilities:

- Apply direct user instructions and preserve the repository engineering policy
  defined by this page.
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
- Reference pages own exact schemas, normative arrays, default values, and
  lifecycle invariants. Topic pages may explain flows and relationships but
  must link to those owners instead of copying a second normative contract.
- `docs/architecture/reference/stage-contracts.md` and
  `docs/architecture/reference/descriptors.md` govern stable domain boundaries.
- Code and tests are the evidence for what is implemented now. They do not
  silently amend an accepted design; a mismatch is recorded as conformance
  debt.
- This page is the single owner of repository-local engineering procedure. Its
  process rules cannot silently amend product behavior owned by another
  architecture contract.

## Change Control

For each non-trivial change:

1. Identify the single architecture owner, assumptions, and success criteria.
2. For design-affecting work, update and settle that owner before behavior code;
   when the user requires design confirmation, implementation starts only after
   that confirmation.
3. Add or update a focused test before behavior code when applicable.
4. Keep edits scoped to the approved task.
5. Run focused verification for the affected boundary and the full regression
   suite when behavior changes.

Comments record only non-obvious rationale. Architecture text and pull-request
descriptions state final behavior and decisions not evident from the diff; they
omit intermediate attempts, rejected alternatives, and never-merged states.

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
question. Do not weaken configuration or add broad exclusions to manufacture a
pass. Until all four gates are green and enforced, record that difference in
[Architecture conformance](conformance.md).

## Documentation Standard

Each current architecture or domain contract has one owning document. Update
that owner in place; do not add phase plans, implementation summaries, handoff
snapshots, acceptance logs, or duplicate design explanations to the working
tree. Keep operational guides executable and current.

Conversation progress, completed-task summaries, test-run logs, and one-off
handoffs belong in the chat or commit/PR description, not in a new Markdown
file. When a temporary document has served its purpose, move any still-current
contract or procedure into its existing owner, remove the temporary file and
its links, and use Git history for retrieval. Do not create an archive directory
or a cleanup report to preserve retired documentation. `conformance.md` retains
only unresolved capability gaps, evidence locations, and closure conditions;
it is not a dated activity log or a local-data inventory.

Audit finding identifiers are review-local coordinates, not architecture
categories. Never create an `F1`/`F2`-style contract, table, or navigation path.
Route each accepted semantic rule into its existing owner; record only a real
implementation-versus-contract gap in `conformance.md`, named by the missing
capability or conflicting representation rather than by the review that found
it. Operator guides and examples may summarize usage, but must link back to the
owner for defaults, precedence, and lifecycle semantics.

For simplification work, classify candidates as `exact clone`,
`semantic duplication`, `duplicate representation`, `dead surface`, or
`intentional symmetry` using code and call-site evidence. Prefer deleting a
source of truth over adding an abstraction. Preserve public compatibility
surfaces unless their owner explicitly changes that contract.
