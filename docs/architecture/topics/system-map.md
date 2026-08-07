# System Map

> **Status:** implemented, with planned and debt nodes linked explicitly.  
> **Read when:** entering the architecture for the first time or deciding which
> subsystem owns a change.  
> **Comes from:** operator intent, registered inputs, descriptors, and machine
> configuration.  
> **Feeds into:** [TUI and planning](tui-and-planning.md),
> [runtime orchestration](runtime-orchestration.md),
> [method execution](execution.md), and [data and storage](data-and-storage.md).  
> **Contract dependencies:** [stage contracts](../reference/stage-contracts.md),
> [run data](../reference/data-contract.md), and
> [architecture governance](../reference/governance.md).  
> **Code evidence:** `app/entrypoints/run.py`, `app/workflow.py`,
> `app/graph/builder.py`, `app/schemas/state.py`, `app/execution/lifecycle.py`,
> and `app/storage/paths.py`.

## What the system is

Gene Agent is a durable genome-workflow coordinator with two public start
boundaries that converge on the same nested lifecycles:

```text
structured manifest + run config --\
                                -> whole-run input freeze
optional mutable TUI draft -----/
  -> parent graph routes one compiled stage subgraph
  -> stage selects and prepares a method
  -> one method attempt is frozen, run, and collected
  -> evidence is published
  -> review either advances, retries, blocks, or terminates
```

The parent LangGraph is deliberately thin, but it is not side-effect free. Its
controller routes by compact state and also records stage/session Ledger events
and terminal reports. Biological method choice and output interpretation stay
inside stage subgraphs. Process invocation stays inside the shared execution
kernel.

## Three graph scales

| Scale | Owner | Responsibility |
|---|---|---|
| Parent graph | `app/graph/` and `app/workflow.py` | Dispatch the active frozen occurrence, advance its cursor, and enforce terminal boundaries |
| Stage subgraph | `app/stages/<stage>/` | Resolve inputs, choose/prepare methods, collect outputs, review, and commit logical resources where applicable |
| Method lifecycle | `app/execution/` plus a stage adapter | Persist one exact attempt, run at most one pending process per transition, publish evidence, and return to stage collection |

Stages share boundary vocabulary, not one mandatory internal topology. A stage
that only registers assets should not be documented as if it has the same nodes
as a parameterized AutoHiC attempt.

## Module ownership

| Module | Owns |
|---|---|
| `app/entrypoints/` | Public structured `run` CLI, optional mutable TUI/planning UI, shared whole-run freeze handoff, lease-scoped inspect/review/resume surfaces, and best-effort terminal presentation |
| `app/workflow.py` and `app/graph/` | Stage registration/default planning, frozen-occurrence routing, checkpointer construction, and parent lifecycle event boundary |
| `app/domains/` | Stage-neutral biological rules and shared typed input interpretation without workflow side effects |
| `app/stages/` | Compiled biological subgraphs, method routing, command preparation, collection, review, and resource commit |
| `app/llm/` | Strict model-facing DTO rendering, invocation, and response-shape parsing; not approval, routing, or persistence ownership |
| `app/schemas/` | Cross-module state, plan, descriptor, artifact, execution, and manifest contracts |
| `app/execution/` | Attempt freeze/run/collect kernel, Runner boundary, environment selection, and durable process evidence |
| `app/storage/` | Run layout, immutable publication/index, Ledger, frozen inputs, and durable evidence records |
| `app/review/` | Gate declarations, allowed decisions, occurrence validation, timeout-request normalization, immutable request/intent/application handling, and registered typed prepare/apply dispatch |
| `app/control/` | Shared review-status projection, lease-checked resume transaction coordination, inspect, authoritative terminal finalization, and lifecycle recovery |
| `app/reports/` | Best-effort operator-status and session-trace projections without control authority; only the session summary currently has a durable-record-only regeneration path |

These modules connect through typed contracts and refs. Their directory order
is not a pipeline, and no module may create a private replacement for an owner
listed in this table.

## Durable fact owners

The checkpoint is a recovery cursor, not a database of the workflow. Current
logical files move through `resource_refs`; original input identity stays in
the asset registry; immutable bodies and produced files live in artifacts; the
artifact index proves identity and bytes; the Ledger records causal events and
the canonical terminal singleton; the shared review-status projection governs
whether a decision may be supplied or an existing intent must be recovered;
reports are regenerable views where supported. Follow
[Data and storage](data-and-storage.md) when deciding where a new fact belongs.

## Current product boundary

The stage registry contains inventory, read QC, genome survey, assembly,
assembly deduplication, assembly review, Hi-C scaffolding, GapCloser, T2T
finishing, and Base Polish. A run's requested-stage list is instead a literal,
ordered sequence of occurrences: stages may be repeated and need not follow
registry order. Freeze adds one implicit inventory occurrence as
`step-000-inventory`, then assigns every requested occurrence a position-based
`step_id`. The frozen `workflow_steps` sequence—not registry order—is runtime
authority. Before freeze, sequence validation rejects an unclosed Assembly or
T2T Review boundary and never inserts Review; a refining occurrence may become
the handoff Review closes. GapCloser and Base Polish remain manual-only
placeholders; an occurrence of either fails closed. Exact rules belong to
[Runtime orchestration](runtime-orchestration.md) and
[Stage contracts](../reference/stage-contracts.md).

Real/operator new runs enter through `gene-agent run --manifest PATH --run-config
PATH --data-dir PATH`. The manifest solely owns
`session_id`; `data-dir` is the layout root, so outputs live under
`<data-dir>/runs/<session_id>/`. The command preflights both documents, freezes
them only while stdin and stdout are attached to real TTYs, then executes the
exact requested occurrence sequence and dependency closure with SQLite. Its
run config must explicitly contain a `requested_stages` JSON
list of `{stage, allow_local_execution}` objects; the boolean is local-process
authorization for that particular occurrence. `requested_stages: null` is
rejected while `[]` is a valid empty biological-stage plan. `gene-agent tui`
remains an optional conversational intake and planning path into that same
freeze/executor.
Other public commands inspect,
audit, review, resume, or bootstrap. Production method execution is a local
subprocess on the CLI host; running on a server means starting the same CLI in
the server checkout, not using an in-agent SSH or scheduler runner.

## Navigate by edge

- User request, plan editing, and whole-run freeze:
  [TUI and planning](tui-and-planning.md).
- Parent routing, stage entry, and terminal behavior:
  [Runtime orchestration](runtime-orchestration.md).
- Frozen commands and process evidence: [Method execution](execution.md).
- Human blocks and durable continuation:
  [Review and resume](review-and-resume.md).
- Exact contracts: follow only the reference links named by the topic page.
- Accepted but unavailable TUI inputs:
  [TUI midstage entry](tui-midstage-entry.md).
- Known mismatches: [Architecture conformance](../reference/conformance.md).
