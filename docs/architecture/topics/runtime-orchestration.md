# Runtime Orchestration

> **Status:** accepted; remaining cross-cutting gaps are listed in
> [Architecture conformance](../reference/conformance.md).
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
> `app/control/occurrence_settlement.py`, `app/control/lifecycle.py`, and
> `app/graph/checkpoint_compaction.py`.

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

The accepted pre-freeze boundary requires manifest validation, before creating
or publishing that frozen transaction, to prove every declared biological
member is a regular file with non-empty
logical content. Supported compressed members must yield at least one
decompressed byte and malformed compression fails. Container byte size alone is
not sufficient. The exact intake invariant is owned by the
[run-data contract](../reference/data-contract.md#49-declared-input-content-is-logically-non-empty).

RunConfig owns the literal occurrence sequence. The absence of
`assembly_review` after Assembly, Hi-C Scaffolding, T2T, or another assembly
handoff producer is not a sequence error, and validation never inserts,
reorders, or repairs Review occurrences. When Review is explicitly present,
workflow projection binds it to the nearest earlier occurrence that owns the
central `assembly` handoff. Structural validation and runtime candidate loading
then verify that exact producer identity.

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

Attached and detached progress call the same occurrence reducer defined by the
[run-data contract](../reference/data-contract.md#410-one-workflow-progress-reducer).
It consumes the verified frozen plan, canonical occurrence/session terminals,
and a verified active gate. The attached controller invokes it only after the
corresponding Ledger terminal and delivery events have committed. Detached
inspection derives the same facts on demand; SQLite checkpoint availability and
the execution progress marker/lock are adjacent operational signals only.
Neither surface infers completion from the workflow cursor, last Ledger row, or
`route_trace`, and neither publishes a progress artifact, progress table,
background watcher, event bus, or second progress authority.

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
is not runtime order or a successor table. The canonical complete-genome
recommendation is the following occurrence sequence:

```text
read_qc
-> genome_survey
-> assembly
-> assembly_dedup
-> assembly_review
-> hic_scaffolding
-> gapcloser
-> t2t
-> base_polish
-> assembly_review
-> done
```

The first Review evaluates the latest pre-scaffolding assembly handoff. The
second occurrence reserves the finish-Review position after Base Polish. It
will evaluate the Base Polish handoff once that stage implements and publishes
one; with the current placeholder implementation, freeze binds this otherwise
unreachable Review to T2T, the nearest actual handoff producer.
`done` is the implicit parent terminal and is not written into RunConfig.
Assembly-only planning may recommend the prefix through the first Review. The
complete recommendation is editable planning guidance, not a RunConfig
requirement: an operator may omit, move, or repeat stages before freeze, and no
helper may rewrite the confirmed literal sequence. The controller does not
inspect FASTQ contents, choose an assembler, build a command, or interpret
biological metrics.

Review placement follows delivery provenance rather than a named pipeline slot.
For example, `assembly -> hic_scaffolding -> assembly_review` intentionally
binds Review to the Hi-C occurrence, while `t2t -> assembly_review` binds it to
T2T. In the canonical complete recommendation, the final Review currently also
binds to T2T because Base Polish is not an assembly-handoff producer and runtime
terminates at its `blocked_not_implemented` boundary before reaching Review.
The intended future contract is that an implemented Base Polish occurrence
publishes its own central assembly handoff, at which point the unchanged
nearest-producer algorithm binds the following Review to Base Polish. Both
`t2t` and `t2t -> base_polish` remain valid operator-authored sequence shapes
without a later Review; the latter fails because Base Polish is unimplemented,
not because Review is missing. The same rule applies when Gap Closing gains its
central assembly handoff.

The controller wrapper is thin but not pure. Every settling stage path returns
an explicit `workflow_step_outcome` for its active occurrence. A
`blocked_human` path returns no occurrence outcome and keeps that occurrence
active. The parent invokes the shared
occurrence-settlement boundary, which validates that handoff against the frozen
plan; neither path derives an outcome from cursor movement, a stage-local
status, or a route marker. For completion the boundary first records any
immutable assembly delivery and then exactly one `stage_completed`; for a
defined skip or failure it records exactly one `stage_skipped` or
`stage_failed`. Terminal recovery invokes the same boundary before writing a
session terminal. The parent also owns `stage_entered` and invokes the shared
progress reducer after settlement. It does not write `session_terminal` or
generate operator reports at blocked or failed boundaries. A stage-local skip
remains distinct from an absent occurrence: the parent never synthesizes skip
events for omitted stages. Any architecture description claiming that the
parent graph has no side effects is incorrect.

## Stage boundary

Each domain node in the parent is a compiled stage subgraph. Stages may resolve
logical resources, plan registered methods, enter the shared method lifecycle,
publish biological artifacts, open review gates, and commit a new resource.
Those are available boundaries, not a mandatory identical sequence for all ten
stages.

The parent registers ten domain stages explicitly in `app/graph/builder.py`; the
subgraphs are composable but there is no dynamic plugin registry. `gapcloser` and
`base_polish` are manual-only placeholders. The canonical complete-genome and
manifest-derived recommendations include their intended positions with local
execution disabled; generic prefix expansion omits them. A frozen occurrence of
either terminates with `blocked_not_implemented`. An absent stage has no runtime
occurrence and is not skipped. `None` is only an unfinished planner/TUI draft
state; it is never executable semantics and public `gene-agent run` rejects JSON
`null` before freeze.

`workflow_steps` is the frozen runtime projection of the operator plan. Each
occurrence carries its canonical `selected_method_names` roster from the frozen
RunConfig. Resume restores and applies only the decision bound to the
interrupted occurrence; it does not rewrite the remaining sequence, method
roster, or frozen run config.

`stage_seeds` are immutable stage-scoped RunConfig projections. Every repeated
occurrence starts from a fresh copy of the same stage seed and its own copied
method roster; it never reuses a prior occurrence's completed cursor.

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
writes or reuses the one canonical `session_terminal` Ledger singleton. A failed
terminal records `failure_scope=occurrence` only after proving a contiguous
settled prefix ending in one matching failed occurrence and no later terminal,
or `failure_scope=controller_boundary` when the preceding occurrence is fully
settled and the next occurrence was never entered; it never fabricates a
`stage_failed` for the latter. The finalizer then attempts checkpoint
compaction. Compaction preserves the latest terminal
checkpoint and does not delete immutable artifacts; a compaction failure is an
operational warning, not a different lifecycle outcome. `ProjectionBoundary`
generates reports and workflow-state projections as best-effort presentation
work: ordinary projection exceptions become bounded warnings and cannot
contaminate lifecycle, review status, checkpoint, or terminal authority. The
remaining debt is report regeneration coverage, not projection-exception
handling.
