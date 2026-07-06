# Task 5: Create Seed Evidence Cases

**Phase**: 10
**Status**: Implemented with internal seed observations
**Depends on**: Task 4
**Created**: 2026-07-02
**Updated**: 2026-07-06

## Goal

Populate `parameter_recommender/cases/*.yaml` with 5-10 seed cases and list them through `parameter_recommender/cases.yaml`. Cases are evidence only: sample facts, parameters used, quality results, and source. Do not encode rules, generic recommendations, or expected LLM answers in cases.

The current seed cases are explicitly labeled `source.kind: seed_observation`.
They are synthetic/internal calibration cases for selector and prompt-contract
testing. Do not treat them as verified literature evidence unless a later edit
replaces or supplements their source blocks with checked external provenance.

## Case Template

```yaml
- case_id: organism_method_year
  source:
    doi: "10.xxxx/example"
    year: 2023
    authors: "FirstAuthor et al."
  features:
    organism: "Species name"
    clade: plant|vertebrate|invertebrate|fungal|protist|other
    genome_size_mb: 1000
    heterozygosity: low|medium|high|unknown
    read_type: hifi|ont|hifi+ont|hifi+ul
    coverage: 40
    read_n50_kb: 18
    mean_quality: null
  params:
    tool: hifiasm|hifiasm_ont
    values:
      purge_level: 1
      purge_similarity: default
      read_length_cutoff: null
      score_cutoff: null
  quality:
    busco_complete: 0.975
    busco_duplicate: 0.023
    n50_mb: 35.2
    assembly_size_mb: 1050
    contig_count: 45
  evidence_note: "What was reported, without turning it into a rule."
```

## Diversity Targets

Include cases across:

- low, medium, and high heterozygosity,
- small, medium, and large genome sizes,
- HiFi and ONT read types,
- low, moderate, and high coverage,
- at least three broad clades.

## Initial Seed Plan

1. Low-heterozygosity small plant, HiFi, high coverage.
2. Medium/high-heterozygosity vertebrate, HiFi, moderate coverage.
3. Medium-heterozygosity invertebrate, HiFi, high coverage.
4. Small fungal genome, HiFi, high coverage.
5. Raw ONT vertebrate, moderate coverage.
6. High-quality ONT plant, high coverage.
7. Very large genome with low coverage.
8. Polyploid plant or haplotype-rich sample.

## Case Selection Requirements

The implementation should not pass all cases to the LLM. It should select a small evidence pack per attempt:

- exact read type/tool matches first,
- then similar coverage/genome-size/heterozygosity buckets,
- then clade similarity,
- keep 3-8 cases,
- include `selected_case_ids` in the attempt ledger.

## Quality Checks

- [x] At least five cases.
- [x] Every case has `source`, `features`, `params`, `quality`, and `evidence_note`.
- [x] Internal seed cases are labeled as internal observations rather than DOI-backed literature evidence.
- [x] No rule language such as "must", "always", or "never" appears in `evidence_note`.
- [x] Cases parse through the existing knowledge loader.

## Acceptance Criteria

- [x] `cases.yaml` exists, lists case shard files, and loads through the strict knowledge-pack loader.
- [x] Seed cases cover HiFi and ONT.
- [x] Seed cases include enough fields for similarity selection.
- [x] Integration tests can select a small case subset for a mock sample.

## Remaining Work

No more seed-case scaffolding belongs in this task. Future work may replace
internal seed observations with verified literature or production-run evidence,
but that is a data-curation update, not a new planner or recommender pathway.
