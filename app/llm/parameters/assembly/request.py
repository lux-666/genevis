"""Build the strict Assembly parameter-recommendation model DTO.

The stage owns the full input profile and immutable knowledge basis.  This LLM
boundary owns only the smaller semantic surface shown to the model.  It builds
that DTO directly; it never creates a richer prompt envelope and then projects
it a second time.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.domains.assembly.profile_evidence import build_profile_evidence
from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog


_PARAMETER_SPEC_FIELDS = (
    "value_type",
    "allowed_values",
    "min",
    "max",
    "description",
    "notes",
)
_LITERATURE_SELECTION_FIELDS = (
    "retrieval_backend",
    "selected_case_ids",
    "selected_count",
    "eligible_case_count",
    "total_cases",
    "warning",
    "evidence_available",
)
_LITERATURE_CASE_FIELDS = (
    "schema_version",
    "case_id",
    "case_type",
    "source",
    "applicability",
    "observed_facts",
    "intervention",
    "rationale_claims",
    "outcomes",
    "source_excerpts",
    "numeric_match",
)
_METHOD_GUIDANCE_FIELDS = (
    "guidance_id",
    "methods",
    "parameters",
    "relevant_metrics",
    "guidance",
    "caveats",
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


class AssemblyParameterRequestDTO(BaseModel):
    """Validated top-level contract for one Assembly model request.

    Nested biological evidence keeps its existing domain-owned shape.  This
    model owns the model-visible top-level surface and rejects accidental audit
    fields before prompt rendering.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_contract: Literal["parameter_recommendation_v1"]
    method_id: str = Field(min_length=1)
    task: ParameterAttemptTask
    observations: list[dict[str, Any]]
    adjustable_parameters: dict[str, dict[str, Any]]
    method_guidance: list[dict[str, Any]] | None = None
    literature_selection: dict[str, Any] | None = None
    literature_cases: list[dict[str, Any]] | None = None
    retry_context: dict[str, Any] | None = None


def build_parameter_request_dto(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    prompt_profile: dict[str, Any],
    task: ParameterAttemptTask,
    basis_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Build only the model-visible parameter recommendation DTO."""

    if method_id != catalog.method_id:
        raise ValueError(
            f"parameter request method {method_id!r} does not match catalog {catalog.method_id!r}"
        )
    if basis_snapshot.get("method_id") != catalog.method_id:
        raise ValueError("parameter request basis method does not match the catalog")

    observations, _ = build_profile_evidence(prompt_profile)
    adjustable_parameters = _model_adjustable_parameters(catalog)

    request: dict[str, Any] = {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": method_id,
        "task": task,
        "observations": _model_observations(observations),
        "adjustable_parameters": adjustable_parameters,
    }
    _add_visible(
        request,
        "method_guidance",
        _model_method_guidance(basis_snapshot.get("method_guidance")),
    )
    _add_visible(
        request,
        "literature_selection",
        _model_mapping_fields(
            basis_snapshot.get("literature_selection"),
            _LITERATURE_SELECTION_FIELDS,
        ),
        already_projected=True,
    )
    _add_visible(
        request,
        "literature_cases",
        _model_literature_cases(basis_snapshot.get("literature_cases")),
        already_projected=True,
    )
    if task != "recommend_initial":
        _add_visible(
            request,
            "retry_context",
            _model_retry_context(_build_retry_context(prompt_profile, task=task)),
            already_projected=True,
        )
    return AssemblyParameterRequestDTO.model_validate(request).model_dump(
        mode="json",
        exclude_none=True,
    )


def _model_adjustable_parameters(
    catalog: MethodParameterCatalog,
) -> dict[str, Any]:
    adjustable_parameters: dict[str, Any] = {}
    for key, spec in catalog.parameters.items():
        if spec.level == 2:
            adjustable_parameters[key] = _model_parameter_spec(
                spec.model_dump(mode="json")
            )
    return adjustable_parameters


def _model_parameter_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in _PARAMETER_SPEC_FIELDS:
        if field not in value:
            continue
        child = _model_safe_value(value[field])
        if child is not _OMIT:
            projected[field] = child
    return projected


def _build_retry_context(profile: dict[str, Any], *, task: str) -> dict[str, Any]:
    previous = dict(profile.get("previous_attempt") or {})
    review = dict(profile.get("assembly_review") or {})
    signals: list[dict[str, Any]] = []
    for signal_id, path, value in (
        (
            "execution.status",
            "previous_attempt.execution_status",
            previous.get("execution_status"),
        ),
        (
            "execution.status_detail",
            "previous_attempt.execution_status_detail",
            previous.get("execution_status_detail"),
        ),
        (
            "execution.exit_code",
            "previous_attempt.exit_code",
            previous.get("exit_code"),
        ),
        (
            "execution.stderr_ref",
            "previous_attempt.stderr_ref",
            previous.get("stderr_ref"),
        ),
        (
            "execution.stdout_ref",
            "previous_attempt.stdout_ref",
            previous.get("stdout_ref"),
        ),
        (
            "review.failure_mode",
            "assembly_review.failure_mode",
            review.get("failure_mode"),
        ),
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


def _model_observations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("assembly parameter request observations must be a list")
    projected: list[dict[str, Any]] = []
    for index, observation in enumerate(value):
        if not isinstance(observation, Mapping):
            raise ValueError(
                f"assembly parameter request observations[{index}] must be an object"
            )
        if not {"id", "section", "name", "value"}.issubset(observation):
            raise ValueError(
                f"assembly parameter request observations[{index}] has an invalid shape"
            )
        item = _model_mapping_fields(
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


def _model_mapping_fields(value: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(value, Mapping):
        return _OMIT
    projected: dict[str, Any] = {}
    for field in fields:
        if field not in value:
            continue
        child = _model_safe_value(value[field])
        if child is not _OMIT:
            projected[field] = child
    return projected if projected else _OMIT


def _model_literature_cases(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        return _OMIT
    cases = []
    for raw_case in value:
        case = _model_mapping_fields(raw_case, _LITERATURE_CASE_FIELDS)
        if case is not _OMIT:
            cases.append(case)
    return cases if cases else _OMIT


def _model_method_guidance(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        return _OMIT
    cards = []
    for raw_card in value:
        card = _model_mapping_fields(raw_card, _METHOD_GUIDANCE_FIELDS)
        if card is not _OMIT:
            cards.append(card)
    return cards if cards else _OMIT


def _model_retry_context(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _OMIT
    projected: dict[str, Any] = {}
    task = _model_safe_value(value.get("task"))
    if task is not _OMIT:
        projected["task"] = task
    for field in ("previous_attempt", "assembly_review"):
        child = _model_safe_value(value.get(field))
        if child is not _OMIT:
            projected[field] = child
    signal_ids = [
        signal_id
        for signal_id in (value.get("signal_ids") or [])
        if isinstance(signal_id, str) and not _is_audit_locator(signal_id)
    ]
    if signal_ids:
        projected["signal_ids"] = signal_ids
    signals = _model_safe_value(value.get("signals"))
    if signals is not _OMIT:
        projected["signals"] = signals
    return projected if projected else _OMIT


def _add_visible(
    target: dict[str, Any],
    field: str,
    value: Any,
    *,
    already_projected: bool = False,
) -> None:
    if value is _OMIT:
        return
    projected = value if already_projected else _model_safe_value(value)
    if projected is not _OMIT:
        target[field] = projected


def _model_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if _mapping_identifies_audit_metadata(value):
            return _OMIT
        projected: dict[str, Any] = {}
        for raw_key, raw_child in value.items():
            key = str(raw_key)
            if _is_audit_or_locator_key(key):
                continue
            child = _model_safe_value(raw_child)
            if child is not _OMIT:
                projected[key] = child
        return projected if projected else _OMIT

    if isinstance(value, (list, tuple)):
        projected_items = []
        for item in value:
            projected = _model_safe_value(item)
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
    if normalized in {"attempt_id", "attempt_number"} or normalized.endswith(
        "_attempt_id"
    ):
        return True
    return False
