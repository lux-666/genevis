# Task 4: Create parameter_recommender Knowledge Pack

**Phase**: 10
**Status**: Pending
**Depends on**: Task 1
**Created**: 2026-07-02
**Updated**: 2026-07-03

## Goal

Create the knowledge pack used by the parameter recommender. This pack must support the closed loop:

```text
read_qc + genome_survey + selected cases + rules + skill
 -> LLM recommends L2 params
 -> validator accepts/rejects
 -> command runs
 -> failure log or review metrics feed retry
```

The prompt must receive the complete L2 surface from the selected method catalog. Case selection may be a small evidence subset, but parameter selection is not a subset mechanism: every cataloged L2 parameter is visible to the recommender unless humans remove that parameter from the catalog.

## Directory

```text
descriptors/knowledge_packs/assembly/parameter_recommender/
├── cases.yaml
├── rules.yaml
└── SKILL.md
```

## File Responsibilities

### cases.yaml: Evidence Only

Cases are facts from papers or prior verified runs. They must not contain generic rules.

```yaml
- case_id: grouper_hifi_2021
  source:
    doi: "10.xxxx/example"
    year: 2021
    authors: "Shao et al."
  features:
    organism: "Epinephelus awoara"
    clade: vertebrate
    genome_size_mb: 1018
    heterozygosity: medium
    read_type: hifi
    coverage: 37
    read_n50_kb: 18
  params:
    tool: hifiasm
    purge_level: 1
    purge_similarity: default
  quality:
    busco_complete: 0.968
    busco_duplicate: 0.018
    n50_mb: 42.3
    assembly_size_mb: 1020
    contig_count: 78
  evidence_note: "Reported hifiasm assembly with moderate heterozygosity and conservative purge settings."
```

### rules.yaml: Boundaries Only

Rules encode hard validation boundaries. They should reject impossible, unsafe, or explicitly disallowed values. They should not explain how to optimize.

```yaml
rules:
  - rule_id: high_heterozygosity_purge_ceiling
    condition:
      heterozygosity: high
    constraints:
      purge_level: [0, 1]
    reason: "Aggressive purging is blocked for high-heterozygosity samples."

  - rule_id: low_coverage_read_length_ceiling
    condition:
      coverage: {max: 30}
    constraints:
      read_length_cutoff: [1000, 3000]
    reason: "Low coverage must not discard too many reads."

global_constraints:
  purge_level:
    allowed: [0, 1, 2, 3]
  read_length_cutoff:
    min: 1000
    max: 50000
  score_cutoff:
    allowed: [8, 10, 12, 15]
```

### SKILL.md: Explainable Heuristics Only

Skill text explains how to reason when choosing L2 parameters. It may describe patterns and failure diagnosis, but it must not replace `rules.yaml`.

Required guidance:

- Match current sample to selected evidence cases by read type, genome size, heterozygosity, coverage, quality, and clade.
- Use L1 facts as facts, not guesses.
- Recommend only L2 parameter keys from the method catalog.
- It is acceptable to omit an L2 parameter when evidence is weak.
- Prefer conservative changes under uncertainty.
- On command failure, use the last 100 log lines to diagnose whether retrying parameters is meaningful.
- On review failure, adjust parameters according to the failure mode rather than random exploration.

## Case Selection

Before prompting the LLM, code should select a small set of cases:

1. Filter by compatible `tool` / `read_type`.
2. Score similarity using available facts:
   - read type exact match,
   - coverage bucket,
   - genome size bucket,
   - heterozygosity bucket,
   - clade if available,
   - read N50 / quality when available.
3. Pass the top 3-8 cases to the prompt.
4. If too few cases match, include a `case_selection_warning` in the prompt.

The LLM should not receive the full case database by default.

## Prompt Payload Contract

```yaml
task: recommend_initial | retry_after_execution_failure | retry_after_review_failure
method_id: hifiasm_ont
readonly_facts:
  genome_size_mb: 1000
  coverage: 42
  read_n50_kb: 30
  heterozygosity: medium
  repeat_fraction: 0.45
modifiable_parameters:
  purge_level:
    cli: "-l"
    allowed_values: [0, 1, 2, 3]
    default: 1
    description: "Purge duplication level"
  read_length_cutoff:
    cli: "--rl-cut"
    allowed_values: [1000, 3000, 5000, 10000, 15000]
selected_cases: []
rules: {}
previous_attempt: null
execution_failure_log_tail: null
review_failure: null
required_output:
  params: "dict containing only modifiable parameter keys"
  reasoning: "short explanation tied to facts/cases/failure"
  confidence: "float 0-1"
  selected_case_ids: "case ids used"
  omitted_parameters: "L2 keys intentionally left unset"
  uncertainty_notes: "list"
```

## LLM Output Contract

```json
{
  "params": {
    "purge_level": 1,
    "read_length_cutoff": 3000
  },
  "reasoning": "Medium heterozygosity and moderate coverage support conservative purging; ONT coverage allows a modest read length cutoff.",
  "confidence": 0.78,
  "selected_case_ids": ["grouper_hifi_2021"],
  "omitted_parameters": ["kmer_length", "minimizer_window"],
  "uncertainty_notes": ["Heterozygosity is qualitative."]
}
```

Validation must reject:

- output keys not present as L2 parameters in the catalog,
- attempts to write L0 or L1 keys,
- values outside catalog or rules boundaries,
- invalid JSON.

## Acceptance Criteria

- [ ] Pack directory exists.
- [ ] `cases.yaml` has valid evidence-case structure.
- [ ] `rules.yaml` has at least five hard boundary rules.
- [ ] `SKILL.md` explains initial recommendation and both retry modes.
- [ ] Loader returns rules, cases, and skill text.
- [ ] Prompt builder uses selected cases, not the entire case database.
- [ ] Prompt builder includes every L2 parameter from the selected method catalog, with no hidden L2 allowlist.
