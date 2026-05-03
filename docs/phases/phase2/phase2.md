# Phase 2: CycloneSEQ Contract And First Method Slice

## 1. Phase Goal

Phase 2 designs the full CycloneSEQ genome assembly and annotation workflow,
then implements only one narrow executable vertical slice first.

Target backbone:

1. [ ] inventory
2. [ ] read_qc
3. [ ] genome_survey
4. [ ] assembly
5. [ ] assembly_review
6. [ ] hic_scaffolding
7. [ ] repeat_annotation
8. [ ] gene_annotation
9. [ ] function_annotation

Phase 2 assumes the user input is BGI CycloneSEQ downsampled or production
FASTQ / FASTQ.gz. Inventory should identify it as `raw_long_reads`.

The first executable slice is:

```text
raw_long_reads
-> read_qc.long_read_stats
-> SeqKit stats
-> stats artifact
-> ledger event
```

This phase must not pretend the whole assembly pipeline is executable. It should
make the complete contracts clear enough that later coding assistants know where
to ask for human decisions instead of inventing behavior.

## 2. Reference From `rna_agent`

Use `/Users/a1/Documents/BGIwork/rna_agent` as the implementation-pattern
reference.

Reusable patterns:

- [ ] phase README plus numbered task docs
- [ ] one real method path before broad feature expansion
- [ ] descriptor-driven method selection
- [ ] frozen attempt specs before runner execution
- [ ] small state plus artifact references
- [ ] ledger events for attempts and artifacts
- [ ] explicit preflight gates before LLM, Forge, or runner calls

Do not copy:

- single-cell stage names
- h5ad-specific input contracts
- Harmony-specific prompt assets
- annotation/benchmark assumptions from RNA workflows

## 3. Phase 1 Items Moved Here

These were deliberately left unchecked in Phase 1 and now belong to Phase 2 or
later Phase 2 follow-up work.

- [ ] Define real human review/resume protocol for assembly review and other
  gates.
- [ ] Align ledger, artifact, descriptor, and runner contracts toward the proven
  `rna_agent` shape.
- [ ] Decide requested-stage semantics before reintroducing any `--stages` CLI
  option.
- [ ] Add normalized manifest artifact only if Phase 2 needs immutable manifest
  snapshots for runner or audit.
- [ ] Add registry event metadata to asset provenance after ledger event IDs or
  registry write events are defined.

Human decision rule:

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## 4. CycloneSEQ Stage Contract Summary

### Inventory

- [x] Input: FASTQ / FASTQ.gz manifest paths.
- [x] Output: `raw_long_reads` in asset registry.
- [ ] Human question: what manifest metadata uniquely identifies CycloneSEQ when
  filename/platform evidence is ambiguous?

### Read QC

- [ ] Substep: `long_read_stats`
  - candidates: SeqKit stats, NanoPlot, pyfastx
  - output: raw/filtered read stats, N50, total bases, quality summary
- [ ] Substep: `length_filter`
  - candidates: seqkit seq, Filtlong, fastplong, chopper
  - output: `filtered_long_reads`
- [ ] Substep: `make_fofn`
  - output: `filtered_long_reads.fofn`
  - purpose: stable input contract for assembly and downstream stages

### Genome Survey

- [ ] Full mode: if short DNA reads exist, plan Jellyfish/Meryl plus
  GenomeScope2/Smudgeplot.
- [ ] Fallback mode: if only CycloneSEQ long reads exist, estimate coverage and
  genome size roughly, mark confidence low, and avoid pretending this is a
  k-mer survey equivalent.

### Assembly

- [ ] Candidate set: Flye, hifiasm, NextDenovo, Canu.
- [ ] Each method produces one candidate assembly plus logs and method metadata.
- [ ] Planner should select a primary candidate and at most one fallback unless
  the human explicitly broadens the run.

### Assembly Review

- [ ] Gate candidates with QUAST, BUSCO, Merqury, read mapping coverage,
  purge_dups_check, and contamination_check when inputs permit.
- [ ] Decide accept, rerun, purge, stop for human review, or continue with
  explicit low-confidence notes.
- [ ] Human question: what thresholds should be default for CycloneSEQ projects?

### Hi-C Scaffolding

- [ ] If Hi-C reads exist, plan YaHS or Juicer + 3D-DNA + Juicebox.
- [ ] If Hi-C reads are absent, mark skipped and continue annotation on
  contig-level genome.

### Repeat Annotation

- [ ] Run before gene annotation.
- [ ] Candidate set: EDTA, RepeatModeler, RepeatMasker.
- [ ] Outputs: repeat library, repeat GFF3, softmasked genome.

### Gene Annotation

- [ ] Inputs: softmasked genome plus optional RNA-seq, Iso-Seq, and protein
  evidence.
- [ ] Candidate set: BRAKER3, MAKER, AUGUSTUS, Miniprot.
- [ ] Outputs: genes.gff3, transcripts.fa, proteins.fa.

### Function Annotation

- [ ] Input: proteins.fa.
- [ ] Candidate set: InterProScan, eggNOG-mapper, DIAMOND, KofamScan.
- [ ] Outputs: gene_function.tsv, GO, KEGG, Pfam/InterPro tables.

## 5. Task Order

1. [ ] [01_phase2_decisions.md](01_phase2_decisions.md) - Migrate Phase 1 leftovers into Phase 2 decisions.
2. [ ] [02_cycloneseq_method_inventory.md](02_cycloneseq_method_inventory.md) - Define CycloneSEQ stage and method inventory.
3. [ ] [03_artifact_contracts.md](03_artifact_contracts.md) - Define artifact contract and asset transitions.
4. [ ] [04_decision_gates.md](04_decision_gates.md) - Define decision gates and human-help triggers.
5. [ ] [05_seqkit_stats_slice.md](05_seqkit_stats_slice.md) - Specify SeqKit stats vertical slice.
6. [ ] [06_runner_ledger_alignment.md](06_runner_ledger_alignment.md) - Plan descriptor, FrozenAttemptSpec, runner, and
   ledger alignment.
7. [ ] [07_verification_handoff.md](07_verification_handoff.md) - Define verification, acceptance, and handoff
   checkpoints.

## 6. Verification

Documentation-only changes:

```bash
git diff --check
```

Before implementing Phase 2 code:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
```
