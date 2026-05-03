# Task 7: Define Verification, Acceptance, And Handoff Checkpoints

Status: planned.

## Goal

Make Phase 2 measurable and handoff-friendly before implementation begins.

## Acceptance Checklist

- [ ] CycloneSEQ FASTQ / FASTQ.gz inventory path remains valid.
- [ ] Read QC stage has documented substeps: long-read stats, length filter,
  make FOFN.
- [ ] Genome survey has full mode and CycloneSEQ-only fallback mode.
- [ ] Assembly has a documented candidate set, not a single hardcoded method.
- [ ] Assembly review is the main decision gate.
- [ ] Hi-C absence produces a skip record and does not block annotation.
- [ ] Repeat annotation precedes gene annotation.
- [ ] Function annotation consumes `proteins.fa`.
- [ ] The first executable slice runs SeqKit stats only.
- [ ] Unclear design points instruct coding assistants to ask the human.

## Verification Commands

Documentation-only:

```bash
git diff --check
```

Implementation baseline:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
```

Later SeqKit slice:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc -v
```

## Handoff Rule

- [ ] Update `docs/handoff.md` after each Phase 2 task.
- [ ] Keep checkboxes current.
- [ ] Commit documentation changes separately from code changes when practical.
- [ ] Stop and ask before changing stage order, method candidate sets, gate
  thresholds, or requested-stage semantics.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
