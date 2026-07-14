"""Assembly stage nodes for candidate planning."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.control.lifecycle import ResumeValidationError, timeout_terminal_update
from app.control.ledger import Ledger
from app.domains.assembly.methods_evidence import uses_hifiasm_methods_contract
from app.domains.assembly.selection import (
    AssemblySelection,
    compatible_methods,
    default_primary_method,
    is_method_read_type_compatible,
    method_read_type,
    normalize_method_id,
    normalize_read_type,
    resolve_read_type,
    route_from_read_type as _route_from_read_type,
)
from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.stages.assembly.constants import (
    ASSEMBLY_AUTO_CANDIDATES,
    ASSEMBLY_CANDIDATES,
    ASSEMBLY_COMPATIBILITY_CANDIDATES as ASSEMBLY_COMPATIBILITY_CANDIDATES,
    FLYE_METHOD,
    FLYE_READ_TYPE_FLAGS as FLYE_READ_TYPE_FLAGS,
    GENOME_SIZE_REQUIRED,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
    NEXTDENOVO_METHOD,
)
from app.stages.assembly.decision import (
    AssemblyDecisionRequest,
    AssemblyPromptContract,
    make_assembly_decision,
)
from app.knowledge.packs import load_knowledge_pack
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.schemas.artifacts import (
    ASSEMBLY_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD,
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_LLM_RESPONSE,
    ASSEMBLY_PARAMETER_PROMPT_PAYLOAD,
    ASSEMBLY_PARAMETER_RETRIEVAL_QUERY,
    ASSEMBLY_PARAMETER_RETRIEVAL_TRACE,
    ASSEMBLY_PARAMETER_REVIEW_FAILURE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_VALIDATION,
    ASSEMBLY_ROUTE_DECISION,
    ASSEMBLY_CANDIDATE_BUNDLE,
    ArtifactRef,
    ArtifactMeta,
    AssemblyCandidateBundle,
    AssemblyInputProfile,
    ParameterAttempt,
    AssemblyRouteDecision,
    file_sha256,
)
from app.review.base import ResumeReviewRequest
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.plan import AssemblyCandidatePlan
from app.schemas.shared_parameters import PLATFORM_TO_SHARED_READ_TYPE, SHARED_TO_METHOD_READ_TYPE
from app.schemas.state import TopLevelState
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_profile import build_full_parameter_profile, flatten_profile_facts
from app.stages.assembly.parameter_validation import (
    validate_parameter_evidence,
    validate_recommended_parameters,
)
from app.stages.assembly.prompt_payload import build_prompt_payload

ROUTE_DECISION_MAP = {
    "select_cyclone": "cyclone",
    "select_ont": "ont",
    "select_hifi": "hifi",
}
ROUTE_VALUES = {"cyclone", "ont", "hifi"}


def _assembly_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "assembly")


def _update_assembly_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "assembly", payload)


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_assembly_output(state, {"status": "failed", "reason": reason})


def _resolve_data_path_ref(data_dir: Path, path_ref: str) -> Path:
    path = Path(path_ref)
    if path.is_absolute():
        return path
    if path_ref.startswith("data/"):
        return data_dir / path_ref.removeprefix("data/")
    return path


def enter_assembly(state: TopLevelState) -> dict:
    """Mark assembly as running."""
    return {"supervision_state": "running_stage"}


def resolve_extended_inputs(state: TopLevelState) -> dict:
    """Resolve long-read, HiFi, Hi-C, platform, and route hints for assembly."""
    assembly_out = _assembly_output(state)
    read_qc_ref = _read_qc_assembly_input_ref(state)
    asset_long_ref = _asset_input_ref(state)
    long_read_platform = _long_read_platform(state)
    hifi_reads_ref = assembly_out.get("hifi_reads_ref") or _hifi_asset_input_ref(state)
    has_explicit_hifi_ref = bool(assembly_out.get("hifi_reads_ref"))
    if not hifi_reads_ref and long_read_platform == "pacbio_hifi":
        hifi_reads_ref = read_qc_ref or asset_long_ref
    ont_reads_ref = (
        None
        if hifi_reads_ref and (has_explicit_hifi_ref or long_read_platform == "pacbio_hifi")
        else read_qc_ref or asset_long_ref
    )
    hic_reads_ref = assembly_out.get("hic_reads_ref") or _asset_ref_by_type(
        state,
        {"hic_reads", "hi_c_reads"},
    )

    configured_route = assembly_out.get("assembly_route") or "auto"
    existing_selection = _selection_from_output(assembly_out)
    hinted_read_type = resolve_read_type(
        ("assembly_selection.read_type", existing_selection.read_type if existing_selection else None),
        ("assembly_route", configured_route if configured_route != "auto" else None),
        ("shared_parameters.read_type", _shared_parameters(state).get("read_type")),
        ("hifi_reads_ref", "pacbio_hifi" if hifi_reads_ref else None),
        ("read_type_prior", _normalized_read_type_prior(state)),
        context="assembly.resolve_extended_inputs",
    )
    if hinted_read_type == "pacbio_hifi":
        primary_long_reads_ref = hifi_reads_ref or read_qc_ref or asset_long_ref
    else:
        primary_long_reads_ref = ont_reads_ref or hifi_reads_ref
    if hinted_read_type == "pacbio_hifi" and primary_long_reads_ref and not hifi_reads_ref:
        hifi_reads_ref = primary_long_reads_ref
    if not primary_long_reads_ref:
        return _fail_update(state, "assembly requires long reads (clean_long_reads or long_reads.fofn)")

    primary_platform = "pacbio_hifi" if hinted_read_type == "pacbio_hifi" or (hifi_reads_ref and not ont_reads_ref) else long_read_platform
    input_source = "read_qc" if read_qc_ref else "asset_registry"
    if hifi_reads_ref and not ont_reads_ref:
        input_source = "run_config" if assembly_out.get("hifi_reads_ref") else "asset_registry"

    genome_size_prior = _genome_size_prior(state)
    read_type_prior = _read_type_prior(state)
    target_level = assembly_out.get("target_level") or "draft"
    explicit_route = configured_route if configured_route in ROUTE_VALUES else None
    input_ref = primary_long_reads_ref
    input_type = "long_reads.fofn" if str(input_ref).endswith(".fofn") else "clean_long_reads"

    return _update_assembly_output(
        state,
        {
            "status": "resolved_extended",
            "primary_long_reads_ref": primary_long_reads_ref,
            "primary_long_read_platform": primary_platform,
            "ont_reads_ref": ont_reads_ref,
            "hifi_reads_ref": hifi_reads_ref,
            "hic_reads_ref": hic_reads_ref,
            "platform": primary_platform,
            "has_hifi": bool(hifi_reads_ref),
            "has_hic": bool(hic_reads_ref),
            "target_level": target_level,
            "genome_size_prior": genome_size_prior,
            "explicit_route": explicit_route,
            "input_ref": input_ref,
            "input_type": input_type,
            "input_source": input_source,
            "genome_survey_result_ref": _genome_survey_result_ref(state),
            "shared_parameters": _shared_parameters(state),
            "read_type_prior": read_type_prior,
            "normalized_read_type": hinted_read_type,
        },
    )


def infer_input_profile(state: TopLevelState) -> dict:
    """Write an AssemblyInputProfile and recommendation/final selection."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "resolved_extended":
        return {}

    existing_selection = _selection_from_output(assembly_out)
    explicit_route = assembly_out.get("explicit_route")
    if existing_selection or explicit_route:
        selection = existing_selection or _selection_from_route(
            explicit_route,
            source="run_config",
            rationale=f"run_config explicitly selected {explicit_route} read type",
        )
        if selection is None:
            return _fail_update(state, f"unsupported assembly route: {explicit_route}")
        profile = _build_input_profile(
            state,
            assembly_out,
            recommended_route=_route_from_read_type(selection.read_type),
            reasoning=selection.rationale,
            uncertainty_notes=[],
            profile_source="run_config_explicit",
        )
        profile_ref = _write_assembly_json_artifact(
            state,
            "input_profile.json",
            profile.model_dump(mode="json"),
            artifact_type=ASSEMBLY_INPUT_PROFILE,
            method_name="assembly.input_profiler",
        )
        decision = AssemblyRouteDecision(
            session_id=state["session_id"],
            selected_route=_route_from_read_type(selection.read_type),
            decided_by="run_config_explicit",
            rationale=profile.recommendation_reasoning,
            metadata={"assembly_selection": selection.model_dump(mode="json")},
        )
        decision_ref = _write_assembly_json_artifact(
            state,
            "route_decision.json",
            decision.model_dump(mode="json"),
            artifact_type=ASSEMBLY_ROUTE_DECISION,
            method_name="assembly.route_selector",
        )

        return _update_assembly_output(
            state,
            {
                "status": "profile_ready",
                "input_profile": profile.model_dump(mode="json"),
                "input_profile_ref": profile_ref,
                "route_decision": decision.model_dump(mode="json"),
                "route_decision_ref": decision_ref,
                "assembly_selection": selection.model_dump(mode="json"),
                "selected_route": _route_from_read_type(selection.read_type),
                "method_decision": _resolve_method_decision(
                    assembly_out.get("method_decision"),
                    _legacy_method_decision(selection),
                ),
            },
        )

    ctx = load_knowledge_pack(
        "route_selector",
        strict=False,
        include_actions=True,
        required_files=(),
    )
    recommendation = _recommend_route_with_decision_boundary(state, assembly_out, ctx)
    recommended_route = recommendation["recommended_route"]
    recommended_selection = (
        _selection_from_route(
            recommended_route,
            source="recommendation",
            rationale=str(recommendation.get("reasoning") or "selector recommendation"),
        )
        if recommended_route != "insufficient_data"
        else None
    )

    profile = _build_input_profile(
        state,
        assembly_out,
        recommended_route=recommended_route,
        reasoning=str(recommendation.get("reasoning") or "route selector recommendation"),
        uncertainty_notes=list(recommendation.get("uncertainty_notes") or []),
        profile_source="llm_inferred",
    )
    profile_ref = _write_assembly_json_artifact(
        state,
        "input_profile.json",
        profile.model_dump(mode="json"),
        artifact_type=ASSEMBLY_INPUT_PROFILE,
        method_name="assembly.input_profiler",
    )
    return _update_assembly_output(
        state,
        {
            "status": "profile_ready",
            "input_profile": profile.model_dump(mode="json"),
            "input_profile_ref": profile_ref,
            "recommended_assembly_selection": recommended_selection.model_dump(mode="json") if recommended_selection else None,
        },
    )


def select_route(state: TopLevelState) -> dict:
    """Confirm or block on the assembly selection recommendation."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "profile_ready":
        return {}
    if assembly_out.get("assembly_selection"):
        return _update_assembly_output(state, {"status": "route_selected"})

    profile = dict(assembly_out.get("input_profile") or {})
    recommended_route = profile.get("recommended_route")
    if recommended_route == "insufficient_data":
        return _block_for_route_review(state, "assembly_route_insufficient_data")

    # All LLM recommendations require human gate (unless max_blocked_seconds configured)
    return _block_for_route_review(state, "assembly_selection_human_confirmation_required")


def apply_route_decision(state: TopLevelState, request: ResumeReviewRequest, *, _selection_source: str = "human") -> dict[str, Any]:
    """Apply a human or timeout assembly-selection gate decision.

    Args:
        state: Current state
        request: Resume request
        _selection_source: Internal source set by timeout logic, not user-controllable
    """
    if request.stage != "assembly":
        raise ResumeValidationError("assembly selection resume requires stage assembly")
    if request.decision == "stop_human_review":
        return {
            "current_stage": "assembly",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "assembly route selection stopped by human review",
        }

    selected_route = ROUTE_DECISION_MAP.get(request.decision)
    if selected_route not in ROUTE_VALUES:
        raise ResumeValidationError(f"unsupported assembly selection decision: {request.decision}")

    assembly_out = _assembly_output(state)
    read_type, read_type_source = _requested_read_type(request, assembly_out, selected_route)
    method = _requested_method(request, assembly_out, read_type)
    try:
        selection = AssemblySelection(
            read_type=read_type,
            primary_method=method,
            source="timeout" if _selection_source == "timeout" else "human",
            rationale="; ".join(request.notes) if request.notes else f"{_selection_source} selected {selected_route} read type",
            metadata={"resume_decision": request.decision, **request.metadata, "read_type_source": read_type_source},
        )
    except ValueError as exc:
        raise ResumeValidationError(str(exc)) from exc

    decided_by = "llm_auto" if _selection_source == "timeout" else "human_override"

    decision = AssemblyRouteDecision(
        session_id=state["session_id"],
        selected_route=selected_route,
        decided_by=decided_by,
        rationale=selection.rationale,
        metadata={
            "assembly_selection": selection.model_dump(mode="json"),
            "resume_decision": request.decision,
            **request.metadata,
            "read_type_source": read_type_source,
        },
    )
    decision_ref = _write_assembly_json_artifact(
        state,
        "route_decision.json",
        decision.model_dump(mode="json"),
        artifact_type=ASSEMBLY_ROUTE_DECISION,
        method_name="assembly.route_selector",
    )

    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {
        **assembly_out,
        "status": "route_selected",
        "route_decision": decision.model_dump(mode="json"),
        "route_decision_ref": decision_ref,
        "assembly_selection": selection.model_dump(mode="json"),
        "selected_route": selected_route,
        "method_decision": _resolve_method_decision(
            assembly_out.get("method_decision"),
            _legacy_method_decision(selection),
        ),
    }
    return {
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def apply_parameter_approval_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> dict[str, Any]:
    """Apply one exactly-bound human decision to a validated recommendation."""

    if request.stage != "assembly":
        raise ResumeValidationError("parameter approval resume requires stage assembly")
    if request.gate_id != "parameter_approval":
        raise ResumeValidationError(
            "parameter approval resume requires explicit gate_id=parameter_approval"
        )
    assembly_out = _assembly_output(state)
    interrupt = state.get("active_interrupt") or assembly_out.get("active_interrupt")
    if not isinstance(interrupt, dict) or interrupt.get("gate_id") != "parameter_approval":
        raise ResumeValidationError("no unresolved parameter_approval gate")
    if request.gate_id != interrupt.get("gate_id"):
        raise ResumeValidationError("parameter approval gate_id does not match active interrupt")
    allowed = list(interrupt.get("decisions_allowed") or [])
    if request.decision not in allowed or request.decision not in {
        "approve_parameters", "reject_parameters"
    }:
        raise ResumeValidationError("unsupported parameter approval decision")

    required = ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")
    missing = [key for key in required if key not in request.metadata]
    if missing:
        raise ResumeValidationError(
            f"parameter approval metadata missing required bindings: {missing}"
        )
    for key in required:
        if request.metadata.get(key) != interrupt.get(key):
            raise ResumeValidationError(f"stale parameter approval binding: {key}")

    pending = assembly_out.get("pending_parameter_recommendation")
    if not isinstance(pending, dict):
        raise ResumeValidationError("active parameter approval has no pending recommendation")
    if pending.get("attempt_id") != interrupt.get("attempt_id"):
        raise ResumeValidationError("pending recommendation attempt does not match interrupt")
    if pending.get("parameter_recommendation_hash") != interrupt.get("recommendation_hash"):
        raise ResumeValidationError("pending recommendation hash does not match interrupt")
    try:
        pending_attempt = ParameterAttempt.model_validate(pending).model_dump(mode="json")
    except ValueError as exc:
        raise ResumeValidationError("pending parameter recommendation is invalid") from exc
    recommendation_ref = pending.get("parameter_recommendation_ref") or {}
    if recommendation_ref.get("sha256") != interrupt.get("recommendation_hash"):
        raise ResumeValidationError("immutable recommendation artifact hash mismatch")
    recommendation_path = _resolve_data_path_ref(
        absolute_data_dir(state), str(recommendation_ref.get("path_ref") or "")
    )
    if not recommendation_path.exists() or file_sha256(recommendation_path) != interrupt.get("recommendation_hash"):
        raise ResumeValidationError("immutable recommendation artifact is missing or changed")
    try:
        recommendation_payload = json.loads(recommendation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResumeValidationError("immutable recommendation artifact is unreadable") from exc
    artifact_attempt = recommendation_payload.get("attempt") if isinstance(recommendation_payload, dict) else None
    if not isinstance(artifact_attempt, dict):
        raise ResumeValidationError("immutable recommendation artifact has no attempt proposal")
    try:
        artifact_attempt = ParameterAttempt.model_validate(artifact_attempt).model_dump(mode="json")
    except ValueError as exc:
        raise ResumeValidationError("immutable recommendation artifact attempt is invalid") from exc
    artifact_semantic = _canonical_parameter_proposal(artifact_attempt)
    pending_semantic = _canonical_parameter_proposal(pending_attempt)
    if artifact_semantic != pending_semantic:
        raise ResumeValidationError("pending recommendation differs semantically from artifact")
    proposal_semantic_hash = _parameter_proposal_semantic_hash(artifact_semantic)
    if interrupt.get("proposal_semantic_hash") != proposal_semantic_hash:
        raise ResumeValidationError("parameter approval proposal semantic hash mismatch")

    try:
        attempts = _validate_parameter_attempt_chain(
            assembly_out.get("parameter_attempts") or []
        )
    except (ValueError, TypeError) as exc:
        raise ResumeValidationError("parameter approval attempt chain is invalid or mutated") from exc
    attempt_id = int(interrupt["attempt_id"])
    previous_id = interrupt.get("previous_attempt_id")
    if any(item.get("attempt_id") == attempt_id for item in attempts):
        raise ResumeValidationError("parameter approval gate was already resolved")
    if attempt_id == 0:
        if attempts or previous_id is not None:
            raise ResumeValidationError("initial parameter approval chain head changed")
    else:
        if not attempts or attempts[-1].get("attempt_id") != previous_id or attempt_id != int(previous_id) + 1:
            raise ResumeValidationError("parameter approval attempt chain head changed")
        previous = attempts[-1]
        if not previous.get("seal_hash"):
            raise ResumeValidationError("parameter retry previous attempt is not sealed")
        if pending.get("previous_attempt_seal_hash") != previous.get("seal_hash"):
            raise ResumeValidationError("parameter retry previous attempt seal changed")
        initial = attempts[0]
        if pending.get("inherited_initial_attempt_id") != initial.get("attempt_id"):
            raise ResumeValidationError("parameter retry initial attempt binding changed")
        if pending.get("inherited_initial_snapshot_ref") != _attempt_snapshot_ref(initial):
            raise ResumeValidationError("parameter retry initial snapshot changed")
        if pending.get("initial_recommendation_contract") != initial.get("initial_recommendation_contract"):
            raise ResumeValidationError("parameter retry initial contract changed")
        if pending.get("catalog_snapshot") != initial.get("catalog_snapshot"):
            raise ResumeValidationError("parameter retry catalog snapshot changed")
        if pending.get("inherited_evidence_payload_ref") != initial.get("evidence_payload_ref"):
            raise ResumeValidationError("parameter retry inherited evidence changed")
        if pending.get("inherited_profile_ref") != initial.get("input_profile_ref"):
            raise ResumeValidationError("parameter retry inherited profile changed")

    decision_ref = _write_attempt_json_artifact(
        state,
        attempt_id,
        "parameter_approval_decision.json",
        {
            "session_id": state["session_id"],
            "stage": "assembly",
            "gate_id": "parameter_approval",
            "decision": request.decision,
            "metadata": {key: request.metadata[key] for key in required},
            "decided_by": request.decided_by,
            "notes": list(request.notes),
        },
        artifact_type=ASSEMBLY_PARAMETER_APPROVAL_DECISION,
        method_name="assembly.parameter_approval",
    )
    Ledger(absolute_data_dir(state)).record_review_decision(
        state["session_id"],
        "assembly",
        gate_id="parameter_approval",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=decision_ref.path_ref,
        metadata={key: request.metadata[key] for key in required},
        decided_by=request.decided_by,
    )
    outputs = dict(state.get("stage_outputs") or {})
    if request.decision == "reject_parameters":
        rejected_output = {
            **assembly_out,
            "status": "parameter_recommendation_rejected_by_human",
            "reason": "parameter_recommendation_rejected_by_human",
            "active_interrupt": None,
            "pending_parameter_recommendation": None,
            "parameter_rejection": {
                "attempt_id": attempt_id,
                "task": interrupt.get("task"),
                "previous_attempt_id": previous_id,
                "recommendation_ref": recommendation_ref,
                "recommendation_hash": interrupt.get("recommendation_hash"),
                "decision_ref": decision_ref.model_dump(mode="json"),
                "decision": request.decision,
            },
        }
        rejected_output["parameter_attempt_timeline"] = build_parameter_attempt_timeline(
            rejected_output,
            status="parameter_recommendation_rejected_by_human",
            reason="parameter_recommendation_rejected_by_human",
        )
        outputs["assembly"] = rejected_output
        return {
            "current_stage": "assembly",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "parameter_recommendation_rejected_by_human",
            "stage_outputs": outputs,
        }

    approved = {
        **pending,
        "status": "recommended",
        "parameter_approval_decision_ref": decision_ref.model_dump(mode="json"),
        "parameter_approval_recommendation_hash": interrupt.get("recommendation_hash"),
    }
    # Validate the final appended representation before changing any counter.
    approved = ParameterAttempt.model_validate(approved).model_dump(mode="json")
    attempts.append(approved)
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    if interrupt.get("task") == "retry_after_execution_failure":
        retry_counts["execution"] = int(retry_counts.get("execution", 0)) + 1
    elif interrupt.get("task") == "retry_after_review_failure":
        retry_counts["review"] = int(retry_counts.get("review", 0)) + 1
    outputs["assembly"] = {
        **assembly_out,
        "status": "parameters_approved",
        "current_parameter_attempt_id": attempt_id,
        "parameter_attempts": attempts,
        "retry_counts": retry_counts,
        "active_interrupt": None,
        "pending_parameter_recommendation": None,
        "proposed_parameter_attempt_id": None,
    }
    return {
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def apply_selection_timeout(state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]) -> dict[str, Any]:
    """Commit the current assembly recommendation after a configured timeout."""
    active_interrupt = state.get("active_interrupt") or {}
    selection_payload = active_interrupt.get("recommended_assembly_selection")
    if not isinstance(selection_payload, dict) and active_interrupt.get("recommended_route") in ROUTE_VALUES:
        selection = _selection_from_route(
            str(active_interrupt.get("recommended_route")),
            source="recommendation",
            rationale=str(active_interrupt.get("recommendation_reasoning") or "timeout committed legacy recommendation"),
        )
        selection_payload = selection.model_dump(mode="json") if selection else None
    if not isinstance(selection_payload, dict):
        return _timeout_stop_update(state, request, timeout)
    try:
        route = _route_from_read_type(str(selection_payload.get("read_type")))
    except ValueError:
        return _timeout_stop_update(state, request, timeout)
    decision = {"hifi": "select_hifi", "ont": "select_ont", "cyclone": "select_cyclone"}.get(route)
    if not decision:
        return _timeout_stop_update(state, request, timeout)
    timeout_request = request.model_copy(
        update={
            "decision": decision,
            "method_name": selection_payload.get("primary_method"),
            "notes": [*request.notes, f"timeout committed recommended assembly selection: {selection_payload.get('primary_method')}"],
            "metadata": {
                **request.metadata,
                "read_type": selection_payload.get("read_type"),
                "blocked_timeout": timeout,
                "auto_accepted": True,
            },
        }
    )
    return apply_route_decision(state, timeout_request, _selection_source="timeout")


def build_assembly_candidate_set(state: TopLevelState) -> dict:
    """Build compatible and rejected assembly candidates from descriptors."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") not in {"resolved", "route_selected"}:
        return {}

    selection = _selection_from_output(assembly_out)
    if selection is None:
        return _fail_update(state, "assembly_selection is required before candidate planning")

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    planned_methods = _planned_candidate_methods(assembly_out, selection)
    missing = [method for method in planned_methods if method not in registry.methods]
    if missing:
        return _fail_update(state, f"assembly descriptors missing: {missing}")

    genome_size_prior = assembly_out.get("genome_size_prior")
    candidate_plans: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    for method_name in planned_methods:
        requires_genome_size = method_name in GENOME_SIZE_REQUIRED
        rejection_reason = None
        if not is_method_read_type_compatible(method_name, selection.read_type):
            rejection_reason = f"method_read_type_incompatible:{selection.read_type}"
        elif requires_genome_size and not genome_size_prior:
            rejection_reason = "genome_size_required"
        status = "rejected" if rejection_reason else "compatible"
        if rejection_reason:
            rejected[method_name] = rejection_reason
        candidate_plans.append(
            AssemblyCandidatePlan(
                method_name=method_name,
                status=status,
                required_inputs=["long_reads"],
                optional_inputs=["genome_survey_result", "genome_size_prior"],
                requires_genome_size=requires_genome_size,
                rejection_reason=rejection_reason,
            ).model_dump(mode="json")
        )

    return _update_assembly_output(
        state,
        {
            "status": "candidate_set_ready",
            "candidate_methods": planned_methods,
            "candidate_plans": candidate_plans,
            "rejected_candidates": rejected,
        },
    )


def _planned_candidate_methods(assembly_out: dict[str, Any], selection: AssemblySelection) -> list[str]:
    methods = list(dict.fromkeys([selection.primary_method, *ASSEMBLY_AUTO_CANDIDATES]))
    if selection.fallback_method and selection.fallback_method not in methods:
        methods.append(selection.fallback_method)
    for legacy_method in _legacy_requested_methods(assembly_out):
        if legacy_method not in methods:
            methods.append(legacy_method)
    return methods


def _legacy_requested_methods(assembly_out: dict[str, Any]) -> list[str]:
    decision = dict(assembly_out.get("method_decision") or {})
    methods: list[str] = []
    for key in ("primary", "fallback"):
        raw = decision.get(key)
        if not raw:
            continue
        try:
            method = normalize_method_id(str(raw))
        except ValueError:
            continue
        if method not in methods:
            methods.append(method)
    return methods


def select_primary_and_fallback(state: TopLevelState) -> dict:
    """Select one primary and at most one fallback candidate."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "candidate_set_ready":
        return {}

    compatible = [
        candidate["method_name"]
        for candidate in assembly_out.get("candidate_plans", [])
        if candidate.get("status") == "compatible"
    ]
    selection = _selection_from_output(assembly_out)
    if selection is None:
        return _block_for_human(state, "assembly_selection_missing")
    requested_primary = selection.primary_method
    requested_fallback = selection.fallback_method

    if requested_primary:
        if requested_primary not in ASSEMBLY_CANDIDATES:
            return _block_for_human(state, f"unknown_assembly_method: {requested_primary}")
        if requested_primary not in compatible:
            reason = assembly_out.get("rejected_candidates", {}).get(requested_primary, "method_not_compatible")
            return _block_for_human(state, reason)
        primary = requested_primary
    elif compatible:
        primary = compatible[0]
    else:
        return _block_for_human(state, "no_compatible_assembly_candidate")

    fallback = None
    if requested_fallback:
        if requested_fallback not in ASSEMBLY_CANDIDATES:
            return _block_for_human(state, f"unknown_assembly_method: {requested_fallback}")
        if requested_fallback not in compatible:
            return _block_for_human(
                state,
                assembly_out.get("rejected_candidates", {}).get(requested_fallback, "method_not_compatible"),
            )
        if requested_fallback != primary:
            fallback = requested_fallback
    else:
        fallback = next((method for method in compatible if method != primary), None)

    selected = [primary, *([fallback] if fallback else [])][:2]
    return _update_assembly_output(
        state,
        {
            "status": "selected",
            "primary_method": primary,
            "fallback_method": fallback,
            "selected_methods": selected,
        },
    )


def recommend_assembly_parameters(state: TopLevelState) -> dict:
    """Recommend and validate L2 parameters for the selected assembly method."""

    assembly_out = _assembly_output(state)
    if assembly_out.get("status") not in {
        "selected",
        "retry_parameters_after_execution_failure",
        "retry_parameters_after_review_failure",
    }:
        return {}

    method_name = _selected_parameter_method(assembly_out)
    if method_name is None:
        return {}
    task = _parameter_prompt_task(assembly_out)
    initial_snapshot = next(
        (
            dict(item) for item in assembly_out.get("parameter_attempts") or []
            if isinstance(item, dict) and item.get("attempt_id") == 0
        ),
        {},
    )
    try:
        if task == "recommend_initial":
            catalog = load_parameter_catalog(method_name)
        else:
            from app.schemas.parameter_catalog import MethodParameterCatalog
            catalog = MethodParameterCatalog.model_validate(initial_snapshot["catalog_snapshot"])
            if catalog.method_id != method_name.rsplit(".", 1)[-1]:
                return _retry_transition_failure(state, "policy_snapshot_hash_mismatch")
    except (KeyError, FileNotFoundError, ValueError) as exc:
        reason = "policy_snapshot_mismatch" if task != "recommend_initial" else f"Assembly parameter catalog is not usable: {exc}"
        return _fail_update(state, reason) if task == "recommend_initial" else _retry_transition_failure(state, reason)

    l2_keys = [key for key, spec in catalog.parameters.items() if spec.level == 2]
    if not l2_keys:
        return _fail_update(state, f"Assembly parameter catalog has no L2 parameters: {catalog.method_id}")

    attempt_id = int(
        assembly_out.get("proposed_parameter_attempt_id")
        if assembly_out.get("proposed_parameter_attempt_id") is not None
        else assembly_out.get("current_parameter_attempt_id")
        or len(assembly_out.get("parameter_attempts") or [])
    )
    previous_attempt = _previous_parameter_attempt(assembly_out, attempt_id)
    attempts_before = [
        dict(item) for item in assembly_out.get("parameter_attempts") or []
        if isinstance(item, dict)
    ]
    initial_attempt = next(
        (item for item in attempts_before if item.get("attempt_id") == 0), {}
    )
    if task != "recommend_initial":
        if not previous_attempt or previous_attempt.get("attempt_id") != attempt_id - 1:
            return _retry_transition_failure(state, "missing_failure_signal")
        if not previous_attempt.get("seal_hash"):
            return _retry_transition_failure(state, "unsealed_previous_attempt")
        retry_request = dict(assembly_out.get("parameter_retry_request") or {})
        if retry_request.get("previous_attempt_id") != previous_attempt.get("attempt_id"):
            return _retry_transition_failure(state, "missing_failure_signal")
        if retry_request.get("previous_attempt_seal_hash") != previous_attempt.get("seal_hash"):
            return _retry_transition_failure(state, "previous_attempt_seal_hash_mismatch")
    full_profile = build_full_parameter_profile(
        state,
        assembly_out=assembly_out,
        previous_attempt=previous_attempt,
        assembly_review=assembly_out.get("review_failure"),
    )
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]

    try:
        prompt_contract = _parameter_prompt_contract(method_name, task, initial_snapshot)
        prompt_payload = build_prompt_payload(
            method_id=catalog.method_id,
            catalog=catalog,
            full_profile=full_profile,
            task=task,
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=data_dir,
            initial_attempt_snapshot=initial_attempt,
        )
    except Exception as exc:
        return _fail_update(state, f"Assembly parameter prompt payload failed: {exc}")

    # Append replay inputs to the artifact index.
    replay_artifacts = []
    profile_ref = prompt_payload.get("input_profile_ref")
    prompt_ref = prompt_payload.get("prompt_payload_ref")
    retrieval_query_ref = prompt_payload.get("retrieval_query_ref")
    retrieval_trace_ref = prompt_payload.get("retrieval_trace_ref")
    # Backward-compatible read for pre-lean sealed payloads still carrying nested refs.
    if not retrieval_query_ref or not retrieval_trace_ref:
        nested = prompt_payload.get("case_selection") or prompt_payload.get("case_retrieval") or {}
        retrieval_query_ref = retrieval_query_ref or nested.get("query_ref")
        retrieval_trace_ref = retrieval_trace_ref or nested.get("trace_ref")
    evidence_payload_ref = prompt_payload.get("evidence_payload_ref")
    case_selection = (
        prompt_payload.get("case_selection")
        or prompt_payload.get("case_retrieval")
        or {}
    )
    retrieval_query_hash = (
        prompt_payload.get("retrieval_query_hash")
        or case_selection.get("query_hash")
    )
    retrieval_trace_hash = (
        prompt_payload.get("retrieval_trace_hash")
        or case_selection.get("trace_hash")
    )
    artifact_specs = (
        (profile_ref, ASSEMBLY_PARAMETER_INPUT_PROFILE),
        (prompt_ref, ASSEMBLY_PARAMETER_PROMPT_PAYLOAD),
        (retrieval_query_ref, ASSEMBLY_PARAMETER_RETRIEVAL_QUERY),
        (retrieval_trace_ref, ASSEMBLY_PARAMETER_RETRIEVAL_TRACE),
        (evidence_payload_ref, ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD),
    )
    resolved_paths: dict[str, Path] = {}
    for ref, artifact_type in artifact_specs:
        if not ref:
            continue
        artifact_path = _resolve_data_path_ref(data_dir, str(ref))
        if artifact_path and artifact_path.exists():
            resolved_paths[artifact_type] = artifact_path
            replay_artifacts.append(
                ArtifactMeta.from_file(
                    artifact_path,
                    stage="assembly",
                    method_name="assembly.parameter_recommender",
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt_id},
                )
            )
    if replay_artifacts:
        append_artifact_records(data_dir, session_id, replay_artifacts)
    prompt_path = resolved_paths.get(ASSEMBLY_PARAMETER_PROMPT_PAYLOAD)

    decision = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id=session_id,
            prompt_contract=prompt_contract,
            prompt_payload=prompt_payload,
            allow_default_on_error=bool(
                _parameter_recommendation_config(state, assembly_out).get("allow_model_unavailable_defaults", False)
            ),
        )
    )
    llm_response_ref = _write_attempt_json_artifact(
        state,
        attempt_id,
        "llm_response.json",
        {
            "raw_response": decision.raw_response,
            "parsed": decision.parsed,
            "valid": decision.valid,
            "error": decision.error,
            "model_id": decision.model_id,
        },
        artifact_type=ASSEMBLY_PARAMETER_LLM_RESPONSE,
        method_name="assembly.parameter_recommender",
    )

    recommended_params = dict((decision.parsed or {}).get("params") or {}) if decision.valid else {}
    if decision.valid:
        validation = validate_recommended_parameters(
            method_id=catalog.method_id,
            catalog=catalog,
            recommended_params=recommended_params,
            readonly_facts=flatten_profile_facts(full_profile),
            rules=prompt_payload.get("rules") or {},
            full_profile=full_profile,
        )
        evidence_validation = validate_parameter_evidence(
            parameter_evidence=(decision.parsed or {}).get("parameter_evidence"),
            recommended_params=recommended_params,
            prompt_payload=prompt_payload,
            declared_selected_case_ids=(decision.parsed or {}).get("selected_case_ids"),
        )
        validation["violations"].extend(evidence_validation["violations"])
        validation["warnings"].extend(evidence_validation["warnings"])
        validation["valid"] = not validation["violations"]
    else:
        validation = {
            "valid": False,
            "method_id": catalog.method_id,
            "violations": [
                {
                    "param": None,
                    "value": decision.raw_response,
                    "reason": decision.error or "parameter recommendation failed",
                    "severity": "critical",
                }
            ],
            "warnings": [],
        }
    validation_ref = _write_attempt_json_artifact(
        state,
        attempt_id,
        "validation.json",
        validation,
        artifact_type=ASSEMBLY_PARAMETER_VALIDATION,
        method_name="assembly.parameter_validator",
    )

    parsed = decision.parsed or {}
    if uses_hifiasm_methods_contract(prompt_payload):
        selected_case_ids = list(parsed.get("selected_case_ids") or [])
    elif "selected_case_ids" in parsed:
        selected_case_ids = list(parsed.get("selected_case_ids") or [])
    else:
        selected_case_ids = list(
            prompt_payload.get("case_selection", {}).get("selected_case_ids") or []
        )
    previous_rendered = dict(
        previous_attempt.get("rendered_parameters")
        or previous_attempt.get("recommended_params")
        or {}
    )
    proposed_parameter_changes = {
        key: {"from": previous_rendered.get(key), "to": value}
        for key, value in recommended_params.items()
        if previous_attempt and previous_rendered.get(key) != value
    }
    attempt = ParameterAttempt(
        attempt_id=attempt_id,
        method_id=method_name,
        status="recommended" if validation.get("valid") else "validation_failed",
        task=task,
        input_profile_ref=ArtifactRef(
            path_ref=str(prompt_payload["input_profile_ref"]),
            sha256=str(prompt_payload["input_profile_hash"]),
            artifact_type="assembly.parameter.input_profile",
        ),
        prompt_payload_ref=ArtifactRef(
            path_ref=str(prompt_payload["prompt_payload_ref"]),
            sha256=(
                file_sha256(prompt_path)
                if prompt_path and prompt_path.exists()
                else str(prompt_payload["prompt_payload_hash"])
            ),
            artifact_type="assembly.parameter.prompt_payload",
        ),
        retrieval_backend=(
            case_selection.get("retrieval_backend")
            if case_selection.get("retrieval_backend") == "hifiasm_numeric"
            else None
        ),
        retrieval_query_id=(
            case_selection.get("query_id")
            if case_selection.get("retrieval_backend") == "hifiasm_numeric"
            else None
        ),
        retrieval_query_ref=(
            ArtifactRef(
                path_ref=str(retrieval_query_ref),
                sha256=str(retrieval_query_hash),
                artifact_type=ASSEMBLY_PARAMETER_RETRIEVAL_QUERY,
            )
            if retrieval_query_ref and retrieval_query_hash
            else None
        ),
        retrieval_trace_ref=(
            ArtifactRef(
                path_ref=str(retrieval_trace_ref),
                sha256=str(retrieval_trace_hash),
                artifact_type=ASSEMBLY_PARAMETER_RETRIEVAL_TRACE,
            )
            if retrieval_trace_ref and retrieval_trace_hash
            else None
        ),
        evidence_payload_ref=(
            ArtifactRef(
                path_ref=str(evidence_payload_ref),
                sha256=str(prompt_payload["evidence_payload_hash"]),
                artifact_type=ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD,
            )
            if evidence_payload_ref
            else None
        ),
        retrieval_candidate_case_ids=list(
            case_selection.get("candidate_case_ids") or []
            if case_selection.get("retrieval_backend") == "hifiasm_numeric"
            else []
        ),
        retrieval_eligible_case_count=(
            case_selection.get("eligible_case_count")
            if case_selection.get("retrieval_backend") == "hifiasm_numeric"
            else None
        ),
        retrieval_warning=(
            case_selection.get("warning")
            if case_selection.get("retrieval_backend") == "hifiasm_numeric"
            else None
        ),
        llm_response_ref=llm_response_ref,
        validation_ref=validation_ref,
        catalog_ref=f"descriptors/knowledge_packs/assembly/methods/{catalog.method_id}/params.yaml",
        catalog_snapshot=catalog.model_dump(mode="json"),
        knowledge_pack_ref="descriptors/knowledge_packs/assembly/parameter_recommender",
        selected_case_ids=selected_case_ids,
        recommended_params=recommended_params,
        omitted_parameters=list(parsed.get("omitted_parameters") or []),
        parameter_evidence=dict(parsed.get("parameter_evidence") or {}),
        case_assessments=list(parsed.get("case_assessments") or []),
        parameter_decisions=dict(parsed.get("parameter_decisions") or {}),
        proposed_parameter_changes=proposed_parameter_changes,
        recommendation_summary=parsed.get("summary"),
        validation=validation,
        reasoning=parsed.get("reasoning"),
        previous_attempt_id=previous_attempt.get("attempt_id") if previous_attempt else None,
        failure_signature=(
            dict((prompt_payload.get("retry_evidence") or {}))
            if task != "recommend_initial"
            else None
        ),
        failure_log_tail=assembly_out.get("execution_failure_log_tail"),
        review_failure=assembly_out.get("review_failure"),
        attempt_kind="initial" if task == "recommend_initial" else "retry",
        retry_origin=(
            "assembly_execution" if task == "retry_after_execution_failure"
            else "assembly_review" if task == "retry_after_review_failure" else None
        ),
        initial_recommendation_contract=(
            "curated_initial_v1"
            if prompt_contract == "hifiasm_methods_v1"
            else "legacy_initial"
        ) if task == "recommend_initial" else initial_attempt.get("initial_recommendation_contract"),
        inherited_initial_attempt_id=0 if task != "recommend_initial" else None,
        inherited_initial_snapshot_ref=(
            _attempt_snapshot_ref(initial_attempt) if task != "recommend_initial" else None
        ),
        inherited_evidence_payload_ref=(
            initial_attempt.get("evidence_payload_ref") if task != "recommend_initial" else None
        ),
        inherited_profile_ref=(
            initial_attempt.get("input_profile_ref") if task != "recommend_initial" else None
        ),
        previous_attempt_seal_hash=(
            previous_attempt.get("seal_hash") if task != "recommend_initial" else None
        ),
    )
    retry_disposition = (parsed.get("recommendation") or {}).get("retry_disposition")
    if validation.get("valid") and retry_disposition == "stop":
        status = "parameter_retry_stopped"
    elif validation.get("valid") and retry_disposition == "expert_review":
        status = "parameter_retry_expert_review"
    else:
        status = "blocked_human" if validation.get("valid") else "parameter_retry_validation_failed" if task != "recommend_initial" else "parameter_validation_failed"
    attempt_payload = attempt.model_dump(mode="json")
    recommendation_ref = _write_attempt_json_artifact(
        state,
        attempt_id,
        "validated_recommendation.json",
        {"attempt": attempt_payload, "validation": validation},
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION,
        method_name="assembly.parameter_recommender",
    )
    attempt_payload["parameter_recommendation_ref"] = recommendation_ref.model_dump(mode="json")
    attempt_payload["parameter_recommendation_hash"] = recommendation_ref.sha256
    update = {
        "status": status,
        "proposed_parameter_attempt_id": attempt_id,
    }
    if not validation.get("valid"):
        update["reason"] = "Assembly parameter recommendation failed validation"
        update["failed_parameter_recommendation"] = attempt_payload
    elif retry_disposition == "stop":
        update["reason"] = parsed.get("summary") or "Parameter retry stopped by evidence assessment"
    elif retry_disposition == "expert_review":
        update["reason"] = parsed.get("summary") or "Parameter retry requires expert review"
    else:
        parameter_diff = proposed_parameter_changes if previous_attempt else {
            key: {"from": None, "to": value}
            for key, value in recommended_params.items()
        }
        interrupt = _parameter_approval_interrupt(
            attempt=attempt_payload,
            recommendation_ref=recommendation_ref,
            parameter_diff=parameter_diff,
        )
        request_ref = _write_attempt_json_artifact(
            state,
            attempt_id,
            "parameter_approval_request.json",
            interrupt,
            artifact_type="assembly.parameter_approval.request",
            method_name="assembly.parameter_approval",
        )
        interrupt["review_request_artifact_ref"] = request_ref.model_dump(mode="json")
        Ledger(data_dir).record_review_requested(
            session_id,
            "assembly",
            reason="assembly_parameter_approval_required",
            candidates=[
                {
                    "attempt_id": attempt_id,
                    "task": task,
                    "previous_attempt_id": attempt.previous_attempt_id,
                    "recommendation_hash": recommendation_ref.sha256,
                }
            ],
            review_request_artifact_ref=request_ref.path_ref,
            auto_decidable=False,
            pending_human_confirmation=True,
        )
        update.update(
            {
                "block_reason": "assembly_parameter_approval_required",
                "active_interrupt": interrupt,
                "pending_parameter_recommendation": attempt_payload,
            }
        )
    return _update_assembly_output(state, update)


def plan_candidate_attempts(state: TopLevelState) -> dict:
    """Write candidate bundles and select the optional local Flye route."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") not in {"selected", "parameters_recommended", "parameters_approved"}:
        return {}

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    parameter_attempt_id = int(assembly_out.get("current_parameter_attempt_id") or 0)
    attempt_scope = f"parameter_attempt_{parameter_attempt_id}"
    candidate_dir = data_dir / "artifacts" / "assembly" / session_id / attempt_scope / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    local_flye = assembly_out.get("execution_mode") == "local" and assembly_out.get("primary_method") == FLYE_METHOD
    local_nextdenovo = (
        assembly_out.get("execution_mode") == "local"
        and assembly_out.get("primary_method") == NEXTDENOVO_METHOD
    )
    local_hifiasm = (
        assembly_out.get("execution_mode") == "local"
        and assembly_out.get("primary_method") == HIFIASM_METHOD
    )
    local_hifiasm_ont = (
        assembly_out.get("execution_mode") == "local"
        and assembly_out.get("primary_method") == HIFIASM_ONT_METHOD
    )
    candidate_refs: list[str] = []
    artifacts: list[ArtifactMeta] = []
    selected_plans = []
    selection = _selection_from_output(assembly_out)
    for method_name in assembly_out["selected_methods"]:
        short_name = method_name.rsplit(".", 1)[-1]
        fasta_path = data_dir / "artifacts" / "assembly" / session_id / attempt_scope / short_name / "draft_assembly.fasta"
        if method_name == NEXTDENOVO_METHOD:
            log_path = data_dir / "work" / "assembly" / session_id / attempt_scope / "nextdenovo" / "run.cfg"
        elif method_name == HIFIASM_METHOD:
            log_path = data_dir / "work" / "assembly" / session_id / attempt_scope / "hifiasm" / "run_hifiasm.sh"
        elif method_name == HIFIASM_ONT_METHOD:
            log_path = data_dir / "work" / "assembly" / session_id / attempt_scope / "hifiasm_ont" / "run_hifiasm_ont.sh"
        else:
            log_path = data_dir / "work" / "assembly" / session_id / attempt_scope / short_name / "flye_out" / "flye.log"
        bundle_path = candidate_dir / f"{short_name}_candidate.json"
        execution_enabled = (
            (local_flye and method_name == FLYE_METHOD)
            or (local_nextdenovo and method_name == NEXTDENOVO_METHOD)
            or (local_hifiasm and method_name == HIFIASM_METHOD)
            or (local_hifiasm_ont and method_name == HIFIASM_ONT_METHOD)
        )
        method_graph_requested = execution_enabled
        bundle = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=f"{session_id}:{method_name}:{attempt_scope}",
            method_name=method_name,
            status="planned",
            input_ref=assembly_out["input_ref"],
            input_type=assembly_out["input_type"],
            genome_size_prior=assembly_out.get("genome_size_prior"),
            expected_outputs={"draft_assembly_fasta": str(fasta_path)},
            logs={"primary_log": str(log_path)},
            metadata={
                "genome_survey_result_ref": assembly_out.get("genome_survey_result_ref"),
                "read_type_prior": assembly_out.get("read_type_prior"),
                "assembly_selection": selection.model_dump(mode="json") if selection else None,
                "method_read_type": method_read_type(method_name, selection.read_type) if selection else None,
                "execution_enabled": execution_enabled,
                "method_graph_requested": method_graph_requested,
                "draft_assembly_ref_status": "pending_execution" if execution_enabled else "planned_path_not_materialized",
            },
        )
        bundle_path.write_text(
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        candidate_refs.append(str(bundle_path))
        artifact = ArtifactMeta.from_file(
            bundle_path,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            metadata={"candidate_id": bundle.candidate_id, "execution_enabled": execution_enabled},
        )
        artifacts.append(artifact)
        selected_plans.append(
            AssemblyCandidatePlan(
                method_name=method_name,
                status="selected",
                role="primary" if method_name == assembly_out["primary_method"] else "fallback",
                required_inputs=["long_reads"],
                optional_inputs=["genome_survey_result", "genome_size_prior"],
                requires_genome_size=method_name in GENOME_SIZE_REQUIRED,
                candidate_ref=str(bundle_path),
            ).model_dump(mode="json")
        )

    append_artifact_records(data_dir, session_id, artifacts)
    if local_flye:
        status = "flye_execution_planned"
    elif local_nextdenovo:
        status = "nextdenovo_execution_planned"
    elif local_hifiasm:
        status = "hifiasm_execution_planned"
    elif local_hifiasm_ont:
        status = "hifiasm_ont_execution_planned"
    else:
        status = "planned"
    return _update_assembly_output(
        state,
        {
            "status": status,
            "selected_candidate_refs": candidate_refs,
            "candidate_artifact_refs": [str(artifact.file_path) for artifact in artifacts],
            "selected_candidate_plans": selected_plans,
            "substeps": {
                **dict(assembly_out.get("substeps") or {}),
                "flye_assembly": {
                    "name": "flye_assembly",
                    "status": "planned" if local_flye else "skipped",
                    "method_name": FLYE_METHOD,
                    "skip_reason": None if local_flye else "local_execution_not_enabled",
                },
                "nextdenovo_assembly": {
                    "name": "nextdenovo_assembly",
                    "status": "planned" if local_nextdenovo else "skipped",
                    "method_name": NEXTDENOVO_METHOD,
                    "skip_reason": None if local_nextdenovo else "local_execution_not_enabled",
                },
                "hifiasm_assembly": {
                    "name": "hifiasm_assembly",
                    "status": "planned" if local_hifiasm else "skipped",
                    "method_name": HIFIASM_METHOD,
                    "skip_reason": None if local_hifiasm else "local_execution_not_enabled",
                },
                "hifiasm_ont_assembly": {
                    "name": "hifiasm_ont_assembly",
                    "status": "planned" if local_hifiasm_ont else "skipped",
                    "method_name": HIFIASM_ONT_METHOD,
                    "skip_reason": None if local_hifiasm_ont else "local_execution_not_enabled",
                },
            },
        },
    )


def execution_retry_gate(state: TopLevelState) -> dict:
    """Prepare a new parameter attempt after command execution failure."""

    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "failed" or not assembly_out.get("parameter_attempts"):
        return {}
    attempt_id = int(assembly_out.get("current_parameter_attempt_id") or 0)
    attempts = [dict(item) for item in assembly_out.get("parameter_attempts") or [] if isinstance(item, dict)]
    previous = next((item for item in attempts if item.get("attempt_id") == attempt_id), attempts[-1] if attempts else {})
    execution_result = dict(previous.get("execution_result") or {})
    if not execution_result:
        return {}
    failure_log_tail = _failure_log_tail(execution_result)
    if failure_log_tail:
        previous["failure_log_tail"] = failure_log_tail
        previous["stderr_tail"] = _tail_from_path(execution_result.get("stderr_path"))
        previous["stdout_tail"] = _tail_from_path(execution_result.get("stdout_path"))
    previous = _seal_parameter_attempt(previous)
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    used = int(retry_counts.get("execution", 0))
    max_retries = _parameter_recommendation_config(state, assembly_out).get("max_execution_retries", 1)
    updated_attempts = [previous if item.get("attempt_id") == attempt_id else item for item in attempts]
    if used >= int(max_retries):
        exhausted = {
            "status": "execution_parameter_retries_exhausted",
            "reason": "execution_retry_budget_exhausted",
            "parameter_attempts": updated_attempts,
            "execution_failure_log_tail": failure_log_tail,
        }
        exhausted["parameter_attempt_timeline"] = build_parameter_attempt_timeline(
            {**assembly_out, **exhausted},
            status="execution_parameter_retries_exhausted",
            reason="execution_retry_budget_exhausted",
        )
        return _update_assembly_output(
            state,
            exhausted,
        )
    return _update_assembly_output(
        state,
        {
            "status": "retry_parameters_after_execution_failure",
            "proposed_parameter_attempt_id": attempt_id + 1,
            "previous_parameter_attempt": previous,
            "parameter_attempts": updated_attempts,
            "retry_counts": retry_counts,
            "execution_failure_log_tail": failure_log_tail,
            "parameter_retry_request": {
                "origin": "assembly_execution",
                "previous_attempt_id": attempt_id,
                "previous_attempt_seal_hash": previous["seal_hash"],
                "execution_result": execution_result,
            },
        },
    )


def parameter_quality_gate(state: TopLevelState) -> dict:
    """Classify review metrics and prepare a quality-replan attempt if needed."""

    assembly_out = _assembly_output(state)
    review_out = (state.get("stage_outputs") or {}).get("assembly_review") or {}
    metrics = dict(review_out.get("metrics") or review_out.get("summary") or assembly_out.get("review_metrics") or {})
    diagnosis = _review_failure_diagnosis(metrics)
    if diagnosis["passed"]:
        current_id = int(assembly_out.get("current_parameter_attempt_id") or 0)
        completed = _previous_parameter_attempt(assembly_out, current_id + 1)
        completed = {**completed, "review_outcome": diagnosis}
        completed = _seal_parameter_attempt(completed)
        attempts = [
            completed if item.get("attempt_id") == current_id else item
            for item in assembly_out.get("parameter_attempts") or []
            if isinstance(item, dict)
        ]
        passed = {
            "status": "parameter_quality_passed",
            "quality_gate": diagnosis,
            "parameter_attempts": attempts,
        }
        passed["parameter_attempt_timeline"] = build_parameter_attempt_timeline(
            {**assembly_out, **passed}, status="assembly_succeeded", reason="completed"
        )
        return _update_assembly_output(
            state,
            passed,
        )
    current_id = int(assembly_out.get("current_parameter_attempt_id") or 0)
    previous = _previous_parameter_attempt(assembly_out, current_id + 1)
    review_failure_ref = _write_attempt_json_artifact(
        state,
        current_id,
        "review_failure.json",
        {
            "attempt_id": current_id,
            "next_attempt_id": current_id + 1,
            "diagnosis": diagnosis,
            "review_metrics": metrics,
        },
        artifact_type=ASSEMBLY_PARAMETER_REVIEW_FAILURE,
        method_name="assembly.parameter_quality_gate",
    )
    previous = {
        **previous,
        "review_failure": diagnosis,
        "review_outcome": diagnosis,
        "review_failure_ref": review_failure_ref.model_dump(mode="json"),
    }
    previous = _seal_parameter_attempt(previous)
    attempts = [dict(item) for item in assembly_out.get("parameter_attempts") or [] if isinstance(item, dict)]
    updated_attempts = [previous if item.get("attempt_id") == current_id else item for item in attempts]
    if previous and not any(item.get("attempt_id") == current_id for item in updated_attempts):
        updated_attempts.append(previous)
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    used = int(retry_counts.get("review", 0))
    max_retries = _parameter_recommendation_config(state, assembly_out).get("max_review_retries", 2)
    if used >= int(max_retries):
        exhausted = {
            "status": "review_parameter_retries_exhausted",
            "quality_gate": diagnosis,
            "review_failure": diagnosis,
            "parameter_attempts": updated_attempts,
            "reason": "review_retry_budget_exhausted",
        }
        exhausted["parameter_attempt_timeline"] = build_parameter_attempt_timeline(
            {**assembly_out, **exhausted},
            status="review_parameter_retries_exhausted",
            reason="review_retry_budget_exhausted",
        )
        return _update_assembly_output(
            state,
            exhausted,
        )
    return _update_assembly_output(
        state,
        {
            "status": "retry_parameters_after_review_failure",
            "proposed_parameter_attempt_id": current_id + 1,
            "previous_parameter_attempt": previous,
            "parameter_attempts": updated_attempts,
            "retry_counts": retry_counts,
            "review_failure": diagnosis,
            "quality_gate": diagnosis,
            "parameter_retry_request": {
                "origin": "assembly_review",
                "previous_attempt_id": current_id,
                "previous_attempt_seal_hash": previous["seal_hash"],
                "review_failure": diagnosis,
            },
        },
    )


def commit_assembly_plan(state: TopLevelState) -> dict:
    """Route compact assembly candidate refs to assembly_review."""
    assembly_out = _assembly_output(state)
    committed_keys = [
        "artifact_refs",
        "candidate_artifact_refs",
        "candidate_assembly_refs",
        "candidate_methods",
        "candidate_plans",
        "draft_assembly_ref",
        "executed_candidate_refs",
        "fallback_method",
        "genome_size_prior",
        "genome_survey_result_ref",
        "input_ref",
        "input_source",
        "input_type",
        "assembly_selection",
        "normalized_read_type",
        "read_type_prior",
        "method_application_notes",
        "current_parameter_attempt_id",
        "parameter_attempts",
        "primary_method",
        "primary_long_read_platform",
        "rejected_candidates",
        "selected_candidate_plans",
        "selected_candidate_refs",
        "selected_methods",
        "status",
        "retry_counts",
        "substeps",
    ]
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {key: assembly_out[key] for key in committed_keys if key in assembly_out}
    trace = [*state.get("route_trace", []), "assembly_stage"]
    return {
        "current_stage": next_stage_name("assembly"),
        "supervision_state": "running_stage",
        "route_trace": trace,
        "stage_outputs": outputs,
    }


def fail_assembly(state: TopLevelState) -> dict:
    """Stop workflow on assembly planning failure or preflight block."""
    assembly_out = _assembly_output(state)
    status = assembly_out.get("status", "failed")
    default_reason = {
        "blocked_env": "assembly blocked on missing conda environment",
        "missing_tool": "assembly blocked on missing tool",
        "blocked_param": "assembly blocked on missing Flye parameters",
    }.get(status, "assembly failed")
    return {
        "current_stage": "assembly",
        "supervision_state": "failed_terminal",
        "last_error": assembly_out.get("reason", default_reason),
    }


def block_assembly_for_human(state: TopLevelState) -> dict:
    """Pause workflow for a human assembly planning decision."""
    assembly_out = _assembly_output(state)
    reason = assembly_out.get("block_reason", "assembly decision required")
    interrupt = dict(assembly_out.get("active_interrupt") or {})
    if interrupt:
        return {
            "current_stage": "assembly",
            "supervision_state": "blocked_human",
            "active_interrupt": interrupt,
        }
    return {
        "current_stage": "assembly",
        "supervision_state": "blocked_human",
        "active_interrupt": {
            "stage": "assembly",
            "reason": reason,
            "candidate_methods": assembly_out.get("candidate_methods", []),
            "rejected_candidates": assembly_out.get("rejected_candidates", {}),
        },
    }


def _block_for_human(state: TopLevelState, reason: str) -> dict:
    return _update_assembly_output(state, {"status": "blocked_human", "block_reason": reason})


def _block_for_route_review(state: TopLevelState, reason: str) -> dict:
    from datetime import datetime, timezone
    from app.review.base import ReviewPayload
    from app.review.policy import evaluate

    assembly_out = _assembly_output(state)
    profile = dict(assembly_out.get("input_profile") or {})
    recommended_selection = dict(assembly_out.get("recommended_assembly_selection") or {})

    # Construct ReviewPayload for policy evaluation
    payload = ReviewPayload(
        stage="assembly",
        gate_id="route_selection",
        reason=reason,
        decisions_allowed=["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
        signals=[],
        notes=[
            f"Recommended read_type: {recommended_selection.get('read_type')}",
            f"Recommended method: {recommended_selection.get('primary_method')}",
            f"Reasoning: {profile.get('recommendation_reasoning')}",
        ] + profile.get("uncertainty_notes", []),
        auto_decidable=False,
    )

    # Evaluate policy
    outcome = evaluate("assembly", payload, assembly_out.get("review_policy"))

    interrupt = {
        "stage": "assembly",
        "gate_id": "route_selection",
        "reason": reason,
        "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
        "recommended_route": profile.get("recommended_route"),
        "recommended_assembly_selection": recommended_selection,
        "recommendation_reasoning": profile.get("recommendation_reasoning"),
        "uncertainty_notes": profile.get("uncertainty_notes", []),
        "available_routes": _available_routes(assembly_out),
        "available_read_types": sorted({route for route in ["cyclone", "ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi"]}),
        "compatible_methods": {
            read_type: compatible_methods(read_type) for read_type in ["cyclone", "ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi"]
        },
        "blocked_at": datetime.now(timezone.utc).isoformat(),
        "review_policy": assembly_out.get("review_policy") or {"mode": outcome.mode},
        "review_policy_outcome": outcome.model_dump(mode="json"),
    }

    return _update_assembly_output(
        state,
        {
            "status": "blocked_human",
            "block_reason": reason,
            "active_interrupt": interrupt,
            "review_policy_outcome": outcome.model_dump(mode="json"),
        },
    )


def _available_routes(assembly_out: dict[str, Any]) -> list[str]:
    routes = []
    if assembly_out.get("ont_reads_ref"):
        routes.extend(["cyclone", "ont"])
    if assembly_out.get("hifi_reads_ref") or assembly_out.get("has_hifi"):
        routes.append("hifi")
    return _unique_refs(routes)


def _selection_from_output(assembly_out: dict[str, Any]) -> AssemblySelection | None:
    payload = assembly_out.get("assembly_selection")
    if isinstance(payload, AssemblySelection):
        return payload
    if isinstance(payload, dict):
        try:
            return AssemblySelection.model_validate(payload)
        except ValueError:
            return None
    legacy = dict(assembly_out.get("method_decision") or {})
    legacy_primary = legacy.get("primary")
    read_type = resolve_read_type(
        ("normalized_read_type", assembly_out.get("normalized_read_type")),
        ("read_type", assembly_out.get("read_type")),
        ("explicit_route", assembly_out.get("explicit_route")),
        ("read_type_prior", _normalized_read_type_from_prior(assembly_out.get("read_type_prior"))),
        context="assembly.selection_from_output",
    )
    if legacy_primary and read_type:
        try:
            return AssemblySelection(
                read_type=read_type,
                primary_method=str(legacy_primary),
                fallback_method=legacy.get("fallback"),
                source="run_config" if _is_run_config_explicit_method_decision(legacy) else "recommendation",
                rationale="legacy method_decision adapted to assembly_selection",
                metadata={"legacy_method_decision": legacy},
            )
        except ValueError:
            return None
    return None


def _selection_from_route(route: str | None, *, source: str, rationale: str) -> AssemblySelection | None:
    read_type = normalize_read_type(route)
    if not read_type:
        return None
    return AssemblySelection(
        read_type=read_type,
        primary_method=default_primary_method(read_type),
        source=source,
        rationale=rationale,
        metadata={"legacy_route": route},
    )


def _legacy_method_decision(selection: AssemblySelection) -> dict[str, str]:
    source = "run_config_explicit" if selection.source == "run_config" else "assembly_selection"
    payload = {"primary": selection.primary_method, "source": source}
    if selection.fallback_method:
        payload["fallback"] = selection.fallback_method
    return payload


def _resolve_method_decision(existing: Any, route_derived: dict[str, str]) -> dict[str, str]:
    existing_decision = dict(existing or {})
    if _is_run_config_explicit_method_decision(existing_decision):
        return existing_decision
    return route_derived


def _is_run_config_explicit_method_decision(decision: Any) -> bool:
    return isinstance(decision, dict) and decision.get("source") == "run_config_explicit"


def _requested_read_type(request: ResumeReviewRequest, assembly_out: dict[str, Any], selected_route: str) -> tuple[str, str]:
    route_read_type = normalize_read_type(selected_route)
    if route_read_type is None:
        raise ResumeValidationError(f"unsupported assembly route: {selected_route}")

    for source, raw_value in (
        ("metadata.read_type", request.metadata.get("read_type")),
        ("metadata.shared_read_type", request.metadata.get("shared_read_type")),
    ):
        if raw_value is None:
            continue
        read_type = normalize_read_type(raw_value)
        if read_type is None:
            raise ResumeValidationError(f"unsupported assembly {source}: {raw_value!r}")
        if _route_from_read_type(read_type) != selected_route:
            raise ResumeValidationError(
                f"assembly selection {request.decision!r} maps to route {selected_route!r}, "
                f"but {source}={raw_value!r} normalizes to read_type {read_type!r}"
            )
        return read_type, source

    prior_read_type = resolve_read_type(
        ("read_type_prior", _normalized_read_type_from_prior(assembly_out.get("read_type_prior"))),
        context="assembly.requested_read_type",
    )
    if prior_read_type and _route_from_read_type(prior_read_type) == selected_route:
        return prior_read_type, "read_type_prior"
    return route_read_type, "selected_route"


def _requested_method(request: ResumeReviewRequest, assembly_out: dict[str, Any], read_type: str) -> str:
    try:
        if request.method_name:
            return normalize_method_id(request.method_name)
        recommended = dict(assembly_out.get("recommended_assembly_selection") or {})
        recommended_method = recommended.get("primary_method")
        if recommended_method and recommended.get("read_type") == read_type:
            return normalize_method_id(str(recommended_method))
        existing = _selection_from_output(assembly_out)
        if existing and existing.read_type == read_type:
            return existing.primary_method
        return default_primary_method(read_type)
    except ValueError as exc:
        raise ResumeValidationError(str(exc)) from exc


def _timeout_stop_update(state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]) -> dict[str, Any]:
    return timeout_terminal_update(state, request, timeout)


def _assembly_review_policy_mode(assembly_out: dict[str, Any]) -> str:
    policy = dict(assembly_out.get("review_policy") or {})
    return str(policy.get("mode") or "strict")


def _write_assembly_json_artifact(
    state: TopLevelState,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
) -> str:
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    artifact_dir = data_dir / "artifacts" / "assembly" / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        path,
        stage="assembly",
        method_name=method_name,
        artifact_type=artifact_type,
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return str(path)


def _write_attempt_json_artifact(
    state: TopLevelState,
    attempt_id: int,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    artifact_dir = data_dir / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / filename
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise ResumeValidationError(
                f"immutable parameter-attempt artifact already exists with different content: {path}"
            )
    else:
        path.write_text(serialized, encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        path,
        stage="assembly",
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={"attempt_id": attempt_id},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return ArtifactRef(
        path_ref=f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/{filename}",
        sha256=file_sha256(path),
        artifact_type=artifact_type,
    )


def _selected_parameter_method(assembly_out: dict[str, Any]) -> str | None:
    if (
        assembly_out.get("status") == "selected"
        and assembly_out.get("execution_mode") != "local"
    ):
        return None
    primary = assembly_out.get("primary_method")
    if isinstance(primary, str):
        return str(primary)
    selection = _selection_from_output(assembly_out)
    if selection:
        return selection.primary_method
    return None


def _parameter_prompt_task(assembly_out: dict[str, Any]) -> str:
    status = assembly_out.get("status")
    if status == "retry_parameters_after_execution_failure":
        return "retry_after_execution_failure"
    if status == "retry_parameters_after_review_failure":
        return "retry_after_review_failure"
    return "recommend_initial"


def _previous_parameter_attempt(assembly_out: dict[str, Any], attempt_id: int) -> dict[str, Any]:
    explicit = assembly_out.get("previous_parameter_attempt")
    if isinstance(explicit, dict):
        return explicit
    attempts = [item for item in assembly_out.get("parameter_attempts") or [] if isinstance(item, dict)]
    if not attempts:
        return {}
    return next((item for item in attempts if item.get("attempt_id") == attempt_id - 1), attempts[-1])


def _parameter_approval_interrupt(
    *,
    attempt: dict[str, Any],
    recommendation_ref: ArtifactRef,
    parameter_diff: dict[str, Any],
) -> dict[str, Any]:
    failure_facts: dict[str, Any] = {}
    if attempt.get("failure_signature"):
        failure_facts["signals"] = attempt["failure_signature"]
    if attempt.get("failure_log_tail"):
        failure_facts["failure_log_tail"] = attempt["failure_log_tail"]
    if attempt.get("review_failure"):
        failure_facts["review_failure"] = attempt["review_failure"]
    return {
        "stage": "assembly",
        "gate_id": "parameter_approval",
        "reason": "assembly_parameter_approval_required",
        "attempt_id": attempt["attempt_id"],
        "task": attempt.get("task"),
        "previous_attempt_id": attempt.get("previous_attempt_id"),
        "recommendation_ref": recommendation_ref.model_dump(mode="json"),
        "recommendation_hash": recommendation_ref.sha256,
        "proposal_semantic_hash": _parameter_proposal_semantic_hash(attempt),
        "parameter_diff": parameter_diff,
        "rationale": attempt.get("recommendation_summary") or attempt.get("reasoning"),
        "failure_facts": failure_facts,
        "evidence_refs": {
            key: attempt.get(key)
            for key in (
                "input_profile_ref",
                "prompt_payload_ref",
                "retrieval_query_ref",
                "retrieval_trace_ref",
                "evidence_payload_ref",
                "catalog_ref",
                "knowledge_pack_ref",
                "inherited_initial_snapshot_ref",
            )
            if attempt.get(key) is not None
        },
        "decisions_allowed": ["approve_parameters", "reject_parameters"],
        "auto_decidable": False,
    }


_PARAMETER_PROPOSAL_POST_WRITE_FIELDS = {
    "parameter_recommendation_ref",
    "parameter_recommendation_hash",
    "parameter_approval_decision_ref",
    "parameter_approval_recommendation_hash",
    "sealed_at",
    "seal_hash",
}


def _canonical_parameter_proposal(attempt: dict[str, Any]) -> dict[str, Any]:
    """Return every proposal semantic that approval will append or later freeze."""

    normalized = ParameterAttempt.model_validate(attempt).model_dump(mode="json")
    return {
        key: value
        for key, value in normalized.items()
        if key not in _PARAMETER_PROPOSAL_POST_WRITE_FIELDS
    }


def _parameter_proposal_semantic_hash(attempt: dict[str, Any]) -> str:
    canonical = _canonical_parameter_proposal(attempt)
    return sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _seal_parameter_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    value = dict(attempt)
    semantic = {key: item for key, item in value.items() if key not in {"sealed_at", "seal_hash"}}
    digest = sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    if value.get("seal_hash"):
        if value["seal_hash"] != digest:
            raise ValueError("sealed parameter attempt was mutated")
        return value
    value["sealed_at"] = datetime.now(timezone.utc).isoformat()
    value["seal_hash"] = digest
    return value


def _attempt_snapshot_ref(attempt: dict[str, Any]) -> str | None:
    if not attempt:
        return None
    return attempt.get("seal_hash") or sha256(
        json.dumps(attempt, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _retry_transition_failure(state: TopLevelState, reason: str) -> dict[str, Any]:
    return _update_assembly_output(
        state,
        {"status": "parameter_retry_invalid_transition", "reason": reason},
    )


def _validate_parameter_attempt_chain(raw_attempts: list[Any]) -> list[dict[str, Any]]:
    """Validate the complete ordered, sealed parameter-attempt history."""

    attempts: list[dict[str, Any]] = []
    for item in raw_attempts:
        if not isinstance(item, dict):
            raise ValueError("parameter attempt chain contains a non-mapping entry")
        verified = _seal_parameter_attempt(dict(item))
        attempts.append(ParameterAttempt.model_validate(verified).model_dump(mode="json"))
    if not attempts:
        return attempts

    initial = attempts[0]
    if initial.get("attempt_id") != 0 or initial.get("previous_attempt_id") is not None:
        raise ValueError("parameter attempt chain must start at attempt 0")
    if initial.get("attempt_kind") != "initial":
        raise ValueError("parameter attempt 0 must be initial")
    if initial.get("initial_recommendation_contract") not in {
        "legacy_initial", "curated_initial_v1"
    }:
        raise ValueError("parameter attempt 0 has no known initial contract")
    if any(
        initial.get(key) is not None
        for key in (
            "retry_origin",
            "inherited_initial_attempt_id",
            "inherited_initial_snapshot_ref",
            "inherited_evidence_payload_ref",
            "inherited_profile_ref",
            "previous_attempt_seal_hash",
        )
    ):
        raise ValueError("parameter attempt 0 contains retry-only links")

    initial_snapshot_ref = _attempt_snapshot_ref(initial)
    for sequence, attempt in enumerate(attempts):
        if attempt.get("attempt_id") != sequence:
            raise ValueError("parameter attempt IDs must match fixed list order")
        if sequence == 0:
            continue
        previous = attempts[sequence - 1]
        if attempt.get("attempt_kind") != "retry":
            raise ValueError("non-initial parameter attempts must be retries")
        if attempt.get("previous_attempt_id") != sequence - 1:
            raise ValueError("parameter attempt chain has a broken previous-attempt link")
        if attempt.get("previous_attempt_seal_hash") != previous.get("seal_hash"):
            raise ValueError("parameter attempt chain has a broken previous-seal link")
        if attempt.get("inherited_initial_attempt_id") != 0:
            raise ValueError("parameter retry has a broken initial-attempt link")
        if attempt.get("inherited_initial_snapshot_ref") != initial_snapshot_ref:
            raise ValueError("parameter retry has a broken initial-snapshot link")
        if attempt.get("initial_recommendation_contract") != initial.get(
            "initial_recommendation_contract"
        ):
            raise ValueError("parameter retry initial contract drifted")
        if attempt.get("catalog_snapshot") != initial.get("catalog_snapshot"):
            raise ValueError("parameter retry catalog snapshot drifted")
        if attempt.get("inherited_evidence_payload_ref") != initial.get("evidence_payload_ref"):
            raise ValueError("parameter retry inherited evidence drifted")
        if attempt.get("inherited_profile_ref") != initial.get("input_profile_ref"):
            raise ValueError("parameter retry inherited profile drifted")
    return attempts


def build_parameter_attempt_timeline(
    assembly_out: dict[str, Any], *, status: str, reason: str
) -> dict[str, Any]:
    """Derive a deterministic terminal timeline from the sealed attempt chain."""

    attempts = _validate_parameter_attempt_chain(
        assembly_out.get("parameter_attempts") or []
    )
    for sequence, attempt in enumerate(attempts):
        if int(attempt["attempt_id"]) != sequence:
            raise ValueError("parameter attempt timeline requires contiguous attempt IDs")
        expected_previous = None if sequence == 0 else sequence - 1
        if attempt.get("previous_attempt_id") != expected_previous:
            raise ValueError("parameter attempt timeline contains a broken previous-attempt link")
        if not attempt.get("seal_hash"):
            raise ValueError("parameter attempt timeline requires sealed attempts")
        if not attempt.get("parameter_approval_decision_ref"):
            raise ValueError("parameter attempt timeline requires an approval decision ref")
        if attempt.get("parameter_approval_recommendation_hash") != attempt.get("parameter_recommendation_hash"):
            raise ValueError("parameter attempt timeline approval hash mismatch")

    initial_contract = attempts[0].get("initial_recommendation_contract") if attempts else None
    if attempts and initial_contract not in {"legacy_initial", "curated_initial_v1"}:
        raise ValueError("parameter attempt timeline has missing/unknown initial contract")
    entries = []
    for sequence, attempt in enumerate(attempts):
        execution = attempt.get("execution_result")
        review = attempt.get("review_outcome") or attempt.get("review_failure")
        entries.append(
            {
                "sequence": sequence,
                "attempt_id": attempt["attempt_id"],
                "previous_attempt_id": attempt.get("previous_attempt_id"),
                "attempt_kind": attempt.get("attempt_kind"),
                "retry_origin": attempt.get("retry_origin"),
                "frozen_parameters_ref": attempt.get("validation_ref"),
                "command_ref": attempt.get("command_ref"),
                "execution": execution,
                "stderr_tail": attempt.get("stderr_tail"),
                "stdout_tail": attempt.get("stdout_tail"),
                "review": review,
                "failure_signal_ref": attempt.get("review_failure_ref") or (
                    attempt.get("execution_result") if execution and execution.get("status") != "succeeded" else None
                ),
                "parameter_delta": attempt.get("proposed_parameter_changes") or {},
                "inherited_initial_snapshot_ref": attempt.get("inherited_initial_snapshot_ref") or attempts[0].get("seal_hash"),
                "parameter_approval": {
                    "decision": "approve_parameters",
                    "decision_ref": attempt.get("parameter_approval_decision_ref"),
                    "recommendation_ref": attempt.get("parameter_recommendation_ref"),
                    "recommendation_hash": attempt.get("parameter_approval_recommendation_hash"),
                },
                "seal_hash": attempt.get("seal_hash"),
            }
        )
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    terminal = {
        "status": status,
        "reason": reason,
        "final_attempt_id": attempts[-1]["attempt_id"] if attempts else None,
        "execution_retries_used": int(retry_counts.get("execution", 0)),
        "review_retries_used": int(retry_counts.get("review", 0)),
        "result_ref": assembly_out.get("draft_assembly_ref"),
        "parameter_rejection": assembly_out.get("parameter_rejection"),
    }
    chain_hash = sha256(
        json.dumps(
            [(item["attempt_id"], item["seal_hash"]) for item in attempts],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "initial_recommendation_contract": initial_contract,
        "entries": entries,
        "terminal": terminal,
        "source_attempt_chain_hash": chain_hash,
    }


def _upsert_parameter_attempt(attempts: list[Any], attempt: dict[str, Any]) -> list[dict[str, Any]]:
    typed = [ParameterAttempt.model_validate(item).model_dump(mode="json") for item in attempts if isinstance(item, dict)]
    return [item for item in typed if item.get("attempt_id") != attempt["attempt_id"]] + [attempt]


def _parameter_prompt_contract(
    method_name: str,
    task: str,
    initial_snapshot: dict[str, Any] | None = None,
) -> AssemblyPromptContract:
    """Choose the decision contract from trusted routing and pack configuration."""

    if method_name != HIFIASM_METHOD:
        return "legacy"
    if task != "recommend_initial":
        contract = (initial_snapshot or {}).get("initial_recommendation_contract")
        if contract == "curated_initial_v1":
            return "hifiasm_methods_v1"
        if contract == "legacy_initial":
            return "legacy"
        raise ValueError("retry is missing attempt 0 initial recommendation contract")
    pack = load_knowledge_pack("parameter_recommender")
    strategies = pack.get("strategies") or {}
    if not isinstance(strategies, dict):
        raise ValueError("parameter recommender strategies must be a mapping")
    numeric = strategies.get("hifiasm_numeric")
    if numeric is None:
        return "legacy"
    if not isinstance(numeric, dict):
        raise ValueError("hifiasm_numeric strategy must be a mapping")
    cutover_enabled = numeric.get("initial_cutover_enabled", False)
    if not isinstance(cutover_enabled, bool):
        raise ValueError("hifiasm_numeric.initial_cutover_enabled must be a bool")
    return "hifiasm_methods_v1" if cutover_enabled else "legacy"


def _failure_log_tail(execution_result: dict[str, Any]) -> str:
    return _tail_from_path(execution_result.get("stderr_path")) or _tail_from_path(execution_result.get("stdout_path")) or ""


def _tail_from_path(path_value: Any, *, line_count: int = 100) -> str:
    if not path_value:
        return ""
    path = Path(str(path_value))
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-line_count:])


def _parameter_recommendation_config(state: TopLevelState, assembly_out: dict[str, Any]) -> dict[str, Any]:
    config = dict(assembly_out.get("parameter_recommendation") or {})
    shared = state.get("shared_parameters") or {}
    if isinstance(shared, dict):
        assembly_config = dict((shared.get("assembly") or {}).get("parameter_recommendation") or {})
        config = {**assembly_config, **config}
    return {
        "max_execution_retries": int(config.get("max_execution_retries", 1)),
        "max_review_retries": int(config.get("max_review_retries", 2)),
        "allow_model_unavailable_defaults": bool(config.get("allow_model_unavailable_defaults", False)),
    }


def _review_failure_diagnosis(metrics: dict[str, Any]) -> dict[str, Any]:
    complete = _float_or_none(_first_metric(metrics, "busco_complete", "busco_complete_ratio"))
    duplicate = _float_or_none(_first_metric(metrics, "busco_duplicate", "busco_duplicate_ratio"))
    size_ratio = _float_or_none(metrics.get("assembly_size_ratio"))
    n50 = _float_or_none(_first_metric(metrics, "contig_n50", "n50"))
    contig_count = _float_or_none(metrics.get("contig_count"))
    normalized_complete = _ratio(complete)
    normalized_duplicate = _ratio(duplicate)
    passed = (
        normalized_complete is not None
        and normalized_complete >= 0.95
        and (normalized_duplicate is None or normalized_duplicate <= 0.10)
        and (size_ratio is None or 0.75 <= size_ratio <= 1.25)
    )
    if passed:
        failure_mode = None
        suggestions: dict[str, str] = {}
    elif normalized_duplicate is not None and normalized_duplicate >= 0.15 and size_ratio is not None and size_ratio >= 1.20:
        failure_mode = "under_purged"
        suggestions = {"purge_level": "consider increasing"}
    elif normalized_complete is not None and normalized_complete <= 0.90 and size_ratio is not None and size_ratio <= 0.80:
        failure_mode = "over_purged"
        suggestions = {"purge_level": "consider decreasing"}
    elif n50 is not None and contig_count is not None and n50 < 1_000_000 and contig_count > 500:
        failure_mode = "fragmented"
        suggestions = {"overlap_parameters": "consider less aggressive filtering"}
    elif normalized_complete is not None and normalized_complete <= 0.90:
        failure_mode = "low_completeness"
        suggestions = {"read_filtering": "consider retaining more reads"}
    else:
        failure_mode = "execution_or_metrics_missing"
        suggestions = {}
    return {
        "passed": passed,
        "failure_mode": failure_mode,
        "metrics": metrics,
        "suggestions": suggestions,
    }


def _first_metric(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if metrics.get(key) is not None:
            return metrics.get(key)
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100.0 if value > 1 else value


def _build_input_profile(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    *,
    recommended_route: str,
    reasoning: str,
    uncertainty_notes: list[str],
    profile_source: str,
) -> AssemblyInputProfile:
    return AssemblyInputProfile(
        session_id=state["session_id"],
        primary_long_reads_ref=assembly_out.get("primary_long_reads_ref"),
        primary_long_read_platform=assembly_out.get("primary_long_read_platform"),
        ont_reads_ref=assembly_out.get("ont_reads_ref"),
        hifi_reads_ref=assembly_out.get("hifi_reads_ref"),
        hic_reads_ref=assembly_out.get("hic_reads_ref"),
        estimated_genome_size=assembly_out.get("genome_size_prior"),
        target_level=assembly_out.get("target_level", "draft"),
        has_hifi=bool(assembly_out.get("hifi_reads_ref")),
        has_hic=bool(assembly_out.get("hic_reads_ref")),
        recommended_route=recommended_route,
        recommendation_confidence=0.0,  # deprecated field, kept for schema compatibility
        recommendation_reasoning=reasoning,
        uncertainty_notes=uncertainty_notes,
        profile_source=profile_source,
        notes=[],
        metadata={"route_selector_version": "phase8_slice1"},
    )


def _recommend_route_with_decision_boundary(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    prompt_payload = _route_selector_payload(state, assembly_out, ctx)
    decision = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="route_selection",
            session_id=state["session_id"],
            prompt_contract="legacy",
            prompt_payload=prompt_payload,
            system_prompt="You are an expert genome assembly route selector.\n\n" + str(ctx.get("skill_text") or ""),
            allowed_actions=list(ctx.get("allowed_actions") or []),
        )
    )
    if decision.valid and decision.parsed:
        payload = decision.parsed
        return {
            "recommended_route": payload.get("recommended_route"),
            "reasoning": str(payload.get("reasoning", payload.get("recommendation_reasoning", ""))),
            "uncertainty_notes": list(payload.get("uncertainty_notes") or []),
            "source": "llm",
        }
    return {
        "recommended_route": "insufficient_data",
        "reasoning": "route selector LLM unavailable; route selection requires human/data review",
        "source": "llm_unavailable",
        "uncertainty_notes": [
            f"route selector LLM unavailable: {decision.error or 'invalid decision'}",
        ],
    }


def _route_selector_payload(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    data_profile = {
        "session_id": state.get("session_id"),
        "ont_reads_ref": assembly_out.get("ont_reads_ref"),
        "hifi_reads_ref": assembly_out.get("hifi_reads_ref"),
        "hic_reads_ref": assembly_out.get("hic_reads_ref"),
        "platform": assembly_out.get("platform"),
        "target_level": assembly_out.get("target_level"),
        "genome_size_prior": assembly_out.get("genome_size_prior"),
        "has_hifi": assembly_out.get("has_hifi"),
        "has_hic": assembly_out.get("has_hic"),
    }
    return {
        "data_profile": data_profile,
        "rules": ctx.get("rules") or {},
        "cases": ctx.get("cases") or [],
        "allowed_actions": ctx.get("allowed_actions") or [],
        "required_output": {
            "recommended_route": "one of allowed_actions",
            "reasoning": "short explanation",
            "uncertainty_notes": "list of strings",
        },
    }

def _unique_refs(refs: list[str]) -> list[str]:
    unique = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


def _read_qc_assembly_input_ref(state: TopLevelState) -> str | None:
    read_qc_out = (state.get("stage_outputs") or {}).get("read_qc") or {}
    for key in ["assembly_input_ref", "filtered_long_reads_fofn_ref", "long_reads_fofn_ref"]:
        value = read_qc_out.get(key)
        if value:
            return str(value)
    return None


def _asset_input_ref(state: TopLevelState) -> str | None:
    return _asset_ref_by_type(state, {"clean_long_reads", "raw_long_reads"})


def _asset_ref_by_type(state: TopLevelState, asset_types: set[str]) -> str | None:
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return None
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return None
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for asset in registry.get("assets", []):
        if asset.get("asset_type") in asset_types:
            return str(asset.get("path"))
    return None


def _genome_size_prior(state: TopLevelState) -> int | None:
    assembly_out = _assembly_output(state)
    params = dict(assembly_out.get("flye_parameters") or {})
    if params.get("estimated_genome_size"):
        return int(params["estimated_genome_size"])
    decision = dict(assembly_out.get("method_decision") or {})
    if decision.get("genome_size_prior"):
        return int(decision["genome_size_prior"])
    shared = _shared_parameters(state)
    if shared.get("estimated_genome_size"):
        return int(shared["estimated_genome_size"])
    inventory = (state.get("stage_outputs") or {}).get("inventory") or {}
    if inventory.get("genome_size_estimate"):
        return int(inventory["genome_size_estimate"])
    survey = (state.get("stage_outputs") or {}).get("genome_survey") or {}
    if survey.get("genome_size_prior"):
        return int(survey["genome_size_prior"])
    estimates = dict(survey.get("estimates") or {})
    for key in ["genome_size_prior", "genome_size"]:
        if estimates.get(key):
            return int(estimates[key])
    return None


def _read_type_prior(state: TopLevelState) -> dict[str, str] | None:
    assembly_out = _assembly_output(state)
    selection = _selection_from_output(assembly_out)
    if selection:
        return {
            **SHARED_TO_METHOD_READ_TYPE[selection.read_type],
            "shared_read_type": selection.read_type,
            "source": "assembly_selection",
        }
    params = dict(assembly_out.get("flye_parameters") or {})
    if params.get("read_type"):
        return {"flye": str(params["read_type"]), "source": "flye_parameters"}

    shared = _shared_parameters(state)
    shared_read_type = shared.get("read_type")
    if shared_read_type in SHARED_TO_METHOD_READ_TYPE:
        return {
            **SHARED_TO_METHOD_READ_TYPE[str(shared_read_type)],
            "shared_read_type": str(shared_read_type),
            "source": "shared_parameters",
        }

    platform = _long_read_platform(state)
    inferred = PLATFORM_TO_SHARED_READ_TYPE.get(platform or "")
    if inferred:
        return {
            **SHARED_TO_METHOD_READ_TYPE[inferred],
            "shared_read_type": inferred,
            "source": "manifest_platform",
        }
    return None


def _normalized_read_type_prior(state: TopLevelState) -> str | None:
    return _normalized_read_type_from_prior(_read_type_prior(state))


def _normalized_read_type_from_prior(prior: Any) -> str | None:
    if not isinstance(prior, dict):
        return None
    return resolve_read_type(("read_type_prior.shared_read_type", prior.get("shared_read_type")), context="assembly.read_type_prior")


def _shared_parameters(state: TopLevelState) -> dict[str, Any]:
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        return shared.model_dump(exclude_none=True)
    return dict(shared)


def _long_read_platform(state: TopLevelState) -> str | None:
    read_qc_out = (state.get("stage_outputs") or {}).get("read_qc") or {}
    if read_qc_out.get("platform"):
        return str(read_qc_out["platform"])
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return None
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return None
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for asset in registry.get("assets", []):
        if asset.get("asset_type") in {"raw_long_reads", "clean_long_reads"} and asset.get("platform"):
            return str(asset["platform"])
    return None


def _hifi_asset_input_ref(state: TopLevelState) -> str | None:
    ref = _asset_ref_by_type(
        state,
        {"pacbio_hifi", "clean_hifi_reads", "hifi_reads"},
    )
    if ref:
        return ref
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return None
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return None
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for asset in registry.get("assets", []):
        if (
            asset.get("asset_type") in {"raw_long_reads", "clean_long_reads"}
            and asset.get("platform") == "pacbio_hifi"
            and asset.get("path")
        ):
            return str(asset["path"])
    return None


def _genome_survey_result_ref(state: TopLevelState) -> str | None:
    survey = (state.get("stage_outputs") or {}).get("genome_survey") or {}
    value = survey.get("result_artifact_ref")
    return str(value) if value else None
