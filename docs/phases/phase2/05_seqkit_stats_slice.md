# 05 SeqKit Stats Slice

Status: planned.

## Goal

Define the first real executable path without expanding into full pipeline
execution.

## Target Slice

```text
inventory
-> read_qc
-> long_read_stats
-> seqkit stats
-> read stats artifact
-> ledger event
-> read_qc commit
```

## Required Behavior

- [ ] Read `raw_long_reads` from the asset registry.
- [ ] Plan `read_qc.long_read_stats` with SeqKit as the first method.
- [ ] Create a descriptor-backed method attempt.
- [ ] Freeze the command before execution.
- [ ] Run only on explicit local execution mode.
- [ ] Write stdout/stderr/log paths as artifacts.
- [ ] Parse or preserve SeqKit stats output as a read stats artifact.
- [ ] Record ledger events for attempt started, succeeded/failed, and artifact
  registered.
- [ ] Keep state limited to refs and small summaries.

## Out Of Scope

- [ ] NanoPlot execution.
- [ ] pyfastx execution.
- [ ] read filtering execution.
- [ ] assembly execution.
- [ ] automatic retry.
- [ ] remote execution.

## Human Help Triggers

- [ ] If SeqKit is not installed in the target environment.
- [ ] If command templates must support multiple FASTQ files differently than a
  FOFN.
- [ ] If stats thresholds are needed to decide pass/fail.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## Verification For Later Implementation

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
