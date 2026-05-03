# Phase 2 CycloneSEQ Single-Line Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> before implementing code from this plan. Steps use checkbox (`- [ ]`) syntax
> for implementation tracking. Documentation checkboxes are not evidence that
> code exists.

**Goal:** Build a single-line CycloneSEQ genome assembly and annotation workflow
contract from FASTQ / FASTQ.gz inventory through function annotation, while
making SeqKit stats the first real executable method.

**Architecture:** Phase 2 follows the biological backbone in order:
`inventory -> read_qc -> genome_survey -> assembly -> assembly_review ->
hic_scaffolding -> repeat_annotation -> gene_annotation -> function_annotation`.
Each numbered task advances one stage or one concrete substep. The only real
tool execution planned first is SeqKit stats; later tools are candidate
descriptors and contracts until their own tasks implement them.

**Tech Stack:** Python 3.11, LangGraph `StateGraph`, Pydantic v2, PyYAML,
standard-library `unittest`, descriptor-backed CLI methods, local runner,
JSON/JSONL artifacts and ledger records.

---

## Phase 2 Input Assumption

The user provides BGI CycloneSEQ downsampled or production FASTQ / FASTQ.gz.
Inventory identifies the input as:

```text
raw_long_reads
```

Accepted suffixes:

- `.fastq`
- `.fq`
- `.fastq.gz`
- `.fq.gz`

Optional inputs may include short DNA reads, Hi-C reads, RNA-seq, Iso-Seq, long
RNA, protein evidence, existing assembly, or known genome size.

## Mainline Backbone

```text
inventory
-> read_qc
-> genome_survey
-> assembly
-> assembly_review
-> hic_scaffolding
-> repeat_annotation
-> gene_annotation
-> function_annotation
```

## Read QC Substeps

Phase 2 starts with SeqKit but keeps the full read QC shape:

1. `long_read_stats`
   - candidates: SeqKit stats, NanoPlot, pyfastx
   - output: raw/filtered read stats, N50, total bases, quality summary
2. `length_filter`
   - candidates: seqkit seq, Filtlong, fastplong, chopper
   - output: `filtered_long_reads`
3. `make_fofn`
   - output: `filtered_long_reads.fofn` or `long_reads.fofn`
   - purpose: stable input contract for assembly and downstream stages

## Stage Method Summary

`genome_survey` has two modes:

- full mode with short reads: Jellyfish/Meryl + GenomeScope2/Smudgeplot;
- CycloneSEQ-only fallback: rough coverage/genome-size estimate with low
  confidence.

`assembly` uses a candidate set:

- Flye
- hifiasm
- NextDenovo
- Canu

Each method produces a candidate assembly. `assembly_review` compares candidates
using QUAST, BUSCO, Merqury, read mapping coverage, purge_dups_check, and
contamination_check.

`hic_scaffolding` runs YaHS or Juicer + 3D-DNA + Juicebox only when Hi-C reads
exist and chromosome-level assembly is requested. Otherwise it writes a skip
record and annotation continues on the contig-level genome.

`repeat_annotation` runs before `gene_annotation` and produces repeat library,
repeat GFF3, and softmasked genome using EDTA, RepeatModeler, RepeatMasker, and
optional TRF.

`gene_annotation` consumes softmasked genome plus optional RNA-seq, Iso-Seq,
long RNA, and protein evidence. Candidate methods include BRAKER3, MAKER,
AUGUSTUS, and Miniprot. Outputs are `genes.gff3`, `transcripts.fa`, and
`proteins.fa`.

`function_annotation` consumes `proteins.fa` and uses InterProScan,
eggNOG-mapper, DIAMOND, and KofamScan candidates to produce `gene_function.tsv`,
GO, KEGG, Pfam, and InterPro tables.

## Plan Files

- [01 Inventory CycloneSEQ FASTQ](01_inventory_cycloneseq_fastq.md)
- [02 Read QC SeqKit Stats](02_read_qc_seqkit_stats.md)
- [03 Read QC Filter And FOFN Contract](03_read_qc_filter_fofn.md)
- [04 Genome Survey Full And Fallback Modes](04_genome_survey_modes.md)
- [05 Assembly Candidate Planning](05_assembly_candidates.md)
- [06 Assembly Review Gate](06_assembly_review_gate.md)
- [07 Hi-C Scaffolding Or Skip](07_hic_scaffolding.md)
- [08 Repeat Annotation](08_repeat_annotation.md)
- [09 Gene Annotation](09_gene_annotation.md)
- [10 Function Annotation](10_function_annotation.md)
- [11 Phase 2 Verification Handoff](11_phase2_verification_handoff.md)

## Recommended Task Order

1. Make CycloneSEQ FASTQ inventory register `raw_long_reads`.
2. Implement SeqKit stats as the first real descriptor-backed method.
3. Add read QC filter and FOFN contracts.
4. Add genome survey full/fallback mode contracts.
5. Add assembly candidate planning.
6. Add assembly review gate contracts.
7. Add Hi-C scaffold/skip behavior.
8. Add repeat annotation contract.
9. Add gene annotation contract.
10. Add function annotation contract.
11. Verify and update handoff.

## Phase 2 Done Criteria

- CycloneSEQ FASTQ / FASTQ.gz inventory path is tested.
- SeqKit stats runs from `raw_long_reads`, writes artifacts, and records ledger
  events.
- Read QC can express stats, filter, and FOFN substeps.
- Genome survey can express full, fallback, and skipped modes.
- Assembly can express candidate methods and candidate assembly refs.
- Assembly review can block for human decision without inventing thresholds.
- Hi-C absence produces a skip record and does not block annotation.
- Repeat annotation precedes gene annotation.
- Gene annotation outputs `genes.gff3`, `transcripts.fa`, and `proteins.fa`.
- Function annotation consumes `proteins.fa`.
- Graph state stores refs and summaries, not large files or metric tables.

## Human Decision Rule

Stop and ask before choosing:

- CycloneSEQ platform profile defaults;
- read filtering thresholds;
- assembly method defaults;
- assembly review thresholds;
- purge/polish cutoffs;
- Hi-C contact-map policy;
- gene evidence merge policy;
- requested-stage semantics;
- review/resume payload schema.

## Verification

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

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
