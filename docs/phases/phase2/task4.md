# Task 4: Define Decision Gates And Human-Help Triggers

Status: planned.

## Goal

Make stage decisions explicit before implementing resume behavior or automated
reruns.

## Gate Inventory

- [ ] inventory review gate for ambiguous genomic long reads vs long RNA reads
- [ ] read QC gate when filtering loses too much yield
- [ ] genome survey confidence gate for CycloneSEQ-only fallback mode
- [ ] assembly method-selection gate when genome size, coverage, or resources are
  unclear
- [ ] assembly review gate for accept, rerun, purge, stop, or continue
- [ ] Hi-C manual review gate when contact maps suggest misjoins
- [ ] annotation evidence gate when RNA-seq, Iso-Seq, or protein evidence changes
  method choice

## Assembly Review Signals

- [ ] QUAST summary
- [ ] BUSCO completeness, fragmentation, duplication
- [ ] Merqury QV and completeness when short reads exist
- [ ] read mapping coverage and depth distribution
- [ ] purge_dups_check signal
- [ ] contamination_check signal

## Phase 1 Leftover Included Here

- [ ] Define review/resume payloads before implementing resume code.
- [ ] Define whether human approval mutates the original state, writes a review
  artifact, or both.
- [ ] Define how rejected candidates remain traceable.

## Human Help Triggers

- [ ] If thresholds for accept/rerun/purge are needed.
- [ ] If multiple assembly candidates disagree and no deterministic rule exists.
- [ ] If a coding assistant reaches resume behavior and no human approval payload
  schema exists.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## Verification

```bash
git diff --check
```
