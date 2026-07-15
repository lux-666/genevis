# Phase 10 Replay Contract

Phase 10 replay is based on typed `parameter_attempts` plus session/attempt-scoped artifacts.
New recommendation attempts are append-only; the current attempt entry may be
completed in place as freeze, execution, and review facts become available.

## Attempt Entry

Each attempt is a `ParameterAttempt` with:

- `attempt_id`, `method_id`, and `status`;
- `input_profile_ref` and `prompt_payload_ref` as `ArtifactRef` objects;
- optional `llm_response_ref` and `validation_ref`;
- optional `command_ref` and `review_failure_ref`;
- `catalog_ref`, `knowledge_pack_ref`, and `selected_case_ids`;
- `recommended_params`, `omitted_parameters`, `parameter_sources`, `rendered_parameters`, `argv`, and `validation`;
- optional `previous_attempt_id`, `execution_result`, `failure_log_tail`, and `review_failure`.

An `ArtifactRef` contains `path_ref`, `sha256`, and `artifact_type`.

## Artifact Paths

The running graph writes main-flow refs as paths:

```text
data/artifacts/assembly/<session>/parameter_attempt_<N>/input_profile.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/prompt_payload.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/llm_response.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/validation.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/command.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/review_failure.json
```

`path_ref` is the replay location. `sha256` is for integrity verification. Hash-only refs are not the main runtime scheme.

## Replay Steps

1. Load the selected `parameter_attempts` entry.
2. Verify `input_profile_ref.path_ref` and `prompt_payload_ref.path_ref` against their hashes.
3. Inspect `llm_response_ref` and `validation_ref`.
4. Reconstruct the command from recorded `parameter_sources` and compare it with recorded `argv`.
   Inspect `rendered_parameters` for method-specific config values that are
   materialized outside the generic argv command builder.
5. Inspect `command_ref` for the frozen argv/script metadata when present.
6. For retries, follow `previous_attempt_id` and inspect `failure_log_tail`, `review_failure`, or `review_failure_ref`.
7. When preparing a retry, keep the earlier attempt's recommendation, command,
   and attempt directory stable; append `attempt_id + 1` for the new
   recommendation. Retry gates may complete the reviewed/executed attempt in
   place with lifecycle facts such as `execution_result`, `failure_log_tail`,
   `review_failure`, and `review_failure_ref`.

## Standalone Attempt Artifacts

Command metadata and review failure context are written as attempt-scoped `command.json` and `review_failure.json` files and referenced from the compact `ParameterAttempt` entry with `ArtifactRef` fields. These files are completion artifacts for the same attempt, not replacements for a prior recommendation artifact.
