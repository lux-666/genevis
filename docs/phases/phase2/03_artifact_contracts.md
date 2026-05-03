# 03 Artifact Contracts

Status: planned.

## Goal

Define stable asset and artifact transitions for the full CycloneSEQ workflow so
later code can remain reference-oriented.

## Asset Transitions

- [ ] `raw_long_reads` -> `clean_long_reads`
- [ ] `clean_long_reads` -> `filtered_long_reads.fofn`
- [ ] `clean_short_dna_reads` -> `genome_survey_result` when short reads exist
- [ ] CycloneSEQ-only reads -> low-confidence `genome_survey_result` fallback
- [ ] `clean_long_reads` + optional `genome_survey_result` -> candidate
  `draft_assembly`
- [ ] selected `draft_assembly` -> `final_assembly_for_scaffolding_or_annotation`
- [ ] optional Hi-C reads + final assembly -> `chromosome_scaffolded_assembly`
- [ ] final assembly -> `repeat_annotation` + `softmasked_genome`
- [ ] softmasked genome + optional evidence -> `gene_models`,
  `transcripts.fa`, `proteins.fa`
- [ ] `proteins.fa` -> `function_annotation`

## Required Artifact Families

- [ ] read stats artifact
- [ ] filtered long-read FASTQ artifact
- [ ] FOFN artifact
- [ ] genome survey report artifact
- [ ] assembly candidate bundle
- [ ] assembly evaluation bundle
- [ ] Hi-C scaffold bundle or skip record
- [ ] repeat annotation bundle
- [ ] gene annotation bundle
- [ ] function annotation bundle

## Phase 1 Leftovers Included Here

- [ ] Decide whether to write normalized manifest artifacts.
- [ ] Define registry event metadata only after ledger event IDs or registry
  write events exist.
- [ ] Keep graph state small: paths, IDs, summaries, and decision refs only.

## Human Help Triggers

- [ ] If artifact directory layout or retention policy is not already specified.
- [ ] If a coding assistant wants to store large tables or FASTQ/FASTA content in
  graph state.
- [ ] If normalized manifest snapshots need legal, audit, or production naming
  guarantees.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## Verification

```bash
git diff --check
```
