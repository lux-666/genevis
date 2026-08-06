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
gene-agent run --manifest PATH --run-config PATH --data-dir PATH [--non-interactive]
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
per-session execution lock, creates the Ledger session, constructs the small
`TopLevelState`, projects run-config values into their owned stage cursors,
builds the parent graph with the resulting executor-owned `FrozenInputBundle`,
and invokes it with the session ID as the LangGraph thread ID. Inventory
validation and asset registration consume this already verified in-memory
manifest, RunConfig, canonical declarations, and run-owned paths; they do not
reopen an unchecked control file. The bundle is closure context, not a graph
channel or checkpoint payload. A new process validates again before building
its graph. The public run always uses SQLite. Foreground review additionally
requires stdin and stdout to be real TTYs. With `--non-interactive` or without
that TTY capability, a human gate persists `blocked_human` and prints
`inspect`, `review`, and `resume` continuations instead of prompting.

The frozen RunConfig review policy defaults to
`review_policy.max_blocked_seconds: 300` and permits per-stage overrides only
for stages registered with the review registry. `-1` waits indefinitely; `0`
immediately triggers the gate's existing automatic resolver when it is eligible;
a positive value starts an Enter-claim countdown that triggers the same resolver
at expiry. The timer never chooses from registry defaults. Assembly Review keeps
its evidence-qualified LLM recommender, assembly route selection keeps its
recommendation timeout applier, and gates without an existing resolver remain
manual. Resolver failure leaves the checkpoint blocked for manual review.
This mechanism exists only while the originating run is foreground and
interactive. Enter cancels it and opens manual prompts. Ctrl-C, EOF, process
exit, missing TTY capability, or `--non-interactive` preserve a durable human
block; there is no daemon/background timer. Starting the frozen session again
does not invoke an already-blocked checkpoint or restore its timer. Subsequent
`review` and `resume` commands are manual and must not infer a deadline from the
original block time. The public interfaces have no strict/auto review mode or
resume-timeout override.

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
a separate planning helper: the Assembly template appends `assembly_review`,
and the T2T template repeats it after `t2t`. Neither helper rewrites a
literal RunConfig. The controller does not inspect FASTQ contents, choose an
assembler, build a command, or interpret biological metrics.

The controller wrapper is thin but not pure. It records stage-entered,
stage-completed, stage-failed, and session-terminal Ledger events, and writes
an operator report at blocked or failed boundaries. A stage-local skip, when a
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

Only completed or failed sessions are eligible for terminal checkpoint
compaction. Compaction preserves the latest terminal checkpoint and does not
delete immutable artifacts. A compaction failure is caught and recorded as
`terminal_compaction_skipped`. Report generation runs before that guarded
compaction block: a report exception is surfaced as a terminal-finalization
error (and as the `terminal_report` phase during resume), although it still does
not rewrite the biological state already committed by the graph.
