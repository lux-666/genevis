# Task 10: Integration Tests for Parameter Closed Loop

**Phase**: 10
**Status**: Pending
**Depends on**: Tasks 1-9
**Created**: 2026-07-02
**Updated**: 2026-07-03

## Goal

Test the minimum closed loop:

```text
profile -> select cases -> recommend L2 params -> validate -> build argv -> freeze -> run/fail -> retry -> review/pass-or-retry
```

The tests should prove the system is executable, replayable, can fail safely, can retry deterministically, and exposes the full cataloged L2 parameter surface rather than a hand-picked prompt subset.

## Test 1: Catalog and Prompt Permissions

```python
def test_catalog_three_level_permissions():
    catalog = load_parameter_catalog("hifiasm_ont")

    assert catalog.parameters["output_prefix"].level == 0
    assert catalog.parameters["threads"].level == 0
    assert catalog.parameters["ont_mode"].level == 0
    assert catalog.parameters["hg_size"].level == 1
    assert catalog.parameters["hom_cov"].level == 1
    assert catalog.parameters["purge_level"].level == 2
    assert catalog.parameters["read_length_cutoff"].level == 2
    assert catalog.parameters["kmer_length"].level == 2


def test_prompt_excludes_l0_and_includes_l1_l2():
    payload = build_parameter_prompt_payload(...)
    expected_l2 = {key for key, spec in catalog.parameters.items() if spec.level == 2}
    assert "output_prefix" not in payload["readonly_facts"]
    assert "threads" not in payload["readonly_facts"]
    assert "ont_mode" not in payload["modifiable_parameters"]
    assert "genome_size_mb" in payload["readonly_facts"]
    assert "purge_level" in payload["modifiable_parameters"]
    assert set(payload["modifiable_parameters"]) == expected_l2
```

## Test 2: Validation Blocks Wrong-Level Output

```python
def test_llm_cannot_modify_l0_or_l1():
    result = validate_recommended_parameters(
        method_id="hifiasm_ont",
        catalog=catalog,
        recommended_params={
            "threads": 64,
            "hg_size": "2g",
            "purge_level": 1,
        },
        readonly_facts={"genome_size_mb": 1000},
        rules={},
    )

    assert not result["valid"]
    assert {v["param"] for v in result["violations"]} >= {"threads", "hg_size"}
```

## Test 3: Happy Path Builds Replayable Attempt

```python
def test_initial_recommendation_to_command_builder(tmp_path, mock_llm):
    state = create_state_with_read_qc_and_genome_survey(
        platform="ont",
        genome_size=1_000_000_000,
        coverage=42,
        read_n50=30_000,
    )

    result = recommend_assembly_parameters(state)
    assembly = result["stage_outputs"]["assembly"]

    assert assembly["status"] == "parameters_recommended"
    attempt = assembly["parameter_attempts"][0]
    assert attempt["attempt_id"] == 0
    assert attempt["prompt_payload_ref"]
    assert attempt["llm_response_ref"]
    assert attempt["validation"]["valid"] is True

    sources = build_parameter_sources(...)
    argv = build_command(method_id="hifiasm_ont", catalog=catalog, parameter_sources=sources)

    assert argv[0] == "hifiasm"
    assert "--ont" in argv
    assert "--hg-size" in argv
    assert "-l" in argv
    assert "--rl-cut" in argv
```

## Test 4: Command Failure Retry Uses Last 100 Lines

```python
def test_execution_failure_retry_prompt_has_log_tail(tmp_path, mock_runner, mock_llm):
    state = create_state_ready_for_hifiasm_ont_attempt(...)
    mock_runner.fail_with_stderr("\n".join(f"line {i}" for i in range(150)))

    failed = run_hifiasm_ont_attempt(state)
    retry = execution_retry_gate(failed)

    assembly = retry["stage_outputs"]["assembly"]
    assert assembly["status"] == "retry_parameters_after_execution_failure"
    assert assembly["current_parameter_attempt_id"] == 1
    assert "line 50" not in assembly["execution_failure_log_tail"]
    assert "line 149" in assembly["execution_failure_log_tail"]

    replanned = recommend_assembly_parameters(retry_state)
    attempt = replanned["stage_outputs"]["assembly"]["parameter_attempts"][1]
    assert attempt["attempt_id"] == 1
    assert attempt["previous_attempt_id"] == 0
```

## Test 5: Review Failure Retry

```python
def test_review_failure_replans_parameters(mock_llm):
    state = create_state_with_review_metrics(
        busco_complete=0.94,
        busco_duplicate=0.21,
        assembly_size_ratio=1.35,
    )

    gate = parameter_quality_gate(state)
    assert gate["stage_outputs"]["assembly"]["review_failure"]["failure_mode"] == "under_purged"

    replanned = recommend_assembly_parameters(gate_state)
    attempt = replanned["stage_outputs"]["assembly"]["parameter_attempts"][-1]
    assert attempt["attempt_id"] == 1
    assert attempt["review_failure"]["failure_mode"] == "under_purged"
```

## Test 6: Replay Contract

```python
def test_attempt_ledger_is_replayable():
    attempt = load_parameter_attempt(...)

    assert attempt["catalog_ref"]
    assert attempt["knowledge_pack_ref"]
    assert attempt["prompt_payload_ref"]
    assert attempt["llm_response_ref"]
    assert attempt["parameter_sources"]["l0_fixed"]
    assert attempt["parameter_sources"]["l1_readonly"] is not None
    assert attempt["parameter_sources"]["l2_recommended"] is not None
    assert attempt["argv"][0] == "hifiasm"
```

## Test 7: Retry Caps

```python
def test_max_execution_retries_stop():
    state = create_failed_state(execution_retry_count=1, max_execution_retries=1)
    result = execution_retry_gate(state)
    assert result["supervision_state"] == "failed_terminal"


def test_max_review_retries_stop():
    state = create_review_failed_state(review_retry_count=2, max_review_retries=2)
    result = parameter_quality_gate(state)
    assert result["stage_outputs"]["assembly"]["status"] in {"parameter_retries_exhausted", "blocked_human"}
```

## Fixtures Needed

- Mock LLM returning valid L2 params.
- Mock LLM returning invalid JSON.
- Mock LLM returning L0/L1 keys.
- Mock execution runner that writes stderr/stdout.
- Synthetic read_qc summary with total bases, N50, and quality.
- Synthetic genome_survey result with genome size and heterozygosity.
- Synthetic assembly_review metrics for under-purged, over-purged, fragmented, and pass.

## Acceptance Criteria

- [ ] Tests cover initial pass path.
- [ ] Tests prove the prompt includes the full cataloged L2 surface, not a hand-picked subset.
- [ ] Tests cover validation failure before execution.
- [ ] Tests cover command failure retry with log tail.
- [ ] Tests cover review failure retry.
- [ ] Tests prove attempt entries are append-only and replayable.
- [ ] Tests confirm only the L0/L1/L2 permission model remains.
