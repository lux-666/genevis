# Agent Handoff

First file to read when taking over the repository. Records current state,
next best task, verification, and the rules that govern when to stop.

For per-phase implementation history, read the relevant `docs/phases/phaseN/`
files. For the doc map, read `docs/README.md`.

## Current Status

Phases 0–3 and Phase 4 in-code (tasks 01, 02, 03, 05, 06, 07) are complete.
Phase 5 task 01 (`genome_survey` env_dispatch wiring) is complete in code
and has server/HPC validation evidence.
Phase 5 task 03 is implemented and server/HPC validated for the existing
assembly -> RepeatMasker backbone. Phase 5 task 04 review policy is implemented
locally; server smoke should use an auto-mode fixture with database provenance
so the final review report is emitted on the normal successful terminal path.

The graph runs end-to-end through every documented stage. Real local
execution is opt-in per stage and is currently wired for: `read_qc`
(SeqKit), `genome_survey` (Jellyfish + GenomeScope2), `assembly` (Flye),
`assembly_review` (QUAST), `hic_scaffolding` (YaHS), `repeat_annotation`
(RepeatMasker for provided libraries). All other methods are
planning-only contracts.

Stage details and contracts live in `docs/stage_contracts.md`.
Per-phase task records live under `docs/phases/`. The full implemented
vs. not-implemented bullet list lives in the top-level `README.md`
("Current Implementation"); do not duplicate it here.

## Phase 4 Real Biology Acceptance Run Record

First real-biology smoke that satisfies Phase 4 task 04 for the Flye
mainline. QUAST was intentionally skipped on this arm64 host because
`env_geno_exec` lacks `quast.py` for arm64; `assembly_review` was kept
on the planning path. The chain advanced through real `read_qc` and
real `assembly.candidate.flye` and paused at the `assembly_review`
human gate without QUAST metrics.

Run parameters:

- host: `darwin-arm64`, `env_geno_exec` has `seqkit` and `flye` 2.9.6;
  no `quast.py`
- session id (CLI): `phase4-real-bht-20260513-a`
- manifest: `data/phase4_real_smoke/manifest.yaml`
- run config: `data/phase4_real_smoke/run_config.json`
  - `local_exec = ["read_qc", "assembly"]` (assembly_review omitted to
    skip QUAST)
  - `flye_parameters = { read_type: "nano-raw", estimated_genome_size:
    5_000_000, threads: 4 }`
- input: `data/test/bht.Nanopore.pass.first500.fa`
  (500 CycloneSEQ/Nanopore reads, 1,492,367 bp, avg 2,984.7 bp, min 71,
  max 8,571, N50 4,510; basecaller output packaged as FASTA)
- data dir: `data/phase4_real_smoke/out`

Observed outcome:

- `route_trace`: `inventory_stage → read_qc_stage → genome_survey_stage →
  assembly_stage`
- `current_stage`: `assembly_review`
- `supervision_state`: `blocked_human`
- `active_interrupt.reason`:
  `assembly_review_human_decision_required`
- `assembly.candidate.flye` status: `executed`
- draft assembly: 3 contigs, 15,263 bp total, min 3,161, avg 5,087.7,
  max 6,102 bp (as expected for far-sub-1× coverage against a 5 Mbp
  prior; biology is honest, not inflated)

Artifact trail (all under `data/phase4_real_smoke/out/artifacts/`,
recorded in `<data_dir>/artifacts/<session>/artifacts.jsonl`):

- `asset_registry/phase4-real-bht-20260513/registry.json`
- `read_qc/phase4-real-bht-20260513-a/seqkit_stats.tsv`
- `read_qc/phase4-real-bht-20260513-a/read_qc_summary.json`
- `read_qc/phase4-real-bht-20260513-a/long_reads.fofn`
- `assembly/phase4-real-bht-20260513-a/candidates/flye_candidate.json`
- `assembly/phase4-real-bht-20260513-a/flye/draft_assembly.fasta`
- `assembly_review/phase4-real-bht-20260513-a/assembly_eval_bundle.planned.json`
- `assembly_review/phase4-real-bht-20260513-a/assembly_review_request.json`
- Flye `flye.log` registered as an artifact under
  `assembly:assembly.candidate.flye:flye.log`

Three small bugs surfaced and were fixed in this pass (relative
`--data-dir` redirect target, manifest/CLI session id mismatch, path
escape in adopted manifest session id) plus an arm64 `quast.py`
environment note. Diagnoses and regression tests are in git log around
2026-05-14 — do not re-document here.

## Phase 4 Server/HPC QUAST Gate Run Record

Server/HPC validation that closes the Phase 5 task 01 server gate.

Run parameters:

- host: `task-p18z19700n0076dcs-south1-cocsckda-7bf85588f4-mj48h`
  under `/data/work/genevis`
- session id: `phase4-real-bht-20260515-e`
- manifest: `data/phase4_real_smoke/manifest.yaml`
- run config: `data/phase4_real_smoke/run_config.json`
  - `local_exec` includes `assembly_review`, so QUAST ran through
    `env_dispatch` / `LocalRunner` on the server
- data dir: `data/phase4_real_smoke/out`
- checkpoint backend: `sqlite`

Observed initial smoke outcome:

- `current_stage`: `assembly_review`
- `supervision_state`: `blocked_human`
- `route_trace`: `inventory_stage -> read_qc_stage -> genome_survey_stage ->
  assembly_stage`
- `assembly.candidate.flye` status: `executed`
- QUAST signal status: `available`
- QUAST metrics: `contig_count=3`, `largest_contig=6675`,
  `total_length=15971`, `gc_percent=51.0`, `n50=6135`, `l50=2`

Confirmed QUAST artifact refs:

- `/data/work/genevis/data/phase4_real_smoke/out/artifacts/assembly_review/phase4-real-bht-20260515-e/quast/phase4-real-bht-20260515-e_assembly.candidate.flye/quast_out/report.tsv`
- `/data/work/genevis/data/phase4_real_smoke/out/artifacts/assembly_review/phase4-real-bht-20260515-e/quast/phase4-real-bht-20260515-e_assembly.candidate.flye/quast_out/report.txt`
- `/data/work/genevis/data/phase4_real_smoke/out/artifacts/assembly_review/phase4-real-bht-20260515-e/quast/phase4-real-bht-20260515-e_assembly.candidate.flye/quast_summary.json`
- `data/phase4_real_smoke/out/artifacts/assembly_review/phase4-real-bht-20260515-e/assembly_review_request.json`

Resume smoke:

- command used `gene-agent resume --session-id phase4-real-bht-20260515-e
  --decision-file data/phase4_real_smoke/decision.json --data-dir
  data/phase4_real_smoke/out`
- resumed route:
  `inventory_stage -> read_qc_stage -> genome_survey_stage -> assembly_stage
  -> assembly_review_stage -> hic_scaffolding_stage ->
  repeat_annotation_stage -> gene_annotation_stage`
- final `current_stage`: `function_annotation`
- final `supervision_state`: `failed_terminal`
- expected error: `missing database_provenance for function_annotation`

This is the intended downstream stop for the current workflow: Phase 4
server execution and durable resume work, and function annotation still
requires explicit database provenance before terminal completion.

## Phase 5 Existing Assembly RepeatMasker Server Run Record

Server/HPC validation that closes Phase 5 task 03.

Run parameters:

- host: `task-p18z19700n0076dcs-south1-cocsckda-7bf85588f4-mj48h`
  under `/data/work/genevis`
- session id: `phase5-existing-asm-20260515-e`
- manifest: `/data/work/genevis/data/phase5_existing_asm_smoke/manifest.yaml`
- run config: `/data/work/genevis/data/phase5_existing_asm_smoke/run_config.json`
  - `local_exec = ["repeat_annotation"]`
- data dir: `/data/work/genevis/data/phase5_existing_asm_smoke/out`
- checkpoint backend: `sqlite`

Inputs:

- existing assembly:
  `/data/work/genevis/data/phase4_real_smoke/out/artifacts/assembly/phase4-real-bht-20260515-e/flye/draft_assembly.fasta`
- provided repeat library:
  `/data/work/genevis/data/phase5_existing_asm_smoke/provided_repeat_library.fa`

Observed outcome:

- `current_stage`: `function_annotation`
- `supervision_state`: `failed_terminal`
- expected error: `missing database_provenance for function_annotation`
- `route_trace`: `inventory_stage -> assembly_review_stage ->
  hic_scaffolding_stage -> repeat_annotation_stage ->
  gene_annotation_stage`
- smoke script reached:
  `== Phase 5 RepeatMasker smoke reached expected provenance gate ==`

Confirmed artifact refs:

- `/data/work/genevis/data/phase5_existing_asm_smoke/out/artifacts/repeat_annotation/phase5-existing-asm-20260515-e/repeat_annotation_bundle.json`
- `/data/work/genevis/data/phase5_existing_asm_smoke/out/artifacts/repeat_annotation/phase5-existing-asm-20260515-e/repeatmasker/phase5-existing-asm-20260515-e/softmasked_genome.fasta`
- `/data/work/genevis/data/phase5_existing_asm_smoke/out/artifacts/repeat_annotation/phase5-existing-asm-20260515-e/repeatmasker/phase5-existing-asm-20260515-e/repeat_annotation.gff3`
- `/data/work/genevis/data/phase5_existing_asm_smoke/out/artifacts/gene_annotation/phase5-existing-asm-20260515-e/gene_annotation_bundle.json`
- `/data/work/genevis/data/phase5_existing_asm_smoke/out/artifacts/asset_registry/phase5-existing-asm-20260515-e/registry.json`

This is the intended Phase 5 task 03 terminal condition: RepeatMasker and
gene planning are real/recorded, while function annotation refuses to
complete without explicit database provenance.

## Phase 5 Review Policy Run Record

Phase 5 task 04 added strict/auto review policy support. The local test fixture
records final reports at:

- `artifacts/final_report/<session>/review_decision_report.json`

Server acceptance is pending and split into two narrow smokes:

- `scripts/server_phase5_auto_review_smoke.sh` validates the deterministic
  `auto_rule` path and the final report. It uses the existing-assembly fixture
  (`data/phase5_auto_review/`), patches the runtime
  `<data_dir>/work/gene_annotation/<session_id>/proteins.fa` into
  `function_annotation.database_provenance.input_proteins_ref`, and asserts the
  workflow reaches `commit_function_annotation` and writes
  `review_decision_report.json` with at least one `auto_rule` decision. It does
  NOT validate LLM provisional review — `deterministic_safe` bypasses
  `human_review_gate`.
- `scripts/server_phase5_llm_review_smoke.sh` validates LLM provisional review.
  It uses the raw-inputs fixture (`data/phase5_llm_review/`), runs Flye + QUAST
  locally so eval signals become `available`, and asserts the
  `assembly_review.review_recommendation` artifact and an
  `auto_decision_committed` event with `decided_by="llm_provisional"`. The run
  blocks at `hic_scaffolding` because Hi-C is `auto_decidable=false` in auto
  mode; that blocked state is the expected terminal condition for this smoke.
  Requires `GENE_AGENT_LLM_API_KEY` exported.

Once each smoke is executed on the HPC host, record session id, exit code, and
the relevant artifact refs (final report, recommendation artifact, function
bundle) here. The existing function-provenance failure smoke is not
final-report evidence — failed-terminal paths intentionally do not write that
artifact in task 04.

## Cloud Test Conventions

For server/HPC command snippets, prefer the explicit environment prefix:

```bash
conda run -p /data/work/conda_envs/gene_agent python <script>.py
```

Run smoke/server scripts with absolute paths for `--manifest`,
`--run-config`, and especially `--data-dir`. Relative data dirs can be
captured into generated shell scripts and later reinterpreted from a
runner work directory. The RepeatMasker failure on 2026-05-15 was this
class of bug.

Preferred Phase 5 smoke form:

```bash
bash scripts/server_phase5_repeatmasker_smoke.sh \
  --manifest    /data/work/genevis/data/phase5_existing_asm_smoke/manifest.yaml \
  --run-config  /data/work/genevis/data/phase5_existing_asm_smoke/run_config.json \
  --session-id  phase5-existing-asm-20260515-e \
  --data-dir    /data/work/genevis/data/phase5_existing_asm_smoke/out
```

Avoid relying on relative `--data-dir` values such as
`data/phase5_existing_asm_smoke/out` in cloud tests.

## Read Order

Read these in order before non-trivial work:

1. `CLAUDE.md`
2. `deep-research-report.md`
3. `docs/governance.md`
4. `docs/architecture.md`
5. `docs/stage_contracts.md`
6. `docs/descriptor_spec.md`
7. `docs/phases/phase_completion_plan.md`
8. The phase doc most relevant to the work; see `docs/README.md` for
   the index.
9. This file again

`/Users/a1/Documents/BGIwork/rna_agent` (the sibling reference project)
is a pattern source only. Do not copy single-cell business names into
this project.

## Verification Commands

The canonical commands live in the top-level `README.md` "Verification"
section. Run them after each substantive change:

- the unittest discover loop
- the `compileall` smoke
- the graph smoke one-liner

`PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache` matters in the local Mac
sandbox because the default bytecode cache may target
`~/Library/Caches`, which is not writable.

Expected graph smoke output:

```text
done
['inventory_stage', 'read_qc_stage', 'genome_survey_stage', 'assembly_stage', 'assembly_review_stage', 'hic_scaffolding_stage', 'repeat_annotation_stage', 'gene_annotation_stage', 'function_annotation_stage']
```

## Next Best Task

Phase 4 server QUAST/resume validation and Phase 5 task 03 RepeatMasker
server validation are both recorded. The next best task is to choose the
next Phase 5 acceptance target explicitly before implementation, likely
database provenance handling for `function_annotation` or the next
stage-specific server gate.

Do not implement broad assembly, annotation, requested-stage routing,
database downloads, or genome-size conflict thresholds before the
corresponding human decision is explicit in the task docs. Do not
reintroduce in-agent remote execution; the workflow is Mac dev →
server `git pull` (see `docs/deployment/mac_dev_server_pull.md`).

## Immediate Design Decisions Already Made

- Stage order is fixed in `app/stages/__init__.py`.
- `genome_survey` and `hic_scaffolding` are documented stages but must
  be skippable.
- Genome survey output is a prior for assembly, not a hard prerequisite.
- The only real assembler execution path is explicit local Flye; default
  assembly behavior is planning-only. NextDenovo / Canu / hifiasm are
  planning placeholders only.
- Assembly review is the main mandatory human gate. Existing assembly
  inputs auto-accept as a deterministic pass-through; assembly-stage
  candidates do not auto-accept on QUAST metrics.
- Hi-C scaffold method choice and contact-map acceptance require human
  approval.
- `TopLevelState` stays small and reference-based; large outputs go
  through artifacts and ledger.
- Real method execution must go through descriptors → frozen attempt
  spec → runner; local execution is opt-in per stage output.
- Function annotation database provenance is required before terminal
  commit; missing or incomplete provenance fails rather than advances.
- In-agent remote execution is intentionally unsupported; server runs
  happen after `git pull` on the server checkout.

## Known Risks

- `DescriptorRegistry.load()` validates package/method references but
  not asset compatibility.
- Tools in `env_geno_exec` may be missing on a given host. `EnvResolver`
  preflight surfaces this as `blocked_env` / `missing_tool` with an
  `env_preflight_blocked` ledger event before any biological command
  runs. The legacy shell-PATH bypass is preserved only when callers
  explicitly seed `stage_outputs.<stage>.env.PATH` (used by the
  fake-binary fixture tests).
- `LocalRunner` is the only runner. When a frozen attempt sets
  `conda_env` or `conda_prefix`, it dispatches through `conda run`;
  otherwise the legacy shell path is used.
- Review interrupts have both in-process structured resume helpers and
  a durable cross-process flow. Phase 5 task 02 wired LangGraph's
  `SqliteSaver` into `app/graph/checkpointer.py`; the default backend
  is selected by `_resolve_checkpointer_backend(args)` with priority
  explicit `--checkpointer` flag > `GENE_AGENT_CHECKPOINTER` env var >
  `"sqlite"`. The checkpoint database lives at
  `<data_dir>/checkpoints/checkpoints.sqlite` and a session blocked at
  `assembly_review` or `hic_scaffolding` can be resumed across processes
  with `gene-agent resume --session-id <id> --decision-file <path>
  --data-dir <dir>`. The resume CLI loads `.env` (`main` entry),
  records `resume_started` / `resume_completed` / `resume_failed`
  ledger events around the in-process resume helper, and rejects
  `--checkpointer memory`. `gene-agent inspect` reports
  `decision_requirements` (stage / decisions_allowed / candidate_ids)
  reconstructed from the latest review request artifact. The decision
  file must validate against `app.review.base.ResumeReviewRequest`.
  `tests/__init__.py` sets `GENE_AGENT_CHECKPOINTER=memory` for the
  test suite, which means all unittest invocations need `-t .` so the
  package init runs (every script and doc in this repo already does
  this).
- Requested-stage routing is not implemented; the CLI flag is
  intentionally not exposed.
- SeqKit, Flye, QUAST, and YaHS are not installed in the local Mac
  verification environment; tests use temporary fake binaries for
  success paths and cover missing/failing binary behavior.
- `scripts/server_phase4_smoke.sh` is the only server-side smoke entry
  and is scoped to the Phase 4 Flye → QUAST mainline; it does not
  exercise `assembly_review` resume, `hic_scaffolding`, or
  `repeat_annotation`. When Phase 5 task 02 / task 03 need server-side
  smoke coverage they should add their own scripts rather than
  overload this one.
- `app.control.lifecycle.inspect_session()` reports
  `"checkpoint_persistence": "sqlite"` and
  `"durable_resume_available": True` when the sqlite db exists for
  `<data_dir>/checkpoints/checkpoints.sqlite`, else stays on
  `memory_only` / `False`. `inspect_state()` (in-memory introspection)
  intentionally does not carry those fields — persistence is a
  property of the checkpoint backend, not of the in-memory state.

For per-stage planning vs. execution status (which methods are wired,
which are descriptor-only), read the top-level `README.md` "Current
Implementation" / "Not implemented" lists rather than restating here.

## Stop Conditions

Stop and ask the user before:

- changing the stage order
- removing any documented stage
- introducing a new top-level persistence backend
- adding a heavy external dependency
- making destructive filesystem or git changes
- running real bioinformatics tools on large data

## Handoff Update Rule

Before ending any substantial work session, update:

- Current Status (one-line summary, not a phase diary)
- Phase 4 Real Biology Acceptance Run Record (when a new acceptance
  pass produces new refs)
- Next Best Task
- Known Risks (only when a risk changes shape — do not restate
  per-stage status that already lives in `README.md` or phase docs)
- Verification Commands (only when commands change)
