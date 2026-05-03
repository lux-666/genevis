# 02 CycloneSEQ Method Inventory

Status: planned.

## Goal

Document the complete CycloneSEQ genome assembly and annotation method inventory
before coding broad stage behavior.

## Input Assumption

- [ ] User provides CycloneSEQ FASTQ / FASTQ.gz.
- [ ] Inventory registers the input as `raw_long_reads`.
- [ ] Optional inputs may include short DNA reads, Hi-C reads, RNA-seq, Iso-Seq,
  protein evidence, existing assembly, or known genome size.

## Stage Method Inventory

### Read QC

- [ ] `long_read_stats`
  - candidates: seqkit stats, NanoPlot, pyfastx
  - outputs: raw read stats, filtered read stats, N50, total bases, quality
    summary
- [ ] `length_filter`
  - candidates: seqkit seq, Filtlong, fastplong, chopper
  - output: `filtered_long_reads`
- [ ] `make_fofn`
  - output: `filtered_long_reads.fofn`

### Genome Survey

- [ ] Full mode with short DNA reads:
  - Jellyfish or Meryl
  - GenomeScope2
  - Smudgeplot
- [ ] Fallback mode with CycloneSEQ only:
  - coverage estimate
  - rough genome size estimate
  - low confidence flag

### Assembly

- [ ] Flye
- [ ] hifiasm
- [ ] NextDenovo
- [ ] Canu

### Assembly Review

- [ ] QUAST
- [ ] BUSCO
- [ ] Merqury when low-error short reads exist
- [ ] read mapping coverage
- [ ] purge_dups_check
- [ ] contamination_check

### Hi-C Scaffolding

- [ ] YaHS
- [ ] Juicer + 3D-DNA + Juicebox
- [ ] skipped path when Hi-C reads are absent

### Repeat Annotation

- [ ] EDTA
- [ ] RepeatModeler
- [ ] RepeatMasker

### Gene Annotation

- [ ] BRAKER3
- [ ] MAKER
- [ ] AUGUSTUS
- [ ] Miniprot

### Function Annotation

- [ ] InterProScan
- [ ] eggNOG-mapper
- [ ] DIAMOND
- [ ] KofamScan

## Human Help Triggers

- [ ] If a tool should be added or removed from the candidate set.
- [ ] If CycloneSEQ should map to ONT-like, CLR-like, or a separate platform
  profile in descriptors.
- [ ] If default read filtering thresholds are needed.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## Verification

```bash
git diff --check
```
