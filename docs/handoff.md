# Agent Handoff

This is the first file a future agent should read when taking over this
repository.

## Current Status

The project has a Phase 1 implementation commit:

```text
fad12d3 Implement Phase 1: Parent Graph and Inventory MVP
```

The Phase 1 correction pass is captured in `docs/phases/phase1/phase1.md`, with
task details under `docs/phases/phase1/`.

Phase 2 planning is captured in `docs/phases/phase2/phase2.md` and the numbered
task files under `docs/phases/phase2/`. Phase 2 is CycloneSEQ-focused: build the
single-line FASTQ / FASTQ.gz genome assembly and annotation contract from
inventory through function annotation, with the first real executable method
slice `read_qc.long_read_stats -> SeqKit stats`.

Completed:

- Repository scaffold exists.
- Parent graph routes through all genome domain stages and handles terminal failures.
- Shared schema contracts exist for state, assets, plans, descriptors, artifacts,
  commands, events, and manifests.
- Descriptor roots and initial ONT/CLR asset policy exist.
- Documentation now defines architecture, governance, stage contracts, descriptor
  rules, and phase plan.
- Structure tests cover stage order, initial state shape, and required document
  roots.
- **Input manifest schema** (`InputManifest`, `InputAssetSpec`) validates asset
  types, file existence, format, compression, platform, and layout.
- **Inventory stage** loads manifests, validates inputs, registers assets, writes
  the asset registry JSON artifact, and records ledger events.
- Inventory routing supports success, failure, and review-required paths.
- Ambiguous long-read vs long-RNA inputs trigger an `active_interrupt` and stop
  at inventory.
- Existing assembly inputs route to `assembly_review` and publish skip reasons.
- CycloneSEQ FASTQ / FASTQ.gz inventory input is accepted as genomic long-read
  platform metadata and registers as `raw_long_reads`.
- `read_qc` is the first realized production stage. It resolves `raw_long_reads`,
  runs or preserves `read_qc.long_read_stats.seqkit_stats`, plans optional length
  filtering, executes exactly one approved `seqkit seq` min-length filter when
  explicitly requested, writes `long_reads.fofn` or `filtered_long_reads.fofn`,
  records artifacts/ledger events, and commits only compact summaries plus refs to
  graph state.
- `genome_survey` has a real conditional stage. It chooses full k-mer survey
  planning when `clean_short_dna_reads` exists, can opt into local Jellyfish +
  GenomeScope2 execution with explicit `survey_parameters`, writes a
  low-confidence CycloneSEQ fallback record for long-read-only inputs, or writes
  `skip_reason: no_low_error_short_reads` when no usable survey input exists.
- `assembly` has a real deterministic candidate planning stage plus an explicit
  opt-in local Flye execution route. It resolves a read QC FOFN or registered
  `clean_long_reads`, auto-plans Flye plus compatible NextDenovo/Canu routes for
  ONT/CLR, keeps hifiasm as an explicit HiFi/non-ONT compatibility route, writes
  `draft_assembly.candidate` JSON bundles for at most one primary plus one
  fallback, can run the primary Flye candidate locally when
  `stage_outputs.assembly.execution_mode == "local"`, registers a real draft
  assembly FASTA plus command artifacts, and routes candidate refs to
  `assembly_review`.
- `assembly_review` has a real human-gated review stage plus an explicit opt-in
  local QUAST evaluation route. It consumes existing assembly inputs or assembly
  candidate bundle refs, plans QUAST/BUSCO/Merqury/mapping/purge/contamination
  signals when inputs permit, can run QUAST locally for materialized draft FASTA
  candidates when `stage_outputs.assembly_review.execution_mode == "local"`,
  registers QUAST report artifacts and non-decisional metrics, records review and
  attempt ledger events, auto-accepts only existing assembly inputs, blocks
  assembly-stage candidates for human decision without inventing thresholds, and
  records post-review pass/purge/polish action provenance before publishing
  `final_assembly_for_scaffolding_or_annotation`.
- `hic_scaffolding` has a real scaffold/skip planning stage plus an explicit
  opt-in local YaHS route. It consumes the reviewed assembly, resolves
  registered Hi-C read/alignment assets, writes explicit skip records when Hi-C
  is absent or chromosome-level assembly was not requested, can run YaHS locally
  when `stage_outputs.hic_scaffolding.execution_mode == "local"`, YaHS is explicitly
  selected, and a materialized Hi-C alignment input exists, registers scaffold candidate/review
  artifacts, preserves the pre-Hi-C assembly as the annotation fallback, and
  blocks for contact-map human review before chromosome-scale promotion.
- `repeat_annotation` has a real planning stage plus an explicit opt-in local
  RepeatMasker route. It resolves `final_assembly_for_annotation`, handles
  provided or de novo repeat library strategy, can run RepeatMasker locally when
  `stage_outputs.repeat_annotation.execution_mode == "local"`, RepeatMasker is
  explicitly selected, and a provided repeat library exists, registers real
  softmasked genome/repeat GFF artifacts, and passes `softmasked_genome_ref` to
  gene annotation.
- `gene_annotation` has a real planning stage that consumes softmasked genome,
  records evidence-aware candidate methods, and publishes planned `proteins.fa`.
- `function_annotation` has a real terminal planning stage that consumes planned
  proteins, records InterProScan/eggNOG-mapper/DIAMOND/KofamScan candidates,
  requires database provenance, and completes the workflow at `done`.
- CLI `gene-agent run --session-id ... --manifest ...` executes the full graph.
- CLI `gene-agent ask "..."` can start from user data files or folders by
  extracting source paths and stable hints, materializing
  `data/manifests/{session_id}/manifest.yaml`, and then reusing the structured
  `run --manifest ...` path. Existing `.yaml` / `.json` manifests still pass
  through directly.
- The misleading `--stages` CLI flag is intentionally not exposed.
- Phase 2 planning docs follow the `rna_agent` implementation-plan style and are
  split by real workflow progress: CycloneSEQ FASTQ inventory, SeqKit stats,
  read filtering/FOFN, genome survey, assembly candidates, assembly review,
  Hi-C scaffold/skip, repeat annotation, gene annotation, function annotation,
  and verification handoff.
- Phase 2 task docs 01-10 include concise implementation/file-change notes for
  onboarding without reading all stage code first.
- Phase 2 documentation defines the CycloneSEQ target backbone, read QC
  substeps, genome_survey full/fallback modes, assembly candidates,
  assembly_review gates, Hi-C skip/scaffold policy, repeat/gene/function
  annotation contracts, artifact boundaries, and human-help triggers.
- Phase 4 task1 ships the environment resolution layer:
  `config/envs.yaml` carries stage/method conda env mappings;
  `app/execution/env_resolver.py` resolves method > stage > default
  precedence, accepts either `conda_env` names or `conda_prefix` paths, and
  exposes a `preflight(...)` probe that returns `ok` / `blocked_env` /
  `missing_tool` without requiring shell activation.
  `FrozenAttemptSpec` now carries optional `conda_bin`, `conda_env`, and
  `conda_prefix`, and `LocalRunner` dispatches through `conda run` when
  either env field is set.
- Phase 4 task2 wires `read_qc` into the shared env dispatcher
  (`app/execution/env_dispatch.py`): when the caller does not supply an
  explicit `stage_outputs.read_qc.env.PATH` bypass, preflight runs before
  each SeqKit attempt is frozen. Preflight failures set
  `stage_outputs.read_qc.status` to `blocked_env` or `missing_tool`, emit
  an `env_preflight_blocked` ledger event, and terminate the stage. A
  missing raw_long_reads path surfaces as `blocked_input`.
  `collect_seqkit_artifacts` also writes and registers
  `read_qc_summary.json` (artifact type `read_qc.long_read_summary`).
  Inventory now absolutises relative manifest paths against the manifest
  directory so attempt scripts that chdir into `data/work/...` can reach
  the real files. CLI `gene-agent run` has a repeatable `--local-exec STAGE`
  flag that seeds `stage_outputs[STAGE].execution_mode = "local"`. Committed
  fixtures live under `tests/fixtures/phase4/`; a real-data smoke test
  (`Phase4ReadQCRealSmokeTests`) runs end-to-end when `env_geno_exec` with
  `seqkit` is present and is skipped otherwise.

Not completed:

- No broad executable bioinformatics method catalog beyond the realized SeqKit,
  Jellyfish/GenomeScope2, Flye, explicit QUAST, explicit YaHS, and explicit
  RepeatMasker slices.
- RemoteRunner execution is not implemented.
- Local polish/purge action execution, raw Hi-C alignment generation,
  Juicer/3D-DNA/Juicebox execution, de novo repeat library execution, gene
  annotation execution, and function annotation execution are not implemented.
- Durable cross-process graph resume is not implemented because the scaffold still uses a memory-only LangGraph checkpointer. In-process review resume helpers exist for `assembly_review` and `hic_scaffolding`, and CLI `inspect` can read durable ledger/artifact records.

## Read Order

Read these files in order:

1. `CLAUDE.md`
2. `deep-research-report.md`
3. `docs/governance.md`
4. `docs/architecture.md`
5. `docs/stage_contracts.md`
6. `docs/descriptor_spec.md`
7. `docs/phases/phase1/phase1.md`
8. `docs/phases/phase2/phase2.md`
9. `docs/phases/phase2/01_inventory_cycloneseq_fastq.md`
10. `docs/phases/phase2/02_read_qc_seqkit_stats.md`
11. `docs/phases/phase2/03_read_qc_filter_fofn.md`
12. `docs/phases/phase2/04_genome_survey_modes.md`
13. `docs/phases/phase2/05_assembly_candidates.md`
14. `docs/phases/phase2/06_assembly_review_gate.md`
15. `docs/phases/phase2/07_hic_scaffolding.md`
16. `docs/phases/phase2/08_repeat_annotation.md`
17. `docs/phases/phase2/09_gene_annotation.md`
18. `docs/phases/phase2/10_function_annotation.md`
19. `docs/phases/phase2/11_phase2_verification_handoff.md`
20. `docs/phases/phase_completion_plan.md`
21. This file again

Use `/Users/a1/Documents/BGIwork/rna_agent` as a reference for patterns only.
Do not copy single-cell business names into this project.

## Verification Commands

Use the conda environment already used for scaffold verification:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_assembly_review tests.test_hic_scaffolding -v
```

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
```

Graph smoke:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -c "from app.graph.builder import build_graph; from app.schemas.state import initial_state; g=build_graph(); s=initial_state(session_id='smoke', input_manifest_ref='inputs/manifest.yaml'); r=g.invoke(s, config={'configurable': {'thread_id': 'smoke'}}); print(r['current_stage']); print(r['route_trace'])"
```

Expected graph smoke output:

```text
done
['inventory_stage', 'read_qc_stage', 'genome_survey_stage', 'assembly_stage', 'assembly_review_stage', 'hic_scaffolding_stage', 'repeat_annotation_stage', 'gene_annotation_stage', 'function_annotation_stage']
```

The `PYTHONPYCACHEPREFIX` is important in this sandbox because default Python
bytecode cache writes may try to use `~/Library/Caches`, which is not writable.

## Next Best Task

Continue Phase 4 from task 03 (`docs/phases/phase4/03_flye_quast_human_gate.md`) — wire `assembly` (Flye) and `assembly_review` (QUAST) frozen attempts to `EnvResolver` with the same blocked_env / missing_tool / blocked_param vocabulary, and prove the mainline smoke through the human gate. Do not start local polish/purge execution, repeat/gene/function execution, or broader Hi-C alignment/manual-review tooling without explicit human approval.

Do not implement broad assembly, annotation, requested-stage routing, database
downloads, genome-size conflict thresholds, or RemoteRunner before the
corresponding human decision is explicit in the task docs.

## Immediate Design Decisions Already Made

- Stage order is fixed in `app/stages/__init__.py`.
- `genome_survey` exists as a stage but must be skippable.
- Genome survey output is a prior for assembly, not a hard prerequisite.
- Assembly candidate planning may use a genome-size prior. The only real
  assembler execution path is explicit local Flye execution; default assembly
  behavior remains planning-only.
- Assembly review is the main human gate. Existing assembly input can
  auto-accept as a deterministic pass-through, but assembly-stage candidates do
  not auto-accept from QUAST metrics or without an approved review/resume policy.
- `hic_scaffolding` exists as a stage but must be skippable.
- Hi-C scaffold method choice and contact-map acceptance require human approval.
- Assembly review is the main human-gated decision point.
- `TopLevelState` should stay small and reference-based.
- Real method execution must go through descriptors, frozen attempt specs, and
  runners.
- Local execution is opt-in per stage output; the default graph path may plan
  SeqKit stats and continue without running it.
- Documentation is part of the product, not an afterthought.
- Function annotation database provenance is required before terminal commit;
  missing or incomplete provenance should fail rather than silently advance.

## Known Risks

- `DescriptorRegistry.load()` is scaffold-level only. It validates package/method
  references but does not yet validate asset compatibility.
- `inventory`, the realized SeqKit/read-filter/FOFN `read_qc` path,
  `genome_survey`, `assembly`, `assembly_review`, `hic_scaffolding`,
  `repeat_annotation`, `gene_annotation`, and `function_annotation` have real
  routing. Most stages after read QC are still planning/contract stages rather
  than executable bioinformatics methods.
- Genome survey has an opt-in local Jellyfish + GenomeScope2 path for
  `clean_short_dna_reads`, but Meryl and Smudgeplot remain planning placeholders
  only. Genome-size conflict review thresholds are intentionally not implemented.
- Assembly has an opt-in local Flye execution path for the primary ONT/CLR draft
  assembly candidate. NextDenovo and Canu remain planning placeholders and are not
  executed as automatic fallbacks. hifiasm is registered as an explicit
  HiFi/non-ONT compatibility route, not an ONT/CLR automatic candidate.
- Assembly review has an opt-in local QUAST route for materialized draft FASTA
  candidates and a post-review action provenance contract for pass/purge/polish
  promotion. BUSCO, Merqury, minimap2-based mapping, purge_dups,
  contamination checks, and local polish/purge action execution remain planning
  placeholders only.
- Hi-C scaffolding has an opt-in local YaHS route for materialized assembly plus
  Hi-C alignment inputs. Raw Hi-C read alignment, Juicer, 3D-DNA, and Juicebox
  remain planning/manual placeholders only.
- Repeat annotation has an opt-in local RepeatMasker route for provided repeat
  libraries. EDTA, RepeatModeler, and TRF remain planning placeholders only, and
  de novo repeat library generation is not executed.
- Gene annotation descriptors for BRAKER3, MAKER, AUGUSTUS, Miniprot, HISAT2,
  StringTie, IsoQuant, and GeMoMa are planning placeholders only; there is no
  execution path for those tools.
- Function annotation descriptors for InterProScan, eggNOG-mapper, DIAMOND, and
  KofamScan are planning placeholders only; there is no execution path or
  automatic database download for those tools.
- `LocalRunner` has only minimal subprocess execution; `RemoteRunner`
  intentionally remains unimplemented. When a frozen attempt sets
  `conda_env` or `conda_prefix`, `LocalRunner` dispatches through
  `conda run`; otherwise the legacy shell-based execution is used.
  `read_qc` populates those fields via `EnvResolver` when no PATH bypass is
  supplied. `assembly`, `assembly_review`, `hic_scaffolding`, and
  `repeat_annotation` stage nodes have not been rewired yet, so their local
  execution still trusts whatever shell env the operator configured.
- Review interrupts now have in-process structured resume helpers, but there is no durable cross-process `ResumeInterrupt` flow until a persistent checkpointer is added.
- Requested-stage routing is not implemented; the CLI flag is intentionally not
  exposed.
- SeqKit, Flye, QUAST, and YaHS are not installed in this local environment during
  verification; tests use temporary fake `seqkit`, `flye`, `quast.py`, and `yahs`
  executables for success paths and cover missing/failing binary behavior.

## Files Most Likely To Change In Phase 2

- `app/schemas/plan.py`
- `app/stages/read_qc/nodes.py`
- `app/stages/read_qc/builder.py`
- `app/stages/read_qc/routing.py`
- `app/stages/genome_survey/nodes.py`
- `app/stages/genome_survey/routing.py`
- `app/execution/local_runner.py`
- `app/schemas/commands.py`
- `app/schemas/artifacts.py`
- `config/defaults.yaml`
- `descriptors/packages/` and `descriptors/methods/`
- new tests under `tests/`
- docs touched when behavior changes

## Stop Conditions

Stop and ask the user before:

- changing the stage order
- removing any documented stage
- introducing a new top-level persistence backend
- adding a heavy external dependency
- making destructive filesystem or git changes
- running real bioinformatics tools on large data

## Handoff Update Rule

Before ending any substantial work session, update these sections:

- Current Status
- Next Best Task
- Known Risks
- Verification Commands, if commands changed
