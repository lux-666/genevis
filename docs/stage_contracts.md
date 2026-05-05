# Stage Contracts

This document defines the stable biological stage boundaries. The current code
uses placeholder subgraphs; these contracts define what those subgraphs must
become as implementation proceeds.

## Shared Stage Shape

Every stage should evolve toward:

```text
enter
-> validate_inputs
-> plan_methods
-> method_family_subgraph
-> collect_outputs
-> review_outputs
-> commit_stage
```

Required behavior:

- `enter` performs no external side effects.
- `validate_inputs` checks only required references and lightweight metadata.
- `plan_methods` creates a small `StagePlan`.
- `method_family_subgraph` runs descriptor-selected methods.
- `collect_outputs` registers artifacts and compact metadata.
- `review_outputs` runs rules and may trigger a human gate.
- `commit_stage` writes ledger events and advances parent routing.

## Asset Terms

Use these asset names consistently:

- `raw_long_reads`
- `clean_long_reads`
- `raw_short_dna_reads`
- `clean_short_dna_reads`
- `raw_rnaseq_reads`
- `clean_rnaseq_reads`
- `raw_isoseq_or_long_rna_reads`
- `clean_isoseq_reads`
- `transcript_ccs_reads`
- `assembly_fasta_registered`
- `draft_assembly`
- `genome_survey_result`
- `hic_reads_registered`
- `chromosome_scaffolded_assembly`
- `repeat_annotation`
- `softmasked_genome`
- `gene_models`
- `predicted_proteins`
- `function_annotation`

## 1. Inventory

Purpose: register user inputs and existing outputs as standard assets.

Inputs:

- input manifest path
- files or directories listed in the manifest
- optional user metadata such as sample ID, platform, target assembly level,
  genome size estimate, or existing assembly path

Outputs:

- asset registry artifact
- `asset_registry_ref` in state
- ledger event for registered assets

Planning rules:

- Register files, do not transform them.
- Infer platform only when evidence is clear.
- If ONT/CLR genomic long reads and long RNA reads are ambiguous, stop at a
  review gate.
- If the user provides only `assembly.fa`, route later stages from existing
  assembly and skip read QC, survey, and assembly.

Do not:

- run NanoPlot, FastQC, Jellyfish, or assembly tools here
- compute expensive checksums synchronously for huge files

## 2. Read QC

Purpose: create clean read assets and QC reports.

Inputs:

- `raw_long_reads`
- optional `raw_short_dna_reads`
- optional `raw_rnaseq_reads`
- optional `raw_isoseq_or_long_rna_reads`

Outputs:

- `clean_long_reads`
- optional `clean_short_dna_reads`
- optional `clean_rnaseq_reads`
- optional `clean_isoseq_reads`
- QC report artifacts

Planning rules:

- For ONT, default to NanoPlot plus SeqKit stats.
- Treat Filtlong as optional and conservative.
- For CLR, prefer length/statistics preparation and FOFN generation.
- For short DNA and RNA-seq, SOAPnuke/FastQC planning is allowed.
- Do not over-filter long reads before Flye unless the user or review policy
  explicitly requests it.

Review triggers:

- filtered long reads lose too much total yield
- long-read coverage appears far below target
- FASTQ format or compression is damaged

## 3. Genome Survey

Purpose: estimate genome size, heterozygosity, and repeat fraction from
low-error short DNA reads.

Inputs:

- required: `clean_short_dna_reads`

Outputs:

- `genome_survey_result`
- survey report artifact

Planning rules:

- Run only when `clean_short_dna_reads` exists.
- Use Jellyfish plus GenomeScope as the default path.
- Never feed ONT/CLR raw long reads into GenomeScope by default.
- If short DNA reads are missing, skip with
  `no_low_error_short_reads` and set genome size source to
  `user_or_optional`.

Review triggers:

- abnormal k-mer spectrum
- failed GenomeScope model fit
- inconsistent genome size estimate

## 4. Assembly

Purpose: assemble `clean_long_reads` into `draft_assembly`.

Inputs:

- required: `clean_long_reads`
- optional: `genome_survey_result`
- optional: user-provided genome size

Outputs:

- `draft_assembly`
- assembler logs and reports

Planning rules:

- Flye is the simplest raw-read path.
- NextDenovo is preferred for CLR, large genomes, high heterozygosity, or when
  correct-then-assemble is desired.
- Canu is a conservative fallback with heavier resource expectations.
- `genome_survey_result` provides priors; it is not a universal hard dependency.
- Limit automatic attempts to one primary method plus one fallback.

Review triggers:

- genome size needed but absent for selected method
- assembler memory fallback required
- assembly output missing or clearly too small/large

## 5. Assembly Review

Purpose: evaluate draft assembly and decide whether polish or purge actions are
needed.

Inputs:

- required: `draft_assembly`
- optional: `clean_long_reads`
- optional: `clean_short_dna_reads`

Outputs:

- `assembly_eval_report`
- optional `polished_assembly`
- optional `purged_assembly`
- `final_assembly_for_scaffolding_or_annotation`

Planning rules:

- Always plan QUAST and BUSCO when their descriptors are available.
- Plan Merqury only with `clean_short_dna_reads`.
- Plan NextPolish SGS only with `clean_short_dna_reads`.
- Plan NextPolish LGS with `clean_long_reads`.
- Plan purge_dups with long-read depth evidence.
- Plan Redundans only when paired short reads exist and high heterozygous
  redundancy is plausible.

Review triggers:

- BUSCO duplicated is high
- assembly size is inflated relative to prior
- depth distribution suggests haplotigs
- purge cutoffs are uncertain
- polishing candidate reduces quality metrics

This stage is the main mandatory human gate.

## 6. Hi-C Scaffolding

Purpose: produce chromosome-scale scaffolds when Hi-C data and project goals
justify it.

Inputs:

- required: `final_assembly_for_scaffolding_or_annotation`
- required for execution: `hic_reads_registered`

Outputs:

- `hic_qc_report`
- optional `chromosome_scaffolded_assembly`
- optional `juicebox_review_bundle`

Planning rules:

- Skip if Hi-C reads are absent.
- Skip if the user did not request chromosome-level assembly.
- Put Hi-C-specific QC inside this stage, not generic read QC.
- Prefer HiC-Pro or Juicer for processing, 3D-DNA for scaffolding, and
  Juicebox for manual review.

Review triggers:

- low valid-pair ratio
- abnormal cis/trans or contact distribution
- contact map suggests misjoins

## 7. Repeat Annotation

Purpose: produce repeat library, repeat annotation, and masked genome for gene
structure annotation.

Inputs:

- `final_assembly_for_annotation`
- optional provided repeat library

Outputs:

- `repeat_library`
- `repeat_annotation`
- `softmasked_genome`
- optional `hardmasked_genome`

Planning rules:

- Use a provided repeat library when one is explicitly registered.
- Otherwise record EDTA and RepeatModeler as de novo library candidates without
  choosing a default until descriptor or human policy approves one.
- Plan RepeatMasker for masking and GFF output.
- Record TRF as an optional tandem-repeat helper.
- Propagate explicit high-fragmentation or low-confidence assembly signals into
  repeat annotation confidence without inventing numeric thresholds.

## 8. Gene Annotation

Purpose: generate gene models, transcripts, and predicted proteins.

Inputs:

- required by default: `softmasked_genome`
- fallback: `final_assembly_for_annotation` only when an explicit unmasked
  fallback reason is recorded
- optional: `clean_rnaseq_reads`
- optional: `clean_isoseq_reads`
- optional: `reference_proteins`
- optional: `repeat_annotation`

Outputs:

- `gene_models`
- `transcripts.fa`
- `predicted_proteins`
- evidence report

Planning rules:

- With no evidence, plan an explicit ab initio path such as AUGUSTUS without
  choosing a default method.
- With related proteins, add Miniprot and/or GeMoMa candidates.
- With RNA-seq, add HISAT2/StringTie and hints.
- With Iso-seq or long RNA, add IsoQuant.
- Mixed evidence requires a review/merge policy; do not select one by default.
- Treat EVM as optional compatibility because maintenance status is a risk.

Review triggers:

- RNA and homology evidence conflict
- predicted gene count is far outside expected range
- EVM fails or drops major evidence classes

## 9. Function Annotation

Purpose: annotate predicted proteins with domains, homology, KO, pathway, and
database-derived function.

Inputs:

- required: `predicted_proteins`
- optional: `gene_models`

Outputs:

- `function_annotation`
- InterPro/GO/domain tables
- DIAMOND homology tables
- KO assignment tables
- optional annotated GFF3

Planning rules:

- InterProScan is the domain/motif/GO backbone.
- DIAMOND is the fast homology path for Swiss-Prot, UniProt, or NR.
- KofamScan is the KO/pathway path.
- Record database snapshot/version in provenance.

Review triggers:

- database version is unknown
- annotation coverage is unexpectedly low
- InterProScan is unavailable and fallback-only output is produced
