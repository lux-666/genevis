# Phase 4 Real Local Assembly Mainline

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> before implementing code from this plan. Steps use checkbox (`- [ ]`) syntax
> for implementation tracking. Documentation checkboxes are not evidence that
> code exists.

**Goal:** Move `gene_agent` from a genome workflow planner with narrow real
method slices to a small-real-data local execution MVP for one assembly
mainline.

**Architecture:** Keep the existing parent graph and stage order. Real execution
must continue to flow through descriptor-backed frozen attempts, a runner,
artifact records, ledger events, and compact state refs. Phase 4 adds an
environment resolution layer so each method can run in its configured conda
environment instead of assuming the current shell has every bioinformatics tool.

**Tech Stack:** Python 3.11, LangGraph, Pydantic v2, PyYAML, standard-library
`unittest`, local conda environments, SeqKit, Flye, and QUAST.

---

## Phase 4 Target Mainline

```text
inventory
-> read_qc
-> FOFN
-> assembly by Flye
-> assembly_review by QUAST
-> blocked_human
```

This phase should prove that a small real FASTQ can be registered, summarized
with SeqKit, handed to Flye through a stable FOFN, evaluated with QUAST, and
paused at the human review gate.

## Phase 4 Tasks

1. [Stage-Specific Conda Env And Local Executor](01_stage_specific_conda_env_local_executor.md)
2. [Real FASTQ Read QC Smoke](02_real_fastq_read_qc_smoke.md)
3. [Flye, QUAST, And Human Gate Smoke](03_flye_quast_human_gate.md)
4. [Real Biology Mainline Acceptance](04_real_biology_mainline_acceptance.md)
5. [Structured Local Execution Request](05_structured_local_execution_request.md)
6. [Phase 4 Status Docs And Phase 5 Gate](06_phase4_status_docs_and_phase5_gate.md)

## Current Assumptions

- `gene_agent` conda environment exists and contains the Python orchestrator
  dependencies.
- Bioinformatics tools are configured through `config/envs.yaml`; the current
  shared local execution env is `env_geno_exec`.
- Different methods must not assume one shared activated environment.
- The implementation should use `conda run -n <env_name> <command>` or an
  explicitly configured conda executable.
- Missing envs or tools should block with structured state, not silently skip
  and not produce fake outputs.
- Real-biology Flye acceptance requires a human-supplied FASTQ and explicit
  Flye parameters.

## Phase 4 Explicit Non-Goals

Original Phase 4 scope. Some of these later received narrow opt-in slices;
see the "Phase 4 Implementation Status" table below for the current state.
Broader execution of these method families remains a non-goal for Phase 4
and is deferred to Phase 5:

- `genome_survey` real execution beyond the Jellyfish + GenomeScope2 opt-in
  slice
- `hic_scaffolding` real execution beyond the YaHS opt-in slice (no Juicer,
  3D-DNA, Juicebox, or raw Hi-C alignment)
- `repeat_annotation` real execution beyond the RepeatMasker opt-in slice
  for provided libraries (no EDTA, RepeatModeler, TRF, or de novo library
  generation)
- `gene_annotation` real execution (planning-only contract)
- `function_annotation` real execution (planning-only contract; no database
  downloads)
- BUSCO
- Merqury
- purge_dups
- contamination check
- RemoteRunner
- requested-stage routing
- automatic assembly acceptance thresholds
- automatic fallback assembler execution after Flye failure

## Placeholder Policy

Allowed placeholder behavior:

- `planning_only`
- `skipped` with explicit skip reason
- `blocked_input`
- `blocked_param`
- `blocked_env`
- `missing_tool`

Not allowed:

- Marking a method or stage `completed` when the real command did not run.
- Writing fake biological output files to satisfy downstream stages.
- Treating missing tools as successful skips in local execution mode.
- Running NextDenovo or Canu automatically as Flye fallbacks without a separate
  approved retry/fallback policy.
- Auto-promoting an assembly after QUAST.

## Recommended Execution Order

1. Finish task 01 first, because all real package execution depends on method
   environment resolution.
2. Finish task 02 next, because SeqKit is the narrowest real local execution
   slice and is resolved through the configured execution environment.
3. Finish task 03 after Flye and QUAST environments are configured.
4. Run task 04 with a human-supplied real FASTQ and record the artifact trail.
5. Add task 05 if the Python one-liner smoke path is still the only way to pass
   Flye parameters.
6. Complete task 06 before Phase 5 starts so future agents do not follow stale
   status notes.
7. Only after task 04 succeeds and task 06 is updated should the project
   consider BUSCO, mapping, purge, Hi-C, repeat, gene, or function annotation
   execution.

## Phase 4 Done Criteria

- A configured local executor runs method commands through method/stage-specific
  conda environments.
- A small real FASTQ produces SeqKit stats, a read QC summary JSON, and a FOFN.
- A small real long-read smoke can produce a Flye draft assembly when required
  parameters and environment exist.
- QUAST can evaluate the materialized Flye assembly when its environment exists.
- The workflow stops at `blocked_human` after QUAST and does not auto-promote
  the draft assembly.
- A human-supplied real FASTQ run records the Phase 4 artifact trail in
  `docs/handoff.md`.
- Routine Phase 4 local execution has a structured way to supply Flye
  parameters, or the remaining Python one-liner limitation is explicitly
  documented as the next task.
- All non-mainline downstream stages remain honest planning, skip, or blocked
  states.

## Phase 4 Implementation Status

| Slice | Status |
|-------|--------|
| `config/envs.yaml`, `EnvResolver`, `env_dispatch`, `conda run` in `LocalRunner` | implemented in code |
| `FrozenAttemptSpec` conda fields + mutex validator | implemented in code |
| `read_qc` SeqKit stats / filter / FOFN under `env_geno_exec` | verified by committed fixture `tests/fixtures/phase4/smoke.fastq` |
| `read_qc` real-data smoke on `env_geno_exec` with `seqkit` installed | opt-in `Phase4ReadQCRealSmokeTests` (skipped when env missing) |
| `assembly.candidate.flye` strict `flye_parameters` validation + execution | implemented in code; success path verified with a fake `flye` |
| `assembly_review.eval.quast` + `quast_summary.json` + action provenance | implemented in code; success path verified with a fake `quast.py` |
| `--local-exec STAGE` + `--run-config PATH` structured entry | implemented in code; validated before `Ledger.create_session(...)` |
| `env_geno_exec` preflight → `blocked_env` / `missing_tool` before biological commands | implemented in code |
| Real long-read Flye → QUAST → `blocked_human` acceptance pass | requires human real-data smoke (task 04) |
| Artifact trail recorded in `docs/handoff.md` for the real-data pass | requires human real-data smoke (task 04) |
| BUSCO, Merqury, minimap2 coverage, purge_dups, contamination check | deferred (Phase 5+) |
| NextDenovo, Canu, hifiasm execution | deferred (Phase 5+) |
| Juicer, 3D-DNA, Juicebox, raw Hi-C alignment | deferred (Phase 5+) |
| EDTA, RepeatModeler, TRF, de novo repeat library | deferred (Phase 5+) |
| BRAKER3, MAKER, AUGUSTUS, Miniprot, HISAT2, StringTie, IsoQuant, GeMoMa | deferred (Phase 5+) |
| InterProScan, eggNOG-mapper, DIAMOND, KofamScan + DB provenance execution | deferred (Phase 5+) |
| `RemoteRunner` execution | deferred (Phase 5+) |
| Durable cross-process resume (sqlite/postgres checkpointer + lifecycle cmds) | deferred (Phase 6) |
| `genome_survey` env_dispatch wiring for Jellyfish + GenomeScope2 | deferred (acceptable carry into Phase 5) |

`arm64` hosts: `env_geno_exec` currently has no `quast.py` binary for
arm64, so the real-data acceptance pass for `assembly_review` cannot
fully complete on arm64. The code path handles this as
`status: "missing_tool"` with an `env_preflight_blocked` ledger event.

## Phase 5 Entry Gate

Phase 5 (broader method execution: BUSCO, Merqury, Hi-C alignment,
NextDenovo/Canu, annotation tools, database downloads) **does not start**
until all of the following are true:

- Task 04 has been executed on human-supplied real long reads under a
  working `env_geno_exec`.
- The run produced a real `draft_assembly.fasta`, `report.tsv`,
  `quast_summary.json`, and `assembly_review_request.json` and stopped at
  `supervision_state = "blocked_human"`.
- The session id, artifact refs, and any observed blocked states are
  recorded in `docs/handoff.md` under a "Phase 4 Real Biology
  Acceptance Run Record" (or equivalent) section.

Durable cross-process resume is **not** a Phase 4 prerequisite. It is
tracked under Phase 6 (observability and durable operation) along with
sqlite/postgres checkpointing and lifecycle commands.

## Verification Baseline

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
