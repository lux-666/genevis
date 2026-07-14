"""Model-facing context projection for assembly parameter recommendations.

The assembly stage keeps a richer internal prompt payload for validation,
attestation, replay, and artifact audit.  This module is the one-way boundary
from that internal envelope to the smaller semantic context shown to the LLM.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


MODEL_FACING_PROMPT_FIELDS = (
    "method_id",
    "task",
    "observations",
    "rules",
    "recommendation_policy",
    "skill_instructions",
    "readonly_params",
    "adjustable_params",
    "selected_cases",
    "case_selection",
    "evidence_cases",
    "methods_evidence",
    "retry_evidence",
)

_PARAMETER_SPEC_FIELDS = (
    "value_type",
    "allowed_values",
    "min",
    "max",
    "default",
    "description",
    "notes",
)
_CASE_SELECTION_FIELDS = (
    "warning",
    "evidence_available",
    "case_evidence_required",
    "evidence_required",
)
_EVIDENCE_CASE_FIELDS = (
    "case_id",
    "case_type",
    "source",
    "applicability",
    "observed_facts",
    "intervention",
    "rationale_claims",
    "outcomes",
    "previous_attempt",
    "source_excerpts",
)
_METHODS_EVIDENCE_FIELDS = (
    "effect_areas",
    "response_contract",
    "assessment_rule",
    "recommendation_rule",
)
_INTERNAL_ONLY_FIELDS = frozenset({"evidence_registry", "profile_facts"})
_AUDIT_SCALAR_FIELDS = frozenset(
    {
        "checksum",
        "digest",
        "directories",
        "directory",
        "document",
        "file_name",
        "file_names",
        "filename",
        "filenames",
        "internal_run_id",
        "locator",
        "query_id",
        "sha256",
        "uri",
        "url",
    }
)
_AUDIT_PREFIXES = ("artifact", "session", "storage")
_OMIT = object()


def build_assembly_parameter_context(
    prompt_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Project an internal assembly prompt envelope into its model-visible DTO.

    ``observations`` is the sole profile-fact surface at this boundary.  A
    missing or malformed observation list is therefore an error: silently
    falling back to ``prompt_profile`` would re-introduce storage paths and
    couple the model to the internal profile layout.
    """

    if not isinstance(prompt_payload, Mapping):
        raise TypeError("assembly parameter prompt payload must be a mapping")
    observations = prompt_payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError(
            "assembly parameter prompt payload requires a model-facing observations list"
        )

    context: dict[str, Any] = {}
    for field in MODEL_FACING_PROMPT_FIELDS:
        if field not in prompt_payload:
            continue
        raw_value = prompt_payload[field]
        if field == "observations":
            projected = _project_observations(raw_value)
        elif field in {"readonly_params", "adjustable_params"}:
            projected = _project_parameter_specs(raw_value, field=field)
        elif field == "case_selection":
            projected = _project_mapping_fields(raw_value, _CASE_SELECTION_FIELDS)
        elif field == "evidence_cases":
            projected = _project_evidence_cases(raw_value)
        elif field == "methods_evidence":
            projected = _project_mapping_fields(raw_value, _METHODS_EVIDENCE_FIELDS)
        elif field == "retry_evidence":
            projected = _project_retry_evidence(raw_value)
        else:
            projected = _project_safe_value(raw_value)
        if projected is _OMIT:
            if field == "observations":
                context[field] = []
            continue
        context[field] = projected

    # The type check above guarantees the field exists semantically, including
    # the valid no-observations case represented by an empty list.
    context.setdefault("observations", [])
    return context


def _project_observations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("assembly parameter prompt observations must be a list")
    projected: list[dict[str, Any]] = []
    for index, observation in enumerate(value):
        if not isinstance(observation, Mapping):
            raise ValueError(f"assembly parameter prompt observations[{index}] must be an object")
        if not {"id", "section", "name", "value"}.issubset(observation):
            raise ValueError(
                f"assembly parameter prompt observations[{index}] has an invalid shape"
            )
        item = _project_mapping_fields(
            observation,
            ("id", "section", "group", "name", "value"),
        )
        if (
            item is not _OMIT
            and {"id", "section", "name", "value"}.issubset(item)
            and not _mapping_identifies_audit_metadata(item)
        ):
            projected.append(item)
    return projected


def _project_parameter_specs(value: Any, *, field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"assembly parameter prompt {field} must be an object")

    projected_specs: dict[str, dict[str, Any]] = {}
    for parameter, raw_spec in value.items():
        if not isinstance(raw_spec, Mapping):
            raise ValueError(f"assembly parameter prompt {field}.{parameter} must be an object")
        spec: dict[str, Any] = {}
        for spec_field in _PARAMETER_SPEC_FIELDS:
            if spec_field not in raw_spec:
                continue
            projected = _project_safe_value(raw_spec[spec_field])
            if projected is not _OMIT:
                spec[spec_field] = projected
        projected_specs[str(parameter)] = spec
    return projected_specs


def _project_mapping_fields(value: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(value, Mapping):
        return _OMIT
    projected: dict[str, Any] = {}
    for field in fields:
        if field not in value:
            continue
        child = _project_safe_value(value[field])
        if child is not _OMIT:
            projected[field] = child
    return projected if projected else _OMIT


def _project_evidence_cases(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        return _OMIT
    cases = []
    for raw_case in value:
        case = _project_mapping_fields(raw_case, _EVIDENCE_CASE_FIELDS)
        if case is not _OMIT:
            cases.append(case)
    return cases if cases else _OMIT


def _project_retry_evidence(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _OMIT
    projected: dict[str, Any] = {}
    task = _project_safe_value(value.get("task"))
    if task is not _OMIT:
        projected["task"] = task
    signal_ids = [
        signal_id
        for signal_id in (value.get("signal_ids") or [])
        if isinstance(signal_id, str) and not _is_audit_locator(signal_id)
    ]
    if signal_ids:
        projected["signal_ids"] = signal_ids
    signals = _project_safe_value(value.get("signals"))
    if signals is not _OMIT:
        projected["signals"] = signals
    return projected if projected else _OMIT


def _project_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if _mapping_identifies_audit_metadata(value):
            return _OMIT
        projected: dict[str, Any] = {}
        for raw_key, raw_child in value.items():
            key = str(raw_key)
            if _is_audit_or_locator_key(key):
                continue
            child = _project_safe_value(raw_child)
            if child is not _OMIT:
                projected[key] = child
        return projected if projected else _OMIT

    if isinstance(value, (list, tuple)):
        projected_items = []
        for item in value:
            projected = _project_safe_value(item)
            if projected is not _OMIT:
                projected_items.append(projected)
        return projected_items if projected_items else _OMIT

    if value is None:
        return _OMIT
    if isinstance(value, str) and not value.strip():
        return _OMIT
    return deepcopy(value)


def _mapping_identifies_audit_metadata(value: Mapping[Any, Any]) -> bool:
    locator_fields = ("path", "key", "field", "signal_id")
    if {"id", "section", "name", "value"}.issubset(value):
        locator_fields = (*locator_fields, "id", "name")
    for field in locator_fields:
        locator = value.get(field)
        if isinstance(locator, str) and _is_audit_locator(locator):
            return True
    return False


def _is_audit_locator(value: str) -> bool:
    leaf = value.strip().lower().replace(":", ".").rsplit(".", 1)[-1]
    return _is_audit_or_locator_key(leaf)


def _is_audit_or_locator_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in _INTERNAL_ONLY_FIELDS or normalized in _AUDIT_SCALAR_FIELDS:
        return True
    if normalized in {"path", "paths", "artifact", "artifacts", "session", "storage"}:
        return True
    if normalized.endswith(("_ref", "_refs", "_hash", "_hashes")):
        return True
    if normalized.endswith(
        (
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
            "_uri",
            "_url",
            "_locator",
        )
    ):
        return True
    if "_path_" in normalized:
        return True
    if normalized.startswith(tuple(f"{prefix}_" for prefix in _AUDIT_PREFIXES)):
        return True
    if normalized in {"attempt_id", "attempt_number"} or normalized.endswith("_attempt_id"):
        return True
    return False
