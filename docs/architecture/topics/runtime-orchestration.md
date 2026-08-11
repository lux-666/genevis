# Runtime Orchestration

> **Status:** implemented.
> **Read when:** tracing CLI dispatch, frozen-session startup, parent routing,
> stage selection, or terminal behavior.  
> **Comes from:** a structured public run or [TUI and planning](tui-and-planning.md), then a validated whole-run frozen contract.
> **Feeds into:** compiled stage subgraphs, [method execution](execution.md),
> [review and resume](review-and-resume.md), and terminal reports.  
> **Contract dependencies:** [stage contracts](../reference/stage-contracts.md),
> [run data](../reference/data-contract.md), and
> [resource handoff](../reference/resource-handoff.md).  
> **Code evidence:** `app/entrypoints/run.py`, `app/workflow.py`,
> `app/graph/builder.py`, `app/graph/routing.py`,
> `app/control/lifecycle.py`, and `app/graph/checkpoint_compaction.py`.

## Public entry boundary

The installed `gene-agent` command dispatches to `app.entrypoints.run:main`.
Real/operator new work uses exactly:

```text
gene-agent run --manifest PATH --run-config PATH --data-dir PATH
```

All three paths are required. The manifest solely owns `session_id`; the CLI
does not expose session, checkpointer, local-execution, launch, or ask
overrides. `data-dir` is the layout root and the run lives at
`<data-dir>/runs/<session_id>/`. The handler validates and canonicalizes both
source documents and requires an explicit `requested_stages` JSON list. Each
item is `{ "stage": "<stage>", "allow_local_execution": <boolean> }`.
The list is an ordered occurrence sequence: it preserves operator order, may
depart from registry order, and permits repeated stage names, subject to the
sequence validation below. `allow_local_execution` authorizes only that
occurrence's process execution. `requested_stages` must not be `null`;
`[]` is the valid empty biological-stage plan. The handler freezes that exact
sequence plus its conservative dependency closure.
It publishes `input_manifest.yaml`, `run_config.json`, and
`dependency_manifest.json` before publishing `frozen_inputs.json` last.

RunConfig validation calls `analyze_review_sequence` before freeze. It rejects a
plan that leaves an Assembly or T2T boundary without the required
`assembly_review` closure; it never inserts, reorders, or otherwise repairs a
Review occurrence. The closure is required before the next Assembly, T2T,
manual-only boundary, or termination. An assembly-refining occurrence reached
while that boundary remains open can become the exact handoff Review closes.

`gene-agent tui` is an optional conversational intake/planning boundary that
uses the same frozen publication and executor. Other public subcommands are
`inspect`, `storage-audit`, `resume`, `review`, and `bootstrap`.

`execute_frozen_session` validates the frozen control documents, obtains a
per-session execution lock and active `FrozenExecutionLease`, creates the
Ledger session, constructs the small
`TopLevelState`, projects run-config values into their owned stage cursors,
builds the parent graph with the resulting executor-owned `FrozenInputBundle`,
and invokes it with the session ID as the LangGraph thread ID. Inventory
validation and asset registration consume this already verified in-memory
manifest, RunConfig, canonical declarations, and run-owned paths; they do not
reopen an unchecked control file. The bundle is closure context, not a graph
channel or checkpoint payload. A new process validates again before building
its graph. The public run always uses SQLite. Every new structured run and
confirmed TUI draft requires stdin and stdout to be real TTYs before freeze;
missing capability fails without publishing frozen inputs. An already-frozen
blocked checkpoint remains recoverable through `inspect`, `review`, and
`resume`, but it is not a second non-interactive new-run path.

The frozen RunConfig review policy defaults to
`review_policy.max_blocked_seconds: 300` and permits per-stage overrides only
for stages registered with the review registry. `-1` waits indefinitely; `0`
immediately normalizes the gate's eligible automatic request; a positive value
starts an Enter-claim countdown that uses the same registered timeout-request
normalizer at expiry. The timer never chooses from registry defaults. Assembly
Review keeps its evidence-qualified LLM recommender, assembly route selection
uses its registered recommendation normalizer, and Assembly, AutoHiC, and T2T
parameter approval share a normalizer that accepts only the persisted
recommendation bound to the immutable request. Gates without a normalizer remain
manual. The resume transaction receives the active lease and a
`ProjectionBoundary`; it may fall back to a manual decision only before intent
commit (`awaiting_decision`). A committed automatic intent is recovered with
`request=None`, never replaced by a manual decision. Identity or integrity
drift fails closed.
This mechanism exists only while the originating run is foreground and
interactive. Enter cancels it and opens manual prompts. Ctrl-C, EOF, process
exit preserve a durable human block; there is no daemon/background timer.
Starting the frozen session again
does not invoke an already-blocked checkpoint or restore its timer. Subsequent
`review` and `resume` commands are manual and must not infer a deadline from the
original block time. The public interfaces have no strict/auto review mode or
resume-timeout override.

## Unified interactive execution

The accepted new-run boundary has two different pre-freeze producers but only
one post-freeze execution path:

```text
structured run files -> validate -> freeze --+
                                             +-> execute_frozen_session
TUI intake -> Plan Draft -> confirm -> freeze-+
```

`gene-agent run` treats its manifest and RunConfig as the already-settled
draft. It does not enter the TUI Plan Draft editor or ask for a second
confirmation. Invalid files fail before freeze; valid files freeze and proceed
directly to execution. The TUI remains the path that repeatedly gathers,
checks, describes, edits, and confirms a mutable draft before creating the same
frozen contract.

Every new run requires stdin and stdout attached to a real TTY, checked before
freeze. The public new-run contract has no non-interactive execution
branch. `review` and `resume` remain recovery tools after an interrupted durable
session; they are not a second normal execution lifecycle.

Several contigs or projects remain several independent runs. An external caller
creates one manifest and RunConfig per unit, assigns a collision-free
`session_id`, allocates one terminal per process, and invokes the same command
N times. The product does not add a batch scheduler, cross-session checkpoint,
or durable aggregate-progress owner; a caller may combine read-only inspection
results outside the runs.

After freeze, both producers call the existing `execute_frozen_session`
boundary. That boundary owns one terminal experience for stage progress,
review briefs, Enter-claim countdowns, manual decisions, automatic timeout
decisions, and terminal summaries. Entry points must not implement parallel
gate or progress loops.

Attached progress is rendered from the scalar-only `progress_projection_input`
derived at the parent-controller transition. A corresponding Ledger occurrence
event is committed before its durable transition is presented. Detached
inspection reads the verified frozen workflow plan, Ledger occurrence events,
SQLite checkpoint availability, and the execution progress marker/lock on
demand; checkpoint contents do not independently define status. Neither path
publishes a progress artifact, progress table, background watcher, event bus,
or second progress authority.

## Parent graph

The compiled parent shape is static:

```text
START -> route_controller -> selected stage subgraph
                            -> route_controller -> ... -> END
```

Before graph entry, the frozen RunConfig is projected into `workflow_steps`.
Inventory is always the implicit first occurrence (`step-000-inventory`) and
each requested item becomes the next position-derived ID, for example
`step-001-t2t`, `step-002-assembly_review`, and `step-003-t2t`. This immutable
sequence is the sole runtime routing authority. The controller advances one
position at a time, stops on a human block or terminal failure, and never
appends, reorders, deduplicates, or fills in stages.

Each frozen `assembly_review` step also contains
`review_target: {resource_type: "assembly", producer_step_id: "..."}`. The
producer ID is the exact earlier occurrence selected by the sequence analysis,
not merely a stage name. Before candidate loading, Assembly Review verifies the
central assembly `ResourceRef.source_stage` and `.workflow_step_id` against that
target.

`app.workflow.STAGE_ORDER` is a registration/default-planning/display aid. It
is not runtime order or a successor table. `recommended_workflow_template` is
a separate planning helper: the Assembly template appends `assembly_dedup`
followed by `assembly_review`, and the T2T template includes that same segment
before scaffolding and repeats Review after `t2t`. These are recommendation
defaults, not RunConfig requirements; neither helper rewrites a literal
RunConfig. The controller does not inspect FASTQ contents, choose an assembler,
build a command, or interpret biological metrics.

The controller wrapper is thin but not pure. It writes only occurrence-scoped
stage-entered, explicit stage-skipped, stage-completed, and stage-failed Ledger
facts plus the scalar progress projection input. It does not write
`session_terminal` or generate
operator reports at blocked or failed boundaries. A stage-local skip, when a
stage contract explicitly creates one, remains distinct from the parent: the
parent never synthesizes skip events for absent occurrences. Any architecture
description claiming that the parent graph has no side effects is incorrect.

## Stage boundary

Each domain node in the parent is a compiled stage subgraph. Stages may resolve
logical resources, plan registered methods, enter the shared method lifecycle,
publish biological artifacts, open review gates, and commit a new resource.
Those are available boundaries, not a mandatory identical sequence for all ten
stages.

The parent registers ten domain stages explicitly in `app/graph/builder.py`; the
subgraphs are composable but there is no dynamic plugin registry. `gapcloser` and
`base_polish` are
manual-only placeholders: automatic recommendations omit them, while a frozen
occurrence of either terminates with `blocked_not_implemented`. An absent stage
has no runtime occurrence and is not skipped. `None` is only an unfinished
planner/TUI draft state; it is never executable semantics and public
`gene-agent run` rejects JSON `null` before freeze.

`workflow_steps` is the frozen runtime projection of the operator plan. Resume
restores and applies only the decision bound to the interrupted occurrence; it
does not rewrite the remaining sequence or edit the frozen run config.

`stage_seeds` are immutable stage-scoped RunConfig projections. Every repeated
occurrence starts from a fresh copy of the same stage seed; there is no
occurrence-specific method-configuration seed and no reuse of a prior
occurrence's completed cursor.

When the manifest declares `assembly_fasta_registered`, Inventory registers it
as the central assembly resource. `assembly_review` is the recommended first
analytical occurrence, but public `gene-agent run` does not reject, reorder, or
otherwise rewrite the frozen occurrence sequence according to `STAGE_ORDER`.
Every occurrence that is reached validates the resources its stage contract
requires; omitted stages have neither an occurrence nor a synthetic skip.

## Stop and terminal behavior

`blocked_human` stops graph progression with a compact interrupt that points to
an immutable request. `failed_terminal` stops the run and records bounded error
evidence. A successful route reaches `done` and terminal completion.

Only completed or failed sessions are eligible for authoritative terminal
finalization. After proving the terminal checkpoint, the authoritative finalizer
writes or reuses the one canonical `session_terminal` Ledger singleton, then
attempts checkpoint compaction. Compaction preserves the latest terminal
checkpoint and does not delete immutable artifacts; a compaction failure is an
operational warning, not a different lifecycle outcome. `ProjectionBoundary`
generates reports and workflow-state projections as best-effort presentation
work: ordinary projection exceptions become bounded warnings and cannot
contaminate lifecycle, review status, checkpoint, or terminal authority. The
remaining debt is report regeneration coverage, not projection-exception
handling.
