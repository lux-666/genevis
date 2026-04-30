# Phase Completion Plan

This is the implementation roadmap. `docs/handoff.md` records current status
and the next best task. `docs/stage_contracts.md` defines stage behavior.

## Phase 0: Scaffold and Contracts

Goal: establish the repository shape, state contracts, stage names, descriptor roots, and documentation.

Exit criteria:
- `app/`, `config/`, `descriptors/`, `docs/`, and `tests/` roots exist.
- `TopLevelState` stores only small control fields and references.
- Stage order matches the ONT/CLR workflow.
- Structure tests pass.

Current state: Phase 0 is structurally complete. Future changes in Phase 0
should be limited to correcting contracts or documentation gaps discovered while
starting Phase 1.

## Phase 1: Parent Graph and Inventory MVP

Goal: run the parent graph through inventory with a real input manifest and asset registry output.

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

## Phase 2: QC, Survey, and Assembly Planning

Goal: produce deterministic stage plans without running heavy tools by default.

Tasks:
- Add read QC planner rules for NanoPlot, SeqKit, Filtlong optional, SOAPnuke, and FastQC.
- Add genome survey skip/run policy: run only with `clean_short_dna_reads`.
- Add assembly candidate selection for Flye, NextDenovo, and Canu.
- Add descriptors for the first package/method set.
- Add template-mode FrozenAttemptSpec generation.
- Add plan artifact writing so large planner details do not live in graph state.

Exit criteria:
- Long-read-only inputs skip genome survey with `no_low_error_short_reads`.
- Short DNA reads enable Jellyfish/GenomeScope planning.
- Assembly chooses a primary method and one fallback at most.
- NextDenovo and Canu require genome size; Flye can proceed without it unless
  memory-saving options require a size.

## Phase 3: Runner, Ledger, and First Real Execution

Goal: run one real local or remote command path end to end.

Tasks:
- Implement LocalRunner and artifact collection.
- Implement RemoteRunner using the existing SSH ControlMaster pattern from `rna_agent`.
- Add ledger reconciliation for attempt status and artifacts.
- Execute one light method first, preferably SeqKit stats or QUAST on a fixture.

Exit criteria:
- A frozen attempt runs, writes logs, collects declared artifacts, and records ledger events.
- Failed attempts write stderr/log references and do not corrupt graph state.

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
