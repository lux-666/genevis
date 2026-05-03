# 06 Runner Ledger Alignment

Status: planned.

## Goal

Bring this project closer to the current `rna_agent` reliability level without
copying RNA-specific business logic.

## Contract Pieces

- [ ] package descriptor for SeqKit
- [ ] method descriptor for `read_qc.long_read_stats.seqkit_stats`
- [ ] asset compatibility rule from `raw_long_reads` to read stats artifact
- [ ] command template for FASTQ / FASTQ.gz input
- [ ] FrozenAttemptSpec generated before execution
- [ ] LocalRunner implementation narrow enough for SeqKit stats only at first
- [ ] ledger events for attempts and artifact registration
- [ ] artifact record for stats table and logs

## Requested-Stage Semantics

Do not reintroduce `--stages` until the human chooses one:

1. [ ] Partial graph execution with dependencies already present.
2. [ ] Direct stage entry for smoke tests only.
3. [ ] Dependency-aware replay from inventory through requested stages.

Human choice required before implementation:

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## Human Help Triggers

- [ ] If a coding assistant needs to choose the CLI semantics above.
- [ ] If runner output layout is not specified.
- [ ] If `rna_agent` has multiple possible contract patterns and it is unclear
  which one should be mirrored.

## Verification For Later Implementation

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
