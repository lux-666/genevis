"""Build the immutable basis and strict DTO for parameter recommendation.

The full input profile and knowledge basis are audit facts.  The request DTO is
the smaller, explicit semantic surface shown to the model.  This module builds
that DTO directly; it never creates a richer prompt envelope and then projects
it a second time.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.domains.assembly.case_selector import select_evidence_cases
from app.domains.assembly.method_guidance import load_method_guidance
from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.domains.assembly.profile_evidence import build_profile_evidence
from app.knowledge.packs import load_knowledge_pack
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.stages.assembly.parameter_profile import ParameterPromptTask, build_prompt_profile


_PARAMETER_SPEC_FIELDS = (
    "value_type",
    "allowed_values",
    "min",
    "max",
    "default",
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


def build_parameter_basis_snapshot(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    full_profile: dict[str, Any],
    knowledge_pack_id: str = "parameter_recommender",
    session_id: str | None = None,
    recommendation_id: int | None = None,
    knowledge_root: Path | None = None,
) -> dict[str, Any]:
    """Select and return the full immutable basis for an initial recommendation."""

    if method_id != catalog.method_id:
        raise ValueError(
            f"parameter basis method {method_id!r} does not match catalog {catalog.method_id!r}"
        )
    prompt_profile = build_prompt_profile(full_profile, "recommend_initial")
    knowledge_pack = load_knowledge_pack(knowledge_pack_id, root=knowledge_root)
    literature_cases, literature_selection = select_evidence_cases(
        knowledge_pack["cases"],
        method_id=method_id,
        task="recommend_initial",
        prompt_profile=prompt_profile,
        strategy=knowledge_pack.get("strategies"),
        source_root=knowledge_pack.get("pack_root"),
        query_id=(
            f"{session_id}:parameter_attempt_{recommendation_id}"
            if session_id is not None and recommendation_id is not None
            else None
        ),
    )
    return {
        "schema_version": 2,
        "method_id": catalog.method_id,
        "catalog_snapshot": catalog.model_dump(mode="json"),
        "rules": dict(knowledge_pack.get("rules") or {}),
        "literature_selection": dict(literature_selection),
        "literature_cases": list(literature_cases),
        "method_guidance": _method_guidance(method_id),
    }


def build_parameter_request_dto(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    full_profile: dict[str, Any],
    task: ParameterPromptTask,
    basis_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Build only the model-visible parameter recommendation DTO."""

    if method_id != catalog.method_id:
        raise ValueError(
            f"parameter request method {method_id!r} does not match catalog {catalog.method_id!r}"
        )
    if basis_snapshot.get("method_id") != catalog.method_id:
        raise ValueError("parameter request basis method does not match the catalog")

    prompt_profile = build_prompt_profile(full_profile, task)
    observations, _ = build_profile_evidence(prompt_profile)
    readonly_params, adjustable_params = _partition_model_catalog(catalog)
    baseline_params = _baseline_params(
        task=task,
        adjustable_params=adjustable_params,
        prompt_profile=prompt_profile,
    )

    request: dict[str, Any] = {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": method_id,
        "task": task,
        "observations": _model_observations(observations),
        "readonly_params": readonly_params,
        "adjustable_params": adjustable_params,
    }
    _add_visible(request, "rules", basis_snapshot.get("rules"))
    _add_visible(request, "recommendation_policy", _recommendation_policy())
    _add_visible(
        request,
        "method_guidance",
        _model_method_guidance(basis_snapshot.get("method_guidance")),
    )
    _add_visible(request, "baseline_params", baseline_params)
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
    return request


def previous_execution_overrides(
    full_profile: Mapping[str, Any],
    *,
    task: ParameterPromptTask,
) -> dict[str, Any]:
    """Return retry execution values needed for normalization, not model input."""

    if task == "recommend_initial":
        return {}
    previous = full_profile.get("previous_attempt")
    if not isinstance(previous, Mapping):
        return {}
    execution_params = previous.get("execution_params")
    return dict(execution_params) if isinstance(execution_params, Mapping) else {}


def _partition_model_catalog(
    catalog: MethodParameterCatalog,
) -> tuple[dict[str, Any], dict[str, Any]]:
    readonly_params: dict[str, Any] = {}
    adjustable_params: dict[str, Any] = {}
    for key, spec in catalog.parameters.items():
        if spec.level not in {1, 2}:
            continue
        model_spec = _model_parameter_spec(spec.model_dump(mode="json"))
        if spec.level == 1:
            readonly_params[key] = model_spec
        else:
            adjustable_params[key] = model_spec
    return readonly_params, adjustable_params


def _model_parameter_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in _PARAMETER_SPEC_FIELDS:
        if field not in value:
            continue
        child = _model_safe_value(value[field])
        if child is not _OMIT:
            projected[field] = child
    return projected


def _method_guidance(method_id: str) -> list[dict[str, Any]]:
    if method_id not in {"hifiasm", "hifiasm_ont"}:
        return []
    return load_method_guidance().model_cards_for_method(method_id)


def _recommendation_policy() -> dict[str, Any]:
    return {
        "output": "parameter_patch mapping each changed L2 parameter directly to its value",
        "literature_role": (
            "optional analog and numeric anchor; assess applicability instead of copying"
        ),
        "guidance_role": "mechanistic relationships, not executable rules or fixed values",
        "returned_evidence_references": (
            "not requested; evidence remains in the prompt artifact"
        ),
        "human_review": "every proposed parameter patch is reviewed before execution",
        "case_required": False,
        "unmentioned_parameters": (
            "retain previous effective value on retry; otherwise use catalog/tool default"
        ),
    }


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
