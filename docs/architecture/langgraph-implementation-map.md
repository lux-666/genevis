# LangGraph Implementation Map

> **Status:** current implementation map, with partial and debt units marked
> explicitly.
> **Read when:** determining which graph nodes, method routes, parameter flows,
> and review boundaries actually exist before changing or extending a stage.
> **Comes from:** frozen workflow occurrences, compiled graph builders, descriptor
> registry, method adapters, focused tests, and architecture conformance review.
> **Feeds into:** implementation planning, code review, method onboarding, and
> updates to the exact architecture contracts.
> **Contract dependencies:** [Runtime orchestration](topics/runtime-orchestration.md),
> [Stage contracts](reference/stage-contracts.md),
> [Descriptors](reference/descriptors.md),
> [Parameter recommendation](reference/parameter-recommendation.md),
> [CasePack](reference/casepack.md), and
> [Architecture conformance](reference/conformance.md).
> **Code evidence:** `app/workflow.py`, `app/graph/`, every
> `app/stages/*/builder.py`, `app/execution/`, `descriptors/registry.yaml`, and
> the focused test files named below.

This page projects what is wired, executable, verified, partial, or absent in
the current tree. Exact behavioral rules remain owned by the linked reference
contracts.

## How to read status

A compiled stage is not automatically a completed capability. Each unit is
classified on four independent axes.

| Axis | Values | Interpretation |
|---|---|---|
| Wiring | `wired`, `unwired`, `not-applicable` | Reachability through a graph edge, conditional route, or registered method route |
| Implementation | `executable`, `implemented-no-process`, `partial`, `placeholder`, `absent` | Real runner path, deliberate non-process behavior, incomplete boundary, named shell without a closed loop, or no modeled unit |
| Verification | `tested`, `partially-tested`, `untested` | Presence of focused tests that exercise the claim; this is not a claim that every repository quality gate is currently green |
| Debt | `none`, `known-gap`, `contract-drift` | No recorded limitation, bounded product limitation, or mismatch recorded in the conformance register |

`Not required` is a capability decision, not a failure status. For example,
Inventory does not need an LLM parameter recommender. `Placeholder` means a
named interface or graph position exists without an executable closed loop;
`absent` means there is no canonical stage, registered method, or graph
contract at all.

## Parent graph

The parent graph is a controller loop, not an unconditional linear pipeline.
It selects one active immutable `workflow_step`, invokes the corresponding
stage subgraph, then returns to the controller. Occurrence identity, settlement,
literal ordering, repeated-stage, Review-target, and cursor rules are owned by
the [run-data contract](reference/data-contract.md) and
[stage contracts](reference/stage-contracts.md); this implementation map records
only the current wiring and evidence for those rules.

There are no direct parent edges from one biological stage to the next. Every
compiled stage returns to `route_controller`. Stage subgraphs own biological
selection and evidence; the parent records stage entry and invokes shared
occurrence settlement, while the authoritative finalizer owns session terminal
events. Operator reports are emitted by the executor/projection boundary, not
the parent controller.

| Parent unit | Wiring | Implementation | Verification | Debt | Evidence |
|---|---|---|---|---|---|
| `route_controller` and `route_from_current_stage` | `wired` | `implemented-no-process` | `tested` | `none` | `app/graph/routing.py`; `tests/test_graph_routing.py`, `tests/test_routing_contracts.py` |
| Frozen workflow-occurrence routing and settlement | `wired` | `implemented-no-process` | `tested` | `none` | Every terminal stage path returns the explicit, frozen-occurrence-bound `workflow_step_outcome`; the shared controller/recovery settlement boundary validates it, records declared immutable deliveries before the singleton stage terminal, and rejects cursor/route-marker inference. `app/schemas/state.py`, `app/control/workflow_plan.py`, `app/control/occurrence_settlement.py`, `app/graph/builder.py`, `app/graph/routing.py`; `tests/test_graph_routing.py`, `tests/test_routing_contracts.py`, `tests/test_state_schema.py`, `tests/test_terminal_finalizer.py` |
| Review occurrence placement policy | `wired` | `implemented-no-process` | `tested` | `none` | RunConfig preserves optional literal Review placement; sequence analysis binds only explicit Review occurrences. Deterministic, manifest, and LLM planning use the editable complete recommendation with Gap Closing, Base Polishing, and finish Review. `app/workflow.py`, `app/schemas/run_config.py`, `app/entrypoints/tui_intake.py`, `app/entrypoints/ask_planner.py`; `tests/test_run_config.py`, `tests/test_workflow_plan.py`, `tests/test_tui_intake.py`, `tests/test_ask_plan.py` |
| Base Polish adoption boundary | `wired` | `executable` | `tested` | `bounded` | HiFi Racon, paired evidence, durable adopt/retain/stop decisions; `app/stages/base_polish/`; `tests/test_base_polish.py` |
| SQLite/memory checkpoint selection and durable resume | `wired` | `implemented-no-process` | `tested` | `none` | `app/graph/checkpointer.py`, `app/control/lifecycle.py`; `tests/test_checkpointer.py`, `tests/test_durable_resume.py` |
| Foreground review countdown policy | `wired` | `implemented-no-process` | `tested` | `none` | The frozen policy and registered resolver feed the same typed intent, durable preparation, verified application, and recovery protocol as a manual decision. Countdown exists only in the original attached executor; later review/resume remains manual. Exact values and bindings are owned by the run-data contract. `app/review/application.py`, `app/control/resume_transaction.py`, `app/review/approval.py`; `tests/test_review_application.py`, `tests/test_foreground_review_countdown.py`, `tests/test_parameter_persistence_v2.py` |
| Authoritative terminal finalization, reporting, and checkpoint compaction | `wired` | `implemented-no-process` | `tested` | `known-gap` | `finalize_authoritative_terminal()` proves the checkpoint before recording the singleton terminal Ledger fact, then compacts best-effort. Reports run through `ProjectionBoundary` and cannot alter lifecycle authority. Report regeneration remains the bounded gap in [conformance](reference/conformance.md). `app/control/lifecycle.py`, `app/control/projections.py`, `app/graph/checkpoint_compaction.py`; `tests/test_terminal_finalizer.py`, `tests/test_projections.py`, `tests/test_checkpoint_compaction.py` |

## Cross-cutting implementation boundaries

These boundaries apply across multiple stage graphs. This table identifies the
implemented owner and verification surface; the linked reference document owns
the exact behavioral contract.

| Boundary | Current implementation | Code and test evidence | Contract owner |
|---|---|---|---|
| Public structured-run preflight and whole-run freeze | Structured/TUI paths validate and freeze the literal occurrence sequence. Fixed-stage method rosters derive from registry defaults; Assembly may omit its roster for stage-owned deferred selection. Manifest validation probes one logical byte from uncompressed and gzip content before frozen publication, rejecting empty or unreadable members; bzip2/xz fail preflight until execution support is end-to-end. | `app/entrypoints/run.py`, `app/entrypoints/tui.py`, `app/schemas/manifest.py`, `app/storage/frozen_inputs.py`; `tests/test_run_command.py`, `tests/test_frozen_inputs.py`, `tests/test_manifest.py` | [Runtime orchestration](topics/runtime-orchestration.md), [Data contract](reference/data-contract.md) |
| Attached and detached progress | One pure reducer consumes only the frozen plan, canonical occurrence terminals, and a matching active gate. Attached rendering invokes it after settlement; detached inspection invokes the same reducer. Cursor, route-tail order, and skip-reason fields are not progress inputs. Detached resumability additionally requires verified frozen inputs and a readable checkpoint row for the inspected session, not SQLite-file existence alone. | `app/control/projections.py`, `app/control/lifecycle.py`, `app/graph/builder.py`; `tests/test_projections.py`, `tests/test_graph_routing.py`, `tests/test_run_command.py`, `tests/test_review_resume.py`, `tests/test_inspect_read_model.py` | [Runtime orchestration](topics/runtime-orchestration.md), [Data contract](reference/data-contract.md) |
| Verified frozen context and trace authority | Production run and resume validate one frozen transaction and pass its in-memory `FrozenInputBundle` into the Inventory subgraph. Inventory validation and registration consume its canonical manifest, RunConfig, declarations, and paths. Trace derives workflow steps from a freshly validated bundle, otherwise only occurrence-bearing Ledger/artifact facts. | `app/entrypoints/run.py`, `app/graph/builder.py`, `app/stages/inventory/nodes.py`, `app/reports/trace_writer.py`; `tests/test_frozen_inputs.py`, `tests/test_run_trace.py`, `tests/test_resource_resolution.py` | [Runtime orchestration](topics/runtime-orchestration.md), [Data contract](reference/data-contract.md) |
| Occurrence, method, and attempt fact integrity | Occurrence settlement is explicit and stage terminals are append singletons. Attempt terminals are mutually exclusive under the Ledger append lock; trace projects the conservative canonical terminal and exposes integrity diagnostics for conflicts. Shared lifecycle and planner settlement record one typed durable method disposition per effective method and occurrence; deferred Assembly validates `primary_method == assembly_selection.primary_method` before settlement. | `app/execution/lifecycle.py`, `app/execution/method_outcomes.py`, `app/control/occurrence_settlement.py`, `app/storage/ledger.py`, `app/graph/builder.py`, `app/reports/trace_writer.py`; `tests/test_execution_lifecycle.py`, `tests/test_terminal_finalizer.py`, `tests/test_ledger_v2.py`, `tests/test_graph_routing.py`, `tests/test_run_trace.py`, `tests/test_read_qc.py`, `tests/test_genome_survey.py` | [Execution lifecycle](reference/execution-lifecycle.md), [Data contract](reference/data-contract.md) |
| Local process dispatch | `LocalRunner` invokes the frozen interpreter/script as argv, optionally wrapped by `conda run`, always with `shell=False`. Non-empty `env_setup` shell fragments fail closed on both dispatch paths. | `app/execution/local_runner.py`; `tests/test_execution_runner.py`, `tests/test_env_resolver.py` | [Execution](topics/execution.md), [Execution lifecycle](reference/execution-lifecycle.md) |
| Interactive Inventory-only planning | Ask/TUI distinguish an unanswered selection (`None`) from an explicit empty occurrence list. Plain-text, questionary, and LLM Plan Draft paths accept `[]`, skip biological planning questions, render `Inventory only`, and freeze `requested_stages: []`. | `app/schemas/ask_plan.py`, `app/entrypoints/ask_interactive.py`, `app/entrypoints/tui.py`; `tests/test_ask_plan.py`, `tests/test_ask_interactive.py`, `tests/test_tui.py` | [TUI and planning](topics/tui-and-planning.md), [Runtime orchestration](topics/runtime-orchestration.md) |
| Current assembly candidate continuity and immutable delivery | Every implemented assembly-handoff producer declares an occurrence-bound delivery. Shared settlement verifies its candidate/primary artifact ownership before appending `resource_delivered`; Review requires the frozen producer terminal, its single delivery, the current projection, and the resolved candidate bundle to agree. Reused candidates are valid only through an explicit reused delivery. | `app/control/workflow_plan.py`, `app/control/occurrence_settlement.py`, `app/graph/builder.py`, `app/storage/ledger.py`, `app/storage/assembly_candidates.py`, `app/stages/{inventory,assembly,assembly_dedup,assembly_review,hic_scaffolding,t2t}/nodes.py`; `tests/test_inventory.py`, `tests/test_assembly.py`, `tests/test_assembly_dedup.py`, `tests/test_assembly_review.py`, `tests/test_hic_scaffolding.py`, `tests/test_t2t.py` | [Resource handoff](reference/resource-handoff.md), [Stage contracts](reference/stage-contracts.md) |
| Read QC closed evidence package | SeqKit publishes one direct whole-read-set aggregate for each cardinality; that aggregate owns length and quality metrics, while retained per-file rows serve only as consistency checks. Aggregate and summary provenance reference their real read inputs/aggregate. Input provenance freezes through the resource registry, and zero-retained filtering preserves evidence but fails the occurrence instead of replacing `long_reads`. | `app/stages/read_qc/method_application/support.py`, `app/stages/read_qc/method_application/seqkit_stats.py`, `app/stages/read_qc/method_application/seqkit_length_filter.py`, `app/stages/read_qc/nodes.py`; `tests/test_read_qc.py`, `tests/test_frozen_inputs.py` | [Stage contracts](reference/stage-contracts.md), [Resource handoff](reference/resource-handoff.md) |
| Stage resource resolution | Stage adapters query canonical `ResourceSpec` objects through `resource_resolution.py`. There is no separate repository planning-preflight API or duplicate stage-requirement table. The confirmed-run boundary performs read-only machine readiness for the fixed effective method closure before whole-run freeze; deferred Assembly resolves its selected method only from the frozen dependency snapshot and performs stage-time preflight before attempt construction. | `app/execution/run_readiness.py`, `app/execution/env_resolver.py`, `app/stages/resource_specs.py`, `app/stages/resource_resolution.py`; `tests/test_run_readiness.py`, `tests/test_project_scaffold.py`, stage-focused tests | [Resource handoff](reference/resource-handoff.md), [Project bootstrap](topics/bootstrap.md) |
| Genome Survey logical inputs | The stage boundary exposes only `short_reads` and `long_reads`, prefers the short-read family, and leaves clean/raw lifecycle priority to each resource spec. No-read execution is permitted only when an explicit genome size supports the documented skip path. | `app/stages/genome_survey/nodes.py`, `app/stages/genome_survey/state.py`, `app/stages/resource_specs.py`; `tests/test_genome_survey.py`, `tests/test_genome_size.py` | [Stage contracts](reference/stage-contracts.md) |
| Executable artifact dependencies | Genome Survey k-mer products cross the Jellyfish/GenomeScope2 boundary as indexed `ArtifactRefJson` values. The consumer has no path-string upgrade branch. | `app/stages/genome_survey/method_application/jellyfish.py`, `genomescope2.py`; `tests/test_genome_survey.py` | [Data contract](reference/data-contract.md) |
| Descriptor loading | Registry, package, method, planner, IO, conditional-requirement, and resource-hint YAML models reject undeclared keys. The registry loads as 16 retained, typed method records. | `app/schemas/descriptors.py`; `tests/test_configuration.py` | [Descriptors](reference/descriptors.md) |
| External case consumption | One Assembly v5-derived compact exchange, live directory reads and shared material query; cross-method evidence reaches method choice and target-only parameter advice. No run-level corpus copy or old consumer fallback; Hi-C/T2T extensions deferred. | `app/knowledge/assembly_cases.py`, `assembly_retrieval.py`, `assembly_runtime.py`, `v5_projection.py`; `tests/test_assembly_case_consumption.py`; [software coverage gap](reference/conformance.md#assembly-software-explanation-coverage) | [Assembly case contract](reference/casepack.md) |
| Registered execution classes | The registry is partitioned into 15 `local` methods bound to imported lifecycle/graph objects and one `planner_only` method whose real route is proven to launch no process. | `tests/test_registry_executable_invariant.py`, `descriptors/registry.yaml` | [Descriptors](reference/descriptors.md) |
| AutoHiC output collection | After process success and before the first new publication, collection requires all four frozen products or matching attempt-local indexed recovery records. Missing products fail collection; a later publication failure may leave an indexed orphan without success semantic events. | `app/stages/hic_scaffolding/method_application/support.py`; `tests/test_hic_scaffolding.py` | [AutoHiC](topics/autohic.md), [Stage contracts](reference/stage-contracts.md) |

## Stage node inventory

These rows classify concrete node groups. Nested method nodes are expanded in
the method inventory rather than being hidden inside one stage-level color.

### Inventory and input preparation

| Stage node or capability group | Wiring | Implementation | Verification | Debt | Evidence |
|---|---|---|---|---|---|
| Inventory `enter -> validate_inputs -> register_assets -> commit` | `wired` | `implemented-no-process` | `tested` | `none` | Validated frozen input injection and registered-candidate wrapping preserve logical compressed non-empty input; an existing assembly is declared as an immutable reused delivery at occurrence settlement. `app/stages/inventory/builder.py`, `app/stages/inventory/nodes.py`; `tests/test_inventory.py`, `tests/test_frozen_inputs.py`, `tests/test_resource_resolution.py`, `tests/test_manifest.py` |
| Inventory `review_gate` and `fail` boundaries | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/inventory/builder.py`; `tests/test_inventory.py`, `tests/test_review_resume.py` |
| Read QC `enter`, `resolve_raw_long_reads`, planning, `commit`, `fail` | `wired` | `implemented-no-process` | `tested` | `none` | Direct resources resolve through frozen registry-backed provenance. A successful filter publishes a read-set manifest; a zero-retained result preserves collected evidence and fails the occurrence without replacing `long_reads`. `app/stages/read_qc/builder.py`, `app/stages/read_qc/nodes.py`; `tests/test_read_qc.py` |
| Read QC `seqkit_stats` nested method node | `wired` | `executable` | `tested` | `none` | One direct input-stream aggregate owns whole-read-set length, N50, and quality values; per-file rows are retained only for cross-checks and provenance follows the actual read inputs. `app/stages/read_qc/method_application/support.py`, `app/stages/read_qc/method_application/seqkit_stats.py`; `tests/test_read_qc.py` |
| Read QC `seqkit_length_filter` nested method node, including intentional skip | `wired` | `executable` plus `implemented-no-process` skip | `tested` | `none` | Filtering publishes a read-set manifest and direct post-filter aggregate. Its zero-retained path produces no downstream handoff; the intentional planner skip remains a no-process raw-read pass-through. `app/stages/read_qc/method_application/seqkit_length_filter.py`, `app/stages/read_qc/nodes.py`; `tests/test_read_qc.py` |
| Genome Survey input resolution and `select_survey_mode` | `wired` | `implemented-no-process` | `tested` | `none` | Stage-owned `short_reads | long_reads` resolution and explicit-size skip; `app/stages/genome_survey/nodes.py`, `app/stages/resource_specs.py`; `tests/test_genome_survey.py`, `tests/test_genome_size.py` |
| Genome Survey `jellyfish -> genomescope2` method chain | `wired` | `executable` | `tested` | `none` | Indexed k-mer `ArtifactRefJson` handoff with no string compatibility branch; `app/stages/genome_survey/method_application/`; `tests/test_genome_survey.py` |
| Genome Survey `write_full_survey_result`, explicit-prior skip, `commit`, `fail` | `wired` | `implemented-no-process` | `tested` | `none` | Provided-only mode persists a completed provided result while selected survey methods settle typed durable dispositions independently of the occurrence terminal. `app/stages/genome_survey/builder.py`, `app/stages/genome_survey/nodes.py`; `tests/test_genome_survey.py`, `tests/test_genome_size.py` |

### Assembly production, review, and chromosome finishing

| Stage node or capability group | Wiring | Implementation | Verification | Debt | Evidence |
|---|---|---|---|---|---|
| Assembly input/profile route: `resolve_extended_inputs -> infer_input_profile -> select_route` | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/assembly/builder.py`; `tests/test_assembly.py`, `tests/test_multiplatform_long_reads.py` |
| Assembly selection: `build_assembly_candidate_set -> select_primary` | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/assembly/nodes.py`; `tests/test_assembly.py`, `tests/test_assembly_candidate_bundle.py` |
| Assembly `recommend_parameters` and mandatory approval block | `wired` | `implemented-no-process` model/evidence workflow | `tested` | `known-gap` | Formal schema-3 flow is implemented; method-specific knowledge coverage is uneven; `tests/test_parameter_recommendation_v3_contract.py`, `tests/test_phase11_parameter_approval.py` |
| Assembly `flye`, `nextdenovo`, `hifiasm`, `hifiasm_ont` nested method nodes | `wired` | `executable` | `tested` | method-specific below | `app/stages/assembly/method_application/`; `tests/test_assembly.py` |
| Assembly `execution_retry_gate`, `commit`, `fail`, `block` | `wired` | `implemented-no-process` | `tested` | `none` | A completed occurrence requires an executed candidate and declares a materialized immutable delivery. Planning-only/non-local paths cannot advance; every reject/failure path settles the active occurrence explicitly. `app/stages/assembly/parameters/workflow.py`, `app/stages/assembly/nodes.py`; `tests/test_assembly.py`, `tests/test_phase11_parameter_approval.py`, `tests/test_review_resume.py` |
| Dedup `enter -> plan -> purge_dups -> commit/fail` | `wired` | `executable` | `tested` | `none` | Local execution plus explicit automatic-purge approval can produce a completed/materialized delivery. Missing approval or local authorization completes as pass-through with a skipped `purge_dups` disposition and explicit reused delivery; no materialized candidate is failed with `not_started` and no delivery. `app/stages/assembly_dedup/`; `tests/test_assembly_dedup.py` |
| Assembly Review reusable subgraph, candidate resolution, and `quast -> busco` metrics | `wired` | `executable` | `tested` | `none` | Candidate resolution requires the frozen producer occurrence's completed terminal, exactly one immutable delivery, matching current `ResourceRef`, and the matching indexed candidate/primary bundle before evaluation. `app/stages/assembly_review/`, `app/storage/assembly_candidates.py`; `tests/test_assembly_review.py`, `tests/test_assembly_candidate_bundle.py` |
| Assembly Review rule, review-owned parameter-quality, related-species, comparison nodes | `wired` | `implemented-no-process` | `tested` | `none` | Parameter-quality diagnosis remains in the active `assembly_review` occurrence; it records no Assembly retry cursor or parameter evidence and continues to related-species/human review. `app/stages/assembly_review/nodes.py`; `tests/test_phase10_parameter_catalog.py`, `tests/test_related_species_evidence.py`, `tests/test_related_species_statistics.py`, `tests/test_assembly_review_statistics_integration.py` |
| Assembly Review `human_gate` and adoption/resume transaction | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/assembly_review/builder.py`, `app/control/resume_transaction.py`; `tests/test_review_resume.py` |
| Hi-C `resolve_hic_inputs` and requirement boundary | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/hic_scaffolding/`; `tests/test_hic_scaffolding.py` |
| Hi-C `recommend_autohic_parameters` and approval/retry gates | `wired` | `implemented-no-process` model/evidence workflow | `tested` | `none` | `app/stages/hic_scaffolding/parameters/`; `tests/test_hic_scaffolding.py`, `tests/test_parameter_resolution_validation.py` |
| Hi-C `autohic` nested method node and collection | `wired` | `executable` | `tested` | `none` | Real runner plus fail-closed validation of FASTA, `.hic`, `.assembly`, and HTML, including replay and late-publication failure coverage; `app/stages/hic_scaffolding/method_application/`; `tests/test_hic_scaffolding.py` |
| Hi-C `chromosome_assembly_review`, block, and adoption resume | `wired` | `implemented-no-process` | `tested` | `none` | Candidate construction and current-resource replacement declare a materialized or reused assembly delivery, which shared settlement proves against its indexed artifacts. `app/stages/hic_scaffolding/builder.py`, `app/stages/hic_scaffolding/nodes.py`; `tests/test_hic_scaffolding.py`, `tests/test_assembly_candidate_bundle.py`, `tests/test_review_resume.py` |
| GapCloser HiFi execution and adoption | `wired` | `executable` | `tested` | `bounded` | TGS-GapCloser no-correction route, native coordinate validation, durable manual adoption and materialized/reused assembly delivery; `app/stages/gapcloser/`; `tests/test_gapcloser.py`. Representative-sample acceptance remains open. |
| T2T input and motif chain: `resolve_inputs -> resolve_telomere_motif` | `wired` | `implemented-no-process` with conditional model call | `tested` | `none` | `app/stages/t2t/parameters/workflow.py`; `tests/test_t2t_motif.py` |
| T2T threshold recommendation, approval, and `prepare_parameters` | `wired` | `implemented-no-process` model/evidence workflow | `tested` | `none` | `app/stages/t2t/parameters/`; `tests/test_t2t.py`, `tests/test_parameter_recommendation_v3_contract.py` |
| T2T scans and minimap2 alignment | `wired` | `executable` | `tested` | `none` | `scan_assembly`, `prepare_reads`, `scan_reads`, `alignment`; `app/stages/t2t/nodes.py`; `tests/test_t2t.py` |
| T2T patch, validation, report, adoption gate, commit/fail | `wired` | `implemented-no-process` | `tested` | `none` | Candidate construction/current-resource replacement declare a materialized or reused assembly delivery, which shared settlement proves against its indexed artifacts. `apply_patches`, `validate_patched`, `report`, `gate_review`; `app/stages/t2t/nodes.py`; `tests/test_t2t.py`, `tests/test_assembly_candidate_bundle.py`, `tests/test_review_resume.py` |
| Base Polish | `wired` | `executable` | `tested` | `bounded` | HiFi Racon, paired minimap2 evidence and manual handoff; `tests/test_base_polish.py` |

## Registered method inventory

`descriptors/registry.yaml` currently contains 16 registered methods. Fifteen
declare `execution_kind: local`; the executable invariant binds each ID to an
imported lifecycle, nested method graph, and outer stage node. The sole
`planner_only` method is an intentional no-process choice whose real
planner-to-stage route is tested. There are currently **no descriptor-only
placeholder methods in the registry**. Descriptor loading is strict at the
registry and every nested YAML model boundary.

The `Adapter/lifecycle` and `Collection` columns below describe executable
process wiring only. They do not override the cross-cutting evidence and
settlement debt above.

| Registered method | Descriptor | Adapter/lifecycle | Collection | Verification | Debt or limitation |
|---|---|---|---|---|---|
| `read_qc.long_read_stats.seqkit_stats` | registered | `executable` | implemented | `tests/test_read_qc.py` | direct whole-read-set aggregate with per-file consistency checks and a typed terminal disposition |
| `read_qc.length_filter.seqkit_seq` | registered | `executable` | implemented | `tests/test_read_qc.py` | zero retained reads fail the occurrence without a handoff; requires approved `min_len`; selected executions settle typed outcomes |
| `read_qc.length_filter.skip` | registered | `implemented-no-process` (`planner_only`) | not applicable | `tests/test_read_qc.py`, `tests/test_ask_plan.py` | deliberate raw-read pass-through with a completed no-process disposition |
| `genome_survey.kmer_count.jellyfish` | registered | `executable` | implemented | `tests/test_genome_survey.py` | typed k-mer artifact publication |
| `genome_survey.model.genomescope2` | registered | `executable` | implemented | `tests/test_genome_survey.py` | typed k-mer artifact dependency |
| `assembly.candidate.flye` | registered with catalog | `executable` | implemented | `tests/test_assembly.py`, `tests/test_configuration.py` | none |
| `assembly.candidate.hifiasm` | registered with catalog | `executable` | implemented | `tests/test_assembly.py`, `tests/test_hifiasm_evidence_case.py` | richest current literature case coverage |
| `assembly.candidate.hifiasm_ont` | registered with catalog | `executable` | implemented | `tests/test_assembly.py`, `tests/test_hifiasm_numeric_retrieval.py` | structured guidance exists; numeric case corpus is Hifiasm-centered |
| `assembly.candidate.nextdenovo` | registered with catalog | `executable` | implemented | `tests/test_assembly.py` | catalog exists; no equivalent literature case corpus |
| `assembly_dedup.purge_dups` | registered | `executable` | implemented | `tests/test_assembly_dedup.py` | durable `completed`, `skipped`, or `not_started` disposition is orthogonal to the delivered occurrence and its immutable delivery |
| `assembly_review.eval.quast` | registered | `executable` | implemented | `tests/test_assembly_review.py` | stage-local review, not final integrated QA |
| `assembly_review.eval.busco` | registered | `executable` | implemented | `tests/test_assembly_review.py`, `tests/test_database_resolution.py` | manifest NCBI scientific name selects one compatible host-installed lineage; a RunConfig lineage, when present, is assertion-only; path/version/provenance resolve from the frozen host registry |
| `hic_scaffolding.candidate.autohic` | registered with catalog | `executable` | implemented | `tests/test_hic_scaffolding.py` | all four review products are mandatory at collection |
| `gapcloser.tgs_gapcloser` | registered | `executable` | implemented | `tests/test_gapcloser.py` | HiFi only; manual adoption; coordinate consistency is not a QV claim |
| `base_polish.racon` | registered | `executable` | implemented | `tests/test_base_polish.py` | HiFi only; paired read agreement is not independent QV; adoption or source retention |
| `t2t.alignment.minimap2` | registered with catalog | `executable` | implemented | `tests/test_t2t.py` | none |
`tests/test_registry_executable_invariant.py` derives local IDs from imported
`MethodExecutionLifecycle` bindings, proves those bindings cover exactly the 15
registered local methods, and verifies each shared freeze/run/collect and outer
stage route. It derives the planner-only set from the production planner catalog
and exercises `read_qc.length_filter.skip` through raw-read commit while proving
that no filter frozen attempt, runner call, or method-scoped attempt event is
created. The two sets are disjoint and their union equals the complete registry.

## Parameter capability map

Parameter capability is not one Boolean. Catalog, recommendation, approval,
freeze binding, retry, and biological knowledge coverage are separate facts.

| Scope | Static catalog | Recommendation and immutable evidence | Human approval | Frozen-attempt binding | Retry | Current limitation |
|---|---|---|---|---|---|---|
| Assembly: Flye | implemented | implemented | mandatory | implemented | implemented | catalog present; no method-specific literature case corpus comparable to Hifiasm |
| Assembly: Hifiasm | implemented | implemented | mandatory | implemented | implemented | current evidence retrieval is concentrated here |
| Assembly: Hifiasm-ONT | implemented | implemented | mandatory | implemented | implemented | structured Hifiasm guidance exists; numeric cases remain Hifiasm-centered |
| Assembly: NextDenovo | implemented | implemented | mandatory | implemented | implemented | catalog present; no equivalent literature case corpus |
| AutoHiC | implemented | implemented | mandatory | implemented | implemented | collection fail-closes on a missing declared product; terminal collect failure has no automatic retry |
| T2T/minimap2 thresholds | implemented | implemented after motif resolution | mandatory | implemented | initial recommendation only | no T2T parameter-retry route |
| GapCloser | static bounded RunConfig | no LLM recommendation | manual result adoption | shared frozen attempt | no automatic retry | HiFi-only TGS-GapCloser |
| Base Polish | `base_polish.racon` | minimap2/Racon lifecycle | paired PAF/FASTA/version evidence | manual adoption | source or polished candidate | HiFi only; no independent QV |
| Read QC, Survey, Dedup, Review | registry-derived default methods | not implemented as formal recommenders | not applicable | normal whole-run freeze | not applicable | defaults are internal; do not expose these as user method-selection fields |
| Inventory | not required | not required | input ambiguity review only | not applicable | not applicable | no recommender should be inferred |

The formal schema-3 recommendation chain for Assembly, AutoHiC, and T2T is
implemented. The remaining gap is not “parameter recommendation is missing
everywhere”; it is that method-specific biological knowledge depth is uneven
and most other stages deliberately use static/operator configuration.

## Finishing capabilities and remaining product gaps

Executable finishing routes have explicit input limits and still require
representative server acceptance. `placeholder` identifies a capability without
an executable closed loop; it does not mean the biological capability runs.

| Product capability | Wiring | Implementation | Verification | Debt | Evidence and owner |
|---|---|---|---|---|---|
| Gap closing after chromosome scaffolding | `wired` | `executable` | `tested` | `bounded` | HiFi-only TGS-GapCloser; representative biological acceptance remains open; `tests/test_gapcloser.py`; [stage contract](reference/stage-contracts.md#8-gapcloser) |
| Automated base polishing | `wired` | `executable` | `tested` | `bounded` | HiFi only, representative acceptance pending; `tests/test_base_polish.py`; [stage contract](reference/stage-contracts.md#10-base-polish) |
| Finish Review after Base Polish | `wired` | `executable` | `tested` | `bounded` | Paired source/polished QUAST/BUSCO evaluation; [stage contract](reference/stage-contracts.md#10-base-polish) |
| TUI midstage entry from clean reads or an existing assembly | `unwired` in the public TUI | `placeholder` as accepted design | `partially-tested` | `known-gap` | See [TUI midstage entry](topics/tui-midstage-entry.md) |

The finish Review is a second occurrence of the reusable `assembly_review`
stage, not a separate “final QA” stage. Source retention is valid. An
operator-provided polished assembly is not evidence of an executed method.

Maintenance rules are owned by
[Architecture Governance](reference/governance.md).
