"""AutoHiC restriction-enzyme recommendation and mandatory approval gate."""

from __future__ import annotations

from typing import Any

from app.storage.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.llm.parameters.hic.recommender import (
    PROMPT_BUNDLE,
    autohic_parameter_prompt_hash,
    render_autohic_parameter_messages,
    restore_autohic_parameter_decision,
)
from app.llm.parameters.hic.recommender import (
    recommend_autohic_parameters as invoke_autohic_parameter_model,
)
from app.llm.parameters.hic.request import (
    AUTOHIC_METHOD,
    PARAMETER_RECOMMENDATION_CONTRACT,
    build_autohic_parameter_request,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    HIC_PARAMETER_APPROVAL_DECISION,
    HIC_PARAMETER_BASIS,
    HIC_PARAMETER_INPUT_PROFILE,
    HIC_PARAMETER_MODEL_REQUEST,
    HIC_PARAMETER_MODEL_RESPONSE,
    HIC_PARAMETER_RECOMMENDATION,
    HIC_PARAMETER_RECOMMENDATION_MANIFEST,
    HIC_PARAMETER_RESPONSE_PARSE,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir
from app.stages.hic_scaffolding.method_application.lifecycle import (
    hic_output as _hic_output,
)
from app.stages.hic_scaffolding.method_application.lifecycle import (
    update_hic_output as _update_hic_output,
)
from app.stages.hic_scaffolding.parameters.catalog import (
    load_autohic_parameter_catalog,
)
from app.stages.hic_scaffolding.parameters.storage import (
    AutoHiCParameterPersistenceError,
    read_autohic_parameter_artifact,
    recover_autohic_parameter_artifact,
    write_autohic_parameter_artifact,
)

_RECOMMENDER_METHOD = "hic_scaffolding.parameter_recommender"
_APPROVAL_METHOD = "hic_scaffolding.parameter_approval"


def recommend_autohic_parameters(state: TopLevelState) -> dict:
    """Ask the LLM for restriction_enzyme and always open an approval gate."""

    hic_out = _hic_output(state)
    if hic_out.get("status") != "resolved":
        return {}

    try:
        catalog = load_autohic_parameter_catalog()
    except (FileNotFoundError, ValueError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter catalog is not usable: {exc}",
            failure_kind="parameter_recommendation",
        )

    recommendation_id = 0
    input_profile = _input_profile(hic_out)
    catalog_snapshot = catalog.model_dump(mode="json")

    try:
        model_request = build_autohic_parameter_request(
            input_profile=input_profile,
            catalog=catalog,
        ).model_dump(mode="json")
        input_profile_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "input_profile.json",
            input_profile,
            artifact_type=HIC_PARAMETER_INPUT_PROFILE,
            method_name=_RECOMMENDER_METHOD,
        )
        basis_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "basis_snapshot.json",
            {
                "schema_version": 2,
                "catalog_snapshot": catalog_snapshot,
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
            },
            artifact_type=HIC_PARAMETER_BASIS,
            method_name=_RECOMMENDER_METHOD,
        )
        messages = render_autohic_parameter_messages(model_request)
        model_request_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "model_request.json",
            {
                "schema_version": 2,
                "prompt_bundle_id": PROMPT_BUNDLE,
                "prompt_template_sha256": autohic_parameter_prompt_hash(),
                "messages": messages,
            },
            artifact_type=HIC_PARAMETER_MODEL_REQUEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter request persistence failed: {exc}",
            failure_kind="parameter_recommendation",
        )

    model_response_ref: ArtifactRef | None = None
    try:
        recovered_response = recover_autohic_parameter_artifact(
            state,
            recommendation_id,
            "model_response.json",
            expected_type=HIC_PARAMETER_MODEL_RESPONSE,
            method_name=_RECOMMENDER_METHOD,
        )
        if recovered_response is None:
            decision = invoke_autohic_parameter_model(model_request, messages=messages)
        else:
            model_response_ref, response_record = recovered_response
            decision = restore_autohic_parameter_decision(response_record)
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter response recovery failed: {exc}",
            failure_kind="parameter_recommendation",
        )
    recommendation = decision.recommendation or {}
    response_parse = {
        "schema_version": 1,
        "parsed": bool(decision.valid),
        "error": decision.error,
    }
    try:
        if model_response_ref is None:
            model_response_ref = write_autohic_parameter_artifact(
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
                artifact_type=HIC_PARAMETER_MODEL_RESPONSE,
                method_name=_RECOMMENDER_METHOD,
            )
        recommendation_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "recommendation.json",
            {
                "schema_version": 2,
                "method_id": AUTOHIC_METHOD,
                "task": "recommend_initial",
                "recommendation": recommendation,
            },
            artifact_type=HIC_PARAMETER_RECOMMENDATION,
            method_name=_RECOMMENDER_METHOD,
        )
        response_parse_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "response_parse.json",
            response_parse,
            artifact_type=HIC_PARAMETER_RESPONSE_PARSE,
            method_name="hic_scaffolding.parameter_response_parser",
        )
        manifest_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "recommendation_manifest.json",
            {
                "schema_version": 2,
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
                "stage": "hic_scaffolding",
                "method_id": AUTOHIC_METHOD,
                "task": "recommend_initial",
                "recommendation_id": recommendation_id,
                "status": "pending_approval" if decision.valid else "response_invalid",
                "previous_attempt_id": None,
                "previous_attempt_manifest_ref": None,
                "previous_attempt_seal_hash": None,
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
                },
            },
            artifact_type=HIC_PARAMETER_RECOMMENDATION_MANIFEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter response persistence failed: {exc}",
            failure_kind="parameter_recommendation",
        )

    cursor = ParameterRecommendationCursor(
        recommendation_id=recommendation_id,
        method_id=AUTOHIC_METHOD,
        task="recommend_initial",
        status="pending_approval" if decision.valid else "response_invalid",
        previous_attempt_id=None,
        manifest_ref=manifest_ref,
        recommendation_hash=manifest_ref.sha256,
    )
    if not decision.valid:
        return _fail_update(
            state,
            "AutoHiC parameter recommendation response could not be parsed",
            failure_kind="parameter_recommendation",
            extra={"current_parameter_recommendation": cursor.model_dump(mode="json")},
        )

    enzyme = recommendation["parameters"]["restriction_enzyme"]
    bindings = {
        "attempt_id": recommendation_id,
        "task": "recommend_initial",
        "previous_attempt_id": None,
        "recommendation_hash": manifest_ref.sha256,
    }
    try:
        persisted_request = persist_review_request(
            state,
            stage="hic_scaffolding",
            gate_id="parameter_approval",
            reason="autohic_parameter_approval_required",
            payload={
                "schema_version": 2,
                "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "recommendation_manifest_hash": manifest_ref.sha256,
                "method_id": AUTOHIC_METHOD,
                "task": "recommend_initial",
                "parameters": {"restriction_enzyme": enzyme},
                "rationale": recommendation.get("summary"),
                "response_parse_ref": response_parse_ref.model_dump(mode="json"),
                "recommendation_ref": recommendation_ref.model_dump(mode="json"),
                "decisions_allowed": ["approve_parameters", "reject_parameters"],
            },
            method_name=_APPROVAL_METHOD,
            artifact_type="hic_scaffolding.parameter_approval.request",
            bindings=bindings,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter approval request failed: {exc}",
            failure_kind="parameter_recommendation",
        )

    cursor = cursor.model_copy(
        update={
            "review_request_id": persisted_request.review_request_id,
            "review_request_ref": persisted_request.request_ref,
        }
    )
    interrupt = persisted_request.compact_interrupt(
        stage="hic_scaffolding",
        gate_id="parameter_approval",
        reason="autohic_parameter_approval_required",
        bindings=bindings,
    )
    Ledger(absolute_data_dir(state)).record_review_requested(
        state["session_id"],
        "hic_scaffolding",
        reason="autohic_parameter_approval_required",
        review_request_artifact_ref=persisted_request.request_ref.path_ref,
        review_request_id=persisted_request.review_request_id,
        auto_decidable=False,
        pending_human_confirmation=True,
    )
    return _update_hic_output(
        state,
        {
            "status": "blocked_human",
            "block_reason": "autohic_parameter_approval_required",
            "active_interrupt": interrupt,
            "current_parameter_attempt_id": 0,
            "proposed_parameter_attempt_id": 0,
            "parameter_attempts": [],
            "current_parameter_recommendation": cursor.model_dump(mode="json"),
        },
    )


def apply_autohic_parameter_approval_decision(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> dict[str, Any]:
    """Apply a human decision bound to the immutable AutoHiC recommendation."""

    if request.stage != "hic_scaffolding" or request.gate_id != "parameter_approval":
        raise ResumeValidationError(
            "AutoHiC parameter approval requires hic_scaffolding/parameter_approval"
        )
    hic_out = _hic_output(state)
    interrupt = state.get("active_interrupt") or hic_out.get("active_interrupt")
    if (
        not isinstance(interrupt, dict)
        or interrupt.get("gate_id") != "parameter_approval"
    ):
        raise ResumeValidationError("no unresolved AutoHiC parameter_approval gate")

    required = ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")
    missing = [key for key in required if key not in request.metadata]
    if missing:
        raise ResumeValidationError(
            f"AutoHiC parameter approval metadata missing bindings: {missing}"
        )
    for key in required:
        if request.metadata.get(key) != interrupt.get(key):
            raise ResumeValidationError(
                f"stale AutoHiC parameter approval binding: {key}"
            )

    request_ref_value = interrupt.get("request_ref")
    request_id = interrupt.get("review_request_id")
    if not isinstance(request_ref_value, dict) or not isinstance(request_id, str):
        raise ResumeValidationError(
            "AutoHiC parameter approval has no request identity"
        )
    try:
        request_ref = ArtifactRef.model_validate(request_ref_value)
        request_record = load_review_request(
            absolute_data_dir(state),
            request_ref,
            session_id=state["session_id"],
        )
    except (ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC parameter approval request is missing or changed"
        ) from exc
    if request_record.get(
        "review_request_id"
    ) != request_id or request_ref.sha256 != interrupt.get("request_hash"):
        raise ResumeValidationError(
            "AutoHiC parameter approval request binding mismatch"
        )
    payload = request_record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("AutoHiC parameter approval request has no payload")
    if request.decision not in {
        "approve_parameters",
        "reject_parameters",
    } or request.decision not in set(payload.get("decisions_allowed") or []):
        raise ResumeValidationError("unsupported AutoHiC parameter approval decision")

    try:
        cursor = ParameterRecommendationCursor.model_validate(
            hic_out.get("current_parameter_recommendation")
        )
        manifest_ref = ArtifactRef.model_validate(
            payload.get("recommendation_manifest_ref")
        )
    except ValueError as exc:
        raise ResumeValidationError(
            "AutoHiC parameter approval has no valid recommendation cursor"
        ) from exc
    if (
        cursor.status != "pending_approval"
        or cursor.review_request_id != request_id
        or cursor.manifest_ref != manifest_ref
        or cursor.recommendation_hash != interrupt.get("recommendation_hash")
        or manifest_ref.sha256 != payload.get("recommendation_manifest_hash")
    ):
        raise ResumeValidationError("AutoHiC parameter approval cursor is stale")

    try:
        manifest = read_autohic_parameter_artifact(
            state,
            manifest_ref,
            expected_type=HIC_PARAMETER_RECOMMENDATION_MANIFEST,
        )
        recommendation_ref = ArtifactRef.model_validate(
            manifest.get("refs", {}).get("recommendation_ref")
        )
        recommendation_record = read_autohic_parameter_artifact(
            state,
            recommendation_ref,
            expected_type=HIC_PARAMETER_RECOMMENDATION,
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC parameter recommendation evidence is missing or changed"
        ) from exc
    recommendation = dict(recommendation_record.get("recommendation") or {})
    if not str(
        (recommendation.get("parameters") or {}).get("restriction_enzyme") or ""
    ).strip():
        raise ResumeValidationError(
            "AutoHiC recommendation does not contain restriction_enzyme"
        )
    if hic_out.get("parameter_attempts"):
        raise ResumeValidationError("AutoHiC parameter approval was already resolved")

    try:
        decision_ref = persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=request_id,
            stage="hic_scaffolding",
            gate_id="parameter_approval",
            request_hash=request_ref.sha256,
            request_ref=request_ref,
            decision={
                "schema_version": 2,
                "decision": request.decision,
                "decided_by": request.decided_by,
                "notes": list(request.notes),
                "bindings": {key: request.metadata[key] for key in required},
                "validated_recommendation_manifest_ref": manifest_ref.model_dump(
                    mode="json"
                ),
                "validated_recommendation_hash": manifest_ref.sha256,
            },
            method_name=_APPROVAL_METHOD,
            artifact_type=HIC_PARAMETER_APPROVAL_DECISION,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC parameter approval decision could not be persisted"
        ) from exc

    Ledger(absolute_data_dir(state)).record_review_decision(
        state["session_id"],
        "hic_scaffolding",
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
        outputs["hic_scaffolding"] = {
            **hic_out,
            "status": "failed",
            "failure_kind": "parameter_recommendation_rejected",
            "reason": "autohic_parameter_recommendation_rejected_by_human",
            "active_interrupt": None,
            "current_parameter_recommendation": cursor.model_copy(
                update={"status": "rejected"}
            ).model_dump(mode="json"),
        }
        return {
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "autohic_parameter_recommendation_rejected_by_human",
            "stage_outputs": outputs,
        }

    try:
        attempt = ParameterAttempt(
            attempt_id=cursor.recommendation_id,
            method_id=AUTOHIC_METHOD,
            status="recommended",
            task="recommend_initial",
            previous_attempt_id=None,
            recommendation_manifest_ref=manifest_ref,
            approval_decision_ref=decision_ref,
            recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
        )
    except ValueError as exc:
        raise ResumeValidationError(
            "approved AutoHiC parameter attempt could not be recorded"
        ) from exc
    outputs["hic_scaffolding"] = {
        **hic_out,
        "status": "parameters_approved",
        "active_interrupt": None,
        "current_parameter_attempt_id": cursor.recommendation_id,
        "parameter_attempts": [attempt.model_dump(mode="json")],
        "current_parameter_recommendation": cursor.model_copy(
            update={"status": "approved"}
        ).model_dump(mode="json"),
        "proposed_parameter_attempt_id": None,
    }
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
    }


def load_approved_autohic_parameters(state: TopLevelState) -> dict[str, Any]:
    """Load the approved recommendation consumed by the freeze node."""

    hic_out = _hic_output(state)
    try:
        attempt = current_parameter_attempt(
            hic_out,
            method_id=AUTOHIC_METHOD,
            label="AutoHiC",
        )
    except ParameterAttemptStateError as exc:
        raise AutoHiCParameterPersistenceError(str(exc)) from exc
    if attempt.status != "recommended":
        raise AutoHiCParameterPersistenceError(
            "AutoHiC parameter attempt is not approved and awaiting freeze"
        )
    decision_record = read_autohic_parameter_artifact(
        state,
        attempt.approval_decision_ref,
        expected_type=HIC_PARAMETER_APPROVAL_DECISION,
    )
    decision = decision_record.get("decision")
    if (
        not isinstance(decision, dict)
        or decision.get("decision") != "approve_parameters"
    ):
        raise AutoHiCParameterPersistenceError(
            "AutoHiC parameter attempt has no approved decision"
        )
    if decision.get("validated_recommendation_hash") != (
        attempt.recommendation_manifest_ref.sha256
    ):
        raise AutoHiCParameterPersistenceError(
            "AutoHiC approval is not bound to the current recommendation"
        )
    manifest = read_autohic_parameter_artifact(
        state,
        attempt.recommendation_manifest_ref,
        expected_type=HIC_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    recommendation_ref = ArtifactRef.model_validate(
        (manifest.get("refs") or {}).get("recommendation_ref")
    )
    recommendation_record = read_autohic_parameter_artifact(
        state,
        recommendation_ref,
        expected_type=HIC_PARAMETER_RECOMMENDATION,
    )
    recommendation = recommendation_record.get("recommendation")
    parameters = (
        recommendation.get("parameters") if isinstance(recommendation, dict) else None
    )
    if not isinstance(parameters, dict) or not parameters.get("restriction_enzyme"):
        raise AutoHiCParameterPersistenceError(
            "approved AutoHiC recommendation does not contain restriction_enzyme"
        )
    return dict(parameters)


def _input_profile(hic_out: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "pre_hic_assembly_ref": hic_out.get("pre_hic_assembly_ref"),
        "hic_read_refs": list(hic_out.get("hic_read_refs") or []),
        "hic_read_paths": list(hic_out.get("hic_read_paths") or []),
        "hic_library_metadata": list(hic_out.get("hic_library_metadata") or []),
        "provided_restriction_enzyme": (
            (hic_out.get("autohic_parameters") or {}).get("restriction_enzyme")
        ),
    }


def _fail_update(
    state: TopLevelState,
    reason: str,
    *,
    failure_kind: str,
    extra: dict[str, Any] | None = None,
) -> dict:
    return _update_hic_output(
        state,
        {
            "status": "failed",
            "failure_kind": failure_kind,
            "reason": reason,
            **(extra or {}),
        },
    )
