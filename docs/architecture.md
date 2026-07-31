# Gene Agent Architecture

Gene Agent implements a multiplatform long-read genome workflow for ONT, HiFi,
CLR, and CycloneSEQ. Its biological rationale is retained in
`docs/archive/deep-research-report.md`.

This document is the system overview. Stage inputs and outputs live in
`docs/stage_contracts.md`; cross-cutting contracts are registered in
`docs/architecture/README.md`.

## Principles

1. Keep the parent graph thin: `route_controller` routes to static domain stages.
2. Keep stage boundaries stable and move method choice inside each stage.
3. Store large biological files, logs, scripts, and metric tables outside graph state.
4. Let descriptors and review rules express method compatibility.
5. Freeze the complete runner input after any required recommendation and human
   approval, persist that frozen record before execution, and resume it by ref
   rather than rebuilding it from current configuration.
6. Put human-in-the-loop gates at review boundaries, especially assembly evaluation and Hi-C review.
7. Use one shared parameter-recommendation persistence contract after method
   selection; the implemented Assembly and Hi-C recommenders do not own
   separate audit layouts.
8. Treat each session run directory as the data-ownership boundary. Keep
   checkpoints bounded and ref-oriented: they identify the frozen input and
   durable result needed to resume, but do not become a second owner of them.
9. Keep local process paths inside runtime `ExecutionResult`; persist only the
   canonical, index-backed `ExecutionEvidence` body.

## Parent Flow

```text
START
  -> route_controller
    -> inventory_stage
    -> read_qc_stage
    -> genome_survey_stage
    -> assembly_stage
    -> assembly_dedup_stage
    -> assembly_review_stage
    -> hic_scaffolding_stage
    -> t2t_stage
    -> repeat_annotation_stage
    -> gene_annotation_stage
    -> function_annotation_stage
    -> END
```

`genome_survey` and `hic_scaffolding` are domain stages but may be bypassed by
the parent route derived from the requested stage set and method-selection
facts. Genome survey runs when raw/clean short or long DNA reads are registered;
assembly requires either an explicit user genome size or a survey-derived
genome size. Hi-C scaffolding runs only when Hi-C assets exist and the user
requests chromosome-level assembly. Parameter recommenders do not make any of
these routing decisions.

For the ONT-to-T2T golden path, Hi-C scaffolding publishes its adopted result as
the current `resource_refs["assembly"]`. T2T consumes that current assembly plus
long reads; it does not consume Hi-C reads directly or select a parallel assembly
path from run config.

The parent graph must stay thin. It should route by `current_stage`, call a
compiled stage subgraph, then return to `route_controller`. It should not inspect
FASTQ files, choose Flye vs NextDenovo, submit cluster jobs, or interpret BUSCO
metrics.

## Modules

```text
app/
  workflow.py  canonical stage order, navigation, and failure-boundary vocabulary
  domains/     stage-neutral biological rules and shared input contracts
  graph/       parent graph, route controller, checkpoint factory
  stages/      domain subgraphs and method-family routes
  llm/         stage-specific request DTOs, prompt rendering, model adapters
  schemas/     state, asset, plan, descriptor, artifact contracts
  execution/   shared lifecycle, attempt owners, evidence boundary, local runner
  storage/     run layout, ledger, artifact publication/index, durable evidence
  review/      rule checks and interrupt-backed human gates
  control/     lifecycle commands
  reports/     JSONL trace and user/developer reports
```

Module ownership:

| Module | Owns | Must Not Own |
|---|---|---|
| `graph/` | Parent LangGraph wiring, routing, checkpoint factory | Tool execution, biological method choice |
| `workflow.py` | Canonical stage order, navigation, and terminal-failure routing vocabulary | Stage implementation or persistence |
| `domains/` | Stage-neutral biological decisions and shared input parsing | Workflow side effects |
| `stages/` | Stage subgraphs, method selection/routing, parameter profiles and immutable recommendation bases | Provider prompt rendering or package-level script generation internals |
| `llm/parameters/<stage>/` | Stage-specific strict request DTOs, deterministic message rendering, model invocation, and response-shape parsing | Full audit profiles, persistence, biological/catalog validation, approval, or execution |
| `schemas/` | Cross-module contracts | Side effects |
| `execution/` | Shared freeze/run/collect graph, attempt ownership, running frozen attempts locally, and publishing process logs/evidence | Stage-specific command construction, biological collection, or in-agent remote orchestration |
| `review/` | Rule checks and human gates | Writing checkpoint state directly |
| `storage/` | Run layout, immutable artifact publication/index, parameter evidence, and Ledger persistence | Biological method choice |
| `control/` | Lifecycle semantics | Durable storage implementation, starting tools, or reading huge data files |
| `reports/` | Trace and notebook/report rendering | Workflow control decisions |

## Server Execution

Server execution is achieved by running the same `gene-agent` CLI inside the
server checkout after `git pull`. There is no in-agent remote runner, no SSH
ControlMaster, and no gateway-driven execution. Mac development handles
editing, review, and lightweight tests; real bioinformatics runs happen on
the server. See `docs/deployment/mac_dev_server_pull.md` for the workflow
contract and `scripts/server_*.sh` for the helper scripts.

## State Contract

`TopLevelState` stays small. It stores session IDs, current stage, compact
control cursors, and verified references. It must not store FASTQ/FASTA
contents, parameter catalogs, model payloads, candidate/signal tables, command
logs, generated script bodies, execution results, full metric tables, or
notebook bodies.

This does not require recovery to reconstruct a command from incomplete state.
Freezing happens first: the exact runner input is persisted as immutable
evidence, then the checkpoint stores its verified ref. Resume loads that same
record. Environment mappings, paths, and script identity may therefore exist in
the frozen evidence without being embedded again in the checkpoint.

Data-specific ownership, path, checkpoint-size, parameter-recommendation,
attempt, review, report, audit, and terminal-compaction rules are normative in
`docs/architecture/data-v2.md`, which has no legacy compatibility branch.

State categories:

| Category | Stored In | Examples |
|---|---|---|
| Control recovery facts | LangGraph checkpoint | `current_stage`, compact interrupt ref, current recommendation/attempt cursors |
| Business execution summaries | Ledger | stage transitions, attempt status, review events, terminal event |
| Structured process outcomes | Immutable artifacts | return code, status, bounded error, indexed stdout/stderr refs |
| External world facts | Artifact registry | file path, hash, size, artifact type |
| Method capability facts | Descriptors | inputs, outputs, resources, review rules |
| Operator views | Reports | current status and derived session summary |

Do not collapse these categories. Most long-running workflow failures become
hard to resume when these facts are mixed.

## Stage Shape

Executable stages use the following shape where their domain flow requires each
boundary:

```text
enter -> validate_inputs -> plan_methods -> method_family_subgraph
-> collect_outputs -> review_outputs -> commit_stage
```

The method family subgraph owns package-specific logic. Existing assembly
methods share this boundary without changing the parent graph. Executable
method subgraphs conform to
[`architecture/execution-lifecycle.md`](architecture/execution-lifecycle.md).

## Implemented Scope

The top-level graph contains inventory, read-QC, genome-survey, assembly,
assembly-dedup, assembly-review, Hi-C, T2T, repeat-annotation,
gene-annotation, and function-annotation stages. Every method in the descriptor
registry is executable; future tools remain outside the registry until their
adapter, configuration, routing, and tests exist.

## Executable Method Boundary

Every registered method enters one shared lifecycle after method selection and
any required parameter approval:

```text
prepare and freeze -> run one frozen attempt -> checkpoint
                   -> run next pending attempt -> collect domain artifacts
                   -> review -> commit compact refs
```

The stage adapter owns command preparation and biological collection. Shared
execution owns Runner invocation, process logs, durable execution evidence,
attempt recovery, and failure-policy enforcement. Checkpoints contain compact
owner cursors, not command or result bodies. Cross-stage file publication uses
the logical resources in `architecture/resource-handoff.md`.
