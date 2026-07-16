# Gene Agent Architecture

Gene Agent follows the BioAgent V2 pattern from the reference `rna_agent` project, but the biological workflow is the ONT/CLR long-read genome pipeline described in `deep-research-report.md`.

This document is the system-level architecture. For operational handoff, read
`docs/handoff.md`. For coordinator rules, read `docs/governance.md`. For
stage-by-stage inputs and outputs, read `docs/stage_contracts.md`. Accepted
cross-cutting specifications live under `docs/architecture/`; the accepted Data
Architecture v2 contract is `docs/architecture/data-v2.md`.

## Principles

1. Keep the parent graph thin: `route_controller` routes to static domain stages.
2. Keep stage boundaries stable and move method choice inside each stage.
3. Store large biological files, logs, scripts, and metric tables outside graph state.
4. Let descriptors and review rules express method compatibility.
5. Generate frozen attempt scripts in stage nodes (via the shared `execution/method_runner` helpers) only after descriptor context and validation.
6. Put human-in-the-loop gates at review boundaries, especially assembly evaluation and Hi-C review.
7. Use one shared parameter-recommendation persistence contract after method
   selection; Assembly, Hi-C, and T2T must not invent separate audit layouts.
8. Treat each session run directory as the data-ownership boundary and keep
   checkpoints ref-only and bounded.

## Parent Flow

```text
START
  -> route_controller
    -> inventory_stage
    -> read_qc_stage
    -> genome_survey_stage
    -> assembly_stage
    -> assembly_review_stage
    -> hic_scaffolding_stage
    -> t2t_stage
    -> repeat_annotation_stage
    -> gene_annotation_stage
    -> function_annotation_stage
    -> END
```

`genome_survey` and `hic_scaffolding` are domain stages but must be skippable by planner policy. Genome survey runs when raw/clean short or long DNA reads are registered; assembly requires either an explicit user genome size or a survey-derived genome size. Hi-C scaffolding runs only when Hi-C assets exist and the user requests chromosome-level assembly.

The parent graph must stay thin. It should route by `current_stage`, call a
compiled stage subgraph, then return to `route_controller`. It should not inspect
FASTQ files, choose Flye vs NextDenovo, submit cluster jobs, or interpret BUSCO
metrics.

## Modules

```text
app/
  graph/       parent graph, route controller, checkpoint factory
  stages/      domain subgraphs and method-family routes
  schemas/     state, asset, plan, descriptor, artifact contracts
  execution/   local subprocess runner + shared freeze/run helpers (no in-agent remote execution)
  review/      rule checks and interrupt-backed human gates
  control/     ledger and lifecycle commands
  reports/     JSONL trace and user/developer reports
```

Module ownership:

| Module | Owns | Must Not Own |
|---|---|---|
| `graph/` | Parent LangGraph wiring, routing, checkpoint factory | Tool execution, biological method choice |
| `stages/` | Stage subgraphs, stage planners, method-family routing | Package-level script generation internals |
| `schemas/` | Cross-module contracts | Side effects |
| `execution/` | Running frozen attempts locally, shared freeze/run helpers (`method_runner`), collecting declared outputs | In-agent remote orchestration (intentionally unsupported; see `docs/deployment/mac_dev_server_pull.md`) |
| `review/` | Rule checks and human gates | Writing checkpoint state directly |
| `control/` | Ledger and lifecycle semantics | Starting tools or reading huge data files |
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

Data-specific ownership, path, checkpoint-size, parameter-recommendation,
attempt, review, report, and retention rules are normative in
`docs/architecture/data-v2.md`. Current code may lag that accepted design until
the v2 cutover is complete.

State categories:

| Category | Stored In | Examples |
|---|---|---|
| Control recovery facts | LangGraph checkpoint | `current_stage`, compact interrupt ref, current recommendation/attempt cursors |
| Business execution facts | Ledger | stage transitions, attempts, statuses, review events, terminal event |
| External world facts | Artifact registry | file path, hash, size, artifact type |
| Method capability facts | Descriptors | inputs, outputs, resources, review rules |
| Operator views | Reports | current status and derived session summary |

Do not collapse these categories. Most long-running workflow failures become
hard to resume when these facts are mixed.

## Stage Shape

Executable stages use the following shape; candidate-only methods may stop after
their planning/review nodes:

```text
enter -> validate_inputs -> plan_methods -> method_family_subgraph
-> collect_outputs -> review_outputs -> commit_stage
```

The method family subgraph owns package-specific logic. For example, `assembly` can later contain `flye`, `nextdenovo`, and `canu` families without changing the parent graph.

## Current Implementation Status

The top-level graph contains real inventory, read-QC, assembly, review, Hi-C,
T2T, repeat, gene-annotation, and function-annotation stages. Methods remain
individually scoped: unsupported methods are represented as planning contracts,
while supported methods freeze an auditable command and run through the shared
execution lifecycle.

## Extension Rule

When adding a real method:

1. Add or update asset and method descriptors.
2. Add a focused test for planner behavior.
3. Add a `StagePlan` path before execution.
4. If the method uses parameter recommendation, persist the exact profile,
   model request/response, validation, and approval through the shared Data v2
   contract. The recommender does not select the method or workflow route.
5. Generate or template a `FrozenAttemptSpec` only after required validation
   and approval.
6. Run through a runner boundary.
7. Collect artifacts.
8. Review outputs.
9. Commit compact references to state and bounded causal events to the ledger.

Skipping these steps is allowed only for temporary local experiments outside the
main graph.
