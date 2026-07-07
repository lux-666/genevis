# Task 6: Implement Rule Validator

**Phase**: 10
**Status**: Completed
**Depends on**: Tasks 1 and 4
**Created**: 2026-07-02
**Updated**: 2026-07-07

## Goal

Validate LLM-recommended L2 parameters before execution. The validator is the fail-fast gate between recommendation and command freezing.

It must enforce:

- LLM output may contain only catalog L2 keys.
- L0 and L1 keys are never accepted from LLM output.
- Values must satisfy catalog bounds/enums.
- Values must satisfy `parameter_recommender/rules.yaml`.
- Validation failures stop execution and append a replayable failed attempt entry.

## Location

```text
app/stages/assembly/parameter_validation.py
```

## Core API

```python
from typing import Any


def validate_recommended_parameters(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    recommended_params: dict[str, Any],
    readonly_facts: dict[str, Any],
    rules: dict[str, Any],
    full_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return {'valid': bool, 'violations': [...], 'warnings': [...]}."""
```

## Validation Order

1. **JSON shape**: recommendation has a dict-like `params`.
2. **Permission**: every key in `params` exists in catalog and has `level: 2`.
3. **Catalog constraints**: enforce `allowed_values`, `min`, and `max`.
4. **Rule constraints**: apply conditional rules using the complete full profile when available, plus any explicit readonly facts.
5. **No mutation of facts**: reject any output key with catalog level 0 or 1.

## Rule Matching Semantics

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
```

Condition behavior:

- Missing facts make the rule non-applicable, not failed.
- Scalar condition means exact match.
- `{min: x}` and `{max: y}` apply numeric bounds.
- Constraints may be enum lists or numeric min/max objects.

## Example Violation

Input:

```python
full_profile = {
    "genome_survey": {"heterozygosity": "high"},
    "read_qc": {"effective_coverage_5kb": 35},
}
recommended_params = {"purge_level": 3}
```

Output:

```python
{
    "valid": False,
    "violations": [
        {
            "param": "purge_level",
            "value": 3,
            "rule_id": "high_heterozygosity_purge_ceiling",
            "allowed": [0, 1],
            "severity": "critical",
        }
    ],
    "warnings": [],
}
```

## Failure State

Validation failure should produce:

```yaml
status: parameter_validation_failed
attempt_id: 0
method_id: hifiasm_ont
attempted_params: {}
validation:
  valid: false
  violations: []
prompt_payload_ref: ...
llm_response_ref: ...
```

The executor must not run.

## Tests

- Unknown parameter key fails.
- L0 key in LLM output fails.
- L1 key in LLM output fails.
- Catalog enum violation fails.
- Catalog min/max violation fails.
- Conditional rule violation fails.
- Conditional rules can match dotted full-profile paths such as `genome_survey.heterozygosity`.
- Non-applicable rule is skipped.
- Valid L2 recommendation passes.

## Acceptance Criteria

- [x] Validator implemented.
- [x] Validation result is JSON-serializable.
- [x] Validator can use complete profile facts for hard constraints without exposing all fields to the LLM.
- [x] Validation failure is recorded in `parameter_attempts`.
- [x] Command freezing is impossible after validation failure.
