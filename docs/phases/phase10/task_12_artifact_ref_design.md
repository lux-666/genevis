# Task 12: Artifact Reference Design

## Overview

Design for dual-reference artifact storage system supporting both content-addressable (hash) and session/attempt-scoped paths.

## Problem

Current implementation uses content-addressable hash refs (SHA256):
- data/artifacts/profiles/<hash>.json
- data/artifacts/payloads/<hash>.json

Phase10 ledger contract requires session/attempt-scoped paths:
- data/artifacts/assembly/<session>/input_profile.json
- data/artifacts/assembly/<session>/parameter_attempt_0_prompt.json
- data/artifacts/assembly/<session>/parameter_attempt_0_response.json

These are complementary requirements, not conflicting ones.

## Design

### Dual-Reference System

Context-aware artifact storage with two reference formats:

#### 1. Hash refs (content-addressable)
- **Purpose**: content verification, deduplication detection, replay from any context
- **Format**: 64-char SHA256 hex string
- **Storage**: data/artifacts/{profiles,payloads}/<hash>.json
- **Use case**: tests, standalone invocations, content integrity checks

#### 2. Session/attempt refs (structured paths)
- **Purpose**: human navigation, ledger replay, attempt ordering
- **Format**: data/artifacts/assembly/<session_id>/<artifact_name>.json
- **Examples**:
  - input_profile.json (one per session, may be reused across attempts)
  - parameter_attempt_0_prompt.json
  - parameter_attempt_0_response.json
  - parameter_attempt_1_prompt.json (retry)
- **Use case**: LangGraph execution, ledger recording, audit trail navigation

### Ledger Integration

Attempt ledger entries store BOTH refs:
```yaml
attempt_id: 0
input_profile_ref: "data/artifacts/assembly/s_abc123/input_profile.json"
input_profile_hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
prompt_payload_ref: "data/artifacts/assembly/s_abc123/parameter_attempt_0_prompt.json"
prompt_payload_hash: "d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2"
llm_response_ref: "data/artifacts/assembly/s_abc123/parameter_attempt_0_response.json"
llm_response_hash: "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
```

### Write Strategy

- **Primary storage**: session/attempt path (when session_id and attempt_id are provided)
- **Fallback storage**: hash path (when session context is absent)
- **Compute hash**: for every artifact regardless of storage path
- **Return both refs**: from write functions

### Load Strategy

- Accept either hash or full path
- Detect format: 64-char hex = hash lookup, otherwise = direct path
- Hash lookup checks hash-based storage first, then scans session directories if needed

### API Changes

```python
# Write functions gain optional session/attempt context:
def write_profile_artifact(
    ref: str,
    profile: dict[str, Any],
    session_id: str | None = None,
    data_dir: Path | None = None,
) -> tuple[str, str]:
    '''Returns (path_ref, hash_ref)'''

def write_payload_artifact(
    ref: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    attempt_id: int | None = None,
    data_dir: Path | None = None,
) -> tuple[str, str]:
    '''Returns (path_ref, hash_ref)'''

# Load functions accept either ref format:
def load_artifact(ref: str, artifact_type: Literal["profile", "payload"]) -> dict[str, Any]:
    '''Loads from hash path or session path'''
```

### Backwards Compatibility

- Existing tests use write_artifacts=False or call write functions without session_id
- These continue to use hash-based storage
- load_by_ref() functions remain unchanged, gain path-based lookup as fallback

### Session Directory Structure

```
data/artifacts/assembly/
  s_20260705_143022_abc123/
    input_profile.json              # shared across attempts
    parameter_attempt_0_prompt.json
    parameter_attempt_0_response.json
    parameter_attempt_1_prompt.json  # retry
    parameter_attempt_1_response.json
  s_20260705_150000_def456/
    ...
```

### Input Profile Reuse

- Input profile is session-scoped, not attempt-scoped
- Same profile may be referenced by multiple attempts in the same session
- Hash ref allows deduplication detection across sessions

### Implementation Notes

- session_id should come from TopLevelState or be generated at session start
- attempt_id comes from parameter_attempts ledger (len(attempts))
- Phase10 integration: pass session_id/attempt_id from recommend_assembly_parameters node
- Tests: continue to work without session context (hash-only mode)

## Migration Path

1. Add optional session_id/attempt_id params to write functions (default None)
2. Update return types to tuple[str, str] for (path_ref, hash_ref)
3. Implement session-aware write logic with fallback to hash storage
4. Update load functions to detect ref format and route accordingly
5. Update build_prompt_payload() to accept and forward session/attempt context
6. Phase10 integration: pass session_id/attempt_id from LangGraph state
7. Update ledger schema to store both refs
8. Tests continue to work in hash-only mode

## Status

- **Design**: Complete
- **Implementation**: In progress
- **Integration**: Pending
