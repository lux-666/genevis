"""Opaque evidence IDs for model-visible assembly profile observations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

_PROFILE_EVIDENCE_ID_PREFIX = "pf_"
_PROFILE_EVIDENCE_ID_HASH_LENGTH = 10
_RETRY_FAILURE_SOURCE_ROOTS = (
    "assembly_review",
    "previous_attempt.failure_class",
    "previous_attempt.execution_status",
    "previous_attempt.execution_status_detail",
    "previous_attempt.exit_code",
    "previous_attempt.stderr_ref",
    "previous_attempt.stdout_ref",
    "previous_attempt.resource_facts",
)
_STORAGE_LOCATOR_SUFFIXES = (
    "_ref",
    "_refs",
    "_path",
    "_paths",
    "_dir",
    "_dirs",
    "_file",
    "_files",
    "_filename",
    "_filenames",
    "_file_name",
    "_file_names",
    "_directory",
    "_directories",
)


def build_profile_evidence(
    prompt_profile: Any,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Project profile leaves into model observations and an internal registry.

    IDs are derived from the source path, so adding an unrelated fact does not
    renumber existing evidence. The model surface deliberately omits the source
    path; nested facts expose only a path-free parent ``group`` label. Only
    deterministic application code can resolve an ID back to its source path.
    ``None`` values are omitted because they are not observations.
    """

    leaves: list[tuple[tuple[str, ...], Any]] = []

    def visit(parts: tuple[str, ...], value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key in sorted(value, key=lambda item: str(item)):
                visit((*parts, str(raw_key)), value[raw_key])
            return
        if value is not None and parts:
            leaves.append((parts, value))

    if isinstance(prompt_profile, Mapping):
        visit((), prompt_profile)

    observations: list[dict[str, Any]] = []
    registry: dict[str, str] = {}
    for parts, value in leaves:
        source_path = ".".join(parts)
        evidence_id = _profile_evidence_id(source_path)
        if evidence_id in registry and registry[evidence_id] != source_path:
            raise ValueError(
                "profile evidence ID collision between "
                f"{registry[evidence_id]!r} and {source_path!r}"
            )
        registry[evidence_id] = source_path
        if not _is_storage_locator(parts):
            observation = {
                "id": evidence_id,
                "section": parts[0],
                "name": parts[-1],
                "value": value,
            }
            if len(parts) > 2:
                observation["group"] = parts[-2]
            observations.append(observation)
    return observations, registry


def missing_profile_evidence_ids(
    prompt_payload: Mapping[str, Any] | None,
    evidence_ids: list[Any],
) -> list[str]:
    """Return IDs that are not present on the model-visible observation surface."""

    payload = prompt_payload or {}
    registry = payload.get("evidence_registry")
    observation_ids = {
        observation.get("id")
        for observation in (payload.get("observations") or [])
        if isinstance(observation, Mapping) and isinstance(observation.get("id"), str)
    }
    known_ids = set(registry) & observation_ids if isinstance(registry, Mapping) else set()
    return [
        raw if isinstance(raw, str) else repr(raw)
        for raw in evidence_ids
        if not isinstance(raw, str) or raw not in known_ids
    ]


def profile_evidence_ids_requiring_retry_basis(
    prompt_payload: Mapping[str, Any] | None,
    evidence_ids: list[str],
) -> list[str]:
    """Return cited IDs whose internal sources describe retry failure state."""

    registry = (prompt_payload or {}).get("evidence_registry")
    if not isinstance(registry, Mapping):
        return []
    retry_ids: list[str] = []
    for evidence_id in evidence_ids:
        source_path = registry.get(evidence_id)
        if not isinstance(source_path, str):
            continue
        if any(
            source_path == root or source_path.startswith(f"{root}.")
            for root in _RETRY_FAILURE_SOURCE_ROOTS
        ):
            retry_ids.append(evidence_id)
    return retry_ids


def validate_profile_evidence_projection(prompt_payload: Mapping[str, Any]) -> None:
    """Fail when observations or registry diverge from ``prompt_profile``."""

    expected_observations, expected_registry = build_profile_evidence(
        prompt_payload.get("prompt_profile") or {}
    )
    if prompt_payload.get("observations") != expected_observations:
        raise ValueError("profile observations differ from the prompt profile projection")
    if prompt_payload.get("evidence_registry") != expected_registry:
        raise ValueError("profile evidence registry differs from the prompt profile projection")


def _profile_evidence_id(source_path: str) -> str:
    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    return f"{_PROFILE_EVIDENCE_ID_PREFIX}{digest[:_PROFILE_EVIDENCE_ID_HASH_LENGTH]}"


def _is_storage_locator(parts: tuple[str, ...]) -> bool:
    return any(
        part.lower()
        in {"filename", "filenames", "file_name", "file_names", "directory", "directories"}
        or part.lower() == "input_files"
        or part.lower().endswith(_STORAGE_LOCATOR_SUFFIXES)
        for part in parts
    )
