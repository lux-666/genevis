"""AutoHiC restriction-enzyme recommendation and mandatory approval gate."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from app.storage.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.execution.evidence import (
    load_execution_evidence,
    read_execution_stderr_tail,
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
    HIC_PARAMETER_ATTEMPT_MANIFEST,
    HIC_PARAMETER_BASIS,
    HIC_PARAMETER_INPUT_SNAPSHOT,
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
from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.schemas.parameter_recommendation import (
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
)
from app.stages.hic_scaffolding.parameters.context import build_autohic_recommendation_context
from app.stages.hic_scaffolding.method_application.support import (
    hic_output as _hic_output,
)
from app.stages.hic_scaffolding.method_application.support import (
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
    """Persist one attempt-local sparse recommendation and open approval."""

    hic_out = _hic_output(state)
    if hic_out.get("status") not in {
        "resolved",
        "retry_parameters_after_execution_failure",
        "retry_parameters_after_review_failure",
    }:
        return {}

    task = _parameter_prompt_task(hic_out)
    try:
        attempts_before = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter attempt chain is invalid: {exc}",
            failure_kind="parameter_recommendation",
        )
    recommendation_id = int(
        hic_out.get("proposed_parameter_attempt_id")
        if hic_out.get("proposed_parameter_attempt_id") is not None
        else len(attempts_before)
    )
    previous_attempt = attempts_before[-1] if attempts_before else None
    if task != "recommend_initial":
        retry_request = dict(hic_out.get("parameter_retry_request") or {})
        if (
            previous_attempt is None
            or previous_attempt.get("attempt_id") != recommendation_id - 1
            or not previous_attempt.get("seal_hash")
            or retry_request.get("previous_attempt_id")
            != previous_attempt.get("attempt_id")
            or retry_request.get("previous_attempt_seal_hash")
            != previous_attempt.get("seal_hash")
        ):
            return _fail_update(
                state,
                "AutoHiC retry predecessor evidence is missing or changed",
                failure_kind="parameter_recommendation",
            )

    try:
        catalog = load_autohic_parameter_catalog()
    except (FileNotFoundError, ValueError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter catalog is not usable: {exc}",
            failure_kind="parameter_recommendation",
        )

    try:
        attempt_history = [
            _hydrate_parameter_attempt(
                state,
                attempt,
                include_execution_log=(
                    task == "retry_after_execution_failure"
                    and index == len(attempts_before) - 1
                ),
            )
            for index, attempt in enumerate(attempts_before)
        ]
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC retry history is invalid: {exc}",
            failure_kind="parameter_recommendation",
        )
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(mode="json", exclude_none=True)
    facts = build_canonical_facts(
        shared_parameters=shared if isinstance(shared, dict) else {},
        read_qc_output=(state.get("stage_outputs") or {}).get("read_qc") or {},
        genome_survey_result=(state.get("stage_outputs") or {}).get("genome_survey") or {},
    )
    context = build_autohic_recommendation_context(state, hic_out=hic_out)
    catalog_snapshot = catalog.model_dump(mode="json")

    try:
        model_request = build_autohic_parameter_request(
            facts=facts,
            context=context,
            catalog=catalog,
            task=task,
            attempt_history=attempt_history,
        ).model_dump(mode="json")
        input_profile_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "input_profile.json",
            RecommendationInputSnapshotV3(
                method_id=catalog.method_id,
                task=task,
                facts=facts,
                stage_context=context,
                dependency_refs=RecommendationDependencyRefs(),
            ).model_dump(mode="json"),
            artifact_type=HIC_PARAMETER_INPUT_SNAPSHOT,
            method_name=_RECOMMENDER_METHOD,
        )
        basis_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "basis_snapshot.json",
            {
                "schema_version": 3,
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
                "schema_version": 3,
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
                "task": task,
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
                "task": task,
                "recommendation_id": recommendation_id,
                "status": "pending_approval" if decision.valid else "response_invalid",
                "previous_attempt_id": (
                    previous_attempt.get("attempt_id") if previous_attempt else None
                ),
                "previous_attempt_manifest_ref": (
                    previous_attempt.get("attempt_manifest_ref")
                    if previous_attempt
                    else None
                ),
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
        task=task,
        status="pending_approval" if decision.valid else "response_invalid",
        previous_attempt_id=(
            previous_attempt.get("attempt_id") if previous_attempt else None
        ),
        manifest_ref=manifest_ref,
        recommendation_hash=manifest_ref.sha256,
        previous_attempt_seal_hash=(
            previous_attempt.get("seal_hash") if previous_attempt else None
        ),
    )
    if not decision.valid:
        return _fail_update(
            state,
            "AutoHiC parameter recommendation response could not be parsed",
            failure_kind="parameter_recommendation",
            extra={"current_parameter_recommendation": cursor.model_dump(mode="json")},
        )

    sparse_parameters = dict(recommendation.get("parameters") or {})
    bindings = {
        "attempt_id": recommendation_id,
        "task": task,
        "previous_attempt_id": cursor.previous_attempt_id,
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
                "task": task,
                "parameters": sparse_parameters,
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
            "current_parameter_attempt_id": (
                hic_out.get("current_parameter_attempt_id")
            ),
            "proposed_parameter_attempt_id": recommendation_id,
            "parameter_attempts": attempts_before,
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
    if not isinstance(recommendation.get("parameters"), dict):
        raise ResumeValidationError("AutoHiC recommendation has no parameters mapping")
    try:
        attempts = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        raise ResumeValidationError("AutoHiC parameter attempt chain is invalid") from exc
    if cursor.recommendation_id != len(attempts):
        raise ResumeValidationError("AutoHiC parameter approval attempt is not next")
    if cursor.recommendation_id:
        predecessor = attempts[-1]
        if (
            cursor.previous_attempt_id != predecessor.get("attempt_id")
            or cursor.previous_attempt_seal_hash != predecessor.get("seal_hash")
        ):
            raise ResumeValidationError("AutoHiC parameter retry predecessor changed")

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
            task=cursor.task,
            previous_attempt_id=cursor.previous_attempt_id,
            recommendation_manifest_ref=manifest_ref,
            approval_decision_ref=decision_ref,
            recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
            previous_attempt_seal_hash=cursor.previous_attempt_seal_hash,
        )
    except ValueError as exc:
        raise ResumeValidationError(
            "approved AutoHiC parameter attempt could not be recorded"
        ) from exc
    attempts.append(attempt.model_dump(mode="json"))
    retry_counts = dict(hic_out.get("retry_counts") or {})
    if cursor.task == "retry_after_execution_failure":
        retry_counts["execution"] = int(retry_counts.get("execution", 0)) + 1
    elif cursor.task == "retry_after_review_failure":
        retry_counts["review"] = int(retry_counts.get("review", 0)) + 1
    outputs["hic_scaffolding"] = {
        **hic_out,
        "status": "parameters_approved",
        "active_interrupt": None,
        "current_parameter_attempt_id": cursor.recommendation_id,
        "parameter_attempts": attempts,
        "retry_counts": retry_counts,
        "current_parameter_recommendation": cursor.model_copy(
            update={"status": "approved"}
        ).model_dump(mode="json"),
        "proposed_parameter_attempt_id": None,
        "parameter_retry_request": None,
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
    if not isinstance(parameters, dict):
        raise AutoHiCParameterPersistenceError(
            "approved AutoHiC recommendation has no parameters mapping"
        )
    return dict(parameters)


def _input_profile(
    state: TopLevelState,
    hic_out: dict[str, Any],
    *,
    attempt_history: list[dict[str, Any]],
) -> dict[str, Any]:
    shared = state.get("shared_parameters") or {}
    return {
        "schema_version": 2,
        "pre_hic_assembly_ref": hic_out.get("pre_hic_assembly_ref"),
        "hic_read_refs": list(hic_out.get("hic_read_refs") or []),
        "hic_read_paths": list(hic_out.get("hic_read_paths") or []),
        "hic_library_metadata": list(hic_out.get("hic_library_metadata") or []),
        "provided_restriction_enzyme": (
            (hic_out.get("autohic_parameters") or {}).get("restriction_enzyme")
        ),
        "species_facts": (
            shared.get("species_facts") if isinstance(shared, dict) else None
        ),
        "attempt_history": attempt_history,
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


def autohic_execution_retry_gate(state: TopLevelState) -> dict:
    """Seal a failed execution and apply the operator-owned retry policy."""

    hic_out = _hic_output(state)
    if hic_out.get("status") != "failed" or hic_out.get("failure_kind") != "execution":
        return {}
    try:
        attempts = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
        current_id = int(hic_out["current_parameter_attempt_id"])
        current = ParameterAttempt.model_validate(attempts[current_id])
        if current.execution_ref is None:
            return {}
        sealed = _seal_parameter_attempt(state, current.model_dump(mode="json"))
    except (IndexError, KeyError, OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC execution retry evidence is invalid: {exc}",
            failure_kind="execution",
        )
    attempts[current_id] = sealed
    policy = _parameter_retry_policy(hic_out)
    used = int((hic_out.get("retry_counts") or {}).get("execution", 0))
    if not policy["retry_on_execution_failure"]:
        return _update_hic_output(
            state,
            {
                "status": "failed",
                "reason": hic_out.get("reason") or "AutoHiC execution failed",
                "parameter_attempts": attempts,
            },
        )
    if used >= policy["max_execution_retries"]:
        return _update_hic_output(
            state,
            {
                "status": "execution_parameter_retries_exhausted",
                "reason": "autohic_execution_retry_budget_exhausted",
                "parameter_attempts": attempts,
            },
        )
    return _update_hic_output(
        state,
        {
            "status": "retry_parameters_after_execution_failure",
            "proposed_parameter_attempt_id": current_id + 1,
            "parameter_attempts": attempts,
            "parameter_retry_request": {
                "origin": "autohic_execution",
                "previous_attempt_id": current_id,
                "previous_attempt_seal_hash": sealed["seal_hash"],
                "failure_ref": sealed["execution_ref"],
            },
        },
    )


def prepare_autohic_review_retry(
    state: TopLevelState,
    decision_ref: ArtifactRef,
) -> dict:
    """Seal reviewer feedback and create a ref-only retry cursor."""

    hic_out = _hic_output(state)
    try:
        attempts = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
        current_id = int(hic_out["current_parameter_attempt_id"])
        current = ParameterAttempt.model_validate(attempts[current_id])
        if current.status != "executed":
            raise ValueError("AutoHiC review retry requires an executed attempt")
        reviewed = current.model_copy(
            update={"status": "review_failed", "review_ref": decision_ref}
        ).model_dump(mode="json")
        sealed = _seal_parameter_attempt(state, reviewed)
    except (IndexError, KeyError, OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC review retry evidence is invalid: {exc}",
            failure_kind="review",
        )
    attempts[current_id] = sealed
    policy = _parameter_retry_policy(hic_out)
    used = int((hic_out.get("retry_counts") or {}).get("review", 0))
    if used >= policy["max_review_retries"]:
        return _update_hic_output(
            state,
            {
                "status": "review_parameter_retries_exhausted",
                "reason": "autohic_review_retry_budget_exhausted",
                "parameter_attempts": attempts,
            },
        )
    return _update_hic_output(
        state,
        {
            "status": "retry_parameters_after_review_failure",
            "proposed_parameter_attempt_id": current_id + 1,
            "parameter_attempts": attempts,
            "parameter_retry_request": {
                "origin": "autohic_review",
                "previous_attempt_id": current_id,
                "previous_attempt_seal_hash": sealed["seal_hash"],
                "failure_ref": decision_ref.model_dump(mode="json"),
            },
        },
    )


def _parameter_prompt_task(hic_out: dict[str, Any]) -> str:
    status = hic_out.get("status")
    if status == "retry_parameters_after_execution_failure":
        return "retry_after_execution_failure"
    if status == "retry_parameters_after_review_failure":
        return "retry_after_review_failure"
    return "recommend_initial"


def _parameter_retry_policy(hic_out: dict[str, Any]) -> dict[str, Any]:
    policy = dict(hic_out.get("parameter_retry_policy") or {})
    return {
        "retry_on_execution_failure": bool(
            policy.get("retry_on_execution_failure", False)
        ),
        "max_execution_retries": max(
            0, int(policy.get("max_execution_retries", 0))
        ),
        "max_review_retries": max(0, int(policy.get("max_review_retries", 1))),
    }


def _hydrate_parameter_attempt(
    state: TopLevelState,
    attempt_value: dict[str, Any],
    *,
    include_execution_log: bool = False,
) -> dict[str, Any]:
    attempt = ParameterAttempt.model_validate(attempt_value)
    manifest = read_autohic_parameter_artifact(
        state,
        attempt.recommendation_manifest_ref,
        expected_type=HIC_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    recommendation_ref = ArtifactRef.model_validate(
        (manifest.get("refs") or {}).get("recommendation_ref")
    )
    recommendation = read_autohic_parameter_artifact(
        state, recommendation_ref, expected_type=HIC_PARAMETER_RECOMMENDATION
    ).get("recommendation") or {}
    approval = read_autohic_parameter_artifact(
        state,
        attempt.approval_decision_ref,
        expected_type=HIC_PARAMETER_APPROVAL_DECISION,
    ).get("decision") or {}
    hydrated: dict[str, Any] = {
        "attempt_id": attempt.attempt_id,
        "task": attempt.task,
        "status": attempt.status,
        "parameters": dict(recommendation.get("parameters") or {}),
        "approval_decision": approval.get("decision"),
    }
    if attempt.frozen_parameters_ref is not None:
        frozen = read_autohic_parameter_artifact(
            state,
            attempt.frozen_parameters_ref,
            expected_type="hic_scaffolding.parameter.frozen_parameters",
        )
        hydrated["approved_sparse_parameters"] = dict(
            frozen.get("approved_sparse_parameters") or {}
        )
        hydrated["executor_defaults"] = {
            key: value
            for key, value in dict(frozen.get("executor_defaults") or {}).items()
            if key in {
                "number_of_edit_rounds",
                "translocation_adjust",
                "inversion_adjust",
                "debris_adjust",
                "error_max_epoch",
                "error_min_len",
                "error_max_len",
                "error_filter_score",
                "error_filter_iou_score",
            }
        }
        hydrated["effective_parameters"] = {
            key: value
            for key, value in dict(frozen.get("parameters") or {}).items()
            if key in {
                "restriction_enzyme",
                "number_of_edit_rounds",
                "translocation_adjust",
                "inversion_adjust",
                "debris_adjust",
                "error_max_epoch",
                "error_min_len",
                "error_max_len",
                "error_filter_score",
                "error_filter_iou_score",
            }
        }
    if attempt.execution_ref is not None:
        execution_evidence = load_execution_evidence(
            state,
            attempt.execution_ref,
            expected_stage="hic_scaffolding",
            expected_method_name=AUTOHIC_METHOD,
        )
        execution = execution_evidence.model_dump(mode="json")
        hydrated["execution"] = {
            key: execution.get(key)
            for key in (
                "status",
                "status_detail",
                "return_code",
                "error",
            )
            if execution.get(key) is not None
        }
        if include_execution_log and not execution_evidence.success:
            stderr_tail = read_execution_stderr_tail(state, execution_evidence)
            if stderr_tail:
                hydrated["execution"]["stderr_tail"] = stderr_tail
    if attempt.review_ref is not None:
        hydrated["review"] = read_autohic_parameter_artifact(
            state, attempt.review_ref
        ).get("decision")
    return hydrated


def _seal_parameter_attempt(
    state: TopLevelState, attempt_value: dict[str, Any]
) -> dict[str, Any]:
    value = ParameterAttempt.model_validate(attempt_value).model_dump(mode="json")
    semantic = {
        key: item
        for key, item in value.items()
        if key not in {"sealed_at", "seal_hash", "attempt_manifest_ref"}
    }
    digest = sha256(
        json.dumps(
            semantic, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    if value.get("seal_hash"):
        if value["seal_hash"] != digest or not value.get("attempt_manifest_ref"):
            raise ValueError("sealed AutoHiC parameter attempt was mutated")
        manifest = read_autohic_parameter_artifact(
            state,
            value["attempt_manifest_ref"],
            expected_type=HIC_PARAMETER_ATTEMPT_MANIFEST,
        )
        if manifest.get("seal_hash") != digest:
            raise ValueError("AutoHiC parameter attempt manifest seal mismatch")
        return value
    sealed_at = datetime.now(timezone.utc).isoformat()
    manifest_ref = write_autohic_parameter_artifact(
        state,
        int(value["attempt_id"]),
        "attempt_manifest.json",
        {
            "schema_version": 2,
            "stage": "hic_scaffolding",
            **semantic,
            "sealed_at": sealed_at,
            "seal_hash": digest,
        },
        artifact_type=HIC_PARAMETER_ATTEMPT_MANIFEST,
        method_name=AUTOHIC_METHOD,
        scope="attempt",
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
    state: TopLevelState, raw_attempts: list[Any]
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for item in raw_attempts:
        if not isinstance(item, dict):
            raise ValueError("AutoHiC parameter attempt chain contains a non-mapping")
        value = ParameterAttempt.model_validate(item).model_dump(mode="json")
        if value.get("seal_hash"):
            value = _seal_parameter_attempt(state, value)
        attempts.append(value)
    for sequence, attempt in enumerate(attempts):
        if attempt["attempt_id"] != sequence:
            raise ValueError("AutoHiC parameter attempt IDs must be sequential")
        if sequence == 0:
            if attempt["task"] != "recommend_initial":
                raise ValueError("AutoHiC attempt 0 must be initial")
            continue
        previous = attempts[sequence - 1]
        if attempt.get("previous_attempt_id") != sequence - 1:
            raise ValueError("AutoHiC parameter attempt predecessor is broken")
        if not previous.get("seal_hash"):
            raise ValueError("AutoHiC retry predecessor is not sealed")
        if attempt.get("previous_attempt_seal_hash") != previous.get("seal_hash"):
            raise ValueError("AutoHiC retry predecessor seal is broken")
        if attempt.get("recommendation_contract") != attempts[0].get(
            "recommendation_contract"
        ):
            raise ValueError("AutoHiC recommendation contract drifted")
    return attempts
