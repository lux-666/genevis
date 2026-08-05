# Runtime Orchestration

> **Status:** implemented.  
> **Read when:** tracing CLI dispatch, frozen-session startup, parent routing,
> stage selection, or terminal behavior.  
> **Comes from:** [TUI and planning](tui-and-planning.md) and a validated
> whole-run frozen contract.  
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
Its public subcommands are `tui`, `inspect`, `storage-audit`, `resume`,
`review`, and `bootstrap`. New work starts through the TUI; the internal frozen
executor is not a public `run` command.

`execute_frozen_session` validates the frozen control documents and obtains a
per-session execution lock. `_run_session` then creates the Ledger session,
constructs the small `TopLevelState`, projects run-config values into their
owned stage cursors, builds the parent graph, and invokes it with the session ID
as the LangGraph thread ID.

## Parent graph

The compiled parent shape is static:

```text
START -> route_controller -> selected stage subgraph
                            -> route_controller -> ... -> END
```

`app.workflow.STAGE_ORDER` is the single canonical order. The controller uses
`current_stage`, `requested_stages`, and supervision state to select the next
node, skip unrequested stages, or stop on a human block or terminal failure.
It does not inspect FASTQ contents, choose an assembler, build a command, or
interpret biological metrics.

The controller wrapper is thin but not pure. It records stage-entered,
stage-skipped, stage-completed, stage-failed, and session-terminal Ledger
events, and writes an operator report at blocked or failed boundaries. Any
architecture description claiming that the parent graph has no side effects
is incorrect.

## Stage boundary

Each domain node in the parent is a compiled stage subgraph. Stages may resolve
logical resources, plan registered methods, enter the shared method lifecycle,
publish biological artifacts, open review gates, and commit a new resource.
Those are available boundaries, not a mandatory identical sequence for all 13
stages.

The parent currently contains 13 domain stages. `gapcloser` and `base_polish`
are manual-only placeholders: automatic prefix expansion and manifest-derived
recommendations omit them, ordinary explicit stage sets skip them when absent,
and an explicit request terminates with `blocked_not_implemented`. A legacy
`requested_stages = null` traversal records `stage_skipped:not_implemented`
and advances without a success cursor or completion trace.

`requested_stages` is frozen as the initial operator plan, but it is not an
eternally immutable checkpoint field. After Assembly Review, the approved
annotation-intake transaction may rewrite the remaining requested stage set.
The change is persisted and auditable rather than treated as an edit to the
original frozen run config.

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
