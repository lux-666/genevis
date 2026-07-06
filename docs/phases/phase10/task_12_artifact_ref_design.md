# Task 12: Session/Attempt Artifact Reference Design

**Phase**: 10
**Status**: Design corrected; implementation cleanup pending
**Depends on**: Tasks 8-10
**Created**: 2026-07-05
**Updated**: 2026-07-06

## Goal

Use one runtime artifact-reference scheme for the Phase10 parameter loop:
session/attempt-scoped paths are the primary refs, and SHA256 hashes are
integrity fields. Do not keep hash-only refs or compatibility fallback APIs as
supported main-flow behavior.

## Problem

The prompt payload builder currently supports content-addressable fallback refs
such as:

```text
data/artifacts/profiles/<hash>.json
data/artifacts/payloads/<hash>.json
```

That was useful for standalone tests, but it conflicts with the Phase10 replay
contract. Parameter attempts are ordered within a session, and humans need to
inspect the exact profile, prompt, response, command, logs, validation result,
and review context for each attempt. The runtime ref must therefore be a stable
session/attempt path.

## Final Ref Contract

Every main-flow parameter attempt records path refs plus hashes:

```yaml
attempt_id: 0
input_profile_ref: data/artifacts/assembly/<session>/input_profile.json
input_profile_hash: <sha256>
prompt_payload_ref: data/artifacts/assembly/<session>/parameter_attempt_0/prompt_payload.json
prompt_payload_hash: <sha256>
llm_response_ref: data/artifacts/assembly/<session>/parameter_attempt_0/llm_response.json
llm_response_hash: <sha256>
validation_ref: data/artifacts/assembly/<session>/parameter_attempt_0/validation.json
validation_hash: <sha256>
```

Rules:

- `*_ref` fields are path refs in the running graph.
- `*_hash` fields are for integrity verification only.
- Retry attempts write `parameter_attempt_<N>/` and never overwrite earlier
  attempts.
- The full input profile is session-scoped when unchanged, but the attempt still
  records the profile ref and hash it used.
- Prompt payloads, responses, validation results, command metadata, and failure
  contexts are attempt-scoped.

## Directory Structure

```text
data/artifacts/assembly/<session>/
  input_profile.json
  parameter_attempt_0/
    prompt_payload.json
    llm_response.json
    validation.json
    command.json
    execution_failure_tail.txt
    review_failure.json
  parameter_attempt_1/
    prompt_payload.json
    llm_response.json
    validation.json
    command.json
```

## API Shape

The artifact writer requires session context. Tests may use temporary sessions,
but should not exercise a hash-only runtime branch.

```python
def write_profile_artifact(
    *,
    profile: dict[str, Any],
    session_id: str,
    data_dir: Path,
) -> ArtifactRef:
    """Write input_profile.json and return path ref plus SHA256."""


def write_attempt_artifact(
    *,
    payload: dict[str, Any] | str,
    session_id: str,
    attempt_id: int,
    name: str,
    data_dir: Path,
) -> ArtifactRef:
    """Write data/artifacts/assembly/<session>/parameter_attempt_<N>/<name>."""
```

`ArtifactRef` should be a typed model, not a tuple:

```python
class ArtifactRef(BaseModel):
    path_ref: str
    sha256: str
    artifact_type: str
```

## Cleanup Requirements

- Remove hash-only write paths from the main prompt/profile writer API.
- Remove `write_artifacts=False` as a main-flow behavior. Pure builders may
  still return in-memory payloads, but graph nodes must write artifacts before
  invoking the LLM.
- Remove loader behavior that scans arbitrary session directories by hash as a
  supported replay path. Replay starts from path refs recorded in the attempt.
- Update tests that relied on hash-only refs to create a temporary session and
  assert path refs plus hashes.
- Update Task 8 attempt entries to record `ArtifactRef` fields rather than raw
  tuples or hash strings.

## Acceptance Criteria

- [ ] Main graph writes profile, prompt, response, validation, and command
      metadata under `data/artifacts/assembly/<session>/`.
- [ ] Every attempt-specific artifact is under
      `parameter_attempt_<attempt_id>/`.
- [ ] Attempt entries store path refs and SHA256 integrity fields.
- [ ] No running graph path uses `data/artifacts/profiles/<hash>.json` or
      `data/artifacts/payloads/<hash>.json`.
- [ ] Tests use temporary session ids instead of hash-only fallback refs.
- [ ] Documentation describes only the final session/attempt ref contract.
