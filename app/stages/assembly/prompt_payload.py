"""Build prompt payloads for assembly parameter recommendation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from app.domains.assembly.case_selector import select_evidence_cases
from app.domains.assembly.method_guidance import load_method_guidance
from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.domains.assembly.profile_evidence import build_profile_evidence
from app.knowledge.packs import load_knowledge_pack
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_PROMPT_PAYLOAD,
    ArtifactRef,
    file_sha256,
)
from app.schemas.parameter_catalog import MethodParameterCatalog
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
    """Build the single literature-guided sparse-patch prompt contract."""
    artifact_plan: list[tuple[bytes, ArtifactRef, str]] = []
    session_component, attempt_number = _resolve_artifact_scope(
        write_artifacts=write_artifacts,
        session_id=session_id,
        attempt_id=attempt_id,
    )
    input_profile_ref, profile_hash_ref = _prepare_input_profile_binding(
        full_profile=full_profile,
        write_artifacts=write_artifacts,
        session_component=session_component,
        attempt_number=attempt_number,
        artifact_plan=artifact_plan,
    )

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
    readonly_params, adjustable_params = _partition_prompt_catalog(catalog)
    if task == "recommend_initial":
        knowledge_pack = load_knowledge_pack(knowledge_pack_id, root=knowledge_root)
        literature_cases, literature_selection = select_evidence_cases(
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
        rules = dict(knowledge_pack.get("rules") or {})
        method_guidance = _method_guidance(method_id)
    else:
        literature_cases = list(initial_prompt.get("literature_cases") or [])
        literature_selection = dict(initial_prompt.get("literature_selection") or {})
        rules = dict(initial_prompt.get("rules") or {})
        method_guidance = list(initial_prompt.get("method_guidance") or [])
        if not initial_prompt:
            raise ValueError("retry requires the immutable attempt 0 prompt snapshot")

    observations, evidence_registry = build_profile_evidence(prompt_profile)
    baseline_params = _baseline_params(
        task=task,
        adjustable_params=adjustable_params,
        prompt_profile=prompt_profile,
    )
    baseline_overrides = _baseline_overrides(
        task=task,
        prompt_profile=prompt_profile,
    )
    payload = {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": method_id,
        "task": task,
        "input_profile_ref": input_profile_ref,
        "input_profile_hash": profile_hash_ref,
        "prompt_profile": prompt_profile,
        "observations": observations,
        "evidence_registry": evidence_registry,
        "rules": rules,
        "literature_selection": literature_selection,
        "literature_cases": literature_cases,
        "method_guidance": method_guidance,
        "readonly_params": readonly_params,
        "adjustable_params": adjustable_params,
        "baseline_params": baseline_params,
        "baseline_overrides": baseline_overrides,
        "recommendation_policy": {
            "output": "parameter_patch mapping each changed L2 parameter directly to its value",
            "literature_role": (
                "optional analog and numeric anchor; assess applicability instead of copying"
            ),
            "guidance_role": "mechanistic relationships, not executable rules or fixed values",
            "returned_evidence_references": "not requested; evidence remains in the prompt artifact",
            "human_review": "every proposed parameter patch is reviewed before execution",
            "case_required": False,
            "unmentioned_parameters": (
                "retain previous effective value on retry; otherwise use catalog/tool default"
            ),
        },
    }
    if task != "recommend_initial":
        payload["retry_context"] = _build_retry_context(prompt_profile, task=task)
    return _seal_and_publish_prompt_payload(
        payload=payload,
        write_artifacts=write_artifacts,
        session_component=session_component,
        attempt_number=attempt_number,
        artifact_plan=artifact_plan,
        data_dir=data_dir,
    )


def _resolve_artifact_scope(
    *,
    write_artifacts: bool,
    session_id: str | None,
    attempt_id: int | None,
) -> tuple[str | None, int | None]:
    if not write_artifacts:
        return None, None
    if session_id is None or attempt_id is None:
        raise ValueError("session_id and attempt_id are required when write_artifacts=True")
    return _validate_session_id_component(session_id), _validate_attempt_id(attempt_id)


def _prepare_input_profile_binding(
    *,
    full_profile: dict[str, Any],
    write_artifacts: bool,
    session_component: str | None,
    attempt_number: int | None,
    artifact_plan: list[tuple[bytes, ArtifactRef, str]],
) -> tuple[str, str]:
    input_profile_hash = generate_profile_ref(full_profile)
    if not write_artifacts:
        return input_profile_hash, input_profile_hash

    profile_serialized, profile_ref = _prepare_json_artifact(
        value=full_profile,
        session_id=session_component,
        attempt_id=attempt_number,
        filename="input_profile.json",
        artifact_type=ASSEMBLY_PARAMETER_INPUT_PROFILE,
    )
    artifact_plan.append((profile_serialized, profile_ref, "input_profile.json"))
    return profile_ref.path_ref, profile_ref.sha256


def _partition_prompt_catalog(
    catalog: MethodParameterCatalog,
) -> tuple[dict[str, Any], dict[str, Any]]:
    readonly_params: dict[str, Any] = {}
    adjustable_params: dict[str, Any] = {}
    for key, spec in catalog.parameters.items():
        if spec.level == 1:
            readonly_params[key] = spec.model_dump()
        elif spec.level == 2:
            adjustable_params[key] = spec.model_dump()
    return readonly_params, adjustable_params


def _method_guidance(method_id: str) -> list[dict[str, Any]]:
    if method_id not in {"hifiasm", "hifiasm_ont"}:
        return []
    return load_method_guidance().model_cards_for_method(method_id)


def _baseline_params(
    *,
    task: ParameterPromptTask,
    adjustable_params: dict[str, Any],
    prompt_profile: dict[str, Any],
) -> dict[str, Any]:
    if task != "recommend_initial":
        previous_profile = prompt_profile.get("previous_attempt") or {}
        previous = previous_profile.get("effective_params") or previous_profile.get("params")
        if isinstance(previous, dict):
            return {
                parameter: previous[parameter]
                for parameter in adjustable_params
                if parameter in previous
            }
    return {
        parameter: spec["default"]
        for parameter, spec in adjustable_params.items()
        if isinstance(spec, dict) and spec.get("default") is not None
    }


def _baseline_overrides(
    *,
    task: ParameterPromptTask,
    prompt_profile: dict[str, Any],
) -> dict[str, Any]:
    if task == "recommend_initial":
        return {}
    previous = (prompt_profile.get("previous_attempt") or {}).get("execution_params")
    return dict(previous) if isinstance(previous, dict) else {}


def _seal_and_publish_prompt_payload(
    *,
    payload: dict[str, Any],
    write_artifacts: bool,
    session_component: str | None,
    attempt_number: int | None,
    artifact_plan: list[tuple[bytes, ArtifactRef, str]],
    data_dir: Path | None,
) -> dict[str, Any]:
    # Canonical hash semantics intentionally exclude the two self-referential
    # fields. This is a semantic-content hash, not the serialized file hash.
    payload_for_hash = {
        key: value
        for key, value in payload.items()
        if key not in ("prompt_payload_ref", "prompt_payload_hash")
    }
    payload_hash = generate_payload_ref(payload_for_hash)
    prompt_payload_ref = (
        _artifact_path_ref(session_component, attempt_number, "prompt_payload.json")
        if write_artifacts
        else payload_hash
    )
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


def _build_retry_context(profile: dict[str, Any], *, task: str) -> dict[str, Any]:
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
        "previous_attempt": previous,
        "assembly_review": review,
        "signal_ids": [signal["signal_id"] for signal in signals],
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
