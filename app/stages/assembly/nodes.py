"""Assembly stage nodes for candidate planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.lifecycle import ResumeValidationError
from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.llm.factory import build_chat_model
from app.stages.assembly.constants import (
    ASSEMBLY_AUTO_CANDIDATES,
    ASSEMBLY_CANDIDATES,
    ASSEMBLY_COMPATIBILITY_CANDIDATES,
    FLYE_METHOD,
    FLYE_READ_TYPE_FLAGS,
    GENOME_SIZE_REQUIRED,
    HIFIASM_METHOD,
    NEXTDENOVO_METHOD,
)
from app.stages.assembly.knowledge import load_skill_context
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.schemas.artifacts import (
    ASSEMBLY_INPUT_PROFILE,
    ASSEMBLY_ROUTE_DECISION,
    ASSEMBLY_CANDIDATE_BUNDLE,
    ArtifactMeta,
    AssemblyCandidateBundle,
    AssemblyInputProfile,
    AssemblyRouteDecision,
)
from app.review.base import ResumeReviewRequest
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.plan import AssemblyCandidatePlan
from app.schemas.shared_parameters import PLATFORM_TO_SHARED_READ_TYPE, SHARED_TO_METHOD_READ_TYPE
from app.schemas.state import TopLevelState

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

    primary_long_reads_ref = hifi_reads_ref or ont_reads_ref
    if not primary_long_reads_ref:
        return _fail_update(state, "assembly requires long reads (clean_long_reads or long_reads.fofn)")

    primary_platform = "pacbio_hifi" if hifi_reads_ref else long_read_platform
    input_source = "read_qc" if read_qc_ref else "asset_registry"
    if hifi_reads_ref and not ont_reads_ref:
        input_source = "run_config" if assembly_out.get("hifi_reads_ref") else "asset_registry"

    genome_size_prior = _genome_size_prior(state)
    read_type_prior = _read_type_prior(state)
    target_level = assembly_out.get("target_level") or "draft"
    configured_route = assembly_out.get("assembly_route") or "auto"
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
        },
    )


def infer_input_profile(state: TopLevelState) -> dict:
    """Write an AssemblyInputProfile and optional explicit route decision."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "resolved_extended":
        return {}

    explicit_route = assembly_out.get("explicit_route")
    if explicit_route:
        profile = _build_input_profile(
            state,
            assembly_out,
            recommended_route=explicit_route,
            confidence=1.0,
            reasoning=f"run_config explicitly selected {explicit_route} route",
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
            selected_route=explicit_route,
            decided_by="run_config_explicit",
            rationale=profile.recommendation_reasoning,
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
                "selected_route": explicit_route,
            },
        )

    ctx = load_skill_context("route_selector")
    recommendation = _recommend_route_with_llm(state, assembly_out, ctx)
    recommended_route = recommendation["recommended_route"]
    confidence = float(recommendation["confidence"])
    if confidence < 0.5:
        recommended_route = "insufficient_data"

    profile = _build_input_profile(
        state,
        assembly_out,
        recommended_route=recommended_route,
        confidence=confidence,
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
        },
    )


def select_route(state: TopLevelState) -> dict:
    """Confirm or block on the route recommendation."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "profile_ready":
        return {}
    if assembly_out.get("route_decision"):
        return _update_assembly_output(state, {"status": "route_selected"})

    profile = dict(assembly_out.get("input_profile") or {})
    recommended_route = profile.get("recommended_route")
    confidence = float(profile.get("recommendation_confidence") or 0.0)
    if recommended_route == "insufficient_data":
        return _block_for_route_review(state, "assembly_route_insufficient_data")

    policy_mode = _assembly_review_policy_mode(assembly_out)
    if policy_mode == "auto" and confidence >= 0.7 and recommended_route in ROUTE_VALUES:
        decision = AssemblyRouteDecision(
            session_id=state["session_id"],
            selected_route=recommended_route,
            decided_by="llm_auto",
            rationale=str(profile.get("recommendation_reasoning") or "auto route selection"),
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
                "status": "route_selected",
                "selected_route": recommended_route,
                "route_decision": decision.model_dump(mode="json"),
                "route_decision_ref": decision_ref,
            },
        )

    return _block_for_route_review(state, "assembly_route_human_confirmation_required")


def apply_route_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict[str, Any]:
    """Apply a human route-selection gate decision."""
    if request.stage != "assembly":
        raise ResumeValidationError("assembly route resume requires stage assembly")
    if request.decision == "stop_human_review":
        return {
            "current_stage": "assembly",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "assembly route selection stopped by human review",
        }

    selected_route = ROUTE_DECISION_MAP.get(request.decision)
    if selected_route not in ROUTE_VALUES:
        raise ResumeValidationError(f"unsupported assembly route decision: {request.decision}")

    assembly_out = _assembly_output(state)
    decision = AssemblyRouteDecision(
        session_id=state["session_id"],
        selected_route=selected_route,
        decided_by="human_override",
        rationale="; ".join(request.notes) if request.notes else f"human selected {selected_route} route",
        metadata={"resume_decision": request.decision},
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
        "selected_route": selected_route,
        "route_decision": decision.model_dump(mode="json"),
        "route_decision_ref": decision_ref,
    }
    return {
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def build_assembly_candidate_set(state: TopLevelState) -> dict:
    """Build compatible and rejected assembly candidates from descriptors."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") not in {"resolved", "route_selected"}:
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    planned_methods = _planned_candidate_methods(assembly_out)
    missing = [method for method in planned_methods if method not in registry.methods]
    if missing:
        return _fail_update(state, f"assembly descriptors missing: {missing}")

    genome_size_prior = assembly_out.get("genome_size_prior")
    candidate_plans: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    for method_name in planned_methods:
        requires_genome_size = method_name in GENOME_SIZE_REQUIRED
        rejection_reason = "genome_size_required" if requires_genome_size and not genome_size_prior else None
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


def _planned_candidate_methods(assembly_out: dict[str, Any]) -> list[str]:
    decision = dict(assembly_out.get("method_decision") or {})
    methods = list(ASSEMBLY_AUTO_CANDIDATES)
    for key in ["primary", "fallback"]:
        method = decision.get(key)
        if method in ASSEMBLY_COMPATIBILITY_CANDIDATES and method not in methods:
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
    decision = dict(assembly_out.get("method_decision") or {})
    requested_primary = decision.get("primary")
    requested_fallback = decision.get("fallback")

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


def plan_candidate_attempts(state: TopLevelState) -> dict:
    """Write candidate bundles and select the optional local Flye route."""
    assembly_out = _assembly_output(state)
    if assembly_out.get("status") != "selected":
        return {}

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    candidate_dir = data_dir / "artifacts" / "assembly" / session_id / "candidates"
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
    candidate_refs: list[str] = []
    artifacts: list[ArtifactMeta] = []
    selected_plans = []
    for method_name in assembly_out["selected_methods"]:
        short_name = method_name.rsplit(".", 1)[-1]
        fasta_path = data_dir / "artifacts" / "assembly" / session_id / short_name / "draft_assembly.fasta"
        if method_name == NEXTDENOVO_METHOD:
            log_path = data_dir / "work" / "assembly" / session_id / "nextdenovo" / "run.cfg"
        elif method_name == HIFIASM_METHOD:
            log_path = data_dir / "work" / "assembly" / session_id / "hifiasm" / "run_hifiasm.sh"
        else:
            log_path = data_dir / "work" / "assembly" / session_id / short_name / "flye_out" / "flye.log"
        bundle_path = candidate_dir / f"{short_name}_candidate.json"
        execution_enabled = (
            (local_flye and method_name == FLYE_METHOD)
            or (local_nextdenovo and method_name == NEXTDENOVO_METHOD)
            or (local_hifiasm and method_name == HIFIASM_METHOD)
        )
        method_graph_requested = execution_enabled
        bundle = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=f"{session_id}:{method_name}",
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
        "method_application_notes",
        "primary_method",
        "primary_long_read_platform",
        "rejected_candidates",
        "selected_candidate_plans",
        "selected_candidate_refs",
        "selected_methods",
        "status",
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
    assembly_out = _assembly_output(state)
    profile = dict(assembly_out.get("input_profile") or {})
    interrupt = {
        "stage": "assembly",
        "gate_id": "route_selection",
        "reason": reason,
        "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
        "recommended_route": profile.get("recommended_route"),
        "recommendation_reasoning": profile.get("recommendation_reasoning"),
        "uncertainty_notes": profile.get("uncertainty_notes", []),
        "available_routes": _available_routes(assembly_out),
    }
    return _update_assembly_output(
        state,
        {
            "status": "blocked_human",
            "block_reason": reason,
            "active_interrupt": interrupt,
        },
    )


def _available_routes(assembly_out: dict[str, Any]) -> list[str]:
    routes = []
    if assembly_out.get("ont_reads_ref"):
        routes.extend(["cyclone", "ont"])
    if assembly_out.get("hifi_reads_ref") or assembly_out.get("has_hifi"):
        routes.append("hifi")
    return _unique_refs(routes)


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


def _build_input_profile(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    *,
    recommended_route: str,
    confidence: float,
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
        recommendation_confidence=confidence,
        recommendation_reasoning=reasoning,
        uncertainty_notes=uncertainty_notes,
        profile_source=profile_source,
        notes=[],
        metadata={"route_selector_version": "phase8_slice1"},
    )


def _recommend_route_with_llm(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    messages = _route_selector_messages(state, assembly_out, ctx)
    try:
        response = build_chat_model().invoke(messages)
        raw_content = getattr(response, "content", response)
        payload = json.loads(str(raw_content))
        allowed = set(ctx.get("allowed_actions") or [])
        recommended = payload.get("recommended_route")
        if recommended not in allowed:
            raise ValueError(f"recommended_route {recommended!r} not in allowed actions")
        return {
            "recommended_route": recommended,
            "confidence": float(payload.get("confidence", payload.get("recommendation_confidence", 0.0))),
            "reasoning": str(payload.get("reasoning", payload.get("recommendation_reasoning", ""))),
            "uncertainty_notes": list(payload.get("uncertainty_notes") or []),
            "source": "llm",
        }
    except Exception as exc:
        return {
            "recommended_route": "insufficient_data",
            "confidence": 0.0,
            "reasoning": "route selector LLM unavailable; route selection requires human/data review",
            "source": "llm_unavailable",
            "uncertainty_notes": [
                f"route selector LLM unavailable: {type(exc).__name__}",
            ],
        }


def _route_selector_messages(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    ctx: dict[str, Any],
) -> list[dict[str, str]]:
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
    return [
        {
            "role": "system",
            "content": "You are an expert genome assembly route selector.\n\n"
            + str(ctx.get("skill_text") or ""),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "data_profile": data_profile,
                    "rules": ctx.get("rules") or {},
                    "cases": ctx.get("cases") or [],
                    "allowed_actions": ctx.get("allowed_actions") or [],
                    "required_output": {
                        "recommended_route": "one of allowed_actions",
                        "confidence": "float from 0 to 1",
                        "reasoning": "short explanation",
                        "uncertainty_notes": "list of strings",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]

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
