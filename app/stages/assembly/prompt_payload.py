"""Build prompt payloads for assembly parameter recommendation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from app.knowledge.packs import load_knowledge_pack
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_PROMPT_PAYLOAD,
    ArtifactRef,
)
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
    knowledge_root: Path | None = None,
) -> dict[str, Any]:
    """Build complete LLM prompt payload with references for audit trail.

    Args:
        method_id: Method identifier (e.g., "hifiasm")
        catalog: Parameter catalog with L0/L1/L2 specs
        full_profile: Complete profile from build_full_parameter_profile()
        task: Task type (recommend_initial, retry_after_execution_failure, etc.)
        knowledge_pack_id: Knowledge pack to load (default: "parameter_recommender")
        write_artifacts: Write session/attempt artifacts to disk for replay.
        session_id: Session ID for session-scoped artifact paths.
        attempt_id: Attempt ID for attempt-scoped artifact paths.
        data_dir: Optional data directory root (default: Path("data"))

    Returns:
        Structured payload with:
        - input_profile_ref: Path ref when artifacts are written, otherwise hash
        - input_profile_hash: Content hash (always present for integrity verification)
        - prompt_payload_ref: Path ref when artifacts are written, otherwise hash
        - prompt_payload_hash: Canonical hash (excludes self-ref fields)
        - prompt_profile: Task-scoped profile slice
        - catalog_subset: L1 (readonly) + L2 (adjustable) parameters
        - rules: Domain constraint rules
        - selected_cases: Small evidence subset selected from the knowledge pack
        - case_selection: Selection metadata, including selected_case_ids and warnings
        - skill_instructions: LLM guidance
        - readonly_params: L1 parameter specs (context)
        - adjustable_params: L2 parameter specs (output targets)

    Hash fields are integrity fields. The running graph must call this with
    write_artifacts=True, session_id, and attempt_id so refs are replayable paths.
    """
    if write_artifacts and (not session_id or attempt_id is None):
        raise ValueError("session_id and attempt_id are required when write_artifacts=True")

    # 1. Generate input profile reference (for replay)
    input_profile_hash = generate_profile_ref(full_profile)

    # Write full_profile to disk if requested
    if write_artifacts:
        profile_ref = write_profile_artifact(
            profile=full_profile,
            session_id=str(session_id),
            attempt_id=int(attempt_id),
            data_dir=data_dir,
        )
        input_profile_ref = profile_ref.path_ref
        profile_hash_ref = profile_ref.sha256
    else:
        input_profile_ref = input_profile_hash
        profile_hash_ref = input_profile_hash

    # 2. Slice profile by task (reuse existing logic)
    # Import here to avoid circular dependency
    from app.stages.assembly.parameter_profile import build_prompt_profile

    prompt_profile = build_prompt_profile(full_profile, task)

    # 3. Load knowledge pack
    knowledge_pack = load_knowledge_pack(knowledge_pack_id, root=knowledge_root)

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
        source_root=knowledge_pack.get("pack_root"),
    )

    uses_hifiasm_v2 = case_selection.get("retrieval_backend") == "hifiasm_numeric_v2"
    evidence_cases = selected_cases if uses_hifiasm_v2 else []
    legacy_selected_cases = [] if uses_hifiasm_v2 else selected_cases

    # Defensive validation: verify all selected cases match profile read_type
    profile_read_type = prompt_profile.get("assets", {}).get("read_type")
    if profile_read_type:
        for case in evidence_cases if uses_hifiasm_v2 else legacy_selected_cases:
            case_read_type = (
                (case.get("applicability") or {}).get("read_type")
                if uses_hifiasm_v2
                else (case.get("features") or {}).get("read_type")
            )
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
        "selected_cases": legacy_selected_cases,
        "case_selection": case_selection,
        "case_retrieval": case_selection if uses_hifiasm_v2 else None,
        "evidence_cases": evidence_cases,
        "recommendation_policy": {
            "parameter_levels": {
                "L0": "fixed by executor/run configuration; never recommend or modify",
                "L1": "readonly facts; use only as recommendation context",
                "L2": "the only parameters allowed in params",
            },
            "case_evidence_required": False,
            "allowed_evidence_bases": [
                "case",
                "profile_fact",
                "skill_heuristic",
                "retry_failure",
                "catalog_default",
            ],
            "case_citation_rule": (
                "case_ids and span_ids are required only when evidence basis is case"
            ),
            "no_case_behavior": (
                "continue with readonly L1 facts, SKILL heuristics, retry context, and catalog defaults; "
                "prefer conservative L2 changes"
            ),
        },
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
        payload["prompt_payload_ref"] = (
            f"data/artifacts/assembly/{session_id}/parameter_attempt_{int(attempt_id)}/prompt_payload.json"
        )
        payload["prompt_payload_hash"] = payload_hash
        write_payload_artifact(
            payload=payload,
            session_id=str(session_id),
            attempt_id=int(attempt_id),
            data_dir=data_dir,
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
    *,
    profile: dict[str, Any],
    session_id: str,
    attempt_id: int,
    data_dir: Path | None = None,
) -> ArtifactRef:
    """Write profile artifact to disk for replay.

    Args:
        profile: Full profile dict to write
        session_id: Session ID for session-scoped storage
        attempt_id: Attempt ID for immutable attempt-scoped storage
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Path ref plus SHA256 integrity field.
    """
    if data_dir is None:
        data_dir = Path("data")

    profile_dir = data_dir / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / "input_profile.json"
    path_ref = f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/input_profile.json"

    with profile_path.open("w") as f:
        json.dump(profile, f, indent=2)

    # Use actual file hash for integrity verification
    from app.schemas.artifacts import file_sha256
    return ArtifactRef(path_ref=path_ref, sha256=file_sha256(profile_path), artifact_type=ASSEMBLY_PARAMETER_INPUT_PROFILE)


def write_payload_artifact(
    *,
    payload: dict[str, Any],
    session_id: str,
    attempt_id: int,
    data_dir: Path | None = None,
) -> ArtifactRef:
    """Write prompt payload artifact to disk for audit.

    Args:
        payload: Complete prompt payload dict to write
        session_id: Session ID for session-scoped storage
        attempt_id: Attempt ID for attempt-scoped naming
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Path ref plus SHA256 integrity field.
    """
    if data_dir is None:
        data_dir = Path("data")

    payload_dir = data_dir / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = payload_dir / "prompt_payload.json"
    path_ref = f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/prompt_payload.json"

    with payload_path.open("w") as f:
        json.dump(payload, f, indent=2)

    # Use actual file hash for integrity verification
    from app.schemas.artifacts import file_sha256
    return ArtifactRef(path_ref=path_ref, sha256=file_sha256(payload_path), artifact_type=ASSEMBLY_PARAMETER_PROMPT_PAYLOAD)


def load_profile_by_ref(ref: str, data_dir: Path | None = None) -> dict[str, Any]:
    """Load profile artifact from disk by reference ID.

    Args:
        ref: Profile reference path like data/artifacts/assembly/<session>/parameter_attempt_<N>/input_profile.json
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Full profile dict

    Raises:
        FileNotFoundError: If profile artifact does not exist
    """
    if data_dir is None:
        data_dir = Path("data")

    ref_path = ref.removeprefix("data/")
    profile_path = data_dir / ref_path

    with profile_path.open("r") as f:
        return json.load(f)


def load_payload_by_ref(ref: str, data_dir: Path | None = None) -> dict[str, Any]:
    """Load prompt payload artifact from disk by reference ID.

    Args:
        ref: Payload reference path like data/artifacts/assembly/<session>/parameter_attempt_<N>/prompt_payload.json
        data_dir: Optional data root directory (default: Path("data"))

    Returns:
        Complete prompt payload dict

    Raises:
        FileNotFoundError: If payload artifact does not exist
    """
    if data_dir is None:
        data_dir = Path("data")

    ref_path = ref.removeprefix("data/")
    payload_path = data_dir / ref_path

    with payload_path.open("r") as f:
        return json.load(f)
