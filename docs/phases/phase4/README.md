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

## Current Assumptions

- `gene_agent` conda environment exists and contains `seqkit`.
- Flye and QUAST may live in separate environments, not necessarily in
  `gene_agent`.
- Different methods must not assume one shared activated environment.
- The implementation should use `conda run -n <env_name> <command>` or an
  explicitly configured conda executable.
- Missing envs or tools should block with structured state, not silently skip
  and not produce fake outputs.

## Phase 4 Explicit Non-Goals

These remain planning, skip, or blocked-reason paths only:

- `genome_survey` real execution
- `hic_scaffolding` real execution
- `repeat_annotation` real execution
- `gene_annotation` real execution
- `function_annotation` real execution
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
2. Finish task 02 next, because SeqKit is already available in the current
   `gene_agent` environment.
3. Finish task 03 after Flye and QUAST environments are configured.
4. Only after task 03 is stable should the project consider BUSCO, mapping,
   purge, Hi-C, repeat, gene, or function annotation execution.

## Phase 4 Done Criteria

- A configured local executor runs method commands through method/stage-specific
  conda environments.
- A small real FASTQ produces SeqKit stats, a read QC summary JSON, and a FOFN.
- A small real long-read smoke can produce a Flye draft assembly when required
  parameters and environment exist.
- QUAST can evaluate the materialized Flye assembly when its environment exists.
- The workflow stops at `blocked_human` after QUAST and does not auto-promote
  the draft assembly.
- All non-mainline downstream stages remain honest planning, skip, or blocked
  states.

## Verification Baseline

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

