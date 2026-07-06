"""Build prompt payloads for assembly parameter recommendation.

Dual-reference artifact system: hash refs (content-addressable) for deduplication
and session/attempt paths for human navigation. See detailed design in
docs/phases/phase10/task_12_artifact_ref_design.md.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from app.knowledge.packs import load_knowledge_pack
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.domains.assembly.case_selector import evidence_read_type_matches, select_evidence_cases

# Avoid circular import by using local import in function
ParameterPromptTask = Literal[
    "recommend_initial",
    "retry_after_execution_failure",
    "retry_after_review_failure",
]


def build_prompt_payload(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    full_profile: dict[str, Any],
    task: ParameterPromptTask,
    knowledge_pack_id: str = "parameter_recommender",
    write_artifacts: bool = False,
    session_id: str | None = None,
    attempt_id: int | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Build complete LLM prompt payload with references for audit trail.

    Args:
        method_id: Method identifier (e.g., "hifiasm")
        catalog: Parameter catalog with L0/L1/L2 specs
        full_profile: Complete profile from build_full_parameter_profile()
        task: Task type (recommend_initial, retry_after_execution_failure, etc.)
        knowledge_pack_id: Knowledge pack to load (default: "parameter_recommender")
        write_artifacts: Write artifacts to disk for replay (default: False)
        session_id: Optional session ID for session-scoped artifact paths
        attempt_id: Optional attempt ID for attempt-scoped artifact paths (required if session_id provided and write_artifacts=True)
        data_dir: Optional data directory root (default: Path("data"))

    Returns:
        Structured payload with:
        - input_profile_ref: Hash ref (no session context) OR path ref (with session_id)
        - input_profile_hash: Content hash (always present for integrity verification)
        - prompt_payload_ref: Hash ref (no session/attempt context) OR path ref (with both session_id and attempt_id)
        - prompt_payload_hash: Canonical hash (excludes self-ref fields)
        - prompt_profile: Task-scoped profile slice
        - catalog_subset: L1 (readonly) + L2 (adjustable) parameters
        - rules: Domain constraint rules
        - selected_cases: Small evidence subset selected from the knowledge pack
        - case_selection: Selection metadata, including selected_case_ids and warnings
        - skill_instructions: LLM guidance
        - readonly_params: L1 parameter specs (context)
        - adjustable_params: L2 parameter specs (output targets)

    Reference semantics:
        - Without session context: refs are content hashes (SHA256)
        - With session_id only: input_profile_ref is path, prompt_payload_ref remains hash (fallback mode)
        - With session_id + attempt_id: both refs are structured paths for ledger integration
        - Hash fields always present for integrity verification regardless of ref format

    Note on canonical hash:
        The prompt_payload_hash field contains a CANONICAL HASH computed by excluding
        self-referential fields (prompt_payload_ref, prompt_payload_hash) to avoid
        circular dependencies. When write_artifacts=True, the complete payload (WITH
        these fields) is written to disk, so the file content hash will differ from
        the canonical hash. This is intentional for deduplication and verification.
    """
    # 1. Generate input profile reference (for replay)
    input_profile_hash = generate_profile_ref(full_profile)

    # Write full_profile to disk if requested
    if write_artifacts:
        profile_path_ref, profile_hash_ref = write_profile_artifact(
            input_profile_hash, full_profile, session_id=session_id, data_dir=data_dir
        )
        # Use path_ref in payload when session context is provided
        input_profile_ref = profile_path_ref if session_id else profile_hash_ref
    else:
        input_profile_ref = input_profile_hash
        profile_hash_ref = input_profile_hash

    # 2. Slice profile by task (reuse existing logic)
    # Import here to avoid circular dependency
    from app.stages.assembly.parameter_profile import build_prompt_profile

    prompt_profile = build_prompt_profile(full_profile, task)

    # 3. Load knowledge pack
    knowledge_pack = load_knowledge_pack(knowledge_pack_id)

    # 4. Filter catalog to L1 (readonly) + L2 (adjustable)
    readonly_params = {}
    adjustable_params = {}
    for key, spec in catalog.parameters.items():
        if spec.level == 1:
            readonly_params[key] = spec.model_dump()
        elif spec.level == 2:
            adjustable_params[key] = spec.model_dump()

    catalog_subset = {
        "readonly_params": readonly_params,
        "adjustable_params": adjustable_params,
    }

    selected_cases, case_selection = select_evidence_cases(
        knowledge_pack["cases"],
        method_id=method_id,
        task=task,
        prompt_profile=prompt_profile,
        strategy=knowledge_pack.get("strategies"),
    )

    # Defensive validation: verify all selected cases match profile read_type
    profile_read_type = prompt_profile.get("assets", {}).get("read_type")
    if profile_read_type:
        for case in selected_cases:
            case_read_type = (case.get("features") or {}).get("read_type")
            if case_read_type and not evidence_read_type_matches(profile_read_type, case_read_type):
                raise ValueError(
                    f"Case selector failed: case {case.get('case_id')} has read_type={case_read_type} "
                    f"but profile has read_type={profile_read_type}"
                )

    # 5. Build complete payload
    payload = {
        "method_id": method_id,
        "task": task,
        "input_profile_ref": input_profile_ref,
        "input_profile_hash": profile_hash_ref,
        "prompt_profile": prompt_profile,
        "catalog_subset": catalog_subset,
        "rules": knowledge_pack["rules"],
        "selected_cases": selected_cases,
        "case_selection": case_selection,
        "skill_instructions": knowledge_pack["skill"],
        "readonly_params": readonly_params,
        "adjustable_params": adjustable_params,
    }

    # 6. Generate prompt payload reference (for audit)
    # CANONICAL HASH SEMANTICS: We compute the hash by excluding self-referential
    # fields (prompt_payload_ref, prompt_payload_hash) to avoid circular dependencies.
    # This means prompt_payload_hash is NOT a hash of the complete file content,
    # but rather a "canonical hash" of the payload's semantic content for deduplication
    # and verification. Future ledger validation code must exclude these fields before
    # comparing hashes.
    payload_for_hash = {k: v for k, v in payload.items() if k not in ("prompt_payload_ref", "prompt_payload_hash")}
    payload_hash = generate_payload_ref(payload_for_hash)

    # Write prompt_payload to disk if requested
    if write_artifacts:
        # First write without the ref fields to compute canonical hash
        payload_path_ref, payload_hash_ref = write_payload_artifact(
            payload_hash, payload, session_id=session_id, attempt_id=attempt_id, data_dir=data_dir
        )
        # Set both ref fields in the payload
        payload["prompt_payload_ref"] = payload_path_ref if (session_id and attempt_id is not None) else payload_hash_ref
        payload["prompt_payload_hash"] = payload_hash_ref
        # Write again with the ref fields included for completeness.
        # NOTE: This second write ensures the stored artifact contains all metadata,
        # but the hash field represents the canonical content (without self-refs).
        # This intentional design avoids circular dependencies while preserving
        # complete audit trails on disk.
        write_payload_artifact(
            payload_hash, payload, session_id=session_id, attempt_id=attempt_id, data_dir=data_dir
        )
    else:
        payload["prompt_payload_ref"] = payload_hash
        payload["prompt_payload_hash"] = payload_hash

    return payload


def generate_profile_ref(full_profile: dict[str, Any]) -> str:
    """Generate stable reference ID for a full profile (for replay).

    Uses SHA256 hash of canonical JSON representation.

    Args:
        full_profile: Complete profile dict

    Returns:
        Hex string hash (64 chars)
    """
    canonical = json.dumps(full_profile, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_payload_ref(prompt_payload: dict[str, Any]) -> str:
    """Generate stable canonical reference ID for prompt payload (for audit).

    IMPORTANT: This computes a CANONICAL HASH for deduplication and verification,
    NOT a file content hash. The hash is computed by excluding self-referential
    fields (prompt_payload_ref, prompt_payload_hash) to avoid circular dependencies.

    When the payload is written to disk, it includes these ref fields for completeness,
    meaning the file content hash will differ from this canonical hash. This is
    intentional design to enable:
    - Content-addressable deduplication (same semantic content = same hash)
    - Verification without needing to strip fields first in simple comparisons
    - Complete audit trails with full metadata preserved on disk

    Future ledger validation must exclude prompt_payload_ref and prompt_payload_hash
    fields before computing hashes for comparison against this canonical hash.

    Args:
        prompt_payload: Prompt payload dict (with self-referential fields excluded)

    Returns:
        Hex string hash (64 chars) - canonical hash of semantic content
    """
    canonical = json.dumps(prompt_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_profile_artifact(
    ref: str,
    profile: dict[str, Any],
    session_id: str | None = None,
    data_dir: Path | None = None,
) -> tuple[str, str]:
    """Write profile artifact to disk for replay.

    Args:
        ref: Profile reference ID (SHA256 hash)
        profile: Full profile dict to write
        session_id: Optional session ID for session-scoped storage
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Tuple of (path_ref, hash_ref) where:
        - path_ref: Actual file path written (relative to repo root)
        - hash_ref: SHA256 hash for content verification
    """
    if data_dir is None:
        data_dir = Path("data")

    # Determine storage path based on session context
    if session_id:
        # Session-scoped path: data/artifacts/assembly/<session_id>/input_profile.json
        profile_dir = data_dir / "artifacts" / "assembly" / session_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / "input_profile.json"
        path_ref = f"data/artifacts/assembly/{session_id}/input_profile.json"
    else:
        # Hash-based path: data/artifacts/profiles/<hash>.json
        profile_dir = data_dir / "artifacts" / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / f"{ref}.json"
        path_ref = f"data/artifacts/profiles/{ref}.json"

    # Write artifact
    with profile_path.open("w") as f:
        json.dump(profile, f, indent=2)

    return (path_ref, ref)


def write_payload_artifact(
    ref: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    attempt_id: int | None = None,
    data_dir: Path | None = None,
) -> tuple[str, str]:
    """Write prompt payload artifact to disk for audit.

    Args:
        ref: Payload reference ID (SHA256 hash)
        payload: Complete prompt payload dict to write
        session_id: Optional session ID for session-scoped storage
        attempt_id: Optional attempt ID for attempt-scoped naming
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Tuple of (path_ref, hash_ref) where:
        - path_ref: Actual file path written (relative to repo root)
        - hash_ref: SHA256 hash for content verification
    """
    if data_dir is None:
        data_dir = Path("data")

    # Determine storage path based on session context
    if session_id and attempt_id is not None:
        # Attempt-scoped path: data/artifacts/assembly/<session_id>/parameter_attempt_<N>_prompt.json
        payload_dir = data_dir / "artifacts" / "assembly" / session_id
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / f"parameter_attempt_{attempt_id}_prompt.json"
        path_ref = f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}_prompt.json"
    else:
        # Hash-based path: data/artifacts/payloads/<hash>.json
        payload_dir = data_dir / "artifacts" / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / f"{ref}.json"
        path_ref = f"data/artifacts/payloads/{ref}.json"

    # Write artifact
    with payload_path.open("w") as f:
        json.dump(payload, f, indent=2)

    return (path_ref, ref)


def load_profile_by_ref(ref: str, data_dir: Path | None = None) -> dict[str, Any]:
    """Load profile artifact from disk by reference ID.

    Args:
        ref: Profile reference (SHA256 hash or path like data/artifacts/assembly/<session>/input_profile.json)
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Full profile dict

    Raises:
        FileNotFoundError: If profile artifact does not exist
    """
    if data_dir is None:
        data_dir = Path("data")

    # Detect ref type: hash (64 hex chars) vs path
    if len(ref) == 64 and all(c in "0123456789abcdef" for c in ref):
        # Hash ref: use content-addressable storage
        profile_path = data_dir / "artifacts" / "profiles" / f"{ref}.json"
    else:
        # Path ref: strip "data/" prefix if present and resolve relative to data_dir
        ref_path = ref.removeprefix("data/")
        profile_path = data_dir / ref_path

    with profile_path.open("r") as f:
        return json.load(f)


def load_payload_by_ref(ref: str, data_dir: Path | None = None) -> dict[str, Any]:
    """Load prompt payload artifact from disk by reference ID.

    Args:
        ref: Payload reference (SHA256 hash or path like data/artifacts/assembly/<session>/parameter_attempt_<N>_prompt.json)
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Complete prompt payload dict

    Raises:
        FileNotFoundError: If payload artifact does not exist
    """
    if data_dir is None:
        data_dir = Path("data")

    # Detect ref type: hash (64 hex chars) vs path
    if len(ref) == 64 and all(c in "0123456789abcdef" for c in ref):
        # Hash ref: use content-addressable storage
        payload_path = data_dir / "artifacts" / "payloads" / f"{ref}.json"
    else:
        # Path ref: strip "data/" prefix if present and resolve relative to data_dir
        ref_path = ref.removeprefix("data/")
        payload_path = data_dir / ref_path

    with payload_path.open("r") as f:
        return json.load(f)
