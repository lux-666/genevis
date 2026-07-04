# Task 8: Implement Parameter Recommender Node

**Phase**: 10
**Status**: Pending
**Depends on**: Tasks 1-7
**Created**: 2026-07-02
**Updated**: 2026-07-03

## Goal

Implement a LangGraph node that recommends L2 assembly parameters from:

- read_qc facts,
- genome_survey facts,
- selected evidence cases,
- hard rules,
- SKILL heuristics,
- optional retry context.

The node must support three modes:

1. initial recommendation,
2. retry after command execution failure,
3. retry after assembly_review failure.

For the MVP, the node must expose the full L2 surface from the selected method catalog. It may let the LLM omit parameters in its response, but it must not pre-filter L2 parameters down to a hand-picked subset.

## Node API

```python
def recommend_assembly_parameters(state: TopLevelState) -> dict:
    """Recommend L2 parameters and append a replayable parameter attempt."""
```

## Inputs From State

Assembly output should provide:

- selected route / primary method,
- full parameter profile ref,
- prompt profile for the current recommendation task,
- read_qc summary refs or embedded summaries,
- genome_survey result ref or embedded estimates,
- current attempt id,
- previous parameter attempt, if retrying,
- execution failure log tail, if command failed,
- review failure context, if assembly_review failed.

## Method Mapping

Use selected primary method when available. Route fallback:

```python
{
    "hifi": "hifiasm",
    "ont": "hifiasm_ont",
    "cyclone": "hifiasm_ont",
}
```

## Prompt Construction

1. Load method catalog.
2. Build and save the full parameter profile artifact:
   - `sample`
   - `assets`
   - `read_qc`
   - `genome_survey`
   - `previous_attempt`
   - `assembly_review`
3. Build a task-scoped prompt profile from the full profile:
   - initial recommendation: sample + assets + read_qc + genome_survey,
   - execution repair: sample + assets + previous command/params + exit code + failure class + log tails,
   - quality replan: sample + previous params/command/cases + assembly_review metrics.
4. Split parameters by level:
   - L0 excluded,
   - L1 copied into `readonly_facts`,
   - all L2 entries copied into `modifiable_parameters`.
5. Load `parameter_recommender` pack.
6. Select 3-8 cases.
7. Build prompt payload.
8. Save prompt payload as an artifact before invoking the LLM.

Prompt payload:

```yaml
task: recommend_initial
attempt_id: 0
method_id: hifiasm_ont
readonly_facts:
  genome_size_mb: 1000
  coverage: 42
  read_n50_kb: 30
  heterozygosity: medium
prompt_profile:
  sample:
    sample_id: s1
    target_level: draft
  assets:
    read_type: ont
    has_hifi: false
    has_ont: true
  read_qc:
    read_n50: 30000
    effective_coverage_5kb: 42
  genome_survey:
    estimated_genome_size: 1000000000
    heterozygosity: medium
modifiable_parameters:
  purge_level: {cli: "-l", allowed_values: [0, 1, 2, 3], default: 1}
  read_length_cutoff: {cli: "--rl-cut", allowed_values: [1000, 3000, 5000, 10000, 15000]}
selected_cases: []
rules: {}
previous_attempt: null
execution_failure_log_tail: null
review_failure: null
```

Retry after command failure adds:

```yaml
task: retry_after_execution_failure
previous_attempt: {...}
execution_failure_log_tail: "last 100 lines of stderr/stdout"
```

Retry after review failure adds:

```yaml
task: retry_after_review_failure
previous_attempt: {...}
review_failure:
  failure_mode: under_purged
  metrics: {}
  suggestions: {}
```

## LLM Output

```json
{
  "params": {
    "purge_level": 1,
    "read_length_cutoff": 3000
  },
  "reasoning": "...",
  "confidence": 0.8,
  "selected_case_ids": ["case_a"],
  "omitted_parameters": ["kmer_length"],
  "uncertainty_notes": []
}
```

The LLM may omit any L2 parameter. Missing keys mean "do not explicitly set this flag."

## Validation and Attempt Entry

After LLM response:

1. Save raw response as an artifact.
2. Parse JSON.
3. Validate output through Task 6 validator.
4. Append a `parameter_attempts` entry.
5. If valid, set:
   - `status: parameters_recommended`,
   - `current_parameter_attempt_id`,
   - `recommended_params`,
   - `parameter_attempts`.
6. If invalid, set:
   - `status: parameter_validation_failed` or `parameter_recommendation_failed`,
   - no command execution.

Attempt entry minimum:

```yaml
attempt_id: 0
method_id: hifiasm_ont
status: recommended
prompt_payload_ref: ...
input_profile_ref: ...
llm_response_ref: ...
prompt_profile: {}
selected_case_ids: []
recommended_params: {}
validation: {}
reasoning: "..."
confidence: 0.8
```

## Routing

Add the node after primary method selection and before candidate attempt planning:

```text
select_primary_and_fallback
 -> recommend_parameters
 -> validate/freeze path
```

Routing:

- `parameters_recommended` -> plan/freeze method attempt.
- `parameter_recommendation_failed` -> fail or block, depending policy.
- `parameter_validation_failed` -> fail fast.
- no catalog or no L2 parameters -> fail fast for Phase10 MVP, because the loop cannot be tested.

## Tests

- Initial recommendation writes prompt and response refs.
- Full parameter profile is saved for replay and prompt profile contains only task-relevant fields.
- LLM output with L2 params passes.
- LLM output with L1/L0 keys fails.
- Invalid JSON fails before command execution.
- Retry prompt includes last 100 log lines.
- Review retry prompt includes failure mode and metrics.
- Omitted L2 parameters are recorded but not emitted into command.

## Acceptance Criteria

- [ ] Node implemented.
- [ ] Prompt payload has L1 facts and L2 modifiable parameters only.
- [ ] Prompt payload uses a task-scoped profile and does not expose the full profile by default.
- [ ] Prompt payload includes every L2 parameter in the selected method catalog.
- [ ] Attempt entry records the full profile ref.
- [ ] Attempt entry is replayable.
- [ ] Failure branches stop before execution.
- [ ] Retry modes reuse previous attempt context.
