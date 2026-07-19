"""Assembly stage nodes for candidate planning."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.storage.ledger import Ledger
from app.control.lifecycle import ResumeValidationError, timeout_terminal_update
from app.domains.assembly.selection import (
    AssemblySelection,
    PLATFORM_TO_SHARED_READ_TYPE,
    SHARED_TO_METHOD_READ_TYPE,
    compatible_methods,
    default_primary_method,
    is_method_read_type_compatible,
    method_read_type,
    normalize_method_id,
    normalize_read_type,
    resolve_read_type,
    route_from_read_type as _route_from_read_type,
)
from app.domains.genome_size import resolve_genome_size
from app.storage.artifacts import publish_artifact_records
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.workflow import next_stage_name
from app.knowledge.packs import load_knowledge_pack
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.review.policy import evaluate
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_INPUT_PROFILE,
    ASSEMBLY_ROUTE_DECISION,
    ArtifactMeta,
    AssemblyCandidateBundle,
    AssemblyInputProfile,
    AssemblyRouteDecision,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.plan import AssemblyCandidatePlan
from app.schemas.state import TopLevelState
from app.domains.assembly.constants import (
    ASSEMBLY_AUTO_CANDIDATES,
    ASSEMBLY_CANDIDATES,
    FLYE_METHOD,
    GENOME_SIZE_REQUIRED,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
    NEXTDENOVO_METHOD,
)
from app.stages.assembly.decision import (
    AssemblyDecisionRequest,
    make_assembly_decision,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    load_assets_from_registry,
    ordered_unique,
    stage_artifacts_dir,
    stage_output,
    stage_work_dir,
    update_stage_output,
)
from app.storage.publish import publish_file

ROUTE_DECISION_MAP = {
    "select_cyclone": "cyclone",
    "select_ont": "ont",
    "select_hifi": "hifi",
}
ROUTE_VALUES = {"cyclone", "ont", "hifi"}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return update_stage_output(
        state, "assembly", {"status": "failed", "reason": reason}
    )


def enter_assembly(state: TopLevelState) -> dict:
    """Mark assembly as running."""
    return {"supervision_state": "running_stage"}


def resolve_extended_inputs(state: TopLevelState) -> dict:
    """Resolve long-read, HiFi, Hi-C, platform, and route hints for assembly."""
    assembly_out = stage_output(state, "assembly")
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

    resolved_genome_size = resolve_genome_size(state)
    genome_size_prior = resolved_genome_size["value_bp"] if resolved_genome_size else None
    read_type_prior = _read_type_prior(state)
    target_level = assembly_out.get("target_level") or "draft"
    explicit_route = configured_route if configured_route in ROUTE_VALUES else None
    input_ref = primary_long_reads_ref
    input_type = "long_reads.fofn" if str(input_ref).endswith(".fofn") else "clean_long_reads"

    return update_stage_output(
        state,
        "assembly",
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
            "resolved_genome_size": resolved_genome_size,
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
    assembly_out = stage_output(state, "assembly")
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

        return update_stage_output(
            state,
            "assembly",
            {
                "status": "profile_ready",
                "input_profile": profile.model_dump(mode="json"),
                "input_profile_ref": profile_ref,
                "route_decision": decision.model_dump(mode="json"),
                "route_decision_ref": decision_ref,
                "assembly_selection": selection.model_dump(mode="json"),
                "selected_route": _route_from_read_type(selection.read_type),
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
    return update_stage_output(
        state,
        "assembly",
        {
            "status": "profile_ready",
            "input_profile": profile.model_dump(mode="json"),
            "input_profile_ref": profile_ref,
            "recommended_assembly_selection": recommended_selection.model_dump(mode="json") if recommended_selection else None,
        },
    )


def select_route(state: TopLevelState) -> dict:
    """Confirm or block on the assembly selection recommendation."""
    assembly_out = stage_output(state, "assembly")
    if assembly_out.get("status") != "profile_ready":
        return {}
    if assembly_out.get("assembly_selection"):
        return update_stage_output(state, "assembly", {"status": "route_selected"})

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
        review_request_id, review_request_hash = _route_review_binding(state, request)
        decision_ref = persist_review_decision(
            state["data_dir"],
            session_id=state["session_id"],
            review_request_id=review_request_id,
            stage="assembly",
            gate_id="route_selection",
            decision=request,
            request_hash=review_request_hash,
            method_name="assembly.route_selector",
            artifact_type=ASSEMBLY_ROUTE_DECISION,
        )
        Ledger(state["data_dir"]).record_review_decision(
            state["session_id"],
            "assembly",
            gate_id="route_selection",
            decision=request.decision,
            notes=list(request.notes),
            artifact_ref=decision_ref.model_dump(mode="json"),
            metadata=request.metadata,
            decided_by=request.decided_by,
            rationale_artifact_ref=request.rationale_artifact_ref,
            review_request_id=review_request_id,
        )
        return {
            "current_stage": "assembly",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "assembly route selection stopped by human review",
        }

    selected_route = ROUTE_DECISION_MAP.get(request.decision)
    if selected_route not in ROUTE_VALUES:
        raise ResumeValidationError(f"unsupported assembly selection decision: {request.decision}")

    assembly_out = stage_output(state, "assembly")
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
    review_request_id, review_request_hash = _route_review_binding(state, request)
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=review_request_id,
        stage="assembly",
        gate_id="route_selection",
        decision=decision,
        request_hash=review_request_hash,
        method_name="assembly.route_selector",
        artifact_type=ASSEMBLY_ROUTE_DECISION,
    )
    Ledger(state["data_dir"]).record_review_decision(
        state["session_id"],
        "assembly",
        gate_id="route_selection",
        decision=request.decision,
        method_name=method,
        notes=list(request.notes),
        artifact_ref=decision_ref.model_dump(mode="json"),
        metadata=request.metadata,
        decided_by=decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        review_request_id=review_request_id,
    )

    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {
        **assembly_out,
        "status": "route_selected",
        "route_decision": decision.model_dump(mode="json"),
        "route_decision_ref": decision_ref.model_dump(mode="json"),
        "assembly_selection": selection.model_dump(mode="json"),
        "selected_route": selected_route,
    }
    return {
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def _route_review_binding(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> tuple[str, str]:
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get("review_request_id")
    review_request_hash = request.review_request_hash or active_interrupt.get("request_hash")
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError("assembly route decision is missing review request binding")
    return str(review_request_id), str(review_request_hash)


def apply_selection_timeout(state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]) -> dict[str, Any]:
    """Commit the current assembly recommendation after a configured timeout."""
    active_interrupt = state.get("active_interrupt") or {}
    try:
        request_record = load_review_request(
            state["data_dir"],
            active_interrupt.get("request_ref") or {},
            session_id=state["session_id"],
        )
    except (KeyError, ReviewPersistenceError, ValueError):
        return _timeout_stop_update(state, request, timeout)
    request_payload = request_record.get("payload") or {}
    selection_payload = request_payload.get("recommended_assembly_selection")
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
    assembly_out = stage_output(state, "assembly")
    if assembly_out.get("status") not in {"resolved", "route_selected"}:
        return {}

    selection = _selection_from_output(assembly_out)
    if selection is None:
        return _fail_update(state, "assembly_selection is required before candidate planning")

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    planned_methods = _planned_candidate_methods(selection)
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

    return update_stage_output(
        state,
        "assembly",
        {
            "status": "candidate_set_ready",
            "candidate_methods": planned_methods,
            "candidate_plans": candidate_plans,
            "rejected_candidates": rejected,
        },
    )


def _planned_candidate_methods(selection: AssemblySelection) -> list[str]:
    methods = list(dict.fromkeys([selection.primary_method, *ASSEMBLY_AUTO_CANDIDATES]))
    if selection.fallback_method and selection.fallback_method not in methods:
        methods.append(selection.fallback_method)
    return methods


def select_primary_and_fallback(state: TopLevelState) -> dict:
    """Select one primary and at most one fallback candidate."""
    assembly_out = stage_output(state, "assembly")
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
    return update_stage_output(
        state,
        "assembly",
        {
            "status": "selected",
            "primary_method": primary,
            "fallback_method": fallback,
            "selected_methods": selected,
        },
    )


def plan_candidate_attempts(state: TopLevelState) -> dict:
    """Write candidate bundles and select the optional local Flye route."""
    assembly_out = stage_output(state, "assembly")
    if assembly_out.get("status") not in {"selected", "parameters_recommended", "parameters_approved"}:
        return {}

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    parameter_attempt_id = 0
    if assembly_out.get("status") == "parameters_approved":
        method_id = assembly_out.get("primary_method")
        if not isinstance(method_id, str) or not method_id:
            return _fail_update(state, "approved Assembly parameters have no method")
        try:
            parameter_attempt_id = current_parameter_attempt(
                assembly_out,
                method_id=method_id,
                label="Assembly",
            ).attempt_id
        except ParameterAttemptStateError as exc:
            return _fail_update(state, str(exc))
    attempt_scope = f"{parameter_attempt_id:03d}"
    attempt_label = f"parameter_attempt_{parameter_attempt_id}"
    candidate_dir = stage_artifacts_dir(
        state,
        "assembly",
        "attempts",
        attempt_scope,
        "candidates",
    )
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
        fasta_path = stage_artifacts_dir(
            state,
            "assembly",
            "attempts",
            attempt_scope,
            "outputs",
            short_name,
        ) / "draft_assembly.fasta"
        if method_name == NEXTDENOVO_METHOD:
            log_path = stage_work_dir(state, "assembly", "attempts", attempt_scope, "nextdenovo") / "run.cfg"
        elif method_name == HIFIASM_METHOD:
            log_path = stage_work_dir(state, "assembly", "attempts", attempt_scope, "hifiasm") / "run_hifiasm.sh"
        elif method_name == HIFIASM_ONT_METHOD:
            log_path = stage_work_dir(state, "assembly", "attempts", attempt_scope, "hifiasm_ont") / "run_hifiasm_ont.sh"
        else:
            log_path = stage_work_dir(state, "assembly", "attempts", attempt_scope, short_name, "flye_out") / "flye.log"
        bundle_path = candidate_dir / f"{short_name}_candidate.planned.json"
        execution_enabled = (
            (local_flye and method_name == FLYE_METHOD)
            or (local_nextdenovo and method_name == NEXTDENOVO_METHOD)
            or (local_hifiasm and method_name == HIFIASM_METHOD)
            or (local_hifiasm_ont and method_name == HIFIASM_ONT_METHOD)
        )
        method_graph_requested = execution_enabled
        bundle = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=f"{session_id}:{method_name}:{attempt_label}",
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
        temporary = stage_work_dir(
            state,
            "assembly",
            "candidate_planning",
            f".{bundle_path.name}.{uuid4().hex}.tmp",
        )
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            publish_file(temporary, bundle_path)
        finally:
            temporary.unlink(missing_ok=True)
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

    publish_artifact_records(data_dir, session_id, artifacts)
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
    return update_stage_output(
        state,
        "assembly",
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


def commit_assembly_plan(state: TopLevelState) -> dict:
    """Route compact assembly candidate refs to assembly_review."""
    assembly_out = stage_output(state, "assembly")
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
    trace = append_route_trace(state, "assembly_stage")
    return {
        "current_stage": next_stage_name("assembly"),
        "supervision_state": "running_stage",
        "route_trace": trace,
        "stage_outputs": outputs,
    }


def fail_assembly(state: TopLevelState) -> dict:
    """Stop workflow on assembly planning failure or preflight block."""
    assembly_out = stage_output(state, "assembly")
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
    assembly_out = stage_output(state, "assembly")
    reason = assembly_out.get("block_reason", "assembly decision required")
    interrupt = dict(assembly_out.get("active_interrupt") or {})
    if interrupt:
        outputs = dict(state.get("stage_outputs") or {})
        compact_output = dict(assembly_out)
        compact_output.pop("active_interrupt", None)
        outputs["assembly"] = compact_output
        return {
            "current_stage": "assembly",
            "supervision_state": "blocked_human",
            "active_interrupt": interrupt,
            "stage_outputs": outputs,
        }
    persisted = _block_for_route_review(state, reason)
    outputs = dict(persisted.get("stage_outputs") or state.get("stage_outputs") or {})
    persisted_output = dict(outputs.get("assembly") or {})
    interrupt = dict(persisted_output.pop("active_interrupt", None) or {})
    outputs["assembly"] = persisted_output
    return {
        "current_stage": "assembly",
        "supervision_state": "blocked_human",
        "active_interrupt": interrupt,
        "stage_outputs": outputs,
    }


def _block_for_human(state: TopLevelState, reason: str) -> dict:
    return update_stage_output(
        state, "assembly", {"status": "blocked_human", "block_reason": reason}
    )


def _block_for_route_review(state: TopLevelState, reason: str) -> dict:
    assembly_out = stage_output(state, "assembly")
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

    request_payload = {
        **payload.model_dump(mode="json"),
        "recommended_route": profile.get("recommended_route"),
        "recommended_assembly_selection": recommended_selection,
        "recommendation_reasoning": profile.get("recommendation_reasoning"),
        "uncertainty_notes": profile.get("uncertainty_notes", []),
        "available_routes": _available_routes(assembly_out),
        "available_read_types": sorted({route for route in ["cyclone", "ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi"]}),
        "compatible_methods": {
            read_type: compatible_methods(read_type) for read_type in ["cyclone", "ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi"]
        },
        "review_policy": assembly_out.get("review_policy") or {"mode": outcome.mode},
        "review_policy_outcome": outcome.model_dump(mode="json"),
    }
    persisted_request = persist_review_request(
        state,
        stage="assembly",
        gate_id="route_selection",
        reason=reason,
        payload=request_payload,
        method_name="assembly.route_selector",
        artifact_type="assembly.route_selection.request",
    )
    Ledger(state["data_dir"]).record_review_requested(
        state["session_id"],
        "assembly",
        reason=reason,
        candidates=payload.candidates,
        signals=[signal.model_dump(mode="json") for signal in payload.signals],
        review_request_artifact_ref=persisted_request.request_ref.model_dump(mode="json"),
        auto_decidable=False,
        policy_mode=outcome.mode,
        pending_human_confirmation=True,
        review_request_id=persisted_request.review_request_id,
    )
    interrupt = persisted_request.compact_interrupt(
        stage="assembly",
        gate_id="route_selection",
        reason=reason,
    )

    return update_stage_output(
        state,
        "assembly",
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
    return ordered_unique(routes)


def _selection_from_output(assembly_out: dict[str, Any]) -> AssemblySelection | None:
    payload = assembly_out.get("assembly_selection")
    if isinstance(payload, AssemblySelection):
        return payload
    if isinstance(payload, dict):
        try:
            return AssemblySelection.model_validate(payload)
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
        metadata={"route": route},
    )


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
) -> dict[str, Any]:
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    artifact_dir = stage_artifacts_dir(state, "assembly", "control")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = sha256(canonical).hexdigest()[:16]
    logical = Path(filename)
    suffix = logical.suffix or ".json"
    path = artifact_dir / f"{logical.stem}.{content_hash}{suffix}"
    temporary = stage_work_dir(
        state,
        "assembly",
        "persistence",
        f".{path.name}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        publish_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    artifact = ArtifactMeta.from_file(
        path,
        stage="assembly",
        method_name=method_name,
        artifact_type=artifact_type,
    )
    refs = publish_artifact_records(data_dir, session_id, [artifact])
    if len(refs) != 1:
        raise ResumeValidationError("assembly artifact index did not return one ref")
    return refs[0].model_dump(mode="json")


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
        has_rnaseq=bool(
            load_assets_from_registry(
                state,
                ["raw_rnaseq_reads", "clean_rnaseq_reads"],
            )
        ),
        recommended_route=recommended_route,
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
    resolved = resolve_genome_size(state)
    return resolved["value_bp"] if resolved else None


def _read_type_prior(state: TopLevelState) -> dict[str, str] | None:
    assembly_out = stage_output(state, "assembly")
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
