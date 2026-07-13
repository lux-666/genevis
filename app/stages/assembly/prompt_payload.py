"""Build prompt payloads for assembly parameter recommendation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.knowledge.packs import load_knowledge_pack
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD,
    ASSEMBLY_PARAMETER_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_PROMPT_PAYLOAD,
    ASSEMBLY_PARAMETER_RETRIEVAL_QUERY,
    ASSEMBLY_PARAMETER_RETRIEVAL_TRACE,
    ArtifactRef,
    file_sha256,
)
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.domains.assembly.case_selector import select_evidence_cases
from app.domains.assembly.methods_evidence import (
    HIFIASM_EFFECT_AREAS,
    HIFIASM_METHODS_EVIDENCE_CASE_LIMIT,
    _attest_loaded_hifiasm_initial_prompt_snapshot,
    _seal_attested_hifiasm_methods_prompt_payload,
    build_hifiasm_evidence_payload,
    rehydrate_hifiasm_methods_retry_prompt_payload,
    sanitize_hifiasm_methods_prompt_payload,
    uses_hifiasm_methods_contract,
)
from app.domains.assembly.numeric_case_retriever import (
    _RANKED_SELECTION_FIELD,
    _project_hifiasm_attested_selection_audit,
)
from app.stages.assembly.parameter_profile import ParameterPromptTask, build_prompt_profile

_SECURE_ARTIFACT_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.link, os.unlink)
    )
    and os.link in os.supports_follow_symlinks
)

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
    initial_attempt_snapshot: dict[str, Any] | None = None,
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
    artifact_plan: list[tuple[bytes, ArtifactRef, str]] = []
    session_component: str | None = None
    attempt_number: int | None = None
    if write_artifacts:
        if session_id is None or attempt_id is None:
            raise ValueError("session_id and attempt_id are required when write_artifacts=True")
        session_component = _validate_session_id_component(session_id)
        attempt_number = _validate_attempt_id(attempt_id)

    # 1. Generate input profile reference (for replay)
    input_profile_hash = generate_profile_ref(full_profile)

    if write_artifacts:
        profile_serialized, profile_ref = _prepare_json_artifact(
            value=full_profile,
            session_id=session_component,
            attempt_id=attempt_number,
            filename="input_profile.json",
            artifact_type=ASSEMBLY_PARAMETER_INPUT_PROFILE,
        )
        artifact_plan.append((profile_serialized, profile_ref, "input_profile.json"))
        input_profile_ref = profile_ref.path_ref
        profile_hash_ref = profile_ref.sha256
    else:
        input_profile_ref = input_profile_hash
        profile_hash_ref = input_profile_hash

    # 2. Slice profile by task (reuse existing logic)
    prompt_profile = build_prompt_profile(full_profile, task)

    initial_prompt = (
        _load_initial_prompt_snapshot(
            initial_attempt_snapshot or {},
            data_dir=data_dir,
            session_id=session_id,
        )
        if task != "recommend_initial"
        else {}
    )
    # 3. Load policy only for attempt 0. Retries inherit the exact prompt snapshot.
    if task == "recommend_initial":
        knowledge_pack = load_knowledge_pack(knowledge_pack_id, root=knowledge_root)
    else:
        knowledge_pack = {
            "cases": [],
            "rules": dict(initial_prompt.get("rules") or {}),
            "skill": str(initial_prompt.get("skill_instructions") or ""),
        }

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

    if task == "recommend_initial":
        selected_cases, case_selection = select_evidence_cases(
            knowledge_pack["cases"],
            method_id=method_id,
            task=task,
            prompt_profile=prompt_profile,
            strategy=knowledge_pack.get("strategies"),
            source_root=knowledge_pack.get("pack_root"),
            query_id=(
                f"{session_id}:parameter_attempt_{attempt_id}"
                if session_id is not None and attempt_id is not None
                else None
            ),
        )
    else:
        # Retry is a transition inside one run.  It must inherit attempt 0's
        # exact evidence selection and must never invoke a selector/retriever.
        selected_cases = list(
            initial_prompt.get("evidence_cases")
            or initial_prompt.get("selected_cases")
            or []
        )
        inherited_selection = initial_prompt.get("case_retrieval") or initial_prompt.get("case_selection")
        case_selection = dict(inherited_selection or {})
        if not case_selection:
            case_selection = {
                "retrieval_backend": None,
                "selected_case_ids": list((initial_attempt_snapshot or {}).get("selected_case_ids") or []),
                "candidate_case_ids": [],
                "selected_count": len((initial_attempt_snapshot or {}).get("selected_case_ids") or []),
                "eligible_case_count": None,
                "total_cases": None,
                "evidence_available": False,
                "case_evidence_required": False,
                "warning": "retry_inherits_attempt_0_without_retrieval",
            }

    uses_hifiasm_evidence = case_selection.get("retrieval_backend") in {
        "hifiasm_numeric",
    }
    retrieval_query_payload = None
    retrieval_trace_payload = None
    if uses_hifiasm_evidence and task == "recommend_initial":
        (
            case_selection,
            retrieval_query_payload,
            retrieval_trace_payload,
        ) = _materialize_retrieval_artifacts(case_selection)
        if write_artifacts:
            query_serialized, query_artifact = _prepare_json_artifact(
                value=retrieval_query_payload,
                session_id=session_component,
                attempt_id=attempt_number,
                filename="retrieval_query.json",
                artifact_type=ASSEMBLY_PARAMETER_RETRIEVAL_QUERY,
            )
            trace_serialized, trace_artifact = _prepare_json_artifact(
                value=retrieval_trace_payload,
                session_id=session_component,
                attempt_id=attempt_number,
                filename="retrieval_trace.json",
                artifact_type=ASSEMBLY_PARAMETER_RETRIEVAL_TRACE,
            )
            artifact_plan.extend(
                [
                    (query_serialized, query_artifact, "retrieval_query.json"),
                    (trace_serialized, trace_artifact, "retrieval_trace.json"),
                ]
            )
            query_ref, query_hash = query_artifact.path_ref, query_artifact.sha256
            trace_ref, trace_hash = trace_artifact.path_ref, trace_artifact.sha256
        else:
            query_ref = query_hash = _canonical_json_hash(retrieval_query_payload)
            trace_ref = trace_hash = _canonical_json_hash(retrieval_trace_payload)
        case_selection.update(
            {
                "query_ref": query_ref,
                "query_hash": query_hash,
                "trace_ref": trace_ref,
                "trace_hash": trace_hash,
            }
        )
    evidence_cases = (
        selected_cases[:HIFIASM_METHODS_EVIDENCE_CASE_LIMIT]
        if uses_hifiasm_evidence
        else []
    )
    legacy_selected_cases = [] if uses_hifiasm_evidence else selected_cases
    evidence_payload_ref = None
    evidence_payload_hash = None
    methods_evidence = None
    if uses_hifiasm_evidence:
        case_selection = {
            **case_selection,
            "evidence_case_ids": [case["case_id"] for case in evidence_cases],
        }
        if task == "recommend_initial":
            evidence_payload = build_hifiasm_evidence_payload(
                evidence_cases=evidence_cases,
                case_retrieval=case_selection,
                allowed_parameter_ids=set(adjustable_params),
            )
            evidence_payload_hash = _canonical_json_hash(evidence_payload)
            evidence_payload_ref = evidence_payload_hash
            if write_artifacts:
                evidence_serialized, evidence_artifact = _prepare_json_artifact(
                    value=evidence_payload,
                    session_id=session_component,
                    attempt_id=attempt_number,
                    filename="evidence_payload.json",
                    artifact_type=ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD,
                )
                artifact_plan.append(
                    (evidence_serialized, evidence_artifact, "evidence_payload.json")
                )
                evidence_payload_ref = evidence_artifact.path_ref
                evidence_payload_hash = evidence_artifact.sha256
        else:
            evidence_payload_ref = initial_prompt.get("evidence_payload_ref")
            evidence_payload_hash = initial_prompt.get("evidence_payload_hash")
            if not evidence_payload_ref or not evidence_payload_hash:
                raise ValueError("curated retry is missing attempt 0 evidence payload binding")
        case_selection = {
            **case_selection,
            "evidence_payload_ref": evidence_payload_ref,
            "evidence_payload_hash": evidence_payload_hash,
        }
        methods_evidence = dict(initial_prompt.get("methods_evidence") or {}) if task != "recommend_initial" else {
            "effect_areas": list(HIFIASM_EFFECT_AREAS),
            "max_evidence_cases": HIFIASM_METHODS_EVIDENCE_CASE_LIMIT,
            "response_contract": "case_assessments_and_recommendation",
            "assessment_rule": "assess only numeric-shortlist cases and cite only visible spans",
            "recommendation_rule": (
                "return L2 decisions with set, keep_default, or omit actions and at least one evidence basis"
            ),
        }

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
        "case_retrieval": case_selection if uses_hifiasm_evidence else None,
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
    if uses_hifiasm_evidence:
        payload.update(
            {
                "evidence_payload_ref": evidence_payload_ref,
                "evidence_payload_hash": evidence_payload_hash,
                "methods_evidence": methods_evidence,
            }
        )
        if task != "recommend_initial":
            payload["retry_evidence"] = _build_retry_evidence(
                prompt_profile,
                task=task,
            )

    # 6. Close the structured hifiasm Methods boundary before hashing or publishing.
    # Numeric retrieval is shared by hifiasm-family methods, but the response
    # contract remains an independent method-level concern.
    uses_methods_contract = uses_hifiasm_methods_contract(payload)
    if uses_methods_contract:
        authoritative_catalog_snapshot = {
            "catalog_subset": deepcopy(catalog_subset),
            "readonly_params": deepcopy(readonly_params),
            "adjustable_params": deepcopy(adjustable_params),
        }
        if task == "recommend_initial":
            payload = sanitize_hifiasm_methods_prompt_payload(
                payload,
                authoritative_catalog_snapshot=authoritative_catalog_snapshot,
            )
        else:
            payload = rehydrate_hifiasm_methods_retry_prompt_payload(
                initial_prompt,
                payload,
                authoritative_catalog_snapshot=authoritative_catalog_snapshot,
            )

    # 7. Generate prompt payload reference (for audit)
    # CANONICAL HASH SEMANTICS: We compute the hash by excluding self-referential
    # fields (prompt_payload_ref, prompt_payload_hash) to avoid circular dependencies.
    # This means prompt_payload_hash is NOT a hash of the complete file content,
    # but rather a "canonical hash" of the payload's semantic content for deduplication
    # and verification. Future ledger validation code must exclude these fields before
    # comparing hashes.
    payload_for_hash = {k: v for k, v in payload.items() if k not in ("prompt_payload_ref", "prompt_payload_hash")}
    payload_hash = generate_payload_ref(payload_for_hash)

    # Bind the final ref/hash before preparing any publication. Task 4's registry
    # proof covers these exact final values even though the canonical hash excludes
    # the two self-referential fields.
    if write_artifacts:
        prompt_payload_ref = _artifact_path_ref(
            session_component,
            attempt_number,
            "prompt_payload.json",
        )
    else:
        prompt_payload_ref = payload_hash

    if uses_methods_contract:
        payload = _seal_attested_hifiasm_methods_prompt_payload(
            payload,
            prompt_payload_ref=prompt_payload_ref,
            prompt_payload_hash=payload_hash,
        )
    else:
        payload["prompt_payload_ref"] = prompt_payload_ref
        payload["prompt_payload_hash"] = payload_hash

    if write_artifacts:
        prompt_serialized, prompt_artifact = _prepare_json_artifact(
            value=payload,
            session_id=session_component,
            attempt_id=attempt_number,
            filename="prompt_payload.json",
            artifact_type=ASSEMBLY_PARAMETER_PROMPT_PAYLOAD,
        )
        artifact_plan.append((prompt_serialized, prompt_artifact, "prompt_payload.json"))
        for serialized, artifact_ref, filename in artifact_plan:
            _publish_prepared_json_artifact(
                serialized=serialized,
                artifact_ref=artifact_ref,
                session_component=session_component,
                attempt_number=attempt_number,
                filename=filename,
                data_dir=data_dir,
            )

    return payload


def generate_profile_ref(full_profile: dict[str, Any]) -> str:
    """Generate stable reference ID for a full profile (for replay).

    Uses SHA256 hash of canonical JSON representation.

    Args:
        full_profile: Complete profile dict

    Returns:
        Hex string hash (64 chars)
    """
    return _canonical_json_hash(full_profile)


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
    return _canonical_json_hash(prompt_payload)


def _materialize_retrieval_artifacts(
    selection: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate Task 3 and return in-memory retrieval audit payloads only."""

    attested = _project_hifiasm_attested_selection_audit(selection)
    query = dict(attested["query"] or {})
    trace_entries = list(attested["trace"] or [])
    compact = {
        field: attested[field]
        for field in (
            "retrieval_backend",
            "selected_case_ids",
            "candidate_case_ids",
            "selected_count",
            "eligible_case_count",
            "total_cases",
            "evidence_available",
            "case_evidence_required",
            "warning",
        )
    }
    compact[_RANKED_SELECTION_FIELD] = selection[_RANKED_SELECTION_FIELD]
    if "hifiasm_numeric_gate" in selection:
        compact["hifiasm_numeric_gate"] = dict(selection["hifiasm_numeric_gate"])
    trace_payload = {
        "retrieval_backend": attested["retrieval_backend"],
        "query_id": query.get("query_id"),
        "selected_case_ids": list(attested["selected_case_ids"] or []),
        "eligible_case_count": attested["eligible_case_count"],
        "warning": attested["warning"],
        "trace": trace_entries,
    }

    compact["query_id"] = query.get("query_id")
    return compact, query, trace_payload


def _build_retry_evidence(profile: dict[str, Any], *, task: str) -> dict[str, Any]:
    previous = dict(profile.get("previous_attempt") or {})
    review = dict(profile.get("assembly_review") or {})
    signals: list[dict[str, Any]] = []
    for signal_id, path, value in (
        ("execution.status", "previous_attempt.execution_status", previous.get("execution_status")),
        ("execution.status_detail", "previous_attempt.execution_status_detail", previous.get("execution_status_detail")),
        ("execution.exit_code", "previous_attempt.exit_code", previous.get("exit_code")),
        ("execution.stderr_ref", "previous_attempt.stderr_ref", previous.get("stderr_ref")),
        ("execution.stdout_ref", "previous_attempt.stdout_ref", previous.get("stdout_ref")),
        ("review.failure_mode", "assembly_review.failure_mode", review.get("failure_mode")),
    ):
        if value is not None:
            signals.append({"signal_id": signal_id, "path": path, "value": value})
    for metric, value in review.items():
        if metric == "failure_mode" or value is None or isinstance(value, (dict, list)):
            continue
        signals.append(
            {
                "signal_id": f"review.{metric}",
                "path": f"assembly_review.{metric}",
                "value": value,
            }
        )
    return {
        "task": task,
        "signal_ids": [signal["signal_id"] for signal in signals],
        "signal_paths": [signal["path"] for signal in signals],
        "signals": signals,
    }


def _load_initial_prompt_snapshot(
    attempt: dict[str, Any], *, data_dir: Path | None, session_id: str | None
) -> dict[str, Any]:
    ref = attempt.get("prompt_payload_ref") or {}
    path_ref = ref.get("path_ref") if isinstance(ref, dict) else None
    if not path_ref:
        return {}
    if session_id is None:
        raise ValueError("attempt 0 prompt replay requires the current session ID")
    session_component = _validate_session_id_component(session_id)
    expected_path_ref = _artifact_path_ref(
        session_component,
        0,
        "prompt_payload.json",
    )
    if path_ref != expected_path_ref:
        raise ValueError("attempt 0 prompt artifact is outside the current session attempt 0 path")
    expected_file_hash = ref.get("sha256")
    if not isinstance(expected_file_hash, str) or not expected_file_hash:
        raise ValueError("attempt 0 prompt artifact is missing its file hash")
    path = Path(str(path_ref))
    if not path.is_absolute() and str(path_ref).startswith("data/"):
        path = (data_dir or Path("data")) / str(path_ref).removeprefix("data/")
    try:
        if file_sha256(path) != expected_file_hash:
            raise ValueError("attempt 0 prompt artifact hash mismatch")
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("attempt 0 prompt artifact is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("attempt 0 prompt artifact must contain a JSON object")
    if value.get("prompt_payload_ref") != path_ref:
        raise ValueError("attempt 0 prompt artifact path binding mismatch")
    if "hifiasm_methods_prompt_schema_version" in value:
        return _attest_loaded_hifiasm_initial_prompt_snapshot(value)
    return value


def _canonical_json_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
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
    return _write_json_artifact(
        value=profile,
        session_id=session_id,
        attempt_id=attempt_id,
        filename="input_profile.json",
        artifact_type=ASSEMBLY_PARAMETER_INPUT_PROFILE,
        data_dir=data_dir,
    )


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
    return _write_json_artifact(
        value=payload,
        session_id=session_id,
        attempt_id=attempt_id,
        filename="prompt_payload.json",
        artifact_type=ASSEMBLY_PARAMETER_PROMPT_PAYLOAD,
        data_dir=data_dir,
    )


def write_evidence_payload_artifact(
    *,
    payload: dict[str, Any],
    session_id: str,
    attempt_id: int,
    data_dir: Path | None = None,
) -> ArtifactRef:
    """Write the bounded hifiasm Methods evidence payload for replay."""

    return _write_json_artifact(
        value=payload,
        session_id=session_id,
        attempt_id=attempt_id,
        filename="evidence_payload.json",
        artifact_type=ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD,
        data_dir=data_dir,
    )


def _artifact_path_ref(
    session_component: str,
    attempt_number: int,
    filename: str,
) -> str:
    return (
        f"data/artifacts/assembly/{session_component}/"
        f"parameter_attempt_{attempt_number}/{filename}"
    )


def _prepare_json_artifact(
    *,
    value: Any,
    session_id: str,
    attempt_id: int,
    filename: str,
    artifact_type: str,
) -> tuple[bytes, ArtifactRef]:
    """Serialize and validate an immutable artifact entirely in memory."""

    session_component = _validate_session_id_component(session_id)
    attempt_number = _validate_attempt_id(attempt_id)
    serialized = json.dumps(value, indent=2).encode("utf-8")
    artifact_ref = ArtifactRef(
        path_ref=_artifact_path_ref(session_component, attempt_number, filename),
        sha256=hashlib.sha256(serialized).hexdigest(),
        artifact_type=artifact_type,
    )
    return serialized, artifact_ref


def _write_json_artifact(
    *,
    value: Any,
    session_id: str,
    attempt_id: int,
    filename: str,
    artifact_type: str,
    data_dir: Path | None = None,
) -> ArtifactRef:
    """Prepare then publish one immutable attempt-scoped JSON artifact."""

    session_component = _validate_session_id_component(session_id)
    attempt_number = _validate_attempt_id(attempt_id)
    serialized, artifact_ref = _prepare_json_artifact(
        value=value,
        session_id=session_component,
        attempt_id=attempt_number,
        filename=filename,
        artifact_type=artifact_type,
    )
    _publish_prepared_json_artifact(
        serialized=serialized,
        artifact_ref=artifact_ref,
        session_component=session_component,
        attempt_number=attempt_number,
        filename=filename,
        data_dir=data_dir,
    )
    return artifact_ref


def _publish_prepared_json_artifact(
    *,
    serialized: bytes,
    artifact_ref: ArtifactRef,
    session_component: str,
    attempt_number: int,
    filename: str,
    data_dir: Path | None,
) -> None:
    """Publish bytes whose serialization, hash, and ArtifactRef already succeeded."""

    # No post-publication read or validation is needed: the ref was constructed
    # from these exact bytes before any artifact in the attempt plan was published.
    _ = artifact_ref
    _require_secure_artifact_writer_support()
    attempt_component = f"parameter_attempt_{attempt_number}"
    directory_fds: list[int] = []
    namespace_components: list[tuple[str, str]] = []
    attempt_fd: int | None = None
    temp_name: str | None = None
    published_new = False
    try:
        directory_fds, namespace_components = _open_attempt_directory_fds(
            data_dir or Path("data"),
            session_component=session_component,
            attempt_component=attempt_component,
        )
        attempt_fd = directory_fds[-1]
        temp_name, handle = _open_unique_temp_file(attempt_fd, filename)
        with handle:
            _write_and_sync_json(handle, serialized)
            _verify_artifact_namespace(directory_fds, namespace_components)
            try:
                os.link(
                    temp_name,
                    filename,
                    src_dir_fd=attempt_fd,
                    dst_dir_fd=attempt_fd,
                    follow_symlinks=False,
                )
                published_new = True
            except FileExistsError:
                if not _existing_regular_file_matches(
                    attempt_fd,
                    filename,
                    serialized,
                ):
                    raise FileExistsError(
                        errno.EEXIST,
                        "immutable attempt artifact already exists with different content",
                        filename,
                    ) from None
            _fsync_directory(attempt_fd)
            os.unlink(temp_name, dir_fd=attempt_fd)
            _fsync_directory(attempt_fd)
            temp_name = None
            _verify_artifact_namespace(directory_fds, namespace_components)
    except BaseException:
        if published_new and attempt_fd is not None:
            try:
                os.unlink(filename, dir_fd=attempt_fd)
                _fsync_directory(attempt_fd)
            except BaseException:
                # The active publication/namespace error remains authoritative.
                pass
        raise
    finally:
        active_exception = sys.exc_info()[0] is not None
        cleanup_error: BaseException | None = None
        if temp_name is not None and attempt_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=attempt_fd)
                _fsync_directory(attempt_fd)
            except FileNotFoundError:
                try:
                    _fsync_directory(attempt_fd)
                except BaseException as exc:
                    cleanup_error = exc
            except BaseException as exc:  # preserve KeyboardInterrupt/SystemExit
                cleanup_error = exc
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except BaseException as exc:  # pragma: no cover - defensive OS cleanup
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None and not active_exception:
            raise cleanup_error


def _validate_session_id_component(session_id: str) -> str:
    """Return a safe single path component for an artifact session."""

    separators = {"/", "\\", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    if (
        not isinstance(session_id, str)
        or not session_id
        or session_id in {".", ".."}
        or "\x00" in session_id
        or Path(session_id).is_absolute()
        or any(separator in session_id for separator in separators)
    ):
        raise ValueError("session_id must be a safe single path component")
    return session_id


def _validate_attempt_id(attempt_id: int) -> int:
    """Return an exact, non-negative integer attempt identifier."""

    if type(attempt_id) is not int or attempt_id < 0:
        raise ValueError("attempt_id must be an exact non-negative integer")
    return attempt_id


def _require_secure_artifact_writer_support() -> None:
    """Fail closed unless anchored, no-follow POSIX filesystem APIs exist."""

    if not _SECURE_ARTIFACT_DIR_FD_SUPPORTED:
        raise OSError(
            errno.ENOTSUP,
            "secure artifact publication requires POSIX dir_fd and O_NOFOLLOW support",
        )


def _directory_open_flags() -> int:
    """Flags for opening a trusted directory without following a symlink."""

    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_or_create_directory_at(parent_fd: int, name: str, *, label: str) -> int:
    """Open one child directory relative to a trusted parent, never a symlink."""

    created = False
    try:
        os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    if created:
        _fsync_directory(parent_fd)
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{label} must not be a symlink or non-directory") from exc
        raise


def _open_attempt_directory_fds(
    data_root: Path,
    *,
    session_component: str,
    attempt_component: str,
) -> tuple[list[int], list[tuple[str, str]]]:
    """Create/open and retain the root-anchored artifact directory chain."""

    absolute_root = Path(os.path.abspath(data_root))
    components = [(part, "data_dir") for part in absolute_root.parts[1:]]
    components.extend(
        [
            ("artifacts", "artifact root"),
            ("assembly", "assembly artifact directory"),
            (session_component, "session_id directory"),
            (attempt_component, "attempt_id directory"),
        ]
    )
    directory_fds = [os.open("/", _directory_open_flags())]
    try:
        for component, label in components:
            directory_fds.append(
                _open_or_create_directory_at(
                    directory_fds[-1],
                    component,
                    label=label,
                )
            )
        return directory_fds, components
    except BaseException:
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except BaseException:
                pass
        raise


def _verify_artifact_namespace(
    directory_fds: list[int],
    components: list[tuple[str, str]],
) -> None:
    """Require the root-visible directory chain to retain the opened inodes."""

    for index, (component, label) in enumerate(components):
        try:
            probe_fd = os.open(
                component,
                _directory_open_flags(),
                dir_fd=directory_fds[index],
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT}:
                raise ValueError(
                    f"artifact namespace changed during publication at {label}"
                ) from exc
            raise
        try:
            expected = os.fstat(directory_fds[index + 1])
            actual = os.fstat(probe_fd)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError(
                    f"artifact namespace changed during publication at {label}"
                )
        finally:
            active_exception = sys.exc_info()[0] is not None
            try:
                os.close(probe_fd)
            except BaseException:
                if not active_exception:
                    raise


def _open_unique_temp_file(directory_fd: int, filename: str) -> tuple[str, Any]:
    """Create an unpredictable same-directory temporary file by anchored FD."""

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(100):
        temp_name = f".{filename}.{secrets.token_hex(12)}.tmp"
        try:
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            return temp_name, os.fdopen(temp_fd, "wb")
        except BaseException:
            try:
                os.close(temp_fd)
            except BaseException:
                pass
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except BaseException:
                pass
            else:
                try:
                    _fsync_directory(directory_fd)
                except BaseException:
                    pass
            raise
    raise FileExistsError("could not allocate a unique artifact temporary file")


def _existing_regular_file_matches(
    directory_fd: int,
    filename: str,
    expected: bytes,
) -> bool:
    """Compare an existing no-follow regular file through its anchored FD."""

    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        existing_fd = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO}:
            raise FileExistsError(
                errno.EEXIST,
                "immutable attempt artifact exists but is not a regular file",
                filename,
            ) from None
        raise
    try:
        metadata = os.fstat(existing_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise FileExistsError(
                errno.EEXIST,
                "immutable attempt artifact exists but is not a regular file",
                filename,
            )
        if metadata.st_size != len(expected):
            return False
        offset = 0
        while offset < len(expected):
            chunk = os.read(existing_fd, min(64 * 1024, len(expected) - offset))
            if not chunk:
                return False
            if chunk != expected[offset : offset + len(chunk)]:
                return False
            offset += len(chunk)
        return os.read(existing_fd, 1) == b""
    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            os.close(existing_fd)
        except BaseException:
            if not active_exception:
                raise


def _fsync_directory(directory_fd: int) -> None:
    """Sync a published directory entry, tolerating documented unsupported OSes."""

    try:
        os.fsync(directory_fd)
    except OSError as exc:
        unsupported = {errno.EINVAL, errno.ENOTSUP}
        if hasattr(errno, "EOPNOTSUPP"):
            unsupported.add(errno.EOPNOTSUPP)
        if exc.errno not in unsupported:
            raise


def _write_and_sync_json(handle: Any, serialized: bytes) -> None:
    """Write complete JSON bytes and sync them before atomic publication."""

    handle.write(serialized)
    handle.flush()
    os.fsync(handle.fileno())


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
