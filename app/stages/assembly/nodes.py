"""Assembly stage nodes for candidate planning."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, TypedDict, cast
from uuid import uuid4

from app.configuration import load_descriptor_registry
from app.control.errors import ResumeValidationError
from app.control.workflow_plan import advance_workflow_step
from app.domains.assembly.constants import (
    ASSEMBLY_AUTO_CANDIDATES,
    ASSEMBLY_CANDIDATES,
    FLYE_METHOD,
    GENOME_SIZE_REQUIRED,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
    NEXTDENOVO_METHOD,
)
from app.domains.assembly.selection import (
    PLATFORM_TO_SHARED_READ_TYPE,
    SHARED_TO_METHOD_READ_TYPE,
    AssemblySelection,
    AssemblySelectionSource,
    NormalizedReadType,
    compatible_methods,
    default_primary_method,
    is_method_read_type_compatible,
    normalize_method_id,
    normalize_read_type,
    resolve_read_type,
    validate_assembly_selection,
)
from app.domains.assembly.selection import (
    route_from_read_type as _route_from_read_type,
)
from app.domains.genome_size import resolve_genome_size
from app.domains.read_inputs import AssemblyReadInputType
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.knowledge.packs import load_knowledge_pack
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_INPUT_PROFILE,
    ASSEMBLY_ROUTE_DECISION,
    ArtifactMeta,
    ArtifactRef,
    ArtifactRefJson,
    AssemblyCandidateBundle,
    AssemblyInputProfile,
    AssemblyRouteDecision,
)
from app.schemas.plan import AssemblyCandidatePlan
from app.schemas.state import (
    ActiveInterrupt,
    JsonValue,
    StageCursor,
    StateUpdate,
    TopLevelState,
)
from app.stages.assembly.decision import (
    AssemblyDecisionRequest,
    make_assembly_decision,
)
from app.stages.assembly.method_application.support import (
    ASSEMBLY_EXECUTION_CONTRACT_KEY,
    AssemblyExecutionContractParameterError,
    build_assembly_execution_contract,
    load_assembly_execution_contract,
)
from app.stages.assembly.state import (
    ASSEMBLY_STAGE_CURSOR_ADAPTER,
    AssemblyCandidatePlanCursor,
    AssemblyInputProfileCursor,
    AssemblySelectionCursor,
    AssemblyStageCursor,
    assembly_stage_output,
    update_assembly_stage_output,
    validated_assembly_stage_payload,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    load_assets_from_registry,
    ordered_unique,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.stages.read_resolution import (
    RuntimeReadMember,
    resolve_hic_pairs,
    resolve_long_read_members,
)
from app.stages.resource_resolution import update_resource_ref
from app.storage.artifacts import (
    publish_artifact_record,
)
from app.storage.assembly_candidates import load_executed_candidate_bundle
from app.storage.ledger import Ledger

ROUTE_DECISION_MAP: dict[str, SelectedAssemblyRoute] = {
    "select_cyclone": "cyclone",
    "select_ont": "ont",
    "select_hifi": "hifi",
}
ROUTE_VALUES: frozenset[str] = frozenset({"cyclone", "ont", "hifi"})

SelectedAssemblyRoute = Literal["cyclone", "ont", "hifi"]
RecommendedAssemblyRoute = Literal["cyclone", "ont", "hifi", "insufficient_data"]
InputProfileSource = Literal[
    "manifest", "run_config", "run_config_explicit", "llm_inferred"
]


class RouteRecommendation(TypedDict):
    """Validated route recommendation used to construct the input profile."""

    recommended_route: RecommendedAssemblyRoute
    reasoning: str
    uncertainty_notes: list[str]
    source: str


def _fail_update(state: TopLevelState, reason: str) -> StateUpdate:
    return update_assembly_stage_output(state, {"status": "failed", "reason": reason})


def enter_assembly(state: TopLevelState) -> StateUpdate:
    """Mark assembly as running."""
    return {"supervision_state": "running_stage"}


def resolve_extended_inputs(state: TopLevelState) -> StateUpdate:
    """Resolve long-read, HiFi, Hi-C, platform, and route hints for assembly."""
    assembly_out = assembly_stage_output(state)
    try:
        all_members = resolve_long_read_members(state)
    except ValueError as exc:
        return _fail_update(state, f"assembly long-read provenance is invalid: {exc}")
    if not all_members:
        return _fail_update(state, "assembly requires long reads")
    hifi_members = [
        member for member in all_members if member.platform == "pacbio_hifi"
    ]
    ont_members = [member for member in all_members if member.platform == "ont"]
    platforms = list(dict.fromkeys(member.platform for member in all_members))

    configured_route = assembly_out.get("assembly_route") or "auto"
    existing_selection = _selection_from_output(assembly_out)
    single_platform_read_type = (
        PLATFORM_TO_SHARED_READ_TYPE.get(platforms[0]) if len(platforms) == 1 else None
    )
    hinted_read_type = resolve_read_type(
        (
            "assembly_selection.read_type",
            existing_selection.read_type if existing_selection else None,
        ),
        ("assembly_route", configured_route if configured_route != "auto" else None),
        ("shared_parameters.read_type", _shared_parameters(state).get("read_type")),
        ("read_type_prior", _normalized_read_type_prior(state)),
        ("single_registered_platform", single_platform_read_type),
        context="assembly.resolve_extended_inputs",
    )
    if hinted_read_type is None:
        if platforms != ["unknown"]:
            return _fail_update(
                state,
                "mixed long-read platforms require an explicit "
                "assembly method/read type",
            )
        primary_members = list(all_members)
    else:
        primary_members = _members_for_read_type(all_members, hinted_read_type)
        if not primary_members and platforms == ["unknown"]:
            primary_members = list(all_members)
    explicit_hifi_ref = assembly_out.get("hifi_reads_ref")
    if hinted_read_type == "pacbio_hifi" and explicit_hifi_ref:
        try:
            primary_members = resolve_long_read_members(
                state,
                override_paths=[explicit_hifi_ref],
            )
        except ValueError as exc:
            return _fail_update(state, str(exc))
        if any(
            member.platform not in {"pacbio_hifi", "unknown"}
            for member in primary_members
        ):
            return _fail_update(
                state,
                "assembly.hifi_reads_ref did not resolve to a HiFi member",
            )
    if not primary_members:
        return _fail_update(
            state,
            f"assembly has no registered primary reads for {hinted_read_type}",
        )
    if platforms == ["unknown"] and hinted_read_type == "pacbio_hifi":
        hifi_members = list(primary_members)
    elif platforms == ["unknown"] and hinted_read_type in {"ont_raw", "ont_hq"}:
        ont_members = list(primary_members)

    hifiasm_parameters = assembly_out.get("hifiasm_parameters") or {}
    use_ultralong_ont = bool(hifiasm_parameters.get("use_ultralong_ont"))
    use_hic_phasing = bool(hifiasm_parameters.get("use_hic_phasing"))
    selected_method = existing_selection.primary_method if existing_selection else None
    if (use_ultralong_ont or use_hic_phasing) and selected_method != HIFIASM_METHOD:
        return _fail_update(
            state,
            "Hifiasm supplementary inputs require assembly.candidate.hifiasm",
        )
    if use_ultralong_ont and not ont_members:
        return _fail_update(
            state,
            "use_ultralong_ont requires at least one registered ONT member",
        )
    if use_hic_phasing:
        try:
            hic_pairs = resolve_hic_pairs(
                state,
                selection_anchor=assembly_out.get("hic_reads_ref"),
            )
        except ValueError as exc:
            return _fail_update(state, f"Hi-C provenance is invalid: {exc}")
        if not hic_pairs:
            return _fail_update(
                state,
                "use_hic_phasing requires at least one complete registered Hi-C pair",
            )
    else:
        hic_pairs = []

    primary_refs = [member.path for member in primary_members]
    hifi_refs = [member.path for member in hifi_members]
    ont_refs = [member.path for member in ont_members]
    hic_r1_reads = [pair.r1.path for pair in hic_pairs]
    hic_r2_reads = [pair.r2.path for pair in hic_pairs]
    primary_platforms = list(
        dict.fromkeys(member.platform for member in primary_members)
    )
    primary_platform = primary_platforms[0] if len(primary_platforms) == 1 else None
    input_source = str(
        (state.get("resource_refs") or {}).get("long_reads", {}).get("source_stage")
        or "asset_registry"
    )

    resolved_genome_size = resolve_genome_size(state)
    genome_size_prior = (
        resolved_genome_size["value_bp"] if resolved_genome_size else None
    )
    read_type_prior = _read_type_prior(state)
    target_level = assembly_out.get("target_level") or "draft"
    explicit_route = (
        cast(SelectedAssemblyRoute, configured_route)
        if configured_route in ROUTE_VALUES
        else None
    )
    input_ref: str | list[str] = (
        primary_refs[0] if len(primary_refs) == 1 else primary_refs
    )
    input_type: AssemblyReadInputType = (
        "long_reads.fofn"
        if isinstance(input_ref, str) and input_ref.endswith(".fofn")
        else "clean_long_reads"
    )

    return update_assembly_stage_output(
        state,
        {
            "status": "resolved_extended",
            "primary_long_reads_ref": primary_refs[0],
            "primary_long_read_refs": primary_refs,
            "primary_long_read_platform": primary_platform,
            "ont_reads_ref": ont_refs[0] if ont_refs else None,
            "ultralong_read_refs": ont_refs if use_ultralong_ont else [],
            "hifi_reads_ref": hifi_refs[0] if hifi_refs else None,
            "hifi_read_refs": hifi_refs,
            "hic_reads_ref": (
                assembly_out.get("hic_reads_ref")
                or (hic_r1_reads[0] if hic_r1_reads else None)
            ),
            "hic_r1_reads": hic_r1_reads,
            "hic_r2_reads": hic_r2_reads,
            "assembly_input_member_ids": {
                "primary": [member.member_id for member in primary_members],
                "ultralong": (
                    [member.member_id for member in ont_members]
                    if use_ultralong_ont
                    else []
                ),
                "hic_r1": (
                    [pair.r1.member_id for pair in hic_pairs] if use_hic_phasing else []
                ),
                "hic_r2": (
                    [pair.r2.member_id for pair in hic_pairs] if use_hic_phasing else []
                ),
            },
            "assembly_input_member_platforms": {
                "primary": [member.platform for member in primary_members],
                "ultralong": (
                    [member.platform for member in ont_members]
                    if use_ultralong_ont
                    else []
                ),
                "hic_r1": (
                    [pair.r1.platform for pair in hic_pairs] if use_hic_phasing else []
                ),
                "hic_r2": (
                    [pair.r2.platform for pair in hic_pairs] if use_hic_phasing else []
                ),
            },
            "assembly_input_artifact_refs": {
                "primary": [
                    (
                        cast(
                            ArtifactRefJson,
                            member.read_artifact_ref.model_dump(mode="json"),
                        )
                        if member.read_artifact_ref
                        else None
                    )
                    for member in primary_members
                ],
                "ultralong": (
                    [
                        (
                            cast(
                                ArtifactRefJson,
                                member.read_artifact_ref.model_dump(mode="json"),
                            )
                            if member.read_artifact_ref
                            else None
                        )
                        for member in ont_members
                    ]
                    if use_ultralong_ont
                    else []
                ),
                "hic_r1": (
                    [
                        (
                            cast(
                                ArtifactRefJson,
                                pair.r1.read_artifact_ref.model_dump(mode="json"),
                            )
                            if pair.r1.read_artifact_ref
                            else None
                        )
                        for pair in hic_pairs
                    ]
                    if use_hic_phasing
                    else []
                ),
                "hic_r2": (
                    [
                        (
                            cast(
                                ArtifactRefJson,
                                pair.r2.read_artifact_ref.model_dump(mode="json"),
                            )
                            if pair.r2.read_artifact_ref
                            else None
                        )
                        for pair in hic_pairs
                    ]
                    if use_hic_phasing
                    else []
                ),
            },
            "use_ultralong_ont": use_ultralong_ont,
            "use_hic_phasing": use_hic_phasing,
            "platform": primary_platform,
            "has_hifi": bool(hifi_refs),
            "has_hic": bool(hic_pairs),
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


def _members_for_read_type(
    members: list[RuntimeReadMember],
    read_type: NormalizedReadType,
) -> list[RuntimeReadMember]:
    platforms_by_read_type: dict[NormalizedReadType, set[str]] = {
        "pacbio_hifi": {"pacbio_hifi"},
        "pacbio_clr": {"clr"},
        "ont_raw": {"ont"},
        "ont_hq": {"ont"},
        "cyclone": {"cycloneseq"},
    }
    allowed = platforms_by_read_type[read_type]
    return [member for member in members if member.platform in allowed]


def infer_input_profile(state: TopLevelState) -> StateUpdate:
    """Write an AssemblyInputProfile and recommendation/final selection."""
    assembly_out = assembly_stage_output(state)
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
            recommended_route=cast(
                SelectedAssemblyRoute, _route_from_read_type(selection.read_type)
            ),
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
        selected_route = cast(
            SelectedAssemblyRoute, _route_from_read_type(selection.read_type)
        )
        decision = AssemblyRouteDecision(
            session_id=state["session_id"],
            selected_route=selected_route,
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

        return update_assembly_stage_output(
            state,
            {
                "status": "profile_ready",
                "input_profile": cast(
                    AssemblyInputProfileCursor, profile.model_dump(mode="json")
                ),
                "input_profile_ref": profile_ref,
                "route_decision": decision.model_dump(mode="json"),
                "route_decision_ref": decision_ref,
                "assembly_selection": cast(
                    AssemblySelectionCursor, selection.model_dump(mode="json")
                ),
                "selected_route": selected_route,
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
        reasoning=str(
            recommendation.get("reasoning") or "route selector recommendation"
        ),
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
    return update_assembly_stage_output(
        state,
        {
            "status": "profile_ready",
            "input_profile": cast(
                AssemblyInputProfileCursor, profile.model_dump(mode="json")
            ),
            "input_profile_ref": profile_ref,
            "recommended_assembly_selection": cast(
                AssemblySelectionCursor,
                recommended_selection.model_dump(mode="json"),
            )
            if recommended_selection
            else None,
        },
    )


def select_route(state: TopLevelState) -> StateUpdate:
    """Confirm or block on the assembly selection recommendation."""
    assembly_out = assembly_stage_output(state)
    if assembly_out.get("status") != "profile_ready":
        return {}
    if assembly_out.get("assembly_selection"):
        return update_assembly_stage_output(state, {"status": "route_selected"})

    profile = dict(assembly_out.get("input_profile") or {})
    recommended_route = profile.get("recommended_route")
    if recommended_route == "insufficient_data":
        return _block_for_route_review(state, "assembly_route_insufficient_data")

    # All LLM recommendations require human gate (unless max_blocked_seconds configured)
    return _block_for_route_review(
        state, "assembly_selection_human_confirmation_required"
    )


def apply_route_decision(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    _selection_source: str = "human",
) -> StateUpdate:
    """Apply a human or timeout assembly-selection gate decision.

    Args:
        state: Current state
        request: Resume request
        _selection_source: Internal source set by timeout logic, not user-controllable
    """
    if request.stage != "assembly":
        raise ResumeValidationError("assembly selection resume requires stage assembly")
    if request.decision == "stop_human_review":
        (
            review_request_id,
            review_request_hash,
            review_request_ref,
        ) = _route_review_binding(state, request)
        decision_ref = persist_review_decision(
            state["data_dir"],
            session_id=state["session_id"],
            review_request_id=review_request_id,
            stage="assembly",
            gate_id="route_selection",
            decision=request,
            request_hash=review_request_hash,
            request_ref=review_request_ref,
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
    if selected_route is None:
        raise ResumeValidationError(
            f"unsupported assembly selection decision: {request.decision}"
        )

    assembly_out = assembly_stage_output(state)
    read_type, read_type_source = _requested_read_type(
        request, assembly_out, selected_route
    )
    method = _requested_method(request, assembly_out, read_type)
    try:
        selection = AssemblySelection(
            read_type=read_type,
            primary_method=method,
            source="timeout" if _selection_source == "timeout" else "human",
            rationale="; ".join(request.notes)
            if request.notes
            else f"{_selection_source} selected {selected_route} read type",
            metadata={
                "resume_decision": request.decision,
                **request.metadata,
                "read_type_source": read_type_source,
            },
        )
    except ValueError as exc:
        raise ResumeValidationError(str(exc)) from exc

    decided_by: Literal["auto_rule", "human_override"] = (
        "auto_rule" if _selection_source == "timeout" else "human_override"
    )

    decision = AssemblyRouteDecision(
        session_id=state["session_id"],
        selected_route=selected_route,
        decided_by=decided_by,
        rationale=selection.rationale,
        metadata={
            "assembly_selection": cast(
                AssemblySelectionCursor, selection.model_dump(mode="json")
            ),
            "resume_decision": request.decision,
            **request.metadata,
            "read_type_source": read_type_source,
        },
    )
    review_request_id, review_request_hash, review_request_ref = _route_review_binding(
        state, request
    )
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=review_request_id,
        stage="assembly",
        gate_id="route_selection",
        decision=decision,
        request_hash=review_request_hash,
        request_ref=review_request_ref,
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

    stage_update = update_assembly_stage_output(
        state,
        {
            "status": "route_selected",
            "route_decision": decision.model_dump(mode="json"),
            "route_decision_ref": decision_ref.model_dump(mode="json"),
            "assembly_selection": cast(
                AssemblySelectionCursor, selection.model_dump(mode="json")
            ),
            "selected_route": selected_route,
        },
    )
    return {
        **stage_update,
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
    }


def _route_review_binding(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> tuple[str, str, ArtifactRef]:
    active_interrupt: Mapping[str, object] = state.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = request.review_request_hash or active_interrupt.get(
        "request_hash"
    )
    request_ref_value = active_interrupt.get("request_ref")
    if not review_request_id or not review_request_hash or request_ref_value is None:
        raise ResumeValidationError(
            "assembly route decision is missing review request binding"
        )
    try:
        request_ref = ArtifactRef.model_validate(request_ref_value)
        request_record = load_review_request(
            state["data_dir"], request_ref, session_id=state["session_id"]
        )
    except (ReviewPersistenceError, ValueError, TypeError) as exc:
        raise ResumeValidationError(
            "assembly route review request is missing or changed"
        ) from exc
    if (
        request_ref.sha256 != str(review_request_hash)
        or request_record.get("review_request_id") != str(review_request_id)
        or request_record.get("stage") != "assembly"
        or request_record.get("gate_id") != "route_selection"
        or request_record.get("workflow_step_id")
        != active_interrupt.get("workflow_step_id")
    ):
        raise ResumeValidationError("assembly route review request binding mismatch")
    return str(review_request_id), str(review_request_hash), request_ref


def apply_selection_timeout(
    state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]
) -> StateUpdate:
    """Commit the current assembly recommendation after a configured timeout."""
    active_interrupt: Mapping[str, object] = state.get("active_interrupt") or {}
    request_ref = active_interrupt.get("request_ref")
    try:
        request_record = load_review_request(
            state["data_dir"],
            request_ref if isinstance(request_ref, dict) else {},
            session_id=state["session_id"],
        )
    except (KeyError, ReviewPersistenceError, ValueError) as exc:
        raise ResumeValidationError(
            "assembly route timeout request is missing or changed"
        ) from exc
    request_payload = request_record.get("payload") or {}
    selection_payload = request_payload.get("recommended_assembly_selection")
    if not isinstance(selection_payload, dict):
        raise ResumeValidationError(
            "assembly route timeout has no recommended assembly selection"
        )
    try:
        route = _route_from_read_type(str(selection_payload.get("read_type")))
    except ValueError as exc:
        raise ResumeValidationError(
            "assembly route timeout recommendation has an invalid read type"
        ) from exc
    decision = {
        "hifi": "select_hifi",
        "ont": "select_ont",
        "cyclone": "select_cyclone",
    }.get(route)
    if not decision:
        raise ResumeValidationError(
            "assembly route timeout recommendation has no registered decision"
        )
    timeout_request = request.model_copy(
        update={
            "decision": decision,
            "method_name": selection_payload.get("primary_method"),
            "notes": [
                *request.notes,
                (
                    "timeout committed recommended assembly selection: "
                    f"{selection_payload.get('primary_method')}"
                ),
            ],
            "metadata": {
                **request.metadata,
                "read_type": selection_payload.get("read_type"),
                "blocked_timeout": timeout,
                "auto_accepted": True,
            },
        }
    )
    return apply_route_decision(state, timeout_request, _selection_source="timeout")


def build_assembly_candidate_set(state: TopLevelState) -> StateUpdate:
    """Build compatible and rejected assembly candidates from descriptors."""
    assembly_out = assembly_stage_output(state)
    if assembly_out.get("status") not in {"resolved", "route_selected"}:
        return {}

    selection = _selection_from_output(assembly_out)
    if selection is None:
        return _fail_update(
            state, "assembly_selection is required before candidate planning"
        )

    registry = load_descriptor_registry()
    planned_methods = _planned_candidate_methods(selection)
    missing = [method for method in planned_methods if method not in registry.methods]
    if missing:
        return _fail_update(state, f"assembly descriptors missing: {missing}")

    genome_size_prior = assembly_out.get("genome_size_prior")
    candidate_plans: list[AssemblyCandidatePlanCursor] = []
    rejected: dict[str, str] = {}
    for method_name in planned_methods:
        requires_genome_size = method_name in GENOME_SIZE_REQUIRED
        rejection_reason = None
        if not is_method_read_type_compatible(method_name, selection.read_type):
            rejection_reason = f"method_read_type_incompatible:{selection.read_type}"
        elif requires_genome_size and not genome_size_prior:
            rejection_reason = "genome_size_required"
        status: Literal["rejected", "compatible"] = (
            "rejected" if rejection_reason else "compatible"
        )
        if rejection_reason:
            rejected[method_name] = rejection_reason
        candidate_plan = AssemblyCandidatePlan(
            method_name=method_name,
            status=status,
            required_inputs=["long_reads"],
            optional_inputs=["genome_survey_result", "genome_size_prior"],
            requires_genome_size=requires_genome_size,
            rejection_reason=rejection_reason,
        )
        candidate_plans.append(
            cast(
                AssemblyCandidatePlanCursor,
                candidate_plan.model_dump(mode="json"),
            )
        )

    return update_assembly_stage_output(
        state,
        {
            "status": "candidate_set_ready",
            "candidate_methods": planned_methods,
            "candidate_plans": candidate_plans,
            "rejected_candidates": rejected,
        },
    )


def _planned_candidate_methods(selection: AssemblySelection) -> list[str]:
    return list(dict.fromkeys([selection.primary_method, *ASSEMBLY_AUTO_CANDIDATES]))


def select_primary(state: TopLevelState) -> StateUpdate:
    """Select exactly one assembler-origin backbone candidate."""
    assembly_out = assembly_stage_output(state)
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

    if requested_primary:
        if requested_primary not in ASSEMBLY_CANDIDATES:
            return _block_for_human(
                state, f"unknown_assembly_method: {requested_primary}"
            )
        if requested_primary not in compatible:
            reason = assembly_out.get("rejected_candidates", {}).get(
                requested_primary, "method_not_compatible"
            )
            return _block_for_human(state, reason)
        primary = requested_primary
    elif compatible:
        primary = compatible[0]
    else:
        return _block_for_human(state, "no_compatible_assembly_candidate")

    return update_assembly_stage_output(
        state,
        {
            "status": "selected",
            "primary_method": primary,
        },
    )


def plan_candidate_attempts(state: TopLevelState) -> StateUpdate:
    """Write candidate bundles and select the optional local Flye route."""
    assembly_out = assembly_stage_output(state)
    if assembly_out.get("status") not in {
        "selected",
        "parameters_recommended",
        "parameters_approved",
    }:
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

    local_flye = (
        assembly_out.get("execution_mode") == "local"
        and assembly_out.get("primary_method") == FLYE_METHOD
    )
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
    execution_contract = None
    if local_flye or local_nextdenovo or local_hifiasm or local_hifiasm_ont:
        method_id = str(assembly_out["primary_method"])
        try:
            if ASSEMBLY_EXECUTION_CONTRACT_KEY in assembly_out:
                execution_contract = load_assembly_execution_contract(
                    assembly_out,
                    method_id=method_id,
                )
            else:
                execution_contract = build_assembly_execution_contract(
                    assembly_out,
                    method_id=method_id,
                )
        except AssemblyExecutionContractParameterError as exc:
            return update_assembly_stage_output(state, exc.stage_update())
        except ValueError as exc:
            return _fail_update(
                state,
                f"Assembly execution contract is invalid: {exc}",
            )
    planned_candidate_bundle_ref: dict[str, Any] | None = None
    for method_name in [assembly_out["primary_method"]]:
        short_name = method_name.rsplit(".", 1)[-1]
        bundle_path = candidate_dir / f"{short_name}_candidate.planned.json"
        execution_enabled = (
            (local_flye and method_name == FLYE_METHOD)
            or (local_nextdenovo and method_name == NEXTDENOVO_METHOD)
            or (local_hifiasm and method_name == HIFIASM_METHOD)
            or (local_hifiasm_ont and method_name == HIFIASM_ONT_METHOD)
        )
        bundle = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=f"{session_id}:{method_name}:{attempt_label}",
            method_name=method_name,
            status="planned",
            input_type=assembly_out["input_type"],
            input_member_ids=dict(assembly_out.get("assembly_input_member_ids") or {}),
            input_member_platforms=dict(
                assembly_out.get("assembly_input_member_platforms") or {}
            ),
            parameter_attempt_id=parameter_attempt_id,
            genome_size_prior=assembly_out.get("genome_size_prior"),
            expected_outputs=["primary_assembly"],
        )
        temporary = stage_work_dir(
            state,
            "assembly",
            "candidate_planning",
            f".{bundle_path.name}.{uuid4().hex}.tmp",
        )
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(
                bundle.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifact = ArtifactMeta.from_file(
            temporary,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            metadata={
                "candidate_id": bundle.candidate_id,
                "execution_enabled": execution_enabled,
            },
        )
        try:
            published = publish_artifact_record(
                data_dir,
                session_id,
                artifact,
                destination=bundle_path,
            )
        finally:
            temporary.unlink(missing_ok=True)
        bundle_path = published.path
        planned_candidate_bundle_ref = published.ref.model_dump(mode="json")

    if planned_candidate_bundle_ref is None:
        return _fail_update(
            state,
            "selected Assembly method produced no candidate plan",
        )

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
    plan_update: AssemblyStageCursor = {
        "status": status,
        "planned_candidate_bundle_ref": cast(
            ArtifactRefJson, planned_candidate_bundle_ref
        ),
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
                "skip_reason": None
                if local_nextdenovo
                else "local_execution_not_enabled",
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
                "skip_reason": None
                if local_hifiasm_ont
                else "local_execution_not_enabled",
            },
        },
    }
    if execution_contract is not None:
        plan_update["assembly_execution_contract"] = execution_contract.model_dump(
            mode="json"
        )
    return update_assembly_stage_output(state, plan_update)


def commit_assembly_plan(state: TopLevelState) -> StateUpdate:
    """Route compact assembly candidate refs to assembly_review."""
    assembly_out = assembly_stage_output(state)
    committed_keys = [
        "artifact_refs",
        "candidate_methods",
        "candidate_plans",
        "draft_assembly_ref",
        "executed_candidate_bundle_ref",
        "execution_mode",
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
        "planned_candidate_bundle_ref",
        "rejected_candidates",
        "status",
        "retry_counts",
        "substeps",
        ASSEMBLY_EXECUTION_CONTRACT_KEY,
        "assembly_input_member_ids",
        "assembly_input_member_platforms",
        "assembly_input_artifact_refs",
        "use_ultralong_ont",
        "use_hic_phasing",
        "ultralong_read_refs",
        "hic_r1_reads",
        "hic_r2_reads",
    ]
    serialized_output = ASSEMBLY_STAGE_CURSOR_ADAPTER.dump_python(
        assembly_out, mode="json"
    )
    if not isinstance(serialized_output, dict):
        raise TypeError("assembly stage cursor must serialize to a JSON object")
    committed = {
        key: cast(JsonValue, serialized_output[key])
        for key in committed_keys
        if key in serialized_output
    }
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = committed
    trace = append_route_trace(state, "assembly_stage")
    update: StateUpdate = {
        **advance_workflow_step(state, "assembly"),
        "route_trace": trace,
        "stage_outputs": outputs,
    }
    executed_bundle_ref = assembly_out.get("executed_candidate_bundle_ref")
    if executed_bundle_ref is not None:
        try:
            verified = load_executed_candidate_bundle(
                absolute_data_dir(state),
                state["session_id"],
                ArtifactRef.model_validate(executed_bundle_ref),
                expected_method_name=assembly_out.get("primary_method"),
            )
        except (OSError, TypeError, ValueError) as exc:
            return _fail_update(state, f"assembly candidate bundle is invalid: {exc}")
        checkpoint_primary = assembly_out.get("draft_assembly_ref")
        if checkpoint_primary and Path(checkpoint_primary).resolve(
            strict=False
        ) != verified.primary_output_path.resolve(strict=False):
            return _fail_update(
                state,
                "assembly draft path does not match executed candidate primary output",
            )
        update["resource_refs"] = update_resource_ref(
            state,
            "assembly",
            [str(verified.primary_output_path)],
            "assembly",
            {
                "candidate_bundle_ref": verified.ref.model_dump(mode="json"),
                "primary_output_ref": verified.primary_output_ref.model_dump(
                    mode="json"
                ),
                "selected_method": verified.bundle.method_name,
                "candidate_id": verified.bundle.candidate_id,
            },
        )
    elif assembly_out.get("draft_assembly_ref"):
        return _fail_update(
            state,
            "assembly draft output has no executed candidate bundle ref",
        )
    return update


def fail_assembly(state: TopLevelState) -> StateUpdate:
    """Stop workflow on assembly planning failure or preflight block."""
    assembly_out = assembly_stage_output(state)
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


def block_assembly_for_human(state: TopLevelState) -> StateUpdate:
    """Pause workflow for a human assembly planning decision."""
    assembly_out = assembly_stage_output(state)
    reason = assembly_out.get("block_reason", "assembly decision required")
    interrupt = assembly_out.get("active_interrupt")
    if interrupt:
        outputs = dict(state.get("stage_outputs") or {})
        serialized_output = ASSEMBLY_STAGE_CURSOR_ADAPTER.dump_python(
            assembly_out, mode="json"
        )
        if not isinstance(serialized_output, dict):
            raise TypeError("assembly stage cursor must serialize to a JSON object")
        compact_output = cast(StageCursor, serialized_output)
        compact_output.pop("active_interrupt", None)
        outputs["assembly"] = compact_output
        return {
            "current_stage": "assembly",
            "supervision_state": "blocked_human",
            "active_interrupt": cast(ActiveInterrupt, interrupt),
            "stage_outputs": outputs,
        }
    persisted = _block_for_route_review(state, reason)
    outputs = dict(persisted["stage_outputs"])
    persisted_cursor = ASSEMBLY_STAGE_CURSOR_ADAPTER.validate_python(
        outputs.get("assembly") or {}
    )
    interrupt = persisted_cursor.get("active_interrupt")
    persisted_output = dict(outputs.get("assembly") or {})
    persisted_output.pop("active_interrupt", None)
    outputs["assembly"] = persisted_output
    return {
        "current_stage": "assembly",
        "supervision_state": "blocked_human",
        "active_interrupt": cast(ActiveInterrupt | None, interrupt),
        "stage_outputs": outputs,
    }


def _block_for_human(state: TopLevelState, reason: str) -> StateUpdate:
    return update_assembly_stage_output(
        state, {"status": "blocked_human", "block_reason": reason}
    )


def _block_for_route_review(state: TopLevelState, reason: str) -> StateUpdate:
    assembly_out = assembly_stage_output(state)
    profile = assembly_out.get("input_profile")
    recommended_selection_value = assembly_out.get("recommended_assembly_selection")
    if profile is None:
        raise ValueError("assembly input profile is missing")
    recommended_selection: Mapping[str, object] = (
        recommended_selection_value if recommended_selection_value is not None else {}
    )

    payload = ReviewPayload(
        stage="assembly",
        gate_id="route_selection",
        reason=reason,
        decisions_allowed=[
            "select_cyclone",
            "select_ont",
            "select_hifi",
            "stop_human_review",
        ],
        signals=[],
        notes=[
            f"Recommended read_type: {recommended_selection.get('read_type')}",
            f"Recommended method: {recommended_selection.get('primary_method')}",
            f"Reasoning: {profile.get('recommendation_reasoning')}",
        ]
        + list(profile.get("uncertainty_notes", [])),
        auto_decidable=False,
    )

    request_payload = {
        **payload.model_dump(mode="json"),
        "recommended_route": profile.get("recommended_route"),
        "recommended_assembly_selection": recommended_selection,
        "recommendation_reasoning": profile.get("recommendation_reasoning"),
        "uncertainty_notes": profile.get("uncertainty_notes", []),
        "available_routes": _available_routes(assembly_out),
        "available_read_types": sorted(
            {
                route
                for route in [
                    "cyclone",
                    "ont_raw",
                    "ont_hq",
                    "pacbio_clr",
                    "pacbio_hifi",
                ]
            }
        ),
        "compatible_methods": {
            read_type: compatible_methods(read_type)
            for read_type in cast(
                list[NormalizedReadType],
                [
                    "cyclone",
                    "ont_raw",
                    "ont_hq",
                    "pacbio_clr",
                    "pacbio_hifi",
                ],
            )
        },
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
        review_request_artifact_ref=persisted_request.request_ref.model_dump(
            mode="json"
        ),
        auto_decidable=False,
        review_request_id=persisted_request.review_request_id,
    )
    interrupt = persisted_request.compact_interrupt(
        stage="assembly",
        gate_id="route_selection",
        reason=reason,
    )

    return update_assembly_stage_output(
        state,
        {
            "status": "blocked_human",
            "block_reason": reason,
            "active_interrupt": validated_assembly_stage_payload(
                {"active_interrupt": interrupt}
            ).get("active_interrupt"),
        },
    )


def _available_routes(assembly_out: AssemblyStageCursor) -> list[str]:
    routes = []
    if assembly_out.get("ont_reads_ref"):
        routes.extend(["cyclone", "ont"])
    if assembly_out.get("hifi_reads_ref") or assembly_out.get("has_hifi"):
        routes.append("hifi")
    return ordered_unique(routes)


def _selection_from_output(
    assembly_out: AssemblyStageCursor,
) -> AssemblySelection | None:
    payload = assembly_out.get("assembly_selection")
    if isinstance(payload, AssemblySelection):
        return payload
    if isinstance(payload, dict):
        try:
            return validate_assembly_selection(payload)
        except ValueError:
            return None
    return None


def _selection_from_route(
    route: str | None, *, source: AssemblySelectionSource, rationale: str
) -> AssemblySelection | None:
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


def _requested_read_type(
    request: ResumeReviewRequest,
    assembly_out: AssemblyStageCursor,
    selected_route: SelectedAssemblyRoute,
) -> tuple[NormalizedReadType, str]:
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
                f"assembly selection {request.decision!r} maps to route "
                f"{selected_route!r}, "
                f"but {source}={raw_value!r} normalizes to read_type {read_type!r}"
            )
        return read_type, source

    prior_read_type = resolve_read_type(
        (
            "read_type_prior",
            _normalized_read_type_from_prior(assembly_out.get("read_type_prior")),
        ),
        context="assembly.requested_read_type",
    )
    if prior_read_type and _route_from_read_type(prior_read_type) == selected_route:
        return prior_read_type, "read_type_prior"
    return route_read_type, "selected_route"


def _requested_method(
    request: ResumeReviewRequest,
    assembly_out: AssemblyStageCursor,
    read_type: NormalizedReadType,
) -> str:
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
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    artifact = ArtifactMeta.from_file(
        temporary,
        stage="assembly",
        method_name=method_name,
        artifact_type=artifact_type,
    )
    try:
        published = publish_artifact_record(
            data_dir,
            session_id,
            artifact,
            destination=path,
        )
    finally:
        temporary.unlink(missing_ok=True)
    return published.ref.model_dump(mode="json")


def _build_input_profile(
    state: TopLevelState,
    assembly_out: AssemblyStageCursor,
    *,
    recommended_route: RecommendedAssemblyRoute,
    reasoning: str,
    uncertainty_notes: list[str],
    profile_source: InputProfileSource,
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
    assembly_out: AssemblyStageCursor,
    ctx: dict[str, Any],
) -> RouteRecommendation:
    prompt_payload = _route_selector_payload(state, assembly_out, ctx)
    decision = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="route_selection",
            session_id=state["session_id"],
            prompt_payload=prompt_payload,
            system_prompt="You are an expert genome assembly route selector.\n\n"
            + str(ctx.get("skill_text") or ""),
            allowed_actions=list(ctx.get("allowed_actions") or []),
        )
    )
    if decision.valid and decision.parsed:
        payload = decision.parsed
        raw_route = payload.get("recommended_route")
        recommended_route: RecommendedAssemblyRoute = (
            cast(RecommendedAssemblyRoute, raw_route)
            if isinstance(raw_route, str)
            and raw_route in {*ROUTE_VALUES, "insufficient_data"}
            else "insufficient_data"
        )
        raw_notes = payload.get("uncertainty_notes")
        uncertainty_notes = (
            cast(list[str], raw_notes)
            if isinstance(raw_notes, list)
            and all(isinstance(item, str) for item in raw_notes)
            else []
        )
        return {
            "recommended_route": recommended_route,
            "reasoning": str(
                payload.get("reasoning", payload.get("recommendation_reasoning", ""))
            ),
            "uncertainty_notes": uncertainty_notes,
            "source": "llm",
        }
    return {
        "recommended_route": "insufficient_data",
        "reasoning": (
            "route selector LLM unavailable; route selection requires human/data review"
        ),
        "source": "llm_unavailable",
        "uncertainty_notes": [
            f"route selector LLM unavailable: {decision.error or 'invalid decision'}",
        ],
    }


def _route_selector_payload(
    state: TopLevelState,
    assembly_out: AssemblyStageCursor,
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


def _read_type_prior(state: TopLevelState) -> dict[str, str] | None:
    assembly_out = assembly_stage_output(state)
    selection = _selection_from_output(assembly_out)
    if selection:
        return {
            **SHARED_TO_METHOD_READ_TYPE[selection.read_type],
            "shared_read_type": selection.read_type,
            "source": "assembly_selection",
        }
    shared = _shared_parameters(state)
    shared_read_type = normalize_read_type(shared.get("read_type"))
    if shared_read_type is not None:
        return {
            **SHARED_TO_METHOD_READ_TYPE[shared_read_type],
            "shared_read_type": shared_read_type,
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
    return resolve_read_type(
        ("read_type_prior.shared_read_type", prior.get("shared_read_type")),
        context="assembly.read_type_prior",
    )


def _shared_parameters(state: TopLevelState) -> dict[str, Any]:
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        return shared.model_dump(exclude_none=True)
    return dict(shared)


def _long_read_platform(state: TopLevelState) -> str | None:
    try:
        members = resolve_long_read_members(state)
    except ValueError:
        return None
    platforms = list(dict.fromkeys(member.platform for member in members))
    return platforms[0] if len(platforms) == 1 else None


def _genome_survey_result_ref(state: TopLevelState) -> str | None:
    survey = (state.get("stage_outputs") or {}).get("genome_survey") or {}
    value = survey.get("result_artifact_ref")
    return str(value) if value else None
