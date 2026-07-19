"""Data-v2 parameter recommendation, approval, retry, and attempt cursors.

Recommendation semantics stay in the existing domain modules.  This module
only separates immutable evidence from the bounded state required to resume
the assembly parameter loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from app.storage.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.domains.assembly.selection import AssemblySelection
from app.execution.evidence import validate_execution_evidence
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.llm.parameters.assembly.prompt import (
    parameter_prompt_bundle_hash,
    parameter_prompt_bundle_name,
    render_assembly_parameter_messages,
)
from app.llm.parameters.assembly.recommender import (
    recommend_assembly_parameters as invoke_assembly_parameter_model,
)
from app.llm.parameters.assembly.recommender import (
    restore_assembly_parameter_decision,
)
from app.llm.parameters.assembly.request import build_parameter_request_dto
from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_ATTEMPT_MANIFEST,
    ASSEMBLY_PARAMETER_BASIS,
    ASSEMBLY_PARAMETER_EXECUTION,
    ASSEMBLY_PARAMETER_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    ASSEMBLY_PARAMETER_REVIEW,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.state import TopLevelState
from app.stages.assembly.parameters.basis import build_parameter_basis_snapshot
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.assembly.parameters.profile import (
    build_full_parameter_profile,
    build_prompt_profile,
)
from app.stages.assembly.parameters.storage import (
    ParameterPersistenceError,
    load_recommendation_bundle,
    read_json_artifact,
    recover_recommendation_json_artifact,
    write_attempt_json_artifact,
    write_recommendation_json_artifact,
)
from app.stages.base import absolute_data_dir, stage_output, update_stage_output


def recommend_assembly_parameters(state: TopLevelState) -> dict:
    """Run the existing recommender while persisting each fact separately."""

    assembly_out = stage_output(state, "assembly")
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
    attempts_before = _validate_parameter_attempt_chain(
        state,
        assembly_out.get("parameter_attempts") or [],
    )
    initial_attempt = attempts_before[0] if attempts_before else None

    try:
        if task == "recommend_initial":
            catalog = load_parameter_catalog(method_name)
            basis_snapshot: dict[str, Any] | None = None
            basis_ref: ArtifactRef | None = None
        else:
            if initial_attempt is None:
                return _retry_transition_failure(state, "missing_initial_attempt")
            initial_bundle = load_recommendation_bundle(
                state,
                initial_attempt["recommendation_manifest_ref"],
            )
            basis_snapshot = dict(initial_bundle["basis_snapshot"])
            catalog = MethodParameterCatalog.model_validate(
                basis_snapshot["catalog_snapshot"]
            )
            if catalog.method_id != method_name.rsplit(".", 1)[-1]:
                return _retry_transition_failure(state, "policy_snapshot_hash_mismatch")
            basis_ref = ArtifactRef.model_validate(
                initial_bundle["manifest"]["refs"]["basis_snapshot_ref"]
            )
    except (KeyError, FileNotFoundError, ValueError, ParameterPersistenceError) as exc:
        reason = (
            "policy_snapshot_mismatch"
            if task != "recommend_initial"
            else f"Assembly parameter catalog is not usable: {exc}"
        )
        return (
            _fail_update(state, reason)
            if task == "recommend_initial"
            else _retry_transition_failure(state, reason)
        )

    l2_keys = [key for key, spec in catalog.parameters.items() if spec.level == 2]
    if not l2_keys:
        return _fail_update(
            state,
            f"Assembly parameter catalog has no L2 parameters: {catalog.method_id}",
        )

    recommendation_id = int(
        assembly_out.get("proposed_parameter_attempt_id")
        if assembly_out.get("proposed_parameter_attempt_id") is not None
        else assembly_out.get("current_parameter_attempt_id") or len(attempts_before)
    )
    previous_attempt = _previous_parameter_attempt(assembly_out, recommendation_id)
    hydrated_previous: dict[str, Any] = {}
    if task != "recommend_initial":
        if (
            not previous_attempt
            or previous_attempt.get("attempt_id") != recommendation_id - 1
        ):
            return _retry_transition_failure(state, "missing_failure_signal")
        if not previous_attempt.get("seal_hash"):
            return _retry_transition_failure(state, "unsealed_previous_attempt")
        retry_request = dict(assembly_out.get("parameter_retry_request") or {})
        if retry_request.get("previous_attempt_id") != previous_attempt.get(
            "attempt_id"
        ):
            return _retry_transition_failure(state, "missing_failure_signal")
        if retry_request.get("previous_attempt_seal_hash") != previous_attempt.get(
            "seal_hash"
        ):
            return _retry_transition_failure(
                state, "previous_attempt_seal_hash_mismatch"
            )
        try:
            hydrated_previous = hydrate_parameter_attempt(state, previous_attempt)
        except ParameterPersistenceError as exc:
            return _retry_transition_failure(state, str(exc))

    full_profile = build_full_parameter_profile(
        state,
        assembly_out=assembly_out,
        previous_attempt=hydrated_previous,
        assembly_review=hydrated_previous.get("review_failure"),
    )
    prompt_profile = build_prompt_profile(full_profile, task)
    try:
        if task == "recommend_initial":
            basis_snapshot = build_parameter_basis_snapshot(
                method_id=catalog.method_id,
                catalog=catalog,
                prompt_profile=prompt_profile,
            )
        assert basis_snapshot is not None
        request_dto = build_parameter_request_dto(
            method_id=catalog.method_id,
            catalog=catalog,
            prompt_profile=prompt_profile,
            task=task,
            basis_snapshot=basis_snapshot,
        )
    except Exception as exc:
        return _fail_update(state, f"Assembly parameter request failed: {exc}")

    try:
        input_profile_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "input_profile.json",
            full_profile,
            artifact_type=ASSEMBLY_PARAMETER_INPUT_PROFILE,
            method_name="assembly.parameter_recommender",
        )
        if task == "recommend_initial":
            basis_ref = write_recommendation_json_artifact(
                state,
                recommendation_id,
                "basis_snapshot.json",
                basis_snapshot,
                artifact_type=ASSEMBLY_PARAMETER_BASIS,
                method_name="assembly.parameter_recommender",
            )
        assert basis_ref is not None

        messages = render_assembly_parameter_messages(request_dto)
        model_request_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "model_request.json",
            {
                "schema_version": 2,
                "prompt_bundle_id": parameter_prompt_bundle_name(catalog.method_id),
                "prompt_template_sha256": parameter_prompt_bundle_hash(
                    catalog.method_id
                ),
                "parameter_request_schema_version": 2,
                "messages": messages,
            },
            artifact_type=ASSEMBLY_PARAMETER_MODEL_REQUEST,
            method_name="assembly.parameter_recommender",
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state, f"Assembly parameter request persistence failed: {exc}"
        )

    # A stale checkpoint may replay after the nondeterministic response was
    # durably published.  Recover and re-parse that exact response instead of
    # invoking the model again.
    model_response_ref: ArtifactRef | None = None
    try:
        recovered_response = recover_recommendation_json_artifact(
            state,
            recommendation_id,
            "model_response.json",
            expected_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
            method_name="assembly.parameter_recommender",
        )
        if recovered_response is None:
            decision = invoke_assembly_parameter_model(
                request_dto,
                messages=messages,
            )
        else:
            model_response_ref, response_record = recovered_response
            decision = restore_assembly_parameter_decision(
                response_record,
                request_dto,
            )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"Assembly parameter response recovery failed: {exc}",
        )
    try:
        if model_response_ref is None:
            model_response_ref = write_recommendation_json_artifact(
                state,
                recommendation_id,
                "model_response.json",
                {
                    "schema_version": 2,
                    "raw_response": decision.raw_response,
                    "validity": "parsed" if decision.valid else "invalid",
                    "error": decision.error,
                    "model_id": decision.model_id,
                },
                artifact_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
                method_name="assembly.parameter_recommender",
            )
        parsed = decision.parsed or {}
        recommendation_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "recommendation.json",
            {
                "schema_version": 2,
                "method_id": catalog.method_id,
                "task": task,
                "recommendation": parsed,
            },
            artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION,
            method_name="assembly.parameter_recommender",
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state, f"Assembly parameter response persistence failed: {exc}"
        )

    recommended_params = dict(parsed.get("parameters") or {}) if decision.valid else {}
    response_parse = {
        "schema_version": 1,
        "parsed": bool(decision.valid),
        "error": decision.error,
    }
    try:
        response_parse_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "response_parse.json",
            response_parse,
            artifact_type=ASSEMBLY_PARAMETER_RESPONSE_PARSE,
            method_name="assembly.parameter_response_parser",
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state, f"Assembly parameter parse persistence failed: {exc}"
        )

    retry_disposition = parsed.get("retry_disposition")
    if not decision.valid:
        cursor_status = "response_invalid"
        stage_status = "parameter_response_invalid"
    elif retry_disposition == "stop":
        cursor_status = "retry_stopped"
        stage_status = "parameter_retry_stopped"
    elif retry_disposition == "expert_review":
        cursor_status = "expert_review"
        stage_status = "parameter_retry_expert_review"
    else:
        cursor_status = "pending_approval"
        stage_status = "blocked_human"

    previous_manifest_ref = (
        previous_attempt.get("attempt_manifest_ref") if previous_attempt else None
    )
    manifest_payload = {
        "schema_version": 2,
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "stage": "assembly",
        "method_id": method_name,
        "task": task,
        "recommendation_id": recommendation_id,
        "status": cursor_status,
        "previous_attempt_id": (
            previous_attempt.get("attempt_id") if previous_attempt else None
        ),
        "previous_attempt_manifest_ref": previous_manifest_ref,
        "previous_attempt_seal_hash": (
            previous_attempt.get("seal_hash") if previous_attempt else None
        ),
        "refs": {
            "input_profile_ref": input_profile_ref.model_dump(mode="json"),
            "basis_snapshot_ref": basis_ref.model_dump(mode="json"),
            "model_request_ref": model_request_ref.model_dump(mode="json"),
            "model_response_ref": model_response_ref.model_dump(mode="json"),
            "recommendation_ref": recommendation_ref.model_dump(mode="json"),
            "response_parse_ref": response_parse_ref.model_dump(mode="json"),
        },
        "summary": {
            "response_parsed": bool(decision.valid),
            "retry_disposition": retry_disposition,
        },
    }
    try:
        manifest_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "recommendation_manifest.json",
            manifest_payload,
            artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
            method_name="assembly.parameter_recommender",
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(state, f"Assembly recommendation manifest failed: {exc}")

    cursor = ParameterRecommendationCursor(
        recommendation_id=recommendation_id,
        method_id=method_name,
        task=task,
        status=cursor_status,
        previous_attempt_id=(
            previous_attempt.get("attempt_id") if previous_attempt else None
        ),
        manifest_ref=manifest_ref,
        recommendation_hash=manifest_ref.sha256,
        previous_attempt_seal_hash=(
            previous_attempt.get("seal_hash") if previous_attempt else None
        ),
    )
    update: dict[str, Any] = {
        "status": stage_status,
        "proposed_parameter_attempt_id": recommendation_id,
        "current_parameter_recommendation": cursor.model_dump(mode="json"),
    }
    if not decision.valid:
        update["reason"] = (
            "Assembly parameter recommendation response could not be parsed"
        )
        return update_stage_output(state, "assembly", update)
    if retry_disposition in {"stop", "expert_review"}:
        update["reason"] = parsed.get("summary") or (
            "Parameter retry stopped by evidence assessment"
            if retry_disposition == "stop"
            else "Parameter retry requires expert review"
        )
        return update_stage_output(state, "assembly", update)

    bindings = {
        "attempt_id": recommendation_id,
        "task": task,
        "previous_attempt_id": cursor.previous_attempt_id,
        "recommendation_hash": manifest_ref.sha256,
    }
    try:
        persisted_request = persist_review_request(
            state,
            stage="assembly",
            gate_id="parameter_approval",
            reason="assembly_parameter_approval_required",
            payload={
                "schema_version": 2,
                "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "recommendation_manifest_hash": manifest_ref.sha256,
                "method_id": method_name,
                "task": task,
                "parameters": recommended_params,
                "rationale": parsed.get("summary"),
                "response_parse_ref": response_parse_ref.model_dump(mode="json"),
                "recommendation_ref": recommendation_ref.model_dump(mode="json"),
                "failure_signal_ref": (
                    previous_attempt.get("review_ref")
                    or previous_attempt.get("execution_ref")
                    if previous_attempt
                    else None
                ),
                "decisions_allowed": ["approve_parameters", "reject_parameters"],
            },
            method_name="assembly.parameter_approval",
            artifact_type="assembly.parameter_approval.request",
            bindings=bindings,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        return _fail_update(state, f"Assembly parameter review request failed: {exc}")

    cursor = cursor.model_copy(
        update={
            "review_request_id": persisted_request.review_request_id,
            "review_request_ref": persisted_request.request_ref,
        }
    )
    interrupt = persisted_request.compact_interrupt(
        stage="assembly",
        gate_id="parameter_approval",
        reason="assembly_parameter_approval_required",
        bindings=bindings,
    )
    Ledger(absolute_data_dir(state)).record_review_requested(
        state["session_id"],
        "assembly",
        reason="assembly_parameter_approval_required",
        review_request_artifact_ref=persisted_request.request_ref.path_ref,
        review_request_id=persisted_request.review_request_id,
        auto_decidable=False,
        pending_human_confirmation=True,
    )
    update.update(
        {
            "block_reason": "assembly_parameter_approval_required",
            "active_interrupt": interrupt,
            "current_parameter_recommendation": cursor.model_dump(mode="json"),
        }
    )
    return update_stage_output(state, "assembly", update)


def apply_parameter_approval_decision(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> dict[str, Any]:
    """Apply one decision bound to a verified immutable recommendation manifest."""

    if request.stage != "assembly":
        raise ResumeValidationError("parameter approval resume requires stage assembly")
    if request.gate_id != "parameter_approval":
        raise ResumeValidationError(
            "parameter approval resume requires explicit gate_id=parameter_approval"
        )
    assembly_out = stage_output(state, "assembly")
    interrupt = state.get("active_interrupt") or assembly_out.get("active_interrupt")
    if (
        not isinstance(interrupt, dict)
        or interrupt.get("gate_id") != "parameter_approval"
    ):
        raise ResumeValidationError("no unresolved parameter_approval gate")

    required = ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")
    missing = [key for key in required if key not in request.metadata]
    if missing:
        raise ResumeValidationError(
            f"parameter approval metadata missing required bindings: {missing}"
        )
    for key in required:
        if request.metadata.get(key) != interrupt.get(key):
            raise ResumeValidationError(f"stale parameter approval binding: {key}")

    request_ref = interrupt.get("request_ref")
    request_id = interrupt.get("review_request_id")
    if not isinstance(request_ref, dict) or not isinstance(request_id, str):
        raise ResumeValidationError("parameter approval interrupt has no request ref")
    try:
        request_record = load_review_request(
            absolute_data_dir(state),
            request_ref,
            session_id=state["session_id"],
        )
    except (ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "parameter approval request is missing or changed"
        ) from exc
    if request_record.get("review_request_id") != request_id:
        raise ResumeValidationError("parameter approval request identity mismatch")
    validated_request_ref = ArtifactRef.model_validate(request_ref)
    if interrupt.get("request_hash") != validated_request_ref.sha256:
        raise ResumeValidationError("parameter approval request hash mismatch")
    payload = request_record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("parameter approval request has no payload")
    allowed = list(payload.get("decisions_allowed") or [])
    if request.decision not in allowed or request.decision not in {
        "approve_parameters",
        "reject_parameters",
    }:
        raise ResumeValidationError("unsupported parameter approval decision")

    cursor_value = assembly_out.get("current_parameter_recommendation")
    try:
        cursor = ParameterRecommendationCursor.model_validate(cursor_value)
    except ValueError as exc:
        raise ResumeValidationError(
            "parameter approval has no valid recommendation cursor"
        ) from exc
    if cursor.status != "pending_approval" or cursor.review_request_id != request_id:
        raise ResumeValidationError(
            "parameter approval cursor is stale or already resolved"
        )
    if cursor.recommendation_hash != interrupt.get("recommendation_hash"):
        raise ResumeValidationError("parameter approval recommendation hash mismatch")
    manifest_ref_value = payload.get("recommendation_manifest_ref")
    try:
        manifest_ref = ArtifactRef.model_validate(manifest_ref_value)
        bundle = load_recommendation_bundle(state, manifest_ref)
    except (ValueError, ParameterPersistenceError) as exc:
        raise ResumeValidationError(
            "parameter recommendation manifest is invalid"
        ) from exc
    if (
        manifest_ref != cursor.manifest_ref
        or manifest_ref.sha256 != payload.get("recommendation_manifest_hash")
        or manifest_ref.sha256 != interrupt.get("recommendation_hash")
    ):
        raise ResumeValidationError(
            "parameter recommendation manifest binding mismatch"
        )

    attempts = _validate_parameter_attempt_chain(
        state,
        assembly_out.get("parameter_attempts") or [],
    )
    attempt_id = cursor.recommendation_id
    previous_id = cursor.previous_attempt_id
    if any(item.get("attempt_id") == attempt_id for item in attempts):
        raise ResumeValidationError("parameter approval gate was already resolved")
    if attempt_id == 0:
        if attempts or previous_id is not None:
            raise ResumeValidationError("initial parameter approval chain head changed")
    else:
        if (
            not attempts
            or attempts[-1].get("attempt_id") != previous_id
            or attempt_id != int(previous_id) + 1
        ):
            raise ResumeValidationError("parameter approval attempt chain head changed")
        if cursor.previous_attempt_seal_hash != attempts[-1].get("seal_hash"):
            raise ResumeValidationError("parameter retry previous attempt seal changed")

    try:
        decision_ref = persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=request_id,
            stage="assembly",
            gate_id="parameter_approval",
            request_hash=str(interrupt["request_hash"]),
            request_ref=validated_request_ref,
            decision={
                "schema_version": 2,
                "decision": request.decision,
                "decided_by": request.decided_by,
                "notes": list(request.notes),
                "bindings": {key: request.metadata[key] for key in required},
                "request_hash": validated_request_ref.sha256,
                "validated_recommendation_manifest_ref": manifest_ref.model_dump(
                    mode="json"
                ),
                "validated_recommendation_hash": manifest_ref.sha256,
            },
            method_name="assembly.parameter_approval",
            artifact_type=ASSEMBLY_PARAMETER_APPROVAL_DECISION,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "parameter approval decision could not be persisted"
        ) from exc
    Ledger(absolute_data_dir(state)).record_review_decision(
        state["session_id"],
        "assembly",
        gate_id="parameter_approval",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=decision_ref.path_ref,
        metadata={key: request.metadata[key] for key in required},
        decided_by=request.decided_by,
        review_request_id=request_id,
    )

    outputs = dict(state.get("stage_outputs") or {})
    if request.decision == "reject_parameters":
        rejected_cursor = cursor.model_copy(update={"status": "rejected"})
        outputs["assembly"] = {
            **assembly_out,
            "status": "parameter_recommendation_rejected_by_human",
            "reason": "parameter_recommendation_rejected_by_human",
            "active_interrupt": None,
            "current_parameter_recommendation": rejected_cursor.model_dump(mode="json"),
            "parameter_rejection": {
                "recommendation_id": attempt_id,
                "manifest_ref": manifest_ref.model_dump(mode="json"),
                "decision_ref": decision_ref.model_dump(mode="json"),
            },
        }
        return {
            "current_stage": "assembly",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "parameter_recommendation_rejected_by_human",
            "stage_outputs": outputs,
        }

    manifest = bundle["manifest"]
    attempt = ParameterAttempt(
        attempt_id=attempt_id,
        method_id=cursor.method_id,
        status="recommended",
        task=cursor.task,
        previous_attempt_id=previous_id,
        recommendation_manifest_ref=manifest_ref,
        approval_decision_ref=decision_ref,
        recommendation_contract=str(
            manifest.get("recommendation_contract") or PARAMETER_RECOMMENDATION_CONTRACT
        ),
        previous_attempt_seal_hash=cursor.previous_attempt_seal_hash,
    ).model_dump(mode="json")
    attempts.append(attempt)
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    if cursor.task == "retry_after_execution_failure":
        retry_counts["execution"] = int(retry_counts.get("execution", 0)) + 1
    elif cursor.task == "retry_after_review_failure":
        retry_counts["review"] = int(retry_counts.get("review", 0)) + 1
    approved_cursor = cursor.model_copy(update={"status": "approved"})
    outputs["assembly"] = {
        **assembly_out,
        "status": "parameters_approved",
        "current_parameter_attempt_id": attempt_id,
        "parameter_attempts": attempts,
        "retry_counts": retry_counts,
        "active_interrupt": None,
        "current_parameter_recommendation": approved_cursor.model_dump(mode="json"),
        "proposed_parameter_attempt_id": None,
        "parameter_retry_request": None,
    }
    return {
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def execution_retry_gate(state: TopLevelState) -> dict:
    """Prepare a ref-only retry cursor after command execution failure."""

    assembly_out = stage_output(state, "assembly")
    if assembly_out.get("status") != "failed" or not assembly_out.get(
        "parameter_attempts"
    ):
        return {}
    try:
        attempt_id = current_parameter_attempt(
            assembly_out,
            label="Assembly",
        ).attempt_id
    except ParameterAttemptStateError as exc:
        return _fail_update(state, str(exc))
    attempts = _validate_parameter_attempt_chain(
        state,
        assembly_out.get("parameter_attempts") or [],
    )
    previous = next(
        (item for item in attempts if item.get("attempt_id") == attempt_id),
        attempts[-1] if attempts else {},
    )
    execution_ref = previous.get("execution_ref")
    if not execution_ref:
        return {}
    previous = _seal_parameter_attempt(state, previous)
    updated_attempts = [
        previous if item.get("attempt_id") == attempt_id else item for item in attempts
    ]
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    used = int(retry_counts.get("execution", 0))
    max_retries = _parameter_recommendation_config(state, assembly_out).get(
        "max_execution_retries", 1
    )
    if used >= int(max_retries):
        return update_stage_output(
            state,
            "assembly",
            {
                "status": "execution_parameter_retries_exhausted",
                "reason": "execution_retry_budget_exhausted",
                "parameter_attempts": updated_attempts,
            },
        )
    return update_stage_output(
        state,
        "assembly",
        {
            "status": "retry_parameters_after_execution_failure",
            "proposed_parameter_attempt_id": attempt_id + 1,
            "parameter_attempts": updated_attempts,
            "retry_counts": retry_counts,
            "parameter_retry_request": {
                "origin": "assembly_execution",
                "previous_attempt_id": attempt_id,
                "previous_attempt_seal_hash": previous["seal_hash"],
                "failure_ref": execution_ref,
            },
        },
    )


def parameter_quality_gate(state: TopLevelState) -> dict:
    """Persist review evidence and prepare a ref-only quality retry cursor."""

    assembly_out = stage_output(state, "assembly")
    review_out = (state.get("stage_outputs") or {}).get("assembly_review") or {}
    metrics = dict(
        review_out.get("metrics")
        or review_out.get("summary")
        or assembly_out.get("review_metrics")
        or {}
    )
    diagnosis = _review_failure_diagnosis(metrics)
    try:
        current_id = current_parameter_attempt(
            assembly_out,
            label="Assembly",
        ).attempt_id
    except ParameterAttemptStateError as exc:
        return _fail_update(state, str(exc))
    attempts = _validate_parameter_attempt_chain(
        state,
        assembly_out.get("parameter_attempts") or [],
    )
    previous = next(
        (item for item in attempts if item.get("attempt_id") == current_id),
        attempts[-1] if attempts else {},
    )
    if not previous:
        return {}
    review_ref = write_attempt_json_artifact(
        state,
        current_id,
        "review.json",
        {
            "schema_version": 2,
            "attempt_id": current_id,
            "diagnosis": diagnosis,
            "review_metrics": metrics,
        },
        artifact_type=ASSEMBLY_PARAMETER_REVIEW,
        method_name="assembly.parameter_quality_gate",
    )
    previous = {
        **previous,
        "status": "reviewed" if diagnosis["passed"] else "review_failed",
        "review_ref": review_ref.model_dump(mode="json"),
    }
    previous = _seal_parameter_attempt(state, previous)
    updated_attempts = [
        previous if item.get("attempt_id") == current_id else item for item in attempts
    ]
    compact_quality = {
        "passed": bool(diagnosis["passed"]),
        "failure_mode": diagnosis.get("failure_mode"),
        "review_ref": review_ref.model_dump(mode="json"),
    }
    if diagnosis["passed"]:
        return update_stage_output(
            state,
            "assembly",
            {
                "status": "parameter_quality_passed",
                "quality_gate": compact_quality,
                "parameter_attempts": updated_attempts,
            },
        )

    retry_counts = dict(assembly_out.get("retry_counts") or {})
    used = int(retry_counts.get("review", 0))
    max_retries = _parameter_recommendation_config(state, assembly_out).get(
        "max_review_retries", 2
    )
    if used >= int(max_retries):
        return update_stage_output(
            state,
            "assembly",
            {
                "status": "review_parameter_retries_exhausted",
                "quality_gate": compact_quality,
                "parameter_attempts": updated_attempts,
                "reason": "review_retry_budget_exhausted",
            },
        )
    return update_stage_output(
        state,
        "assembly",
        {
            "status": "retry_parameters_after_review_failure",
            "proposed_parameter_attempt_id": current_id + 1,
            "parameter_attempts": updated_attempts,
            "retry_counts": retry_counts,
            "quality_gate": compact_quality,
            "parameter_retry_request": {
                "origin": "assembly_review",
                "previous_attempt_id": current_id,
                "previous_attempt_seal_hash": previous["seal_hash"],
                "failure_ref": review_ref.model_dump(mode="json"),
            },
        },
    )


def hydrate_parameter_attempt(
    state: TopLevelState,
    attempt_value: ParameterAttempt | dict[str, Any],
) -> dict[str, Any]:
    """Rehydrate domain inputs transiently without placing them in checkpoint state."""

    attempt = (
        attempt_value
        if isinstance(attempt_value, ParameterAttempt)
        else ParameterAttempt.model_validate(attempt_value)
    )
    bundle = load_recommendation_bundle(state, attempt.recommendation_manifest_ref)
    recommendation = dict(bundle["recommendation"].get("recommendation") or {})
    basis = dict(bundle["basis_snapshot"])
    hydrated: dict[str, Any] = {
        **attempt.model_dump(mode="json"),
        "catalog_snapshot": basis.get("catalog_snapshot"),
        "parameters": dict(recommendation.get("parameters") or {}),
        "recommended_params": dict(recommendation.get("parameters") or {}),
        "selected_case_ids": list(recommendation.get("used_literature_case_ids") or []),
    }
    if attempt.command_ref is not None:
        command = read_json_artifact(state, attempt.command_ref)
        hydrated.update(
            {
                "argv": list(command.get("argv") or []),
                "parameter_sources": dict(command.get("parameter_sources") or {}),
                "rendered_parameters": dict(command.get("rendered_parameters") or {}),
            }
        )
    if attempt.execution_ref is not None:
        execution = read_json_artifact(
            state,
            attempt.execution_ref,
            expected_type=ASSEMBLY_PARAMETER_EXECUTION,
        )
        if (
            execution.get("attempt_id") != attempt.attempt_id
            or execution.get("method_id") != attempt.method_id
        ):
            raise ParameterPersistenceError(
                "execution evidence is not bound to the parameter attempt"
            )
        hydrated["execution_result"] = validate_execution_evidence(
            state,
            execution.get("result") or {},
        ).model_dump(mode="json")
    if attempt.review_ref is not None:
        review = read_json_artifact(state, attempt.review_ref)
        hydrated["review_failure"] = dict(review.get("diagnosis") or {})
        hydrated["review_outcome"] = dict(review.get("diagnosis") or {})
    return hydrated


def _seal_parameter_attempt(
    state: TopLevelState,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    """Seal a compact attempt and publish its immutable manifest once."""

    value = ParameterAttempt.model_validate(attempt).model_dump(mode="json")
    semantic = {
        key: item
        for key, item in value.items()
        if key not in {"sealed_at", "seal_hash", "attempt_manifest_ref"}
    }
    digest = sha256(
        json.dumps(
            semantic,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if value.get("seal_hash"):
        if value["seal_hash"] != digest or not value.get("attempt_manifest_ref"):
            raise ValueError("sealed parameter attempt was mutated")
        manifest = read_json_artifact(
            state,
            value["attempt_manifest_ref"],
            expected_type=ASSEMBLY_PARAMETER_ATTEMPT_MANIFEST,
        )
        if manifest.get("seal_hash") != digest:
            raise ValueError("parameter attempt manifest seal mismatch")
        return value

    sealed_at = datetime.now(timezone.utc).isoformat()
    manifest_ref = write_attempt_json_artifact(
        state,
        int(value["attempt_id"]),
        "attempt_manifest.json",
        {
            "schema_version": 2,
            "stage": "assembly",
            "method_id": value["method_id"],
            "attempt_id": value["attempt_id"],
            "status": value["status"],
            "task": value["task"],
            "previous_attempt_id": value.get("previous_attempt_id"),
            "recommendation_manifest_ref": value["recommendation_manifest_ref"],
            "approval_decision_ref": value["approval_decision_ref"],
            "frozen_parameters_ref": value.get("frozen_parameters_ref"),
            "command_ref": value.get("command_ref"),
            "frozen_attempt_ref": value.get("frozen_attempt_ref"),
            "execution_ref": value.get("execution_ref"),
            "review_ref": value.get("review_ref"),
            "previous_attempt_seal_hash": value.get("previous_attempt_seal_hash"),
            "sealed_at": sealed_at,
            "seal_hash": digest,
        },
        artifact_type=ASSEMBLY_PARAMETER_ATTEMPT_MANIFEST,
        method_name=value["method_id"],
    )
    return ParameterAttempt.model_validate(
        {
            **value,
            "sealed_at": sealed_at,
            "seal_hash": digest,
            "attempt_manifest_ref": manifest_ref.model_dump(mode="json"),
        }
    ).model_dump(mode="json")


def _validate_parameter_attempt_chain(
    state: TopLevelState,
    raw_attempts: list[Any],
) -> list[dict[str, Any]]:
    """Validate the ordered compact chain and every existing seal."""

    attempts: list[dict[str, Any]] = []
    for item in raw_attempts:
        if not isinstance(item, dict):
            raise ValueError("parameter attempt chain contains a non-mapping entry")
        value = ParameterAttempt.model_validate(item).model_dump(mode="json")
        if value.get("seal_hash"):
            value = _seal_parameter_attempt(state, value)
        attempts.append(value)
    for sequence, attempt in enumerate(attempts):
        if attempt["attempt_id"] != sequence:
            raise ValueError("parameter attempt IDs must match fixed list order")
        if sequence == 0:
            if attempt["task"] != "recommend_initial":
                raise ValueError("parameter attempt 0 must be initial")
            continue
        previous = attempts[sequence - 1]
        if attempt["previous_attempt_id"] != sequence - 1:
            raise ValueError("parameter attempt chain has a broken previous link")
        if not previous.get("seal_hash"):
            raise ValueError("parameter retry predecessor must be sealed")
        if attempt.get("previous_attempt_seal_hash") != previous.get("seal_hash"):
            raise ValueError("parameter attempt chain has a broken previous seal")
        if attempt.get("recommendation_contract") != attempts[0].get(
            "recommendation_contract"
        ):
            raise ValueError("parameter retry recommendation contract drifted")
    return attempts


def build_parameter_attempt_timeline(
    state: TopLevelState,
    assembly_out: dict[str, Any],
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    """Derive a refs-only terminal timeline from compact sealed cursors."""

    attempts = _validate_parameter_attempt_chain(
        state,
        assembly_out.get("parameter_attempts") or [],
    )
    entries = [
        {
            "sequence": sequence,
            "attempt_id": attempt["attempt_id"],
            "previous_attempt_id": attempt.get("previous_attempt_id"),
            "recommendation_manifest_ref": attempt.get("recommendation_manifest_ref"),
            "approval_decision_ref": attempt.get("approval_decision_ref"),
            "frozen_parameters_ref": attempt.get("frozen_parameters_ref"),
            "command_ref": attempt.get("command_ref"),
            "frozen_attempt_ref": attempt.get("frozen_attempt_ref"),
            "execution_ref": attempt.get("execution_ref"),
            "review_ref": attempt.get("review_ref"),
            "attempt_manifest_ref": attempt.get("attempt_manifest_ref"),
            "seal_hash": attempt.get("seal_hash"),
        }
        for sequence, attempt in enumerate(attempts)
    ]
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    return {
        "schema_version": 2,
        "recommendation_contract": (
            attempts[0].get("recommendation_contract") if attempts else None
        ),
        "entries": entries,
        "terminal": {
            "status": status,
            "reason": reason,
            "final_attempt_id": attempts[-1]["attempt_id"] if attempts else None,
            "execution_retries_used": int(retry_counts.get("execution", 0)),
            "review_retries_used": int(retry_counts.get("review", 0)),
            "result_ref": assembly_out.get("draft_assembly_ref"),
            "parameter_rejection": assembly_out.get("parameter_rejection"),
        },
    }


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return update_stage_output(
        state, "assembly", {"status": "failed", "reason": reason}
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
    selection = assembly_out.get("assembly_selection")
    if isinstance(selection, dict):
        try:
            return AssemblySelection.model_validate(selection).primary_method
        except ValueError:
            return None
    return None


def _parameter_prompt_task(assembly_out: dict[str, Any]) -> str:
    status = assembly_out.get("status")
    if status == "retry_parameters_after_execution_failure":
        return "retry_after_execution_failure"
    if status == "retry_parameters_after_review_failure":
        return "retry_after_review_failure"
    return "recommend_initial"


def _previous_parameter_attempt(
    assembly_out: dict[str, Any],
    attempt_id: int,
) -> dict[str, Any]:
    attempts = [
        item
        for item in assembly_out.get("parameter_attempts") or []
        if isinstance(item, dict)
    ]
    if not attempts:
        return {}
    return next(
        (item for item in attempts if item.get("attempt_id") == attempt_id - 1),
        attempts[-1],
    )


def _retry_transition_failure(state: TopLevelState, reason: str) -> dict[str, Any]:
    return update_stage_output(
        state,
        "assembly",
        {"status": "parameter_retry_invalid_transition", "reason": reason},
    )


def _parameter_recommendation_config(
    state: TopLevelState,
    assembly_out: dict[str, Any],
) -> dict[str, Any]:
    config = dict(assembly_out.get("parameter_recommendation") or {})
    shared = state.get("shared_parameters") or {}
    if isinstance(shared, dict):
        assembly_config = dict(
            (shared.get("assembly") or {}).get("parameter_recommendation") or {}
        )
        config = {**assembly_config, **config}
    return {
        "max_execution_retries": int(config.get("max_execution_retries", 1)),
        "max_review_retries": int(config.get("max_review_retries", 2)),
    }


def _review_failure_diagnosis(metrics: dict[str, Any]) -> dict[str, Any]:
    complete = _float_or_none(
        _first_metric(metrics, "busco_complete", "busco_complete_ratio")
    )
    duplicate = _float_or_none(
        _first_metric(metrics, "busco_duplicate", "busco_duplicate_ratio")
    )
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
    elif (
        normalized_duplicate is not None
        and normalized_duplicate >= 0.15
        and size_ratio is not None
        and size_ratio >= 1.20
    ):
        failure_mode = "under_purged"
        suggestions = {"purge_level": "consider increasing"}
    elif (
        normalized_complete is not None
        and normalized_complete <= 0.90
        and size_ratio is not None
        and size_ratio <= 0.80
    ):
        failure_mode = "over_purged"
        suggestions = {"purge_level": "consider decreasing"}
    elif (
        n50 is not None
        and contig_count is not None
        and n50 < 1_000_000
        and contig_count > 500
    ):
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
