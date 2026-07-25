"""Strict v3 Assembly recommendation, approval, retry, and attempt cursors.

Recommendation semantics stay in the existing domain modules.  This module
only separates immutable evidence from the bounded state required to resume
the assembly parameter loop.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from app.control.lifecycle import ResumeValidationError
from app.domains.assembly.selection import AssemblySelection
from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.execution.evidence import (
    load_execution_evidence,
    read_execution_stderr_tail,
)
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
    ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    ASSEMBLY_PARAMETER_REVIEW,
    ArtifactRef,
    ParameterAttempt,
    ParameterAttemptTask,
    ParameterRecommendationCursor,
)
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    AssemblyReviewEvidence,
    ExecutionFailureTrigger,
    ParameterAttemptEvidenceV3,
    ParameterExecutionEvidence,
    ParameterRecommendationManifestRefsV3,
    ParameterRecommendationManifestV3,
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
    RecommendationRetryContext,
    ResolvedParameterSources,
    ReviewFailureTrigger,
)
from app.schemas.state import JsonValue, StateUpdate, TopLevelState
from app.stages.assembly.parameters.basis import build_parameter_basis_snapshot
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.assembly.parameters.context import build_assembly_recommendation_context
from app.stages.assembly.parameters.storage import (
    ParameterPersistenceError,
    read_json_artifact,
    recover_recommendation_json_artifact,
    write_attempt_json_artifact,
    write_recommendation_json_artifact,
)
from app.stages.assembly.state import (
    AssemblyStageCursor,
    assembly_stage_output,
    update_assembly_stage_output,
    validated_assembly_stage_payload,
)
from app.stages.base import absolute_data_dir
from app.storage.ledger import Ledger
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    ParameterRecommendationChainV3,
    build_recommendation_dependency_refs,
    canonical_parameter_dependency,
    load_approved_parameter_chain_v3,
    load_parameter_recommendation_chain_v3,
)


def recommend_assembly_parameters(state: TopLevelState) -> StateUpdate:
    """Run the existing recommender while persisting each fact separately."""

    assembly_out = assembly_stage_output(state)
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
    try:
        attempts_before = _validate_parameter_attempt_chain(
            state,
            assembly_out.get("parameter_attempts") or [],
        )
    except (ParameterPersistenceError, ValueError) as exc:
        return _fail_update(state, f"Assembly parameter inputs failed: {exc}")
    proposed_id = assembly_out.get("proposed_parameter_attempt_id")
    recommendation_id = (
        proposed_id
        if proposed_id is not None
        else assembly_out.get("current_parameter_attempt_id") or len(attempts_before)
    )
    previous_attempt = _previous_parameter_attempt(assembly_out, recommendation_id)
    initial_attempt = (
        ParameterAttempt.model_validate(attempts_before[0]) if attempts_before else None
    )
    retry_context: RecommendationRetryContext | None = None
    basis_ref: ArtifactRef | None = None
    basis_snapshot: dict[str, Any] | None = None

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
        if task == "recommend_initial":
            catalog = load_parameter_catalog(method_name)
            stage_outputs = state.get("stage_outputs") or {}
            shared = state.get("shared_parameters")
            if shared is None:
                shared = {}
            if hasattr(shared, "model_dump"):
                shared = shared.model_dump(mode="json", exclude_none=True)
            facts = build_canonical_facts(
                shared_parameters=shared,
                read_qc_output=stage_outputs.get("read_qc"),
                genome_survey_result=stage_outputs.get("genome_survey"),
                asset_flags={
                    "has_hifi": bool(assembly_out.get("hifi_reads_ref")),
                    "has_ont": bool(assembly_out.get("ont_reads_ref")),
                    "has_hic": bool(assembly_out.get("hic_reads_ref")),
                },
            )
            context = build_assembly_recommendation_context(
                state,
                assembly_out=assembly_out,
                retry_context=None,
            )
            dependency_refs = _initial_assembly_dependency_refs(
                state,
                assembly_out,
            )
        else:
            if initial_attempt is None:
                return _retry_transition_failure(state, "missing_initial_attempt")
            initial_chain = load_parameter_recommendation_chain_v3(
                state,
                manifest_ref=initial_attempt.recommendation_manifest_ref,
                expected_stage="assembly",
                expected_method_id=method_name,
            )
            catalog = initial_chain.catalog
            basis_snapshot = dict(initial_chain.basis_snapshot)
            basis_ref = initial_chain.manifest.refs.basis_snapshot_ref
            facts = initial_chain.input_snapshot.facts
            hydrated_history = tuple(
                hydrate_parameter_attempt(
                    state,
                    attempt,
                    include_execution_log=(
                        task == "retry_after_execution_failure"
                        and index == len(attempts_before) - 1
                    ),
                )
                for index, attempt in enumerate(attempts_before)
            )
            retry_context = _assembly_retry_context(task, hydrated_history)
            initial_context = initial_chain.input_snapshot.stage_context
            if not isinstance(initial_context, AssemblyRecommendationContext):
                raise ValueError("initial Assembly snapshot has wrong stage context")
            context = initial_context.model_copy(update={"retry": retry_context})
            dependency_refs = _retry_assembly_dependency_refs(
                state,
                initial_chain=initial_chain,
                attempts_before=attempts_before,
            )

        if not any(spec.layers.l2 is not None for spec in catalog.parameters.values()):
            raise ValueError(
                f"Assembly parameter catalog has no L2 parameters: {catalog.method_id}"
            )
    except (
        FileNotFoundError,
        KeyError,
        ParameterEvidenceError,
        ParameterPersistenceError,
        ValueError,
    ) as exc:
        return _fail_update(state, f"Assembly parameter inputs failed: {exc}")

    try:
        input_snapshot = RecommendationInputSnapshotV3(
            schema_version=3,
            method_id=catalog.method_id,
            task=task,
            facts=facts,
            stage_context=context,
            dependency_refs=dependency_refs,
        )
        input_snapshot_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "input_snapshot.json",
            input_snapshot.model_dump(mode="json"),
            artifact_type=ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
            method_name="assembly.parameter_recommender",
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(state, f"Assembly input snapshot persistence failed: {exc}")

    try:
        if task == "recommend_initial":
            basis_snapshot = build_parameter_basis_snapshot(
                method_id=catalog.method_id,
                catalog=catalog,
                facts=facts,
                context=context,
            )
            basis_ref = write_recommendation_json_artifact(
                state,
                recommendation_id,
                "basis_snapshot.json",
                basis_snapshot,
                artifact_type=ASSEMBLY_PARAMETER_BASIS,
                method_name="assembly.parameter_recommender",
            )
        if basis_ref is None or basis_snapshot is None:
            raise ValueError("Assembly recommendation basis is missing")

        request_dto = build_parameter_request_dto(
            method_id=catalog.method_id,
            catalog=catalog,
            facts=facts,
            context=context,
            task=task,
            basis_snapshot=basis_snapshot,
        )

        messages = render_assembly_parameter_messages(request_dto)
        model_request_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "model_request.json",
            {
                "schema_version": 3,
                "prompt_bundle_id": parameter_prompt_bundle_name(catalog.method_id),
                "prompt_template_sha256": parameter_prompt_bundle_hash(
                    catalog.method_id
                ),
                "parameter_request_schema_version": 3,
                "messages": messages,
            },
            artifact_type=ASSEMBLY_PARAMETER_MODEL_REQUEST,
            method_name="assembly.parameter_recommender",
        )
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        ParameterPersistenceError,
        ValueError,
    ) as exc:
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

    if not decision.valid:
        cursor_status: Literal["response_invalid", "pending_approval"] = (
            "response_invalid"
        )
        stage_status = "parameter_response_invalid"
    else:
        cursor_status = "pending_approval"
        stage_status = "blocked_human"

    previous_manifest_ref = (
        previous_attempt.get("attempt_manifest_ref") if previous_attempt else None
    )
    try:
        manifest = ParameterRecommendationManifestV3(
            schema_version=3,
            recommendation_contract="parameter_recommendation_v1",
            stage="assembly",
            method_id=catalog.method_id,
            task=task,
            recommendation_id=recommendation_id,
            status=cursor_status,
            previous_attempt_id=(
                previous_attempt.get("attempt_id") if previous_attempt else None
            ),
            previous_attempt_manifest_ref=previous_manifest_ref,
            previous_attempt_seal_hash=(
                previous_attempt.get("seal_hash") if previous_attempt else None
            ),
            refs=ParameterRecommendationManifestRefsV3(
                input_snapshot_ref=input_snapshot_ref,
                basis_snapshot_ref=basis_ref,
                model_request_ref=model_request_ref,
                model_response_ref=model_response_ref,
                recommendation_ref=recommendation_ref,
                response_parse_ref=response_parse_ref,
            ),
            summary={"response_parsed": bool(decision.valid)},
        )
        manifest_ref = write_recommendation_json_artifact(
            state,
            recommendation_id,
            "recommendation_manifest.json",
            manifest.model_dump(mode="json"),
            artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
            method_name="assembly.parameter_recommender",
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(state, f"Assembly recommendation manifest failed: {exc}")

    cursor = ParameterRecommendationCursor(
        schema_version=3,
        recommendation_id=recommendation_id,
        method_id=catalog.method_id,
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
    update: AssemblyStageCursor = {
        "status": stage_status,
        "proposed_parameter_attempt_id": recommendation_id,
        "current_parameter_recommendation": cursor.model_dump(mode="json"),
    }
    if not decision.valid:
        update["reason"] = (
            "Assembly parameter recommendation response could not be parsed"
        )
        return update_assembly_stage_output(state, update)
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
            "active_interrupt": validated_assembly_stage_payload(
                {"active_interrupt": interrupt}
            ).get("active_interrupt"),
            "current_parameter_recommendation": cursor.model_dump(mode="json"),
        }
    )
    return update_assembly_stage_output(state, update)


def apply_parameter_approval_decision(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> StateUpdate:
    """Apply one decision bound to a verified immutable recommendation manifest."""

    if request.stage != "assembly":
        raise ResumeValidationError("parameter approval resume requires stage assembly")
    if request.gate_id != "parameter_approval":
        raise ResumeValidationError(
            "parameter approval resume requires explicit gate_id=parameter_approval"
        )
    assembly_out = assembly_stage_output(state)
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
        chain = load_parameter_recommendation_chain_v3(
            state,
            manifest_ref=manifest_ref,
            expected_stage="assembly",
            expected_method_id=cursor.method_id,
        )
    except (ValueError, ParameterEvidenceError) as exc:
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
    manifest = chain.manifest
    if (
        manifest.status != "pending_approval"
        or manifest.recommendation_id != cursor.recommendation_id
        or manifest.task != cursor.task
        or manifest.previous_attempt_id != cursor.previous_attempt_id
        or manifest.previous_attempt_seal_hash != cursor.previous_attempt_seal_hash
    ):
        raise ResumeValidationError(
            "parameter recommendation cursor does not match its manifest"
        )
    recommendation = chain.recommendation.get("recommendation")
    if not isinstance(recommendation, dict) or not isinstance(
        recommendation.get("parameters"), dict
    ):
        raise ResumeValidationError(
            "parameter recommendation has no parameters mapping"
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
        if previous_id is None:
            raise ResumeValidationError(
                "parameter approval attempt chain has no predecessor"
            )
        if (
            not attempts
            or attempts[-1].get("attempt_id") != previous_id
            or attempt_id != previous_id + 1
        ):
            raise ResumeValidationError("parameter approval attempt chain head changed")
        if cursor.previous_attempt_seal_hash != attempts[-1].get("seal_hash"):
            raise ResumeValidationError("parameter retry previous attempt seal changed")
        predecessor_manifest_ref = attempts[-1].get("attempt_manifest_ref")
        if (
            predecessor_manifest_ref is None
            or manifest.previous_attempt_manifest_ref
            != ArtifactRef.model_validate(predecessor_manifest_ref)
        ):
            raise ResumeValidationError(
                "parameter retry previous attempt manifest changed"
            )

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

    if request.decision == "reject_parameters":
        rejected_cursor = cursor.model_copy(update={"status": "rejected"})
        stage_update = update_assembly_stage_output(
            state,
            {
                "status": "parameter_recommendation_rejected_by_human",
                "reason": "parameter_recommendation_rejected_by_human",
                "active_interrupt": None,
                "current_parameter_recommendation": rejected_cursor.model_dump(
                    mode="json"
                ),
                "parameter_rejection": {
                    "recommendation_id": attempt_id,
                    "manifest_ref": manifest_ref.model_dump(mode="json"),
                    "decision_ref": decision_ref.model_dump(mode="json"),
                },
            },
        )
        return {
            **stage_update,
            "current_stage": "assembly",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "parameter_recommendation_rejected_by_human",
        }

    attempt = ParameterAttempt(
        schema_version=3,
        attempt_id=attempt_id,
        method_id=cursor.method_id,
        status="recommended",
        task=cursor.task,
        previous_attempt_id=previous_id,
        recommendation_manifest_ref=manifest_ref,
        approval_decision_ref=decision_ref,
        recommendation_contract=manifest.recommendation_contract,
        previous_attempt_seal_hash=cursor.previous_attempt_seal_hash,
    ).model_dump(mode="json")
    attempts.append(attempt)
    retry_counts = dict(assembly_out.get("retry_counts") or {})
    if cursor.task == "retry_after_execution_failure":
        retry_counts["execution"] = int(retry_counts.get("execution", 0)) + 1
    elif cursor.task == "retry_after_review_failure":
        retry_counts["review"] = int(retry_counts.get("review", 0)) + 1
    approved_cursor = cursor.model_copy(update={"status": "approved"})
    stage_update = update_assembly_stage_output(
        state,
        {
            "status": "parameters_approved",
            "current_parameter_attempt_id": attempt_id,
            "parameter_attempts": attempts,
            "retry_counts": retry_counts,
            "active_interrupt": None,
            "current_parameter_recommendation": approved_cursor.model_dump(mode="json"),
            "proposed_parameter_attempt_id": None,
            "parameter_retry_request": None,
        },
    )
    return {
        **stage_update,
        "current_stage": "assembly",
        "supervision_state": "running_stage",
        "active_interrupt": None,
    }


def execution_retry_gate(state: TopLevelState) -> StateUpdate:
    """Prepare a ref-only retry cursor after command execution failure."""

    assembly_out = assembly_stage_output(state)
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
        return update_assembly_stage_output(
            state,
            {
                "status": "execution_parameter_retries_exhausted",
                "reason": "execution_retry_budget_exhausted",
                "parameter_attempts": updated_attempts,
            },
        )
    return update_assembly_stage_output(
        state,
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


def parameter_quality_gate(state: TopLevelState) -> StateUpdate:
    """Persist review evidence and prepare a ref-only quality retry cursor."""

    assembly_out = assembly_stage_output(state)
    review_out = (state.get("stage_outputs") or {}).get("assembly_review") or {}
    metrics_value = (
        review_out.get("metrics")
        or review_out.get("summary")
        or assembly_out.get("review_metrics")
    )
    metrics = dict(metrics_value) if isinstance(metrics_value, dict) else {}
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
        return update_assembly_stage_output(
            state,
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
        return update_assembly_stage_output(
            state,
            {
                "status": "review_parameter_retries_exhausted",
                "quality_gate": compact_quality,
                "parameter_attempts": updated_attempts,
                "reason": "review_retry_budget_exhausted",
            },
        )
    return update_assembly_stage_output(
        state,
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


def _assembly_retry_context(
    task: str,
    attempt_history: tuple[ParameterAttemptEvidenceV3, ...],
) -> RecommendationRetryContext:
    if not attempt_history:
        raise ValueError("Assembly retry requires sealed predecessor evidence")
    latest = attempt_history[-1]
    if task == "retry_after_execution_failure":
        execution = latest.execution
        if execution is None:
            raise ValueError("Assembly execution retry has no execution evidence")
        trigger: ExecutionFailureTrigger | ReviewFailureTrigger = (
            ExecutionFailureTrigger(
                status=execution.status,
                return_code=execution.return_code,
                failure_class=execution.failure_class,
                status_detail=execution.status_detail,
            )
        )
    elif task == "retry_after_review_failure":
        review = latest.review
        if not isinstance(review, AssemblyReviewEvidence):
            raise ValueError("Assembly review retry has no Assembly review evidence")
        trigger = ReviewFailureTrigger(review=review)
    else:
        raise ValueError(f"unsupported Assembly parameter task: {task!r}")
    return RecommendationRetryContext(
        trigger=trigger,
        attempt_history=attempt_history,
    )


def _initial_assembly_dependency_refs(
    state: TopLevelState,
    assembly_out: AssemblyStageCursor,
) -> RecommendationDependencyRefs:
    stage_outputs = state.get("stage_outputs") or {}
    read_qc = stage_outputs.get("read_qc") or {}
    genome_survey = stage_outputs.get("genome_survey") or {}
    read_qc_dependency = canonical_parameter_dependency(
        state, _dependency_value(read_qc.get("summary_artifact_ref"))
    )
    survey_dependency = canonical_parameter_dependency(
        state, _dependency_value(genome_survey.get("result_artifact_ref"))
    )
    fact_sources: dict[
        Literal["read_qc_summary", "genome_survey_result"],
        ArtifactRef | Mapping[str, Any] | Path,
    ] = {}
    if read_qc_dependency is not None:
        fact_sources["read_qc_summary"] = read_qc_dependency
    if survey_dependency is not None:
        fact_sources["genome_survey_result"] = survey_dependency

    registry_ref = canonical_parameter_dependency(
        state, state.get("asset_registry_ref")
    )
    if registry_ref is not None:
        stage_inputs = {"asset_registry": registry_ref}
    else:
        raw_inputs = assembly_out.get("input_ref")
        if not isinstance(raw_inputs, list):
            primary_input = raw_inputs or assembly_out.get("primary_long_reads_ref")
            raw_inputs = [primary_input] if primary_input is not None else []
        stage_inputs = {
            f"assembly_input_{index:06d}": dependency
            for index, value in enumerate(raw_inputs)
            if (dependency := canonical_parameter_dependency(state, value)) is not None
        }
    return build_recommendation_dependency_refs(
        state,
        fact_source_paths=fact_sources,
        stage_input_paths=stage_inputs,
    )


def _retry_assembly_dependency_refs(
    state: TopLevelState,
    *,
    initial_chain: ParameterRecommendationChainV3,
    attempts_before: list[dict[str, Any]],
) -> RecommendationDependencyRefs:
    prior_manifest_refs: list[ArtifactRef] = []
    for raw_attempt in attempts_before:
        attempt = ParameterAttempt.model_validate(raw_attempt)
        if attempt.seal_hash is None or attempt.attempt_manifest_ref is None:
            raise ValueError("Assembly retry history contains an unsealed attempt")
        prior_manifest_refs.append(attempt.attempt_manifest_ref)

    initial_refs = initial_chain.input_snapshot.dependency_refs
    return build_recommendation_dependency_refs(
        state,
        fact_source_paths=initial_refs.fact_source_refs,
        stage_input_paths={
            f"stage_input_{index:06d}": ref
            for index, ref in enumerate(initial_refs.stage_input_refs)
        },
        initial_input_snapshot_ref=initial_chain.manifest.refs.input_snapshot_ref,
        prior_attempt_manifest_refs=prior_manifest_refs,
    )


def hydrate_parameter_attempt(
    state: TopLevelState,
    attempt_value: ParameterAttempt | dict[str, Any],
    *,
    include_execution_log: bool = False,
) -> ParameterAttemptEvidenceV3:
    """Build bounded retry evidence from one approved immutable v3 attempt."""

    attempt = (
        attempt_value
        if isinstance(attempt_value, ParameterAttempt)
        else ParameterAttempt.model_validate(attempt_value)
    )
    approved_bundle = load_approved_parameter_chain_v3(
        state,
        attempt=attempt,
        expected_stage="assembly",
        expected_method_id=attempt.method_id,
    )
    recommended_parameters = approved_bundle.approved_l2

    if attempt.frozen_parameters_ref is None:
        raise ValueError("sealed Assembly retry history has no frozen parameters")
    frozen = read_json_artifact(
        state,
        attempt.frozen_parameters_ref,
        expected_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    )
    try:
        frozen_manifest_ref = ArtifactRef.model_validate(
            frozen.get("recommendation_manifest_ref")
        )
        frozen_approval_ref = ArtifactRef.model_validate(
            frozen.get("approval_decision_ref")
        )
    except ValueError as exc:
        raise ValueError(
            "Assembly frozen parameter evidence has invalid chain refs"
        ) from exc
    if (
        frozen.get("schema_version") != 3
        or frozen.get("stage") != "assembly"
        or frozen.get("attempt_id") != attempt.attempt_id
        or frozen.get("method_id") != attempt.method_id
        or frozen_manifest_ref != attempt.recommendation_manifest_ref
        or frozen_approval_ref != attempt.approval_decision_ref
    ):
        raise ValueError(
            "Assembly frozen parameter evidence is not bound to the attempt"
        )
    try:
        resolved = ResolvedParameterSources.model_validate(
            {
                key: frozen[key]
                for key in (
                    "l0_executor",
                    "l1_facts",
                    "l2_approved",
                    "effective_parameters",
                    "effective_source_by_parameter",
                    "applications",
                )
            }
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("Assembly frozen parameter sources are invalid") from exc
    if resolved.l2_approved != recommended_parameters:
        raise ValueError(
            "Assembly frozen approved parameters do not match the recommendation"
        )

    execution: ParameterExecutionEvidence | None = None
    if attempt.execution_ref is not None:
        execution_record = load_execution_evidence(
            state,
            attempt.execution_ref,
            expected_stage="assembly",
            expected_method_name=attempt.method_id,
        )
        stderr_tail: str | None = None
        if include_execution_log and not execution_record.success:
            stderr_tail = read_execution_stderr_tail(state, execution_record)
            if stderr_tail:
                stderr_tail = stderr_tail[-4000:]
        detail = execution_record.status_detail or execution_record.error or None
        execution = ParameterExecutionEvidence(
            status=execution_record.status,
            return_code=execution_record.return_code,
            failure_class=(
                str(execution_record.status) if not execution_record.success else None
            ),
            status_detail=detail[:500] if detail else None,
            resource_metrics=[],
            stderr_tail=stderr_tail,
        )

    review: AssemblyReviewEvidence | None = None
    if attempt.review_ref is not None:
        review_record = read_json_artifact(
            state,
            attempt.review_ref,
            expected_type=ASSEMBLY_PARAMETER_REVIEW,
        )
        if (
            review_record.get("schema_version") != 2
            or review_record.get("attempt_id") != attempt.attempt_id
        ):
            raise ValueError("Assembly review evidence is not bound to the attempt")
        diagnosis = review_record.get("diagnosis")
        metrics = review_record.get("review_metrics")
        if not isinstance(diagnosis, dict) or not isinstance(metrics, dict):
            raise ValueError("Assembly review evidence is malformed")
        review = _assembly_review_evidence(diagnosis, metrics)

    return ParameterAttemptEvidenceV3(
        attempt_id=attempt.attempt_id,
        task=attempt.task,
        status=attempt.status,
        recommended_parameters=recommended_parameters,
        approved_parameters=recommended_parameters,
        effective_parameters=resolved.effective_parameters,
        effective_sources=resolved.effective_source_by_parameter,
        execution=execution,
        review=review,
    )


def _assembly_review_evidence(
    diagnosis: dict[str, Any],
    metrics: dict[str, Any],
) -> AssemblyReviewEvidence:
    quast_source = metrics.get("quast_metrics")
    quast_metrics: dict[str, float] = {}
    if isinstance(quast_source, dict):
        for key in (
            "gc_percent",
            "l50",
            "misassemblies",
            "num_contigs",
            "total_length",
        ):
            value = _float_or_none(quast_source.get(key))
            if value is not None:
                quast_metrics[key] = value

    return AssemblyReviewEvidence(
        failure_mode=(
            str(diagnosis["failure_mode"])[:120]
            if diagnosis.get("failure_mode") is not None
            else None
        ),
        assembly_size=_float_or_none(
            _first_metric(metrics, "assembly_size", "assembly_size_bp", "total_length")
        ),
        assembly_size_ratio=_float_or_none(metrics.get("assembly_size_ratio")),
        contig_count=_int_or_none(
            _first_metric(metrics, "contig_count", "num_contigs")
        ),
        contig_n50=_float_or_none(
            _first_metric(metrics, "contig_n50", "n50", "n50_bp")
        ),
        contig_n90=_float_or_none(_first_metric(metrics, "contig_n90", "n90")),
        largest_contig=_float_or_none(metrics.get("largest_contig")),
        busco_complete=_float_or_none(
            _first_metric(metrics, "busco_complete", "busco_complete_ratio")
        ),
        busco_single=_float_or_none(
            _first_metric(metrics, "busco_single", "busco_single_ratio")
        ),
        busco_duplicate=_float_or_none(
            _first_metric(metrics, "busco_duplicate", "busco_duplicate_ratio")
        ),
        busco_fragmented=_float_or_none(
            _first_metric(metrics, "busco_fragmented", "busco_fragmented_ratio")
        ),
        busco_missing=_float_or_none(
            _first_metric(metrics, "busco_missing", "busco_missing_ratio")
        ),
        quast_metrics=quast_metrics or None,
    )


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
        expected_manifest = {
            "schema_version": 3,
            "stage": "assembly",
            **semantic,
            "sealed_at": value.get("sealed_at"),
            "seal_hash": digest,
        }
        if manifest != expected_manifest:
            raise ValueError("parameter attempt manifest seal mismatch")
        return value

    sealed_at = datetime.now(timezone.utc).isoformat()
    manifest_ref = write_attempt_json_artifact(
        state,
        int(value["attempt_id"]),
        "attempt_manifest.json",
        {
            "schema_version": 3,
            "stage": "assembly",
            **semantic,
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
        if item.get("schema_version") != 3:
            raise ValueError("unsupported_parameter_recommendation_schema")
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
    assembly_out: AssemblyStageCursor,
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


def _fail_update(state: TopLevelState, reason: str) -> StateUpdate:
    return update_assembly_stage_output(state, {"status": "failed", "reason": reason})


def _selected_parameter_method(assembly_out: AssemblyStageCursor) -> str | None:
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


def _parameter_prompt_task(
    assembly_out: AssemblyStageCursor,
) -> ParameterAttemptTask:
    status = assembly_out.get("status")
    if status == "retry_parameters_after_execution_failure":
        return "retry_after_execution_failure"
    if status == "retry_parameters_after_review_failure":
        return "retry_after_review_failure"
    return "recommend_initial"


def _previous_parameter_attempt(
    assembly_out: AssemblyStageCursor,
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


def _retry_transition_failure(state: TopLevelState, reason: str) -> StateUpdate:
    return update_assembly_stage_output(
        state,
        {"status": "parameter_retry_invalid_transition", "reason": reason},
    )


def _parameter_recommendation_config(
    state: TopLevelState,
    assembly_out: AssemblyStageCursor,
) -> dict[str, Any]:
    config = dict(assembly_out.get("parameter_recommendation") or {})
    shared = state.get("shared_parameters") or {}
    if isinstance(shared, dict):
        assembly_config = dict(
            (shared.get("assembly") or {}).get("parameter_recommendation") or {}
        )
        config = {**assembly_config, **config}
    return {
        "max_execution_retries": _cursor_int(config.get("max_execution_retries"), 1),
        "max_review_retries": _cursor_int(config.get("max_review_retries"), 2),
    }


def _dependency_value(
    value: JsonValue | None,
) -> str | dict[str, JsonValue] | None:
    if isinstance(value, (str, dict)):
        return value
    return None


def _cursor_int(value: JsonValue | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (bool, int, float, str)):
        return int(value)
    raise TypeError(
        f"expected integer-compatible cursor value, got {type(value).__name__}"
    )


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
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100.0 if value > 1 else value
