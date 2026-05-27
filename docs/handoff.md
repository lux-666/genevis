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
and server/HPC validated with separate deterministic `auto_rule` final-report
and LLM provisional recommendation smokes. Phase 5 task 05 operator usability is
implemented in code/docs with inspect JSON, next-action output, decision-template
generation, and blocked/failed operator report artifacts. Phase 5 task 06
provided gene annotation adoption is implemented in code and tests; server/HPC
smoke is pending. Phase 5 task 07 assembly review BUSCO evidence is implemented
in code and tests; server/HPC smoke is pending. Phase 5 task 11 function
annotation provenance completion is implemented in code/tests with a server/HPC
completion smoke script added; actual server execution is pending.

The graph runs end-to-end through every documented stage. Real local
execution is opt-in per stage and is currently wired for: `read_qc`
(SeqKit), `genome_survey` (Jellyfish + GenomeScope2), `assembly` (Flye),
`assembly_review` (QUAST + BUSCO when offline lineage provenance is supplied),
`hic_scaffolding` (YaHS), `repeat_annotation` (RepeatMasker for provided
libraries). All other methods are planning-only contracts.

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

Phase 5 task 04 strict/auto review policy support is implemented and server/HPC
validated with two narrow smokes under `/data/work/genevis`.

Deterministic `auto_rule` + final-report smoke:

- script: `scripts/server_phase5_auto_review_smoke.sh`
- session id: `phase5-auto-review-20260517-a`
- data dir: `/data/work/genevis/data/phase5_auto_review/out`
- observed terminal state: `current_stage=done`, `supervision_state=completed`
- expected/observed exit code: `0`
- final report:
  `/data/work/genevis/data/phase5_auto_review/out/artifacts/final_report/phase5-auto-review-20260517-a/review_decision_report.json`
- function bundle:
  `/data/work/genevis/data/phase5_auto_review/out/artifacts/function_annotation/phase5-auto-review-20260517-a/function_annotation_bundle.json`
- scope: validates deterministic existing-assembly `auto_rule` decision and
  terminal final-report generation. It does not validate LLM provisional review
  because `deterministic_safe` bypasses `human_review_gate`.

LLM provisional review smoke:

- script: `scripts/server_phase5_llm_review_smoke.sh`
- session id: `phase5-llm-review-20260518-e`
- data dir: `/data/work/genevis/data/phase5_llm_review/out`
- recommendation artifact:
  `/data/work/genevis/data/phase5_llm_review/out/artifacts/assembly_review/phase5-llm-review-20260518-e/assembly_review_review_recommendation.json`
- ledger:
  `/data/work/genevis/data/phase5_llm_review/out/ledger/phase5-llm-review-20260518-e/events.jsonl`
- observed accepted outcome: LLM provisional decision was recorded with
  `decided_by="llm_provisional"`; with only QUAST available and BUSCO /
  contamination checks pending, the conservative prompt validly chose
  `stop_human_review`, which stopped at `assembly_review` as `failed_terminal`.
- expected/observed exit code: `1` for this accepted conservative-stop outcome
  (the LLM smoke accepts this only when the recommendation artifact and
  `llm_provisional` ledger events are present).
- scope: validates `assembly_review.review_recommendation` artifact metadata
  (`prompt_bundle`, `prompt_paths`, raw and normalized recommendation) plus an
  `auto_decision_committed` ledger event with `decided_by="llm_provisional"`.

The existing function-provenance failure smoke remains useful regression
coverage but is not final-report evidence — failed-terminal paths intentionally
do not write the final report artifact in task 04.

## Phase 5 Function Annotation Provenance Completion

Phase 5 task 11 is implemented locally for the true terminal path:
provided/adopted annotation proteins plus run-config database provenance now
reaches `current_stage=done` and `supervision_state=completed` without running
function annotation tools or fabricating annotation tables.

Implementation highlights:

- `FunctionDatabaseProvenance` now requires `database_source_ref` and non-empty
  provenance text fields, plus the existing `command` or `descriptor_version`
  anchor.
- `function_annotation` validates provenance first, then requires the matched
  `proteins_fasta_ref` to be a materialized local file before writing plan and
  bundle artifacts.
- Function annotation plan, bundle, and final report artifacts are registered in
  both the artifact index and ledger.
- `scripts/server_phase5_function_provenance_smoke.sh` is the dedicated
  server/HPC completion smoke. It reads `predicted_proteins` from the manifest
  using manifest-relative path resolution, patches the run-config placeholder,
  verifies terminal success, and asserts the planned function tables were not
  materialized.

Server/HPC command to run after replacing manifest placeholders with real files:

```bash
bash scripts/server_phase5_function_provenance_smoke.sh \
  --manifest    /data/work/genevis/data/phase5_auto_review/manifest.yaml \
  --run-config  /data/work/genevis/data/phase5_auto_review/run_config.json \
  --session-id  phase5-function-provenance-YYYYMMDD-a \
  --data-dir    /data/work/genevis/data/phase5_function_provenance/out
```

Record the observed session id, exit code `0`, final report path, function
bundle path, and proteins ref here after the server run.

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

## Phase 6 End-to-End Acceptance Run Record

- host: HPC (`/data/work/genevis`)
- git commit: `8d918ae` (feature/hic-scaffolding)
- session_id: `phase6-e2e-20260526-a`
- manifest: `/data/work/genevis/data/phase6_e2e/manifest.yaml`
- run-config: `/data/work/genevis/data/phase6_e2e/run_config.json`
- data_dir: `/data/work/genevis/data/phase6_e2e/out`
- local_exec: `read_qc, assembly, assembly_review, repeat_annotation`
- review_policy: `strict`
- shared_parameters: `read_type=ont_raw, estimated_genome_size=5000000`

Terminal state:

- `current_stage: done`
- `supervision_state: completed`
- `checkpoint_persistence: sqlite`
- `route_trace`: inventory → read_qc → genome_survey → assembly →
  assembly_review → hic_scaffolding → repeat_annotation →
  gene_annotation → function_annotation (all 9 stages)
- `decisions: 1, by_origin: {human: 1}`
- `ledger_events: 38`

Review gate exercised:

- assembly_review blocked with strict policy
- durable sqlite resume with structured decision file
- candidate: `phase6-e2e-20260526-a:assembly.candidate.flye` (executed)
- QUAST ran on flye candidate (3 contigs, N50=6135, total=15971bp)
- NextDenovo candidate planned but not executed (no tool)
- BUSCO skipped (lineage not supplied)
- Decision: `accept` via human decision file

Key artifact refs:

- final_report: `out/artifacts/final_report/phase6-e2e-20260526-a/review_decision_report.json`
- function_bundle: `out/artifacts/function_annotation/phase6-e2e-20260526-a/function_annotation_bundle.json`
- repeat_bundle: `out/artifacts/repeat_annotation/phase6-e2e-20260526-a/repeat_annotation_bundle.json`
- assembly: `out/artifacts/assembly/phase6-e2e-20260526-a/flye/draft_assembly.fasta`
- read_qc: `out/artifacts/read_qc/phase6-e2e-20260526-a/read_qc_summary.json`
- checkpoint: `out/checkpoints/checkpoints.sqlite`

Duplicate-attempt check: PASS (no duplicates after resume).

Issues found: none. Script ran to completion without code fixes.

## Phase 6 Ask Acceptance Run Record

- host: HPC (`/data/work/genevis`)
- git commit: `b33411c` (feature/hic-scaffolding)
- session_id: `phase6-ask-20260527-a`
- script: `scripts/server_phase6_ask_acceptance.sh`
- reads: `/data/work/genevis/data/test/bht.Nanopore.pass.first500.fa`
- run-config: `/data/work/genevis/data/phase6_e2e/run_config.json`
- data_dir: `/data/work/genevis/data/phase6_e2e/out_ask`
- request_text: `对 .../bht.Nanopore.pass.first500.fa 进行基因组组装和注释，这是CycloneSEQ纳米孔测序数据`

LLM intent parsing:

- LLM parsed natural-language request into structured `EntryRequest`
- Manifest materialized from `source_paths` at
  `out_ask/manifests/phase6-ask-20260527-a/manifest.yaml`
- `llm_manifest_created: True`

Terminal state:

- `current_stage: done`
- `supervision_state: completed`
- `checkpoint_persistence: sqlite`
- `route_trace`: inventory → read_qc → genome_survey → assembly →
  assembly_review → hic_scaffolding → repeat_annotation →
  gene_annotation → function_annotation (all 9 stages)
- `ledger_events: 32`

Review gate exercised:

- assembly_review blocked with strict policy
- durable sqlite resume with structured decision file
- candidate: `phase6-ask-20260527-a:assembly.candidate.flye` (executed)
- QUAST ran (3 contigs, N50=6135, total=15971bp)
- Decision: `accept` via human decision file

Key artifact refs:

- final_report: `out_ask/artifacts/final_report/phase6-ask-20260527-a/review_decision_report.json`
- function_bundle: `out_ask/artifacts/function_annotation/phase6-ask-20260527-a/function_annotation_bundle.json`

Duplicate-attempt check: PASS.

Issues found: none. LLM intent path works end-to-end without code fixes.

## Next Best Task

Phase 6 task 02 is fully complete: both `gene-agent run` and
`gene-agent ask` paths validated on HPC with durable resume.
Phase 6 task 03 (interactive human gate and resume CLI) is the next task.

Do not implement broad assembly, annotation, requested-stage routing,
database downloads, or genome-size conflict thresholds before the
corresponding human decision is explicit in the task docs. Do not
reintroduce in-agent remote execution; the workflow is Mac dev →
server `git pull` (see `docs/deployment/mac_dev_server_pull.md`).

Operator workflow for blocked review gates:

```bash
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data --json
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data --write-decision-template /absolute/path/to/decision.json
gene-agent resume --session-id <SESSION> --data-dir /absolute/path/to/data --decision-file /absolute/path/to/decision.json --checkpointer sqlite
```

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
  `decision_requirements` (stage / decisions_allowed / candidate_ids),
  supports `--json`, prints `next_action.*`, and can write a validated
  decision-file template with `--write-decision-template <path>`.
  Blocked and failed-terminal runs write `operator_report.blocked_human`
  or `operator_report.failed_terminal` artifacts; provenance failures
  surface the required `function_annotation.database_provenance` fields.
  The decision file must validate against
  `app.review.base.ResumeReviewRequest`.
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
