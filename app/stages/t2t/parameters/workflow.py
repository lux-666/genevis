"""T2T sparse threshold recommendation and mandatory approval workflow."""

from __future__ import annotations

from typing import Any

from app.control.lifecycle import ResumeValidationError
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
    update_current_parameter_attempt,
)
from app.llm.parameters.t2t.recommender import (
    PROMPT_BUNDLE,
    recommend_t2t_parameters as invoke_t2t_parameter_model,
    render_t2t_parameter_messages,
    restore_t2t_parameter_decision,
    t2t_parameter_prompt_hash,
)
from app.llm.parameters.t2t.motif import (
    MOTIF_RESOLUTION_CONTRACT,
    PROMPT_BUNDLE as MOTIF_PROMPT_BUNDLE,
    build_t2t_motif_request,
    render_t2t_motif_messages,
    resolve_t2t_motif as invoke_t2t_motif_model,
    restore_t2t_motif_decision,
    t2t_motif_prompt_hash,
)
from app.llm.parameters.t2t.request import (
    PARAMETER_RECOMMENDATION_CONTRACT,
    T2T_METHOD,
    build_t2t_parameter_request,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    T2T_PARAMETER_APPROVAL_DECISION,
    T2T_PARAMETER_BASIS,
    T2T_PARAMETER_FROZEN_PARAMETERS,
    T2T_PARAMETER_INPUT_SNAPSHOT,
    T2T_PARAMETER_MODEL_REQUEST,
    T2T_PARAMETER_MODEL_RESPONSE,
    T2T_PARAMETER_RECOMMENDATION,
    T2T_PARAMETER_RECOMMENDATION_MANIFEST,
    T2T_PARAMETER_RESPONSE_PARSE,
    T2T_MOTIF_INPUT_SNAPSHOT,
    T2T_MOTIF_MODEL_REQUEST,
    T2T_MOTIF_MODEL_RESPONSE,
    T2T_MOTIF_RESOLUTION,
    T2T_MOTIF_RESOLUTION_MANIFEST,
    T2T_MOTIF_RESPONSE_PARSE,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.run_config import T2TThresholdProfile
from app.schemas.shared_parameters import normalize_dna_motif
from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.schemas.parameter_recommendation import (
    CanonicalSpeciesFacts,
    RecommendationDependencyRefs,
    T2TMotifInputSnapshotV3,
)
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.stages.t2t.parameters.catalog import load_t2t_parameter_catalog
from app.stages.t2t.parameters.storage import (
    T2TParameterPersistenceError,
    read_t2t_parameter_artifact,
    recover_t2t_parameter_artifact,
    write_t2t_parameter_artifact,
)
from app.storage.ledger import Ledger

_RECOMMENDER_METHOD = "t2t.parameter_recommender"
_APPROVAL_METHOD = "t2t.parameter_approval"
_MOTIF_RESOLVER_METHOD = "t2t.telomere_motif_resolver"


def resolve_t2t_telomere_motif(state: TopLevelState) -> dict:
    """Resolve one motif before any T2T recommendation or sequence scan."""

    output = stage_output(state, "t2t")
    if output.get("status") != "resolved":
        return {}
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(mode="json", exclude_none=True)
    canonical_facts = build_canonical_facts(
        shared_parameters=shared if isinstance(shared, dict) else {},
        read_qc_output=None,
        genome_survey_result=None,
    )
    species_facts: CanonicalSpeciesFacts = canonical_facts.species

    snapshot = T2TMotifInputSnapshotV3(
        facts=species_facts,
        dependency_refs=RecommendationDependencyRefs(),
    )
    try:
        request = build_t2t_motif_request(facts=species_facts)
        input_profile_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_input_profile.json",
            snapshot.model_dump(mode="json"),
            artifact_type=T2T_MOTIF_INPUT_SNAPSHOT,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
        messages = render_t2t_motif_messages(request)
        request_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_model_request.json",
            {
                "schema_version": 2,
                "prompt_bundle_id": MOTIF_PROMPT_BUNDLE,
                "prompt_template_sha256": t2t_motif_prompt_hash(),
                "messages": messages,
            },
            artifact_type=T2T_MOTIF_MODEL_REQUEST,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
    except (OSError, ValueError, T2TParameterPersistenceError) as exc:
        return _fail(state, f"T2T motif request failed: {exc}")

    response_ref: ArtifactRef | None = None
    try:
        recovered = recover_t2t_parameter_artifact(
            state,
            0,
            "motif_model_response.json",
            expected_type=T2T_MOTIF_MODEL_RESPONSE,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
        if recovered is None:
            decision = invoke_t2t_motif_model(request, messages=messages)
        else:
            response_ref, record = recovered
            decision = restore_t2t_motif_decision(record)
        if response_ref is None:
            response_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_model_response.json",
                {
                    "schema_version": 2,
                    "raw_response": decision.raw_response,
                    "validity": "parsed" if decision.valid else "invalid",
                    "error": decision.error,
                    "model_id": decision.model_id,
                },
                artifact_type=T2T_MOTIF_MODEL_RESPONSE,
                method_name=_MOTIF_RESOLVER_METHOD,
            )
        parse_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_response_parse.json",
            {
                "schema_version": 1,
                "parsed": decision.valid,
                "error": decision.error,
            },
            artifact_type=T2T_MOTIF_RESPONSE_PARSE,
            method_name="t2t.telomere_motif_response_parser",
        )
        resolution_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_resolution.json",
            {
                "schema_version": 1,
                "resolution_contract": MOTIF_RESOLUTION_CONTRACT,
                "status": decision.status if decision.valid else "invalid",
                "resolved_telomere_motif": decision.resolved_telomere_motif,
                "source": "llm_species_resolution",
                "summary": decision.summary,
            },
            artifact_type=T2T_MOTIF_RESOLUTION,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
        manifest_ref = write_t2t_parameter_artifact(
            state,
            0,
            "motif_resolution_manifest.json",
            {
                "schema_version": 1,
                "resolution_contract": MOTIF_RESOLUTION_CONTRACT,
                "status": decision.status if decision.valid else "invalid",
                "refs": {
                    "input_profile_ref": input_profile_ref.model_dump(mode="json"),
                    "model_request_ref": request_ref.model_dump(mode="json"),
                    "model_response_ref": response_ref.model_dump(mode="json"),
                    "response_parse_ref": parse_ref.model_dump(mode="json"),
                    "resolution_ref": resolution_ref.model_dump(mode="json"),
                },
            },
            artifact_type=T2T_MOTIF_RESOLUTION_MANIFEST,
            method_name=_MOTIF_RESOLVER_METHOD,
        )
    except (OSError, ValueError, T2TParameterPersistenceError) as exc:
        return _fail(state, f"T2T motif response persistence failed: {exc}")

    refs = {
        "telomere_motif_input_profile_ref": input_profile_ref.model_dump(mode="json"),
        "telomere_motif_model_request_ref": request_ref.model_dump(mode="json"),
        "telomere_motif_model_response_ref": response_ref.model_dump(mode="json"),
        "telomere_motif_response_parse_ref": parse_ref.model_dump(mode="json"),
        "telomere_motif_resolution_manifest_ref": manifest_ref.model_dump(mode="json"),
    }
    failure_refs = {
        **refs,
        "telomere_motif_resolution_ref": resolution_ref.model_dump(mode="json"),
    }
    if not decision.valid:
        return _fail_with_motif_refs(
            state,
            "T2T telomere motif response could not be parsed",
            failure_refs,
        )
    if decision.status != "resolved" or not decision.resolved_telomere_motif:
        return _fail_with_motif_refs(
            state,
            "T2T telomere motif is unresolved for the supplied species facts",
            failure_refs,
        )
    return _persist_t2t_motif_resolution(
        state,
        motif=decision.resolved_telomere_motif,
        source="llm_species_resolution",
        summary=decision.summary,
        resolution_ref=resolution_ref,
        refs=refs,
    )


def _persist_t2t_motif_resolution(
    state: TopLevelState,
    *,
    motif: str,
    source: str,
    summary: str | None,
    refs: dict[str, Any],
    resolution_ref: ArtifactRef | None = None,
) -> dict:
    """Persist the compact result used by every downstream T2T operation."""

    if resolution_ref is None:
        try:
            resolution_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_resolution.json",
                {
                    "schema_version": 1,
                    "resolution_contract": MOTIF_RESOLUTION_CONTRACT,
                    "status": "resolved",
                    "resolved_telomere_motif": motif,
                    "source": source,
                    "summary": summary,
                },
                artifact_type=T2T_MOTIF_RESOLUTION,
                method_name=_MOTIF_RESOLVER_METHOD,
            )
        except (OSError, ValueError, T2TParameterPersistenceError) as exc:
            return _fail(state, f"T2T motif resolution persistence failed: {exc}")
    return update_stage_output(
        state,
        "t2t",
        {
            "status": "motif_resolved",
            "resolved_telomere_motif": motif,
            "resolved_telomere_motif_source": source,
            "resolved_telomere_motif_summary": summary,
            "telomere_motif_resolution_ref": resolution_ref.model_dump(mode="json"),
            **refs,
        },
    )


def _fail_with_motif_refs(
    state: TopLevelState,
    reason: str,
    refs: dict[str, Any],
) -> dict:
    return update_stage_output(
        state,
        "t2t",
        {"status": "failed", "reason": reason, **refs},
    )


def recommend_t2t_parameters(state: TopLevelState) -> dict:
    """Persist one sparse recommendation and block at mandatory approval."""

    output = stage_output(state, "t2t")
    if output.get("status") != "motif_resolved":
        return {}
    read_refs = list(output.get("ont_read_refs") or [])
    if not read_refs:
        return {}
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(mode="json", exclude_none=True)
    species_facts = shared.get("species_facts") if isinstance(shared, dict) else None
    input_profile = {
        "schema_version": 2,
        "species_facts": species_facts,
        "resolved_telomere_motif": output["resolved_telomere_motif"],
        "ont_asset_count": len(read_refs),
        "ont_read_refs": read_refs,
        "input_assembly_ref": output["input_assembly_ref"],
    }
    try:
        catalog = load_t2t_parameter_catalog()
        request = build_t2t_parameter_request(
            input_profile=input_profile, catalog=catalog
        ).model_dump(mode="json", exclude_none=True)
        input_profile_ref = write_t2t_parameter_artifact(
            state,
            0,
            "input_profile.json",
            input_profile,
            artifact_type=T2T_PARAMETER_INPUT_SNAPSHOT,
            method_name=_RECOMMENDER_METHOD,
        )
        basis_ref = write_t2t_parameter_artifact(
            state,
            0,
            "basis_snapshot.json",
            {
                "schema_version": 2,
                "catalog_snapshot": catalog.model_dump(mode="json"),
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
                "executor_defaults": dict(output.get("threshold_defaults") or {}),
            },
            artifact_type=T2T_PARAMETER_BASIS,
            method_name=_RECOMMENDER_METHOD,
        )
        messages = render_t2t_parameter_messages(request)
        request_ref = write_t2t_parameter_artifact(
            state,
            0,
            "model_request.json",
            {
                "schema_version": 2,
                "prompt_bundle_id": PROMPT_BUNDLE,
                "prompt_template_sha256": t2t_parameter_prompt_hash(),
                "messages": messages,
            },
            artifact_type=T2T_PARAMETER_MODEL_REQUEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, T2TParameterPersistenceError) as exc:
        return _fail(state, f"T2T parameter request failed: {exc}")

    response_ref: ArtifactRef | None = None
    try:
        recovered = recover_t2t_parameter_artifact(
            state,
            0,
            "model_response.json",
            expected_type=T2T_PARAMETER_MODEL_RESPONSE,
            method_name=_RECOMMENDER_METHOD,
        )
        if recovered is None:
            decision = invoke_t2t_parameter_model(request, messages=messages)
        else:
            response_ref, record = recovered
            decision = restore_t2t_parameter_decision(record)
        if response_ref is None:
            response_ref = write_t2t_parameter_artifact(
                state,
                0,
                "model_response.json",
                {
                    "schema_version": 2,
                    "raw_response": decision.raw_response,
                    "validity": "parsed" if decision.valid else "invalid",
                    "error": decision.error,
                    "model_id": decision.model_id,
                },
                artifact_type=T2T_PARAMETER_MODEL_RESPONSE,
                method_name=_RECOMMENDER_METHOD,
            )
        recommendation = decision.recommendation or {}
        recommendation_ref = write_t2t_parameter_artifact(
            state,
            0,
            "recommendation.json",
            {
                "schema_version": 2,
                "method_id": T2T_METHOD,
                "task": "recommend_initial",
                "recommendation": recommendation,
            },
            artifact_type=T2T_PARAMETER_RECOMMENDATION,
            method_name=_RECOMMENDER_METHOD,
        )
        parse_ref = write_t2t_parameter_artifact(
            state,
            0,
            "response_parse.json",
            {"schema_version": 1, "parsed": decision.valid, "error": decision.error},
            artifact_type=T2T_PARAMETER_RESPONSE_PARSE,
            method_name="t2t.parameter_response_parser",
        )
        manifest_ref = write_t2t_parameter_artifact(
            state,
            0,
            "recommendation_manifest.json",
            {
                "schema_version": 2,
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
                "stage": "t2t",
                "method_id": T2T_METHOD,
                "task": "recommend_initial",
                "recommendation_id": 0,
                "status": "pending_approval" if decision.valid else "response_invalid",
                "previous_attempt_id": None,
                "previous_attempt_manifest_ref": None,
                "previous_attempt_seal_hash": None,
                "refs": {
                    "input_profile_ref": input_profile_ref.model_dump(mode="json"),
                    "basis_snapshot_ref": basis_ref.model_dump(mode="json"),
                    "model_request_ref": request_ref.model_dump(mode="json"),
                    "model_response_ref": response_ref.model_dump(mode="json"),
                    "recommendation_ref": recommendation_ref.model_dump(mode="json"),
                    "response_parse_ref": parse_ref.model_dump(mode="json"),
                },
                "summary": {"response_parsed": decision.valid},
            },
            artifact_type=T2T_PARAMETER_RECOMMENDATION_MANIFEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, T2TParameterPersistenceError) as exc:
        return _fail(state, f"T2T parameter response persistence failed: {exc}")

    cursor = ParameterRecommendationCursor(
        recommendation_id=0,
        method_id=T2T_METHOD,
        task="recommend_initial",
        status="pending_approval" if decision.valid else "response_invalid",
        manifest_ref=manifest_ref,
        recommendation_hash=manifest_ref.sha256,
    )
    if not decision.valid:
        return update_stage_output(
            state,
            "t2t",
            {
                "status": "parameter_response_invalid",
                "reason": "T2T parameter recommendation response could not be parsed",
                "current_parameter_recommendation": cursor.model_dump(mode="json"),
            },
        )

    bindings = {
        "attempt_id": 0,
        "task": "recommend_initial",
        "previous_attempt_id": None,
        "recommendation_hash": manifest_ref.sha256,
    }
    try:
        persisted = persist_review_request(
            state,
            stage="t2t",
            gate_id="parameter_approval",
            reason="t2t_parameter_approval_required",
            payload={
                "schema_version": 2,
                "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "recommendation_manifest_hash": manifest_ref.sha256,
                "method_id": T2T_METHOD,
                "task": "recommend_initial",
                "resolved_telomere_motif": output["resolved_telomere_motif"],
                "resolved_telomere_motif_source": output.get(
                    "resolved_telomere_motif_source"
                ),
                "parameters": dict(recommendation.get("parameters") or {}),
                "rationale": recommendation.get("summary"),
                "response_parse_ref": parse_ref.model_dump(mode="json"),
                "recommendation_ref": recommendation_ref.model_dump(mode="json"),
                "decisions_allowed": ["approve_parameters", "reject_parameters"],
            },
            method_name=_APPROVAL_METHOD,
            artifact_type="t2t.parameter_approval.request",
            bindings=bindings,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        return _fail(state, f"T2T parameter approval request failed: {exc}")
    cursor = cursor.model_copy(
        update={
            "review_request_id": persisted.review_request_id,
            "review_request_ref": persisted.request_ref,
        }
    )
    interrupt = persisted.compact_interrupt(
        stage="t2t",
        gate_id="parameter_approval",
        reason="t2t_parameter_approval_required",
        bindings=bindings,
    )
    Ledger(absolute_data_dir(state)).record_review_requested(
        state["session_id"],
        "t2t",
        reason="t2t_parameter_approval_required",
        review_request_artifact_ref=persisted.request_ref.path_ref,
        review_request_id=persisted.review_request_id,
        auto_decidable=False,
        pending_human_confirmation=True,
    )
    return update_stage_output(
        state,
        "t2t",
        {
            "status": "blocked_human",
            "block_reason": "t2t_parameter_approval_required",
            "active_interrupt": interrupt,
            "current_parameter_attempt_id": 0,
            "proposed_parameter_attempt_id": 0,
            "parameter_attempts": [],
            "current_parameter_recommendation": cursor.model_dump(mode="json"),
        },
    )


def apply_t2t_parameter_approval_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> dict[str, Any]:
    if request.stage != "t2t" or request.gate_id != "parameter_approval":
        raise ResumeValidationError("T2T parameter approval requires t2t/parameter_approval")
    output = stage_output(state, "t2t")
    interrupt = state.get("active_interrupt") or output.get("active_interrupt")
    if not isinstance(interrupt, dict) or interrupt.get("gate_id") != "parameter_approval":
        raise ResumeValidationError("no unresolved T2T parameter_approval gate")
    required = ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")
    if any(key not in request.metadata for key in required):
        raise ResumeValidationError("T2T parameter approval metadata is incomplete")
    if any(request.metadata.get(key) != interrupt.get(key) for key in required):
        raise ResumeValidationError("stale T2T parameter approval binding")
    try:
        request_ref = ArtifactRef.model_validate(interrupt.get("request_ref"))
        request_record = load_review_request(
            absolute_data_dir(state), request_ref, session_id=state["session_id"]
        )
        cursor = ParameterRecommendationCursor.model_validate(
            output.get("current_parameter_recommendation")
        )
    except (ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError("T2T parameter approval request is missing or changed") from exc
    payload = request_record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("T2T parameter approval request has no payload")
    if request.decision not in {"approve_parameters", "reject_parameters"}:
        raise ResumeValidationError("unsupported T2T parameter approval decision")
    if request.decision not in set(payload.get("decisions_allowed") or []):
        raise ResumeValidationError("T2T parameter approval decision was not offered")
    if (
        request_record.get("review_request_id") != interrupt.get("review_request_id")
        or request_ref.sha256 != interrupt.get("request_hash")
        or cursor.status != "pending_approval"
        or cursor.manifest_ref.sha256 != interrupt.get("recommendation_hash")
    ):
        raise ResumeValidationError("T2T parameter approval cursor is stale")
    try:
        manifest_ref = ArtifactRef.model_validate(payload.get("recommendation_manifest_ref"))
        manifest = read_t2t_parameter_artifact(
            state, manifest_ref, expected_type=T2T_PARAMETER_RECOMMENDATION_MANIFEST
        )
        recommendation_ref = ArtifactRef.model_validate(
            manifest.get("refs", {}).get("recommendation_ref")
        )
        read_t2t_parameter_artifact(
            state, recommendation_ref, expected_type=T2T_PARAMETER_RECOMMENDATION
        )
        decision_ref = persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=str(interrupt["review_request_id"]),
            stage="t2t",
            gate_id="parameter_approval",
            request_hash=request_ref.sha256,
            request_ref=request_ref,
            decision={
                "schema_version": 2,
                "decision": request.decision,
                "decided_by": request.decided_by,
                "notes": list(request.notes),
                "bindings": {key: request.metadata[key] for key in required},
                "validated_recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "validated_recommendation_hash": manifest_ref.sha256,
            },
            method_name=_APPROVAL_METHOD,
            artifact_type=T2T_PARAMETER_APPROVAL_DECISION,
        )
    except (OSError, ValueError, ReviewPersistenceError, T2TParameterPersistenceError) as exc:
        raise ResumeValidationError("T2T parameter recommendation evidence is missing or changed") from exc
    Ledger(absolute_data_dir(state)).record_review_decision(
        state["session_id"],
        "t2t",
        gate_id="parameter_approval",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=decision_ref.path_ref,
        metadata={key: request.metadata[key] for key in required},
        decided_by=request.decided_by,
        review_request_id=str(interrupt["review_request_id"]),
    )
    outputs = dict(state.get("stage_outputs") or {})
    if request.decision == "reject_parameters":
        outputs["t2t"] = {
            **output,
            "status": "failed",
            "reason": "t2t_parameter_recommendation_rejected_by_human",
            "active_interrupt": None,
            "current_parameter_recommendation": cursor.model_copy(
                update={"status": "rejected"}
            ).model_dump(mode="json"),
        }
        return {
            "current_stage": "t2t",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "t2t_parameter_recommendation_rejected_by_human",
            "stage_outputs": outputs,
        }
    attempt = ParameterAttempt(
        attempt_id=0,
        method_id=T2T_METHOD,
        status="recommended",
        task="recommend_initial",
        recommendation_manifest_ref=manifest_ref,
        approval_decision_ref=decision_ref,
        recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
    )
    outputs["t2t"] = {
        **output,
        "status": "parameters_approved",
        "active_interrupt": None,
        "current_parameter_attempt_id": 0,
        "parameter_attempts": [attempt.model_dump(mode="json")],
        "current_parameter_recommendation": cursor.model_copy(
            update={"status": "approved"}
        ).model_dump(mode="json"),
        "proposed_parameter_attempt_id": None,
    }
    return {
        "current_stage": "t2t",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def prepare_t2t_parameter_profile(state: TopLevelState) -> dict:
    """Overlay approved sparse L2 values on executor-owned threshold defaults."""

    output = stage_output(state, "t2t")
    if output.get("status") != "parameters_approved":
        return {}
    try:
        approved = load_approved_t2t_parameters(state)
        defaults = dict(output.get("threshold_defaults") or {})
        effective = T2TThresholdProfile.model_validate(
            {**defaults, **approved}
        ).model_dump(mode="json")
        attempt = current_parameter_attempt(
            output, method_id=T2T_METHOD, label="T2T"
        )
        frozen_ref = write_t2t_parameter_artifact(
            state,
            attempt.attempt_id,
            "frozen_parameters.json",
            {
                "schema_version": 2,
                "stage": "t2t",
                "method_id": T2T_METHOD,
                "approved_sparse_parameters": approved,
                "executor_defaults": defaults,
                "effective_thresholds": effective,
                "recommendation_manifest_ref": attempt.recommendation_manifest_ref.model_dump(mode="json"),
                "approval_decision_ref": attempt.approval_decision_ref.model_dump(mode="json"),
            },
            artifact_type=T2T_PARAMETER_FROZEN_PARAMETERS,
            method_name=T2T_METHOD,
            scope="attempt",
        )
        attempts = update_current_parameter_attempt(
            output,
            method_id=T2T_METHOD,
            label="T2T",
            frozen_parameters_ref=frozen_ref.model_dump(mode="json"),
        )
    except (ValueError, ParameterAttemptStateError, T2TParameterPersistenceError) as exc:
        return _fail(state, f"T2T approved parameters are not executable: {exc}")
    return update_stage_output(
        state,
        "t2t",
        {
            "status": "parameters_ready",
            "thresholds": effective,
            "parameter_attempts": attempts,
        },
    )


def load_approved_t2t_parameters(state: TopLevelState) -> dict[str, Any]:
    output = stage_output(state, "t2t")
    try:
        attempt = current_parameter_attempt(
            output, method_id=T2T_METHOD, label="T2T"
        )
        manifest = read_t2t_parameter_artifact(
            state,
            attempt.recommendation_manifest_ref,
            expected_type=T2T_PARAMETER_RECOMMENDATION_MANIFEST,
        )
        recommendation_ref = ArtifactRef.model_validate(
            manifest.get("refs", {}).get("recommendation_ref")
        )
        record = read_t2t_parameter_artifact(
            state, recommendation_ref, expected_type=T2T_PARAMETER_RECOMMENDATION
        )
    except (ValueError, ParameterAttemptStateError, T2TParameterPersistenceError) as exc:
        raise T2TParameterPersistenceError(str(exc)) from exc
    recommendation = record.get("recommendation")
    parameters = recommendation.get("parameters") if isinstance(recommendation, dict) else None
    if not isinstance(parameters, dict):
        raise T2TParameterPersistenceError("T2T recommendation has no parameters mapping")
    return dict(parameters)


def _fail(state: TopLevelState, reason: str) -> dict:
    return update_stage_output(state, "t2t", {"status": "failed", "reason": reason})
