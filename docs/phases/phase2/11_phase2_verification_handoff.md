# 11 Phase 2 Verification Handoff

## Goal

Keep Phase 2 measurable while the single-line CycloneSEQ plan grows stage by
stage.

## Project Context

The repo currently has only Phase 1 inventory code and placeholder downstream
stages. This handoff task exists so every stage-by-stage change updates the
coordination docs and proves it did not break the scaffold.

## Reuse From `rna_agent`

Reusable patterns:

- phase acceptance checklist;
- exact verification commands with expected scope;
- handoff update before stopping;
- explicit statement of what is not implemented.

Do not copy:

- RNA-specific test names;
- Harmony/annotation/benchmark done criteria.

## Optimization Notes

- Keep documentation-only changes separate from code changes when practical.
- Add new test modules only as their corresponding stage contract becomes code.
- Never mark the whole CycloneSEQ pipeline executable after only SeqKit stats.

## Documentation Acceptance

- [ ] `phase2.md` lists the inventory-to-function-annotation backbone.
- [ ] Task 01 makes CycloneSEQ FASTQ inventory resolve to `raw_long_reads`.
- [ ] Task 02 makes SeqKit stats the first real executable method.
- [ ] Task 03 defines read filtering and FOFN as downstream read QC contracts.
- [ ] Task 04 defines genome survey full/fallback modes.
- [ ] Task 05 defines assembly candidates.
- [ ] Task 06 defines assembly review as the main human gate.
- [ ] Task 07 defines Hi-C scaffold/skip behavior.
- [ ] Task 08 puts repeat annotation before gene annotation.
- [ ] Task 09 defines gene annotation outputs.
- [ ] Task 10 defines function annotation from `proteins.fa`.
- [ ] `docs/handoff.md` points future agents to the current next task.

## Implementation Acceptance

Phase 2 code is only complete when:

- CycloneSEQ FASTQ / FASTQ.gz inventory path remains valid.
- SeqKit stats runs from `raw_long_reads`.
- Read stats artifact, stdout/stderr/log artifacts, artifact index records, and
  ledger events are written.
- Read QC plan can express stats, length filter, and FOFN contracts.
- Genome survey can express full, fallback, and skipped modes.
- Assembly can express candidate planning.
- Assembly review can block for human decision.
- Hi-C absent path produces skip record and annotation can continue.
- Repeat, gene, and function annotation contracts are represented by refs and
  artifact bundles.
- No broad method execution is added without method-specific tasks.

## Required Verification

Documentation-only:

```bash
git diff --check
```

Baseline:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
```

SeqKit slice:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Later full contract tests should be added as the stage contracts become code:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
```

## Handoff Rule

- [ ] Update `docs/handoff.md` after each task changes code behavior.
- [ ] Keep task checkboxes as implementation tracking.
- [ ] Stop and ask before choosing platform defaults, biological thresholds,
  review payloads, requested-stage semantics, or broad method execution.

## Non-Goals

- Do not claim the whole CycloneSEQ pipeline is executable after only SeqKit
  stats.
- Do not call documentation-only checks evidence that code works.
- Do not hide missing tools or skipped tests.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
