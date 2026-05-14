# Gene Agent Architecture

Gene Agent follows the BioAgent V2 pattern from the reference `rna_agent` project, but the biological workflow is the ONT/CLR long-read genome pipeline described in `deep-research-report.md`.

This document is the system-level architecture. For operational handoff, read
`docs/handoff.md`. For coordinator rules, read `docs/governance.md`. For
stage-by-stage inputs and outputs, read `docs/stage_contracts.md`.

## Principles

1. Keep the parent graph thin: `route_controller` routes to static domain stages.
2. Keep stage boundaries stable and move method choice inside each stage.
3. Store large biological files, logs, scripts, and metric tables outside graph state.
4. Let descriptors and review rules express method compatibility.
5. Let Forge generate frozen attempt scripts only after descriptor context and validation.
6. Put human-in-the-loop gates at review boundaries, especially assembly evaluation and Hi-C review.

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
    -> repeat_annotation_stage
    -> gene_annotation_stage
    -> function_annotation_stage
    -> END
```

`genome_survey` and `hic_scaffolding` are domain stages but must be skippable by planner policy. Genome survey runs only when low-error short DNA reads are registered. Hi-C scaffolding runs only when Hi-C assets exist and the user requests chromosome-level assembly.

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
  execution/   local subprocess runner (no in-agent remote execution)
  forge/       context, codegen, validation, freezing
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
| `execution/` | Running frozen attempts locally and collecting declared outputs | In-agent remote orchestration (intentionally unsupported; see `docs/deployment/mac_dev_server_pull.md`) |
| `forge/` | Generating, validating, and freezing attempt scripts | Mutating graph, ledger, lifecycle, or descriptors |
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

`TopLevelState` stays small. It stores session IDs, current stage, manifest reference, registry reference, stage plan reference, interrupt metadata, route trace, and skip reasons. It must not store FASTQ/FASTA contents, command logs, generated script bodies, full metric tables, or notebook bodies.

State categories:

| Category | Stored In | Examples |
|---|---|---|
| Control recovery facts | LangGraph checkpoint | `current_stage`, `active_interrupt`, `route_trace` |
| Business execution facts | Ledger | attempts, statuses, review events |
| External world facts | Artifact registry | file path, hash, size, artifact type |
| Method capability facts | Descriptors | inputs, outputs, resources, review rules |

Do not collapse these categories. Most long-running workflow failures become
hard to resume when these facts are mixed.

## Stage Shape

Each stage should evolve from the current placeholder into:

```text
enter -> validate_inputs -> plan_methods -> method_family_subgraph
-> collect_outputs -> review_outputs -> commit_stage
```

The method family subgraph owns package-specific logic. For example, `assembly` can later contain `flye`, `nextdenovo`, and `canu` families without changing the parent graph.

## Current Scaffold Status

Current placeholder stages use `app/stages/factory.py` and advance immediately.
That behavior exists only to prove parent graph routing and file layout. Phase 1
must replace `inventory` with real manifest validation and asset registration.

The graph smoke should route through all stages and finish at `done`. That does
not mean the biological pipeline is implemented.

## Extension Rule

When adding a real method:

1. Add or update asset and method descriptors.
2. Add a focused test for planner behavior.
3. Add a `StagePlan` path before execution.
4. Generate or template a `FrozenAttemptSpec`.
5. Run through a runner boundary.
6. Collect artifacts.
7. Review outputs.
8. Commit compact references to state and ledger.

Skipping these steps is allowed only for temporary local experiments outside the
main graph.
