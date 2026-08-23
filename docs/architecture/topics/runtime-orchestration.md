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
source documents, freezes the literal operator-authored occurrence plan and its
dependency closure, then enters the shared executor. Fixed-stage method rosters
come from registry defaults; an Assembly roster may be omitted so Assembly can
recommend and confirm its method after upstream QC/survey evidence. Its exact
request/response/recommendation recovery chain is defined once in the
[run-data contract](../reference/data-contract.md#101-assembly-method-recommendation).
Exact
RunConfig fields,
occurrence identity, authorization, empty-plan, publication-order, and immutable
workflow-step rules belong to the
[run-data contract](../reference/data-contract.md), not this explanatory topic.

The accepted pre-freeze boundary requires manifest validation, before creating
or publishing that frozen transaction, to prove every declared biological
member is a regular file with non-empty
logical content. Supported compressed members must yield at least one
decompressed byte and malformed compression fails. Container byte size alone is
not sufficient. The exact intake invariant is owned by the
[run-data contract](../reference/data-contract.md#49-declared-input-content-is-logically-non-empty).

RunConfig owns the literal occurrence sequence. Planning recommendations never
become validation rules or silently insert/reorder Review. The exact reusable
Review placement and producer-binding contract lives in
[stage contracts](../reference/stage-contracts.md).

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
channel or checkpoint payload. A new process validates the frozen transaction
again before building its graph. This is operational consistency checking under
the trusted-local-operator model defined in the data contract. The public run
always uses SQLite. Every new structured run and
confirmed TUI draft requires stdin and stdout to be real TTYs before freeze;
missing capability fails without publishing frozen inputs. An already-frozen
blocked checkpoint remains recoverable through `inspect`, `review`, and
`resume`, but it is not a second non-interactive new-run path.

Foreground gates use the frozen review policy and registered resolver while the
originating executor remains attached. Process interruption preserves a durable
manual block rather than a daemon deadline; later `review` and `resume` do not
recreate the old countdown. Exact values, resolver eligibility, immutable
bindings, and recovery states are owned by the
[review contract](../reference/data-contract.md#12-review-contract) and explained
operationally in [Review and resume](review-and-resume.md).

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

Attached and detached progress call the same reducer defined by the
[run-data contract](../reference/data-contract.md#410-one-workflow-progress-reducer).
Checkpoint, marker, and lock state remain execution diagnostics rather than a
second progress authority.

## Parent graph

The compiled parent shape is static:

```text
START -> route_controller -> selected stage subgraph
                            -> route_controller -> ... -> END
```

Before graph entry, the operator plan is projected into immutable occurrence
records consumed by the controller. The exact IDs, cursor rules, Review target,
and settlement identities are owned by the run-data and stage contracts.

`app.workflow.STAGE_ORDER` is a registration/default-planning/display aid. It
is not runtime order or a successor table. The canonical complete-genome
occurrence recommendation is owned by
[stage contracts](../reference/stage-contracts.md) and produced by
`app.workflow.recommended_workflow_template`; runtime never treats that helper
as a successor table.

The recommendation is editable planning guidance rather than a RunConfig
requirement. The controller does not inspect FASTQ contents, select an assembler,
build commands, or interpret biological metrics. Assembly selection belongs to
the Assembly stage after its upstream evidence, not to the parent controller.
Reusable Review follows proven assembly-delivery provenance; its exact placement
examples and future producer rule remain in the stage contract.

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
`base_polish` are manual-only placeholders and fail closed if reached. Resume
applies only the decision bound to the interrupted immutable occurrence; it does
not rewrite the remaining plan, a fixed method roster, or an already-bound
Assembly selection.

`stage_seeds` are immutable stage-scoped RunConfig projections. Every repeated
occurrence starts from a fresh copy of the same stage seed and its own copied
fixed-method roster; an Assembly seed may carry no roster and lets that
occurrence bind its method from frozen dependencies at stage entry. It never
reuses a prior occurrence's completed cursor.

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
