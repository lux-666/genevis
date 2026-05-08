# Phase Completion Plan

This is the long-range implementation roadmap. `docs/handoff.md` records current
status and the next best task. `docs/stage_contracts.md` defines stage behavior.
Operational phase/task handoffs live under `docs/phases/phase*/`.

## Phase 0: Scaffold and Contracts

Goal: establish the repository shape, state contracts, stage names, descriptor roots, and documentation.

Exit criteria:
- `app/`, `config/`, `descriptors/`, `docs/`, and `tests/` roots exist.
- `TopLevelState` stores only small control fields and references.
- Stage order matches the ONT/CLR workflow.
- Structure tests pass.

Current state: Phase 0 is structurally complete.

## Phase 1: Parent Graph and Inventory MVP

Goal: run the parent graph through inventory with a real input manifest and asset registry output.

Current state: Phase 1 inventory corrections are documented in
`docs/phases/phase1/phase1.md`.

Tasks:
- Add manifest tests first:
  - raw ONT/CLR reads
  - short DNA reads
  - RNA-seq reads
  - Iso-seq or long RNA reads
  - Hi-C reads
  - existing assembly
  - ambiguous long-read vs long-RNA inputs
- Implement manifest schema and validation.
- Implement file existence, extension/format, size, compression, sample ID, and
  platform metadata checks.
- Implement deferred checksum status for huge files.
- Write asset registry JSON artifact.
- Write ledger session and asset registration events.
- Add CLI `gene-agent run --session-id ... --manifest ...`.
- Replace inventory placeholder routing with real success/failure/review paths.

Exit criteria:
- A manifest-only run registers assets and advances to `read_qc`.
- Ambiguous long-read vs long-RNA inputs stop with a clear review requirement.
- Existing assembly inputs can skip read QC, survey, and assembly planning later.
- Tests prove `TopLevelState` stores only references to registry artifacts.

Files likely touched:

- `app/schemas/assets.py`
- `app/schemas/state.py`
- `app/stages/inventory/`
- `app/control/ledger.py`
- `app/entrypoints/run.py`
- `tests/`
- `docs/handoff.md`

## Phase 2: CycloneSEQ Contracts And First Method Slice

Goal: design the full CycloneSEQ FASTQ / FASTQ.gz genome assembly and annotation
workflow, then run one narrow real method path first:
`read_qc.long_read_stats -> SeqKit stats`.

Current state: Phase 2 design tasks are documented in
`docs/phases/phase2/phase2.md`.

Tasks:
- Define the CycloneSEQ stage contract for:
  - inventory
  - read_qc
  - genome_survey
  - assembly
  - assembly_review
  - hic_scaffolding
  - repeat_annotation
  - gene_annotation
  - function_annotation
- Define read QC substeps:
  - long-read stats with SeqKit stats, NanoPlot, or pyfastx
  - length filtering with seqkit seq, Filtlong, fastplong, or chopper
  - FOFN generation for stable assembly input
- Define genome_survey full mode with short DNA reads and fallback mode with
  CycloneSEQ-only long reads.
- Define assembly candidate set: Flye, hifiasm, NextDenovo, Canu.
- Define assembly_review gates with QUAST, BUSCO, Merqury, read mapping
  coverage, purge_dups_check, and contamination_check.
- Define Hi-C skip/scaffold policy, repeat annotation, gene annotation, and
  function annotation contracts.
- Add descriptors and FrozenAttemptSpec support for the first SeqKit stats path.
- Keep unclear thresholds, resume payloads, requested-stage semantics, and tool
  candidate changes as human-decision points.

Exit criteria:
- CycloneSEQ FASTQ / FASTQ.gz inputs are documented as `raw_long_reads`.
- The full stage/method/artifact contract is documented with checkbox handoffs.
- The first executable slice can be implemented without touching assembly,
  annotation, remote execution, or automatic retry.
- Coding assistants are explicitly told to ask the human before inventing
  thresholds, requested-stage semantics, resume payloads, or broad candidate-set
  changes.

## Phase 3: Natural Entry, Review Resume, and Real Execution

Goal: make the coordinator usable from natural language, resume human review gates,
and then add more real execution slices one scoped method at a time.

Current state: Phase 3 planning is documented in `docs/phases/phase3/README.md`.

Tasks:
- Add a natural-language entry parser that produces the existing structured CLI /
  `initial_state(...)` contract without becoming a second workflow engine.
- Add session status/inspect plus human review/resume semantics for
  `assembly_review` and `hic_scaffolding`, with structured decisions, artifacts,
  and ledger events.
- Execute one additional light method after entry/resume are in place, preferably
  QUAST on a tiny fixture or another narrow descriptor-backed slice.
- Keep `RemoteRunner`, requested-stage routing, and broad method execution out of
  scope until their semantics are explicit.

Exit criteria:
- A natural-language request can start the existing graph path through a validated
  structured request.
- A user can inspect a session by `session_id` and see current stage,
  supervision state, active interrupt, and latest artifact refs.
- A blocked review state can resume with a validated decision and auditable
  record without restarting from `initial_state(...)`.
- The next execution slice follows descriptors, frozen attempts, runner boundary,
  artifact collection, and ledger events without duplicating YAML/config roles.

## Phase 4: Assembly Review Gate

Goal: evaluate assembly and pause for human decisions before polish/purge actions.

Tasks:
- Add QUAST/BUSCO/Merqury descriptors and review rules.
- Add interrupt-backed `assembly_review` gate.
- Add conditional planning for NextPolish SGS/LGS, purge_dups, and Redundans.
- Preserve rejected candidates with provenance.

Exit criteria:
- Review metrics produce an approval/skip/action request.
- Resume continues with selected polish/purge actions.
- Rejected polish or purge candidates remain traceable in ledger/artifacts.

## Phase 5: Hi-C, Repeat, Gene, and Function Annotation

Goal: complete the downstream annotation backbone with descriptor-driven method families.

Tasks:
- Add Hi-C scaffolding descriptors and Juicebox manual review bundle.
- Add RepeatModeler/RepeatMasker/TRF method families.
- Add AUGUSTUS, GeMoMa, Miniprot, HISAT2/StringTie, IsoQuant, and optional EVM/ANNEVO paths.
- Add InterProScan, DIAMOND, and KofamScan function annotation.

Exit criteria:
- Existing assembly inputs can enter repeat/gene/function annotation without rerunning assembly.
- Predicted proteins produce at least one function annotation table.
- Database versions and snapshots are recorded in provenance.

## Phase 6: Observability and Durable Operation

Goal: make runs auditable and resumable for long projects.

Tasks:
- Add user/developer notebook traces.
- Add sqlite/postgres checkpointer.
- Add lifecycle commands: pause, resume, retry, abort, review.
- Add database version provenance for annotation resources.

Exit criteria:
- A stopped session resumes from checkpoint and ledger state without duplicate side effects.
- User-facing reports explain what was run, what was skipped, and what requires
  human attention.
