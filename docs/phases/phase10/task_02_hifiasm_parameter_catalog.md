# Task 2: Create hifiasm Parameter Catalog

**Phase**: 10
**Status**: Implemented; runtime binary verification pending
**Depends on**: Task 1
**Created**: 2026-07-02
**Updated**: 2026-07-07

## Goal

Create the first hifiasm catalog using the three-level permission model. This catalog is not an expert safety policy; it is the full supported hifiasm parameter surface available to the closed loop. L0 never enters the prompt, L1 enters as read-only facts, and every L2 entry is eligible for LLM recommendation.

## File

```text
descriptors/knowledge_packs/assembly/methods/hifiasm/params.yaml
```

## Level Assignment

### L0: LLM Does Not See or Modify

Executor and route mechanics:

- `-o`: output prefix
- `-t`: threads
- positional input reads
- `-h`: help
- `--version`: version audit command
- `-i`: ignore cached overlaps/bin and force recomputation
- route-level switches when they are fixed by selected method, such as `--primary`, trio inputs, Hi-C inputs, or UL inputs in non-base variants

### L1: LLM Sees but Does Not Modify

Facts from read_qc, genome_survey, manifest, or run_config:

- `--hg-size`: estimated haploid genome size
- `--hom-cov`: homozygous coverage
- `--n-hap`: haplotype count/ploidy-derived assumption
- derived prompt facts not necessarily emitted as hifiasm flags: coverage, read N50, quality summaries, heterozygosity, repeat fraction

### L2: LLM Sees and May Modify

All behavior-changing tunables from the supported hifiasm CLI surface:

- purge parameters: `-l`, `-s`, `-O`, `--purge-max`
- overlap/error-correction parameters: `-k`, `-w`, `-f`, `-D`, `-N`, `-r`, `-z`, `--max-kocc`
- graph cleaning parameters: `-a`, `-m`, `-p`, `-n`, `-x`, `-y`, `-u`, `--lowQ`, `--b-cov`, `--h-cov`, `--m-rate`, `--ctg-n`
- trio internal thresholds when trio route is selected: `-c`, `-d`, `--t-occ`, `--trio-dual`

For the first MVP, do not stop at a small exercise set. Enumerate every supported hifiasm parameter from the pinned/project-supported hifiasm help output or documented version, assign it to L0/L1/L2, and include every L2 parameter in the prompt contract. The example below is an excerpt showing the shape; the implementation catalog must continue through the full parameter surface. Humans may manually remove risky or unsupported L2 keys later by editing the catalog.

## Catalog Shape Excerpt

```yaml
method_id: hifiasm
tool: hifiasm
read_type: hifi
description: "HiFi-only assembly mode using PacBio high-accuracy long reads"

parameters:
  output_prefix:
    cli: "-o"
    level: 0
    source: executor
    value_type: path
    source_priority:
      - execution.output_prefix
    description: "Output file prefix"

  threads:
    cli: "-t"
    level: 0
    source: run_config
    value_type: int
    default: 32
    min: 1
    source_priority:
      - assembly.hifiasm_parameters.threads
    description: "Number of CPU threads"

  input_reads:
    cli: null
    level: 0
    source: executor
    value_type: path_list
    source_priority:
      - execution.input_reads
    description: "Input HiFi read files"

  ignore_cached_overlaps:
    cli: "-i"
    level: 0
    source: executor
    value_type: boolean
    default: false
    description: "Force recomputation by ignoring cached overlaps/bin"

  primary_mode:
    cli: "--primary"
    level: 0
    source: route_selector
    value_type: boolean
    default: false
    description: "Output primary and alternate contigs"

  hg_size:
    cli: "--hg-size"
    level: 1
    source: genome_survey
    value_type: string
    source_priority:
      - genome_survey.genome_size_prior
      - genome_survey.estimates.genome_size
      - shared_parameters.estimated_genome_size
    description: "Estimated haploid genome size, formatted for hifiasm"

  hom_cov:
    cli: "--hom-cov"
    level: 1
    source: genome_survey
    value_type: int
    source_priority:
      - genome_survey.estimates.hom_cov
      - genome_survey.estimates.kmer_peak_coverage
      - read_qc.estimated_coverage
    description: "Homozygous coverage"

  n_hap:
    cli: "--n-hap"
    level: 1
    source: manifest
    value_type: int
    allowed_values: [1, 2, 3, 4]
    default: 2
    description: "Expected haplotype count"

  purge_level:
    cli: "-l"
    level: 2
    source: parameter_recommender
    value_type: int
    allowed_values: [0, 1, 2, 3]
    default: 1
    description: "Purge duplication level"

  purge_similarity:
    cli: "-s"
    level: 2
    source: parameter_recommender
    value_type: string
    allowed_values: ["default", "0.75", "0.65", "0.55"]
    default: "default"
    description: "Similarity threshold for duplicate haplotig purging"

  duplicate_overlap_min_reads:
    cli: "-O"
    level: 2
    source: parameter_recommender
    value_type: int
    min: 1
    description: "Minimum overlap read support for duplicate haplotig handling"

  purge_max:
    cli: "--purge-max"
    level: 2
    source: parameter_recommender
    value_type: int
    min: 1
    description: "Coverage upper bound for purge duplication"

  kmer_length:
    cli: "-k"
    level: 2
    source: parameter_recommender
    value_type: int
    default: 51
    min: 11
    max: 101
    description: "K-mer length for overlap detection"

  minimizer_window:
    cli: "-w"
    level: 2
    source: parameter_recommender
    value_type: int
    default: 51
    min: 1
    max: 200
    description: "Minimizer window size"

  high_freq_kmer_drop:
    cli: "-D"
    level: 2
    source: parameter_recommender
    value_type: float
    default: 5.0
    min: 0
    description: "Discard ultra-high-frequency k-mers"

  max_overlaps_per_read:
    cli: "-N"
    level: 2
    source: parameter_recommender
    value_type: int
    min: 1
    description: "Maximum overlaps considered per read"
```

## Implementation Notes

- The current implementation catalog exists at `descriptors/knowledge_packs/assembly/methods/hifiasm/params.yaml` and loads through the shared schema. Before release, verify the catalog against the project-pinned hifiasm binary/help output in the execution environment.
- Defaults are documentation and fallback only. If the recommender omits an L2 parameter, the command builder should usually omit it and let hifiasm defaults apply.
- L1 values are emitted only when confidently derived. If a factual value is missing, put `unknown` or omit it from L1 prompt context.
- L0/L1 values are derived through catalog `source_priority`; do not add builder-side hard-coded knowledge for specific parameter names.
- L2 bounds should be coarse safety bounds. Biological rules live in `parameter_recommender/rules.yaml`.
- The prompt should receive all L2 parameters present in this catalog. Manual pruning happens by editing the catalog, not by adding another exposure flag or by maintaining a hidden allowlist.

## Tests

- Load the catalog and reject unknown fields.
- Assert all parameters have levels in `{0, 1, 2}`.
- Assert `output_prefix`, `threads`, and `input_reads` are L0.
- Assert `hg_size`, `hom_cov`, and `n_hap` are L1.
- Assert behavior-changing parameters such as `purge_level`, `kmer_length`, and `high_freq_kmer_drop` are L2.
- Assert the catalog records the supported hifiasm CLI surface rather than only the excerpted MVP examples.

## Acceptance Criteria

- [x] `params.yaml` exists for hifiasm.
- [x] No secondary permission-control fields exist.
- [x] Catalog loads through the new schema.
- [x] The catalog covers the currently documented hifiasm CLI surface, with every behavior-changing non-fact parameter assigned to L2.
- [ ] The project-pinned hifiasm binary/help output has been verified against the catalog in the execution environment.
- [x] The prompt payload builder can include all L2 entries.
- [x] The integrated recommender node consumes that prompt payload in the main graph.
- [x] The command builder can merge L0/L1/L2 sources into a deterministic hifiasm argv.
