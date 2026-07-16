"""Pure prompt-context builder for assembly parameter recommendation."""

from __future__ import annotations

import hashlib
import json
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
    initial_basis_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the existing literature-guided sparse-patch prompt contract.

    Data v2 deliberately keeps this function free of filesystem side effects.
    Its caller persists the input profile, exact rendered request, response,
    parsed recommendation, validation, and immutable manifest separately.
    """

    if write_artifacts:
        raise ValueError(
            "build_prompt_payload is pure in data-v2; persist through parameter_persistence"
        )
    # Retain the semantic query binding while making the former persistence
    # arguments explicitly inert at this pure boundary.
    _ = data_dir
    _ = initial_attempt_snapshot

    prompt_profile = build_prompt_profile(full_profile, task)
    initial_basis = (
        dict(initial_basis_snapshot or {}) if task != "recommend_initial" else {}
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
        if not initial_basis:
            raise ValueError("retry requires the immutable attempt 0 basis snapshot")
        literature_cases = list(initial_basis.get("literature_cases") or [])
        literature_selection = dict(initial_basis.get("literature_selection") or {})
        rules = dict(initial_basis.get("rules") or {})
        method_guidance = list(initial_basis.get("method_guidance") or [])

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
        "input_profile_hash": generate_profile_ref(full_profile),
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
    payload["payload_hash"] = generate_payload_ref(payload)
    return payload


def recommendation_basis_snapshot(
    *,
    catalog: MethodParameterCatalog,
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the immutable policy/evidence basis reused by retry prompts."""

    return {
        "schema_version": 2,
        "method_id": catalog.method_id,
        "catalog_snapshot": catalog.model_dump(mode="json"),
        "rules": dict(prompt_payload.get("rules") or {}),
        "literature_selection": dict(prompt_payload.get("literature_selection") or {}),
        "literature_cases": list(prompt_payload.get("literature_cases") or []),
        "method_guidance": list(prompt_payload.get("method_guidance") or []),
    }


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


def generate_profile_ref(full_profile: dict[str, Any]) -> str:
    """Return a canonical semantic hash for an input profile."""

    return _canonical_json_hash(full_profile)


def generate_payload_ref(prompt_payload: dict[str, Any]) -> str:
    """Return a canonical semantic hash for an internal prompt context."""

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


def _canonical_json_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
