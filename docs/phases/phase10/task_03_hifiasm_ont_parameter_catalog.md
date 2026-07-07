# Task 3: Create hifiasm_ont Parameter Catalog

**Phase**: 10
**Status**: Implemented; runtime binary help and ONT retry proof verified
**Depends on**: Task 1
**Created**: 2026-07-02
**Updated**: 2026-07-07

## Goal

Create the hifiasm_ont catalog for the first executable closed loop. hifiasm_ont is `hifiasm --ont` plus the full supported hifiasm parameter surface that remains valid in ONT mode.

The catalog uses only three levels:

- L0: LLM does not see or modify.
- L1: LLM sees as read-only evidence.
- L2: LLM sees and may recommend.

## File

```text
descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml
```

## Level Assignment

### L0: Fixed Mechanics

- `-o`: output prefix
- `-t`: threads
- positional ONT read inputs
- `--ont`: fixed mode flag for this method
- `--seed`: reproducibility seed when Hi-C mode is used
- `-i`: force recomputation
- CLI meta commands such as `-h` and `--version`

### L1: Read-Only Facts

- `--hg-size`: from genome_survey or explicit prior
- `--hom-cov`: from k-mer profile or coverage estimates
- `--n-hap`: from sample profile
- prompt-only facts: read N50, coverage, quality, heterozygosity, repeat fraction

### L2: Recommender Parameters

All ONT/hifiasm tunables not listed as L0/L1:

- ONT-specific: `--rl-cut`, `--sc-cut`, `--chem-c`, `--chem-f`
- purge: `-l`, `-s`, `-O`, `--purge-max`
- overlap/error-correction: `-k`, `-w`, `-f`, `-D`, `-N`, `-r`, `-z`, `--max-kocc`
- graph cleaning: `-a`, `-m`, `-p`, `-n`, `-x`, `-y`, `-u`, `--lowQ`, `--b-cov`, `--h-cov`, `--m-rate`, `--ctg-n`

For the first MVP, enumerate every supported ONT-valid hifiasm parameter from the pinned/project-supported hifiasm help output or documented version. Classify route mechanics and facts as L0/L1; every remaining behavior-changing tunable belongs in L2 and enters the recommender prompt. The example below is an excerpt showing the shape, not the complete implementation catalog.

## Catalog Shape Excerpt

```yaml
method_id: hifiasm_ont
tool: hifiasm
read_type: ont
description: "ONT simplex assembly mode using Oxford Nanopore long reads"

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
      - assembly.hifiasm_ont_parameters.threads
    description: "Number of CPU threads"

  input_reads:
    cli: null
    level: 0
    source: executor
    value_type: path_list
    source_priority:
      - execution.input_reads
    description: "Input ONT read files; must be FASTQ for ONT mode"

  ont_mode:
    cli: "--ont"
    level: 0
    source: route_selector
    value_type: boolean
    fixed_value: true
    description: "Enable hifiasm ONT mode"

  ignore_cached_overlaps:
    cli: "-i"
    level: 0
    source: executor
    value_type: boolean
    default: false
    description: "Force recomputation by ignoring cached overlaps/bin"

  hg_size:
    cli: "--hg-size"
    level: 1
    source: genome_survey
    value_type: string
    source_priority:
      - genome_survey.genome_size_prior
      - genome_survey.estimates.genome_size
      - shared_parameters.estimated_genome_size
    description: "Estimated haploid genome size"

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

  read_length_cutoff:
    cli: "--rl-cut"
    level: 2
    source: parameter_recommender
    value_type: int
    allowed_values: [1000, 3000, 5000, 10000, 15000]
    default: 1000
    description: "Minimum read length used by ONT assembly"

  score_cutoff:
    cli: "--sc-cut"
    level: 2
    source: parameter_recommender
    value_type: int
    allowed_values: [8, 10, 12, 15]
    default: 10
    description: "ONT score/quality cutoff"

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

  chimeric_support:
    cli: "--chem-c"
    level: 2
    source: parameter_recommender
    value_type: int
    min: 1
    description: "Chimeric read detection support threshold"

  chimeric_flank:
    cli: "--chem-f"
    level: 2
    source: parameter_recommender
    value_type: int
    min: 1
    description: "Chimeric read detection flanking length"
```

## Prompt Behavior

- The current implementation catalog exists at `descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml` and loads through the shared schema. Before release, verify the ONT-valid surface against the project-pinned hifiasm binary/help output in the execution environment.
- Runtime help verification was completed on 2026-07-07 against `/opt/homebrew/Caskroom/miniconda/base/envs/env_geno_exec/bin/hifiasm`, which reports `0.25.0-r726` and satisfies the project-supported `>=0.21.0` constraint in `descriptors/packages/hifiasm.yaml`. The committed verification record is `docs/phases/phase10/hifiasm_runtime_verification_2026-07-07.md`. The test remains skip-safe for normal developer runs, but `PHASE10_REQUIRE_HIFIASM_RUNTIME=1` turns missing `HIFIASM_BIN`/PATH hifiasm into a failure for release verification.
- L0 fields are excluded from prompt payload.
- L1 fields enter `readonly_facts`.
- L2 fields enter `modifiable_parameters`.
- LLM output may contain only L2 keys.
- If the LLM omits an L2 key, the command builder should omit that CLI flag unless a default is explicitly required by the run contract.
- There is no separate prompt allowlist. If an ONT-valid behavior-changing parameter is in the catalog as L2, it is part of the MVP recommender surface.
- L0/L1 values are derived through catalog `source_priority`; method adapters may provide `execution` context values, but generic command building should not hard-code parameter names.

## Tests

- Load hifiasm_ont catalog.
- Assert `ont_mode` is L0 with `fixed_value: true`.
- Assert `hg_size`, `hom_cov`, and `n_hap` are L1.
- Assert `read_length_cutoff`, `score_cutoff`, `purge_level`, `kmer_length`, and `high_freq_kmer_drop` are L2.
- Assert no exposure-control fields exist.
- Assert the catalog covers the supported ONT-valid hifiasm CLI surface rather than only the excerpted examples.

## Acceptance Criteria

- [x] `params.yaml` exists for hifiasm_ont.
- [x] The catalog validates through the three-level schema.
- [x] The catalog covers the currently documented ONT-valid hifiasm CLI surface, with every behavior-changing non-fact parameter assigned to L2.
- [x] The project-pinned hifiasm binary/help output has been verified against the catalog in the execution environment.
- [x] The prompt payload builder can include all L2 entries.
- [x] The integrated recommender node consumes that prompt payload in the main graph.
- [x] The command builder emits `--ont` mechanically from L0 and never from LLM output.
- [x] ONT failure retry can adjust L2 values such as `--rl-cut`, `--sc-cut`, and purge parameters.
