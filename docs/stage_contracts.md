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

- For ONT/CLR, default to SeqKit stats plus deterministic FOFN preparation.
- Treat NanoPlot or similar report generation as optional planning, not required
  for the current executable mainline.
- Treat Filtlong, chopper, fastplong, and seqkit seq as alternative length-filter
  candidates; choose at most one unless a human explicitly approves chaining.
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

- Run full k-mer survey only when `clean_short_dna_reads` exists.
- Use Jellyfish plus GenomeScope as the default full-survey path.
- Never feed ONT/CLR raw long reads into GenomeScope by default.
- If short DNA reads are missing but long reads and a size prior exist, record a
  low-confidence coverage prior; do not label it as a k-mer survey.
- If no usable survey inputs exist, skip with `no_low_error_short_reads` and set
  genome size source to `user_or_optional`.

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

- Flye is the simplest ONT/CLR raw-read path and the default primary candidate.
- NextDenovo is ONT/CLR-compatible for large or complex genomes, but requires a
  genome-size prior or human-supplied genome size.
- Canu is a conservative fallback with heavier resource expectations and also
  requires a genome-size prior.
- Keep hifiasm registered as a non-default HiFi/non-ONT compatibility route; do
  not auto-select it for CycloneSEQ/ONT/CLR-only planning.
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
- optional real local evaluation artifacts for QUAST and BUSCO when
  `assembly_review` local execution is enabled

Planning rules:

- Always plan QUAST and BUSCO when their descriptors are available.
- Plan Merqury only with `clean_short_dna_reads`.
- Plan NextPolish SGS only with `clean_short_dna_reads`.
- Plan NextPolish LGS with `clean_long_reads`.
- Plan purge_dups with long-read depth evidence.
- Plan Redundans only when paired short reads exist and high heterozygous
  redundancy is plausible.
- When `local_exec` includes `assembly_review` and `assembly_review.busco`
  run-config is supplied, run BUSCO in offline mode against the
  operator-provided lineage path; never download lineages.
- BUSCO lineage provenance must include path, lineage name, version, source,
  mode, and threads. Missing lineage marks the BUSCO signal `missing` rather
  than rejecting the candidate.
- Auto review is conservative with QUAST-only evidence; QUAST plus BUSCO
  evidence is required for `auto_decidable` when no disqualifying signals are
  present.

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

### Planning path (no provided annotation assets)

Inputs:

- required by default: `softmasked_genome`
- fallback: `final_assembly_for_annotation` only when an explicit unmasked
  fallback reason is recorded
- optional: `clean_rnaseq_reads`
- optional: `clean_isoseq_reads`
- optional: `reference_proteins`
- optional: `repeat_annotation`

Outputs:

- `GeneAnnotationBundle` with `status="planned"`, `evidence_profile` one of
  `no_evidence / rnaseq / long_rna / protein / mixed_evidence`
- `candidate_methods` and `helper_methods` populated; `method_selection_required=True`

Planning rules:

- With no evidence, plan an explicit ab initio path such as AUGUSTUS without
  choosing a default method.
- With related proteins, add Miniprot and/or GeMoMa candidates.
- With RNA-seq, add HISAT2/StringTie and hints.
- With Iso-seq or long RNA, add IsoQuant.
- Do not treat genomic ONT/CLR long reads as IsoQuant evidence.
- Mixed evidence requires a review/merge policy; do not select one by default.
- Treat EVM as optional compatibility because maintenance status is a risk.

Review triggers:

- RNA and homology evidence conflict
- predicted gene count is far outside expected range
- EVM fails or drops major evidence classes

### Adoption path (provided annotation assets present)

Inputs:

- required: `gene_models` (GFF3/GFF/GTF) from manifest
- required: `predicted_proteins` (FASTA) from manifest
- optional: `transcripts_fasta` (FASTA) from manifest
- genome ref resolved identically to planning path (softmasked preferred)

Outputs:

- `GeneAnnotationBundle` with `status="adopted_existing_annotation"`,
  `evidence_profile="provided_annotation"`
- `genes_gff3_ref`, `predicted_proteins_ref`, `transcripts_fasta_ref` point to
  the actual manifest-provided files (not work-path placeholders)
- `candidate_methods=[]`, `helper_methods=[]`, `method_selection_required=False`
- Artifact records: `gene_annotation.provided_gene_models`,
  `gene_annotation.provided_predicted_proteins`,
  optionally `gene_annotation.provided_transcripts_fasta`,
  `gene_annotation.bundle`

Non-goals for adoption path:

- Does not run BRAKER3, MAKER, AUGUSTUS, Miniprot, HISAT2, StringTie,
  IsoQuant, or GeMoMa.
- Does not infer proteins from GFF3.
- Does not merge provided annotation with additional evidence.
- Evidence conflicts are recorded as review triggers only; they do not block
  adoption or alter the adopted refs.

## 9. Function Annotation

Purpose: annotate predicted proteins with domains, homology, KO, pathway, and
database-derived function.

Inputs:

- required: `predicted_proteins` / `proteins.fa`
- optional: `gene_models`
- optional: `transcripts.fa`
- required before commit: database provenance records whose
  `input_proteins_ref` matches a materialized local predicted-proteins FASTA

Outputs:

- `function_annotation`
- InterPro/GO/domain tables
- DIAMOND homology tables
- KO assignment tables
- optional annotated GFF3

Planning rules:

- Record InterProScan, eggNOG-mapper, DIAMOND, and KofamScan as candidates
  without choosing a default path.
- InterProScan is the domain/motif/GO backbone candidate.
- DIAMOND is the fast homology path candidate for Swiss-Prot, UniProt, or NR.
- KofamScan is the KO/pathway candidate.
- Record database name, version, snapshot date, database source/ref, command or
  descriptor version, and input protein ref in provenance before committing.

Review triggers:

- database version is unknown
- annotation coverage is unexpectedly low
- InterProScan is unavailable and fallback-only output is produced
