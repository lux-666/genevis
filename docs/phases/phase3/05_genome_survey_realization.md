# 05 Genome Survey Realization

Task status: planned.

## Goal

Make `genome_survey` a real conditional stage for projects with low-error short
DNA reads while keeping ONT/CLR-only workflows non-blocking.

## Scope

Real execution should start with one narrow short-read k-mer survey path, for
example Jellyfish or Meryl plus GenomeScope2.

Candidate methods:

- `jellyfish_count`
- `meryl_count`
- `genomescope2_model`
- `smudgeplot_model`

Allowed modes:

- full survey when `clean_short_dna_reads` exists;
- explicit skip or low-confidence prior when only ONT/CLR long reads exist;
- skip when the user provides a trusted `known_genome_size`.

## Boundaries

- Do not run Jellyfish, Meryl, GenomeScope2, or Smudgeplot on ONT/CLR long reads
  by default.
- Do not make genome survey a hard prerequisite for assembly.
- Do not treat a long-read-only fallback as a real k-mer survey.
- Do not invent genome-size confidence thresholds.
- Trigger a `genome_size_conflict` review only after a real short-read survey
  exists and conflicts with a trusted manifest/user genome-size estimate.

## Expected Outputs

- `genome_survey_result`
- `kmer_hist`
- `genome_size_estimate`
- `heterozygosity_estimate`
- `repeat_estimate`
- `genome_survey_bundle`
- k-mer histogram/report refs when full survey runs
- explicit skip or low-confidence notes when full survey cannot run

## Acceptance

- Full survey runs only from short DNA reads.
- ONT/CLR-only workflows proceed to assembly with an explicit survey status.
- Assembly can consume survey output as a prior, not as an input file.
