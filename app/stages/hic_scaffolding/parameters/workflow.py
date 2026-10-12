"""AutoHiC restriction-enzyme recommendation and mandatory approval gate."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from app.control.lifecycle import ResumeValidationError
from app.control.workflow_plan import fail_workflow_step
from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.execution.evidence import (
    ExecutionEvidence,
    load_execution_evidence,
    read_execution_stderr_tail,
)
from app.execution.method_outcomes import settle_method_outcomes
from app.llm.json_response import model_response_record
from app.llm.parameters.hic.recommender import (
    PROMPT_BUNDLE,
    AutoHiCParameterDecision,
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
from app.llm.transaction import recover_or_invoke_response
from app.review.application import (
    PreparedReviewApplication,
    ReviewIntentV1,
    VerifiedReviewApplication,
)
from app.review.approval import (
    ParameterApprovalEffectV1,
    ensure_decision_allowed,
    load_verified_interrupt_request,
    parameter_approval_decision,
    persist_parameter_approval_decision,
    validate_parameter_approval_bindings,
    validate_parameter_recommendation_manifest,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    PersistedReviewRequest,
    ReviewPersistenceError,
    persist_review_request,
)
from app.schemas.artifacts import (
    HIC_PARAMETER_APPROVAL_DECISION,
    HIC_PARAMETER_ATTEMPT_MANIFEST,
    HIC_PARAMETER_BASIS,
    HIC_PARAMETER_FROZEN_PARAMETERS,
    HIC_PARAMETER_INPUT_SNAPSHOT,
    HIC_PARAMETER_MODEL_REQUEST,
    HIC_PARAMETER_MODEL_RESPONSE,
    HIC_PARAMETER_RECOMMENDATION,
    HIC_PARAMETER_RECOMMENDATION_MANIFEST,
    HIC_PARAMETER_RESPONSE_PARSE,
    ArtifactRef,
    ParameterAttempt,
    ParameterAttemptTask,
    ParameterRecommendationCursor,
)
from app.schemas.parameter_cursors import ParameterAttemptJson
from app.schemas.parameter_recommendation import (
    AutoHiCRecommendationContext,
    AutoHiCReviewEvidence,
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
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir
from app.stages.hic_scaffolding.method_application.support import (
    hic_output as _hic_output,
)
from app.stages.hic_scaffolding.method_application.support import (
    update_hic_output as _update_hic_output,
)
from app.stages.hic_scaffolding.parameters.catalog import (
    load_autohic_parameter_catalog,
)
from app.stages.hic_scaffolding.parameters.context import (
    build_autohic_recommendation_context,
)
from app.stages.hic_scaffolding.parameters.storage import (
    ParameterPersistenceError,
)
from app.stages.hic_scaffolding.parameters.storage import (
    read_artifact as read_autohic_parameter_artifact,
)
from app.stages.hic_scaffolding.parameters.storage import (
    recover_artifact as recover_autohic_parameter_artifact,
)
from app.stages.hic_scaffolding.parameters.storage import (
    write_artifact as write_autohic_parameter_artifact,
)
from app.stages.hic_scaffolding.state import HiCScaffoldingCursor
from app.storage.ledger import Ledger
from app.storage.parameter_evidence import (
    ParameterRecommendationChainV3,
    build_recommendation_dependency_refs,
    canonical_parameter_dependency,
    load_parameter_recommendation_chain_v3,
)

_RECOMMENDER_METHOD = "hic_scaffolding.parameter_recommender"
_APPROVAL_METHOD = "hic_scaffolding.parameter_approval"


def autohic_parameter_policy(
    state: TopLevelState,
) -> Literal["advisor", "deterministic_baseline"]:
    """Read the frozen AutoHiC parameter-advisor mode from the stage seed."""

    seed = (state.get("stage_seeds") or {}).get("hic_scaffolding") or {}
    mode = seed.get("parameter_advisor") if isinstance(seed, Mapping) else None
    return cast(
        Literal["advisor", "deterministic_baseline"],
        mode if mode in {"advisor", "deterministic_baseline"} else "advisor",
    )


def _settle_terminal_autohic(
    state: TopLevelState,
    stage_update: StateUpdate,
) -> StateUpdate:
    """Use the shared settlement boundary for a rejected parameter gate."""

    # The adapter imports this workflow at module load, so defer the policy
    # import until a human decision actually terminates the occurrence.
    from app.stages.hic_scaffolding.method_application.autohic import (
        terminal_autohic_method_outcome,
    )

    projected = cast(TopLevelState, {**state, **stage_update})
    return settle_method_outcomes(
        projected,
        stage="hic_scaffolding",
        outcomes=terminal_autohic_method_outcome(projected),
        expected_method_names=(AUTOHIC_METHOD,),
    )


class _EffectiveRetryPolicy(TypedDict):
    """Fully defaulted retry policy used by retry gates."""

    retry_on_execution_failure: bool
    max_execution_retries: int
    max_review_retries: int


def recommend_autohic_parameters(state: TopLevelState) -> StateUpdate:
    """Persist one attempt-local sparse recommendation and open approval."""

    hic_out = _hic_output(state)
    if hic_out.get("status") not in {
        "resolved",
        "retry_parameters_after_execution_failure",
        "retry_parameters_after_review_failure",
    }:
        return {}

    task = _parameter_prompt_task(hic_out)
    policy = autohic_parameter_policy(state)
    try:
        attempts_before = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter attempt chain is invalid: {exc}",
            failure_kind="parameter_recommendation",
        )
    proposed_attempt_id = hic_out.get("proposed_parameter_attempt_id")
    recommendation_id = (
        proposed_attempt_id if proposed_attempt_id is not None else len(attempts_before)
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
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC retry history is invalid: {exc}",
            failure_kind="parameter_recommendation",
        )
    try:
        retry_context = _build_retry_context(task, attempt_history)
        if task == "recommend_initial":
            catalog = load_autohic_parameter_catalog()
            shared = state.get("shared_parameters")
            if shared is None:
                shared = {}
            if hasattr(shared, "model_dump"):
                shared = shared.model_dump(mode="json", exclude_none=True)
            stage_outputs = state.get("stage_outputs") or {}
            facts = build_canonical_facts(
                shared_parameters=shared,
                read_qc_output=stage_outputs.get("read_qc"),
                genome_survey_result=stage_outputs.get("genome_survey"),
            )
            context = build_autohic_recommendation_context(
                state,
                hic_out=hic_out,
            )
            dependency_refs = _initial_dependency_refs(state, hic_out)
        else:
            if policy == "deterministic_baseline":
                return _fail_update(
                    state,
                    "deterministic AutoHiC parameter baseline supports initial only",
                    failure_kind="parameter_recommendation",
                )
            initial_attempt = ParameterAttempt.model_validate(attempts_before[0])
            initial_chain = load_parameter_recommendation_chain_v3(
                state,
                manifest_ref=initial_attempt.recommendation_manifest_ref,
                expected_stage="hic_scaffolding",
                expected_method_id=AUTOHIC_METHOD,
            )
            if not isinstance(
                initial_chain.input_snapshot.stage_context,
                AutoHiCRecommendationContext,
            ):
                raise ValueError("initial AutoHiC snapshot has the wrong stage context")
            catalog = initial_chain.catalog
            facts = initial_chain.input_snapshot.facts
            context = initial_chain.input_snapshot.stage_context.model_copy(
                update={"retry": retry_context}
            )
            dependency_refs = _retry_dependency_refs(
                state,
                initial_chain=initial_chain,
                attempts_before=attempts_before,
            )
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        ParameterPersistenceError,
    ) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter inputs are not usable: {exc}",
            failure_kind="parameter_recommendation",
        )
    catalog_snapshot = catalog.model_dump(mode="json")

    try:
        snapshot = RecommendationInputSnapshotV3(
            schema_version=3,
            method_id=catalog.method_id,
            task=task,
            facts=facts,
            stage_context=context,
            dependency_refs=dependency_refs,
        )
        model_request = build_autohic_parameter_request(
            facts=facts,
            context=context,
            catalog=catalog,
            task=task,
        ).model_dump(mode="json", exclude_none=True)
        input_snapshot_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "input_snapshot.json",
            snapshot.model_dump(mode="json"),
            artifact_type=HIC_PARAMETER_INPUT_SNAPSHOT,
            method_name=_RECOMMENDER_METHOD,
        )
        basis_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "basis_snapshot.json",
            {
                "schema_version": 3,
                "method_id": AUTOHIC_METHOD,
                "catalog_snapshot": catalog_snapshot,
                "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
            },
            artifact_type=HIC_PARAMETER_BASIS,
            method_name=_RECOMMENDER_METHOD,
        )
        recovered_request = recover_autohic_parameter_artifact(
            state,
            recommendation_id,
            "model_request.json",
            expected_type=HIC_PARAMETER_MODEL_REQUEST,
            method_name=_RECOMMENDER_METHOD,
        )
        if recovered_request is not None:
            model_request_ref, request_record = recovered_request
            messages = request_record["messages"]
            if (
                request_record.get("schema_version") != 3
                or request_record.get("prompt_bundle_id") != PROMPT_BUNDLE
                or bool(request_record.get("policy_skipped_model"))
                != (policy == "deterministic_baseline")
                or not isinstance(messages, list)
                or len(messages) != 2
                or messages[0].get("role") != "system"
                or messages[1].get("role") != "user"
                or json.loads(messages[1]["content"]) != model_request
            ):
                raise ValueError("AutoHiC persisted request binding mismatch")
        else:
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
                    **(
                        {"policy_skipped_model": True}
                        if policy == "deterministic_baseline"
                        else {}
                    ),
                },
                artifact_type=HIC_PARAMETER_MODEL_REQUEST,
                method_name=_RECOMMENDER_METHOD,
            )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter request persistence failed: {exc}",
            failure_kind="parameter_recommendation",
        )

    def invoke_decision() -> AutoHiCParameterDecision:
        if policy == "deterministic_baseline":
            return restore_autohic_parameter_decision(
                {
                    "schema_version": 2,
                    "raw_response": json.dumps(
                        {
                            "parameters": {},
                            "summary": (
                                "Deterministic baseline: parameter advisor "
                                "disabled; empty L2 mapping."
                            ),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "validity": "parsed",
                    "error": None,
                    "model_id": "deterministic_baseline_policy",
                }
            )
        return invoke_autohic_parameter_model(model_request, messages=messages)

    try:
        model_response_ref, decision = recover_or_invoke_response(
            recover=lambda: recover_autohic_parameter_artifact(
                state,
                recommendation_id,
                "model_response.json",
                expected_type=HIC_PARAMETER_MODEL_RESPONSE,
                method_name=_RECOMMENDER_METHOD,
            ),
            invoke=invoke_decision,
            restore=restore_autohic_parameter_decision,
            publish=lambda result: write_autohic_parameter_artifact(
                state,
                recommendation_id,
                "model_response.json",
                model_response_record(
                    schema_version=2,
                    raw_response=result.raw_response,
                    valid=result.valid,
                    error=result.error,
                    model_id=result.model_id,
                    model_call=result.model_call.model_dump(mode="json"),
                ),
                artifact_type=HIC_PARAMETER_MODEL_RESPONSE,
                method_name=_RECOMMENDER_METHOD,
            ),
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter response persistence failed: {exc}",
            failure_kind="parameter_recommendation",
        )
    recommendation = decision.recommendation or {}
    response_parse = {
        "schema_version": 1,
        "parsed": bool(decision.valid),
        "error": decision.error,
    }
    try:
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
        manifest = ParameterRecommendationManifestV3(
            schema_version=3,
            recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
            stage="hic_scaffolding",
            method_id=AUTOHIC_METHOD,
            task=task,
            recommendation_id=recommendation_id,
            status="pending_approval" if decision.valid else "response_invalid",
            previous_attempt_id=(
                previous_attempt.get("attempt_id") if previous_attempt else None
            ),
            previous_attempt_manifest_ref=(
                previous_attempt.get("attempt_manifest_ref")
                if previous_attempt
                else None
            ),
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
            summary={
                "response_parsed": bool(decision.valid),
                **(
                    {
                        "policy": "deterministic_baseline",
                        "policy_skipped_model": True,
                        "policy_decision_source": "deterministic_baseline",
                    }
                    if policy == "deterministic_baseline"
                    else {}
                ),
            },
        )
        manifest_ref = write_autohic_parameter_artifact(
            state,
            recommendation_id,
            "recommendation_manifest.json",
            manifest.model_dump(mode="json"),
            artifact_type=HIC_PARAMETER_RECOMMENDATION_MANIFEST,
            method_name=_RECOMMENDER_METHOD,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        return _fail_update(
            state,
            f"AutoHiC parameter response persistence failed: {exc}",
            failure_kind="parameter_recommendation",
        )

    cursor = ParameterRecommendationCursor(
        schema_version=3,
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
            extra={
                "parameter_recommendation_mode": policy,
                "current_parameter_recommendation": cursor.model_dump(mode="json"),
            },
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
                "auto_decidable": False,
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
    if policy == "deterministic_baseline":
        return _auto_approve_autohic_baseline(
            state=state,
            attempts_before=attempts_before,
            cursor=cursor,
            manifest_ref=manifest_ref,
            recommendation_ref=recommendation_ref,
            persisted_request=persisted_request,
            bindings=bindings,
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
    )
    return _update_hic_output(
        state,
        {
            "status": "blocked_human",
            "parameter_recommendation_mode": policy,
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


def _auto_approve_autohic_baseline(
    *,
    state: TopLevelState,
    attempts_before: list[dict[str, Any]],
    cursor: ParameterRecommendationCursor,
    manifest_ref: ArtifactRef,
    recommendation_ref: ArtifactRef,
    persisted_request: PersistedReviewRequest,
    bindings: dict[str, Any],
) -> StateUpdate:
    """Approve the exact empty-L2 policy recommendation without a human gate."""

    try:
        decision_ref = persist_parameter_approval_decision(
            state,
            stage="hic_scaffolding",
            request_id=persisted_request.review_request_id,
            request_ref=persisted_request.request_ref,
            request=ResumeReviewRequest(
                session_id=state["session_id"],
                stage="hic_scaffolding",
                gate_id="parameter_approval",
                review_request_id=persisted_request.review_request_id,
                review_request_hash=persisted_request.request_hash,
                decision="approve_parameters",
                decided_by="auto_rule",
                notes=["approved by deterministic_baseline policy"],
                metadata={
                    **bindings,
                    "source": "policy",
                    "configured_seconds": 0,
                    "elapsed_seconds": 0,
                    "automatic_resolution": {
                        "source": "policy",
                        "configured_seconds": 0,
                        "elapsed_seconds": 0.0,
                        "outcome": "approve_persisted_recommendation",
                    },
                },
                rationale_artifact_ref=recommendation_ref,
            ),
            manifest_ref=manifest_ref,
            method_name=_APPROVAL_METHOD,
            artifact_type=HIC_PARAMETER_APPROVAL_DECISION,
        )
        attempt = ParameterAttempt(
            schema_version=3,
            attempt_id=cursor.recommendation_id,
            method_id=AUTOHIC_METHOD,
            task=cursor.task,
            status="recommended",
            previous_attempt_id=cursor.previous_attempt_id,
            recommendation_manifest_ref=manifest_ref,
            approval_decision_ref=decision_ref,
            recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
            previous_attempt_seal_hash=cursor.previous_attempt_seal_hash,
        )
    except (OSError, ReviewPersistenceError, ValueError) as exc:
        return _fail_update(
            state,
            f"AutoHiC deterministic parameter baseline failed: {exc}",
            failure_kind="parameter_recommendation",
        )
    attempts = [*attempts_before, attempt.model_dump(mode="json")]
    return _update_hic_output(
        state,
        {
            "status": "parameters_approved",
            "parameter_recommendation_mode": "deterministic_baseline",
            "current_parameter_attempt_id": cursor.recommendation_id,
            "parameter_attempts": attempts,
            "current_parameter_recommendation": cursor.model_copy(
                update={"status": "approved"}
            ).model_dump(mode="json"),
            "proposed_parameter_attempt_id": None,
            "parameter_retry_request": None,
        },
    )


@dataclass(frozen=True)
class _ValidatedAutoHiCParameterApproval:
    request: ResumeReviewRequest
    decision: Literal["approve_parameters", "reject_parameters"]
    request_id: str
    request_ref: ArtifactRef
    cursor: ParameterRecommendationCursor
    manifest_ref: ArtifactRef
    manifest: ParameterRecommendationManifestV3


def _validate_autohic_parameter_approval(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: dict[str, Any],
) -> _ValidatedAutoHiCParameterApproval:
    """Validate one exact AutoHiC parameter-approval occurrence without writes."""

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

    validate_parameter_approval_bindings(interrupt, request)
    decision = parameter_approval_decision(request)
    request_ref, verified_record = load_verified_interrupt_request(state, interrupt)
    if request_record != verified_record:
        raise ResumeValidationError(
            "AutoHiC parameter approval request record is stale"
        )
    ensure_decision_allowed(verified_record, request)

    payload = verified_record.get("payload") or {}
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
        or cursor.review_request_id != interrupt.get("review_request_id")
        or cursor.manifest_ref != manifest_ref
        or cursor.recommendation_hash != interrupt.get("recommendation_hash")
        or manifest_ref.sha256 != payload.get("recommendation_manifest_hash")
    ):
        raise ResumeValidationError("AutoHiC parameter approval cursor is stale")

    try:
        chain = load_parameter_recommendation_chain_v3(
            state,
            manifest_ref=manifest_ref,
            expected_stage="hic_scaffolding",
            expected_method_id=AUTOHIC_METHOD,
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC parameter recommendation evidence is missing or changed"
        ) from exc
    validate_parameter_recommendation_manifest(cursor, chain.manifest)
    recommendation = dict(chain.recommendation.get("recommendation") or {})
    if not isinstance(recommendation.get("parameters"), dict):
        raise ResumeValidationError("AutoHiC recommendation has no parameters mapping")
    try:
        attempts = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
    except (OSError, ValueError, ParameterPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC parameter attempt chain is invalid"
        ) from exc
    if cursor.recommendation_id != len(attempts):
        raise ResumeValidationError("AutoHiC parameter approval attempt is not next")
    if cursor.recommendation_id:
        predecessor = attempts[-1]
        if (
            cursor.previous_attempt_id != predecessor.get("attempt_id")
            or cursor.previous_attempt_seal_hash != predecessor.get("seal_hash")
            or chain.manifest.previous_attempt_manifest_ref
            != ArtifactRef.model_validate(predecessor.get("attempt_manifest_ref"))
        ):
            raise ResumeValidationError("AutoHiC parameter retry predecessor changed")
    request_id = interrupt.get("review_request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ResumeValidationError("AutoHiC parameter approval request ID is missing")

    return _ValidatedAutoHiCParameterApproval(
        request=request,
        decision=decision,
        request_id=request_id,
        request_ref=request_ref,
        cursor=cursor,
        manifest_ref=manifest_ref,
        manifest=chain.manifest,
    )


def validate_autohic_parameter_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: dict[str, Any],
) -> ResumeReviewRequest:
    """Registry hook validating an AutoHiC parameter approval before intent write."""

    return _validate_autohic_parameter_approval(state, request, request_record).request


def prepare_autohic_parameter_approval_application(
    state: TopLevelState,
    intent: ReviewIntentV1,
) -> PreparedReviewApplication:
    """Recheck and durably prepare one AutoHiC parameter decision."""

    interrupt = state.get("active_interrupt") or _hic_output(state).get(
        "active_interrupt"
    )
    if not isinstance(interrupt, dict):
        raise ResumeValidationError("no unresolved AutoHiC parameter_approval gate")
    _request_ref, request_record = load_verified_interrupt_request(state, interrupt)
    validated = _validate_autohic_parameter_approval(
        state, intent.request, request_record
    )
    decision_ref = persist_parameter_approval_decision(
        state,
        stage="hic_scaffolding",
        request_id=validated.request_id,
        request_ref=validated.request_ref,
        request=validated.request,
        manifest_ref=validated.manifest_ref,
        method_name=_APPROVAL_METHOD,
        artifact_type=HIC_PARAMETER_APPROVAL_DECISION,
    )
    approved_attempt = None
    try:
        if validated.decision == "approve_parameters":
            approved_attempt = ParameterAttempt(
                schema_version=3,
                attempt_id=validated.cursor.recommendation_id,
                method_id=AUTOHIC_METHOD,
                status="recommended",
                task=validated.cursor.task,
                previous_attempt_id=validated.cursor.previous_attempt_id,
                recommendation_manifest_ref=validated.manifest_ref,
                approval_decision_ref=decision_ref,
                recommendation_contract=PARAMETER_RECOMMENDATION_CONTRACT,
                previous_attempt_seal_hash=validated.cursor.previous_attempt_seal_hash,
            )
    except ValueError as exc:
        raise ResumeValidationError(
            "approved AutoHiC parameter attempt could not be recorded"
        ) from exc
    return PreparedReviewApplication(
        effect_kind="hic_scaffolding.parameter_approval.v1",
        effect=ParameterApprovalEffectV1(
            decision=validated.decision,
            decision_ref=decision_ref,
            manifest_ref=validated.manifest_ref,
            cursor=validated.cursor,
            approved_attempt=approved_attempt,
            rejection_reason="autohic_parameter_recommendation_rejected_by_human",
        ),
        decision_ref=decision_ref,
        required_refs=(validated.manifest_ref,),
    )


def apply_autohic_parameter_approval_application(
    state: TopLevelState,
    application: VerifiedReviewApplication,
) -> StateUpdate:
    """Project a verified AutoHiC approval effect without durable side effects."""

    effect = application.effect
    if not isinstance(effect, ParameterApprovalEffectV1):
        raise TypeError("AutoHiC parameter approval has an invalid effect model")
    hic_out = _hic_output(state)
    cursor = effect.cursor
    if effect.decision == "reject_parameters":
        stage_update = _update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "parameter_recommendation_rejected",
                "reason": effect.rejection_reason,
                "active_interrupt": None,
                "current_parameter_recommendation": cursor.model_copy(
                    update={"status": "rejected"}
                ).model_dump(mode="json"),
            },
        )
        return {
            **_settle_terminal_autohic(state, stage_update),
            **fail_workflow_step(
                state, "hic_scaffolding", str(effect.rejection_reason)
            ),
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": effect.rejection_reason,
            **stage_update,
        }
    if effect.approved_attempt is None:
        raise TypeError("approved AutoHiC parameter effect has no attempt")
    attempts = list(hic_out.get("parameter_attempts") or [])
    attempts.append(
        cast(ParameterAttemptJson, effect.approved_attempt.model_dump(mode="json"))
    )
    retry_counts = dict(hic_out.get("retry_counts") or {})
    if cursor.task == "retry_after_execution_failure":
        retry_counts["execution"] = int(retry_counts.get("execution", 0)) + 1
    elif cursor.task == "retry_after_review_failure":
        retry_counts["review"] = int(retry_counts.get("review", 0)) + 1
    stage_update = _update_hic_output(
        state,
        {
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
        },
    )
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        **stage_update,
    }


def _fail_update(
    state: TopLevelState,
    reason: str,
    *,
    failure_kind: str,
    extra: Mapping[str, object] | None = None,
) -> StateUpdate:
    return _update_hic_output(
        state,
        {
            "status": "failed",
            "failure_kind": failure_kind,
            "reason": reason,
            **(extra or {}),
        },
    )


def autohic_execution_retry_gate(state: TopLevelState) -> StateUpdate:
    """Seal a failed execution and apply the operator-owned retry policy."""

    hic_out = _hic_output(state)
    if hic_out.get("status") != "failed" or hic_out.get("failure_kind") != "execution":
        return {}
    try:
        attempts = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
        current_id = hic_out.get("current_parameter_attempt_id")
        if current_id is None:
            raise ValueError("AutoHiC execution retry has no current attempt")
        current = ParameterAttempt.model_validate(attempts[current_id])
        if current.execution_ref is None:
            return {}
        sealed = _seal_parameter_attempt(state, current.model_dump(mode="json"))
    except (
        IndexError,
        KeyError,
        OSError,
        ValueError,
        ParameterPersistenceError,
    ) as exc:
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
) -> StateUpdate:
    """Seal reviewer feedback and create a ref-only retry cursor."""

    hic_out = _hic_output(state)
    try:
        attempts = _validate_parameter_attempt_chain(
            state, list(hic_out.get("parameter_attempts") or [])
        )
        current_id = hic_out.get("current_parameter_attempt_id")
        if current_id is None:
            raise ValueError("AutoHiC review retry has no current attempt")
        current = ParameterAttempt.model_validate(attempts[current_id])
        if current.status != "executed":
            raise ValueError("AutoHiC review retry requires an executed attempt")
        reviewed = current.model_copy(
            update={"status": "review_failed", "review_ref": decision_ref}
        ).model_dump(mode="json")
        sealed = _seal_parameter_attempt(state, reviewed)
    except (
        IndexError,
        KeyError,
        OSError,
        ValueError,
        ParameterPersistenceError,
    ) as exc:
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


def _parameter_prompt_task(hic_out: HiCScaffoldingCursor) -> ParameterAttemptTask:
    status = hic_out.get("status")
    if status == "retry_parameters_after_execution_failure":
        return "retry_after_execution_failure"
    if status == "retry_parameters_after_review_failure":
        return "retry_after_review_failure"
    return "recommend_initial"


def _parameter_retry_policy(
    hic_out: HiCScaffoldingCursor,
) -> _EffectiveRetryPolicy:
    policy = hic_out.get("parameter_retry_policy") or {}
    return {
        "retry_on_execution_failure": bool(
            policy.get("retry_on_execution_failure", False)
        ),
        "max_execution_retries": max(0, int(policy.get("max_execution_retries", 0))),
        "max_review_retries": max(0, int(policy.get("max_review_retries", 1))),
    }


def _build_retry_context(
    task: ParameterAttemptTask,
    attempt_history: list[ParameterAttemptEvidenceV3],
) -> RecommendationRetryContext | None:
    if task == "recommend_initial":
        if attempt_history:
            raise ValueError(
                "initial AutoHiC recommendation cannot contain retry history"
            )
        return None
    if not attempt_history:
        raise ValueError("AutoHiC retry requires predecessor attempt evidence")

    latest = attempt_history[-1]
    if task == "retry_after_execution_failure":
        execution = latest.execution
        if execution is None:
            raise ValueError("AutoHiC execution retry has no execution evidence")
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
        if not isinstance(review, AutoHiCReviewEvidence):
            raise ValueError("AutoHiC review retry has no AutoHiC review evidence")
        trigger = ReviewFailureTrigger(review=review)
    else:
        raise ValueError(f"unsupported AutoHiC parameter task: {task!r}")
    return RecommendationRetryContext(
        trigger=trigger,
        attempt_history=tuple(attempt_history),
    )


def _initial_dependency_refs(
    state: TopLevelState,
    hic_out: HiCScaffoldingCursor,
) -> RecommendationDependencyRefs:
    stage_outputs = state.get("stage_outputs") or {}
    read_qc = stage_outputs.get("read_qc") or {}
    genome_survey = stage_outputs.get("genome_survey") or {}
    read_qc_summary = read_qc.get("summary_artifact_ref")
    if not isinstance(read_qc_summary, (str, dict)):
        read_qc_summary = None
    genome_survey_result = genome_survey.get("result_artifact_ref")
    if not isinstance(genome_survey_result, (str, dict)):
        genome_survey_result = None
    fact_sources: dict[
        Literal["read_qc_summary", "genome_survey_result"],
        ArtifactRef | Mapping[str, Any] | str | Path | None,
    ] = {
        "read_qc_summary": canonical_parameter_dependency(state, read_qc_summary),
        "genome_survey_result": canonical_parameter_dependency(
            state,
            genome_survey_result,
        ),
    }
    registry_ref = canonical_parameter_dependency(
        state, state.get("asset_registry_ref")
    )
    stage_inputs = {
        "pre_hic_assembly": canonical_parameter_dependency(
            state, hic_out.get("pre_hic_assembly_ref")
        ),
    }
    if registry_ref is not None:
        stage_inputs["asset_registry"] = registry_ref
    else:
        stage_inputs.update(
            {
                f"hic_read_{index:06d}": canonical_parameter_dependency(state, path)
                for index, path in enumerate(hic_out.get("hic_read_paths") or [])
            }
        )
    return build_recommendation_dependency_refs(
        state,
        fact_source_paths=fact_sources,
        stage_input_paths={
            role: value for role, value in stage_inputs.items() if value is not None
        },
    )


def _retry_dependency_refs(
    state: TopLevelState,
    *,
    initial_chain: ParameterRecommendationChainV3,
    attempts_before: list[dict[str, Any]],
) -> RecommendationDependencyRefs:
    attempt_manifest_refs: list[ArtifactRef] = []
    for raw_attempt in attempts_before:
        attempt = ParameterAttempt.model_validate(raw_attempt)
        if attempt.seal_hash is None or attempt.attempt_manifest_ref is None:
            raise ValueError("AutoHiC retry history contains an unsealed attempt")
        attempt_manifest_refs.append(attempt.attempt_manifest_ref)

    initial_refs = initial_chain.input_snapshot.dependency_refs
    return build_recommendation_dependency_refs(
        state,
        fact_source_paths=initial_refs.fact_source_refs,
        stage_input_paths={
            f"stage_input_{index:06d}": ref
            for index, ref in enumerate(initial_refs.stage_input_refs)
        },
        initial_input_snapshot_ref=initial_chain.manifest.refs.input_snapshot_ref,
        prior_attempt_manifest_refs=attempt_manifest_refs,
    )


def _hydrate_parameter_attempt(
    state: TopLevelState,
    attempt_value: dict[str, Any],
    *,
    include_execution_log: bool = False,
) -> ParameterAttemptEvidenceV3:
    attempt = ParameterAttempt.model_validate(attempt_value)
    chain = load_parameter_recommendation_chain_v3(
        state,
        manifest_ref=attempt.recommendation_manifest_ref,
        expected_stage="hic_scaffolding",
        expected_method_id=AUTOHIC_METHOD,
    )
    manifest = chain.manifest
    if (
        manifest.recommendation_id != attempt.attempt_id
        or manifest.task != attempt.task
        or manifest.previous_attempt_id != attempt.previous_attempt_id
        or manifest.previous_attempt_seal_hash != attempt.previous_attempt_seal_hash
    ):
        raise ValueError("AutoHiC attempt does not match its recommendation manifest")

    recommendation = chain.recommendation.get("recommendation")
    if not isinstance(recommendation, dict) or not isinstance(
        recommendation.get("parameters"), dict
    ):
        raise ValueError("AutoHiC recommendation has no parameters mapping")
    recommended_parameters = dict(recommendation["parameters"])

    approval_record = read_autohic_parameter_artifact(
        state,
        attempt.approval_decision_ref,
        expected_type=HIC_PARAMETER_APPROVAL_DECISION,
    )
    approval = approval_record.get("decision")
    if (
        approval_record.get("schema_version") != 2
        or approval_record.get("stage") != "hic_scaffolding"
        or approval_record.get("gate_id") != "parameter_approval"
        or not isinstance(approval, dict)
        or approval.get("schema_version") != 2
        or approval.get("decision") != "approve_parameters"
        or approval.get("validated_recommendation_hash") != chain.manifest_ref.sha256
        or ArtifactRef.model_validate(
            approval.get("validated_recommendation_manifest_ref")
        )
        != chain.manifest_ref
    ):
        raise ValueError("AutoHiC approval is not bound to the recommendation")

    if attempt.frozen_parameters_ref is None:
        raise ValueError("sealed AutoHiC retry history has no frozen parameters")
    frozen = read_autohic_parameter_artifact(
        state,
        attempt.frozen_parameters_ref,
        expected_type=HIC_PARAMETER_FROZEN_PARAMETERS,
    )
    if (
        frozen.get("schema_version") != 3
        or frozen.get("stage") != "hic_scaffolding"
        or frozen.get("method_id") != AUTOHIC_METHOD
        or ArtifactRef.model_validate(frozen.get("recommendation_manifest_ref"))
        != attempt.recommendation_manifest_ref
        or ArtifactRef.model_validate(frozen.get("approval_decision_ref"))
        != attempt.approval_decision_ref
    ):
        raise ValueError(
            "AutoHiC frozen parameter evidence is not bound to the attempt"
        )
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
    if resolved.l2_approved != recommended_parameters:
        raise ValueError(
            "AutoHiC frozen approved parameters do not match the recommendation"
        )

    execution: ParameterExecutionEvidence | None = None
    if attempt.execution_ref is not None:
        if attempt.frozen_attempt_ref is None:
            raise ValueError("AutoHiC execution evidence has no frozen-attempt binding")
        execution_evidence = load_execution_evidence(
            state,
            attempt.execution_ref,
            expected_stage="hic_scaffolding",
            expected_method_name=AUTOHIC_METHOD,
            expected_frozen_attempt_ref=attempt.frozen_attempt_ref,
        )
        stderr_tail: str | None = None
        if include_execution_log and not execution_evidence.success:
            stderr_tail = read_execution_stderr_tail(state, execution_evidence)
            if stderr_tail:
                stderr_tail = stderr_tail[-4000:]
        execution = ParameterExecutionEvidence(
            status=execution_evidence.status,
            return_code=execution_evidence.return_code,
            failure_class=(
                execution_evidence.status if not execution_evidence.success else None
            ),
            status_detail=_execution_status_detail(execution_evidence),
            resource_metrics=[],
            stderr_tail=stderr_tail,
        )

    review: AutoHiCReviewEvidence | None = None
    if attempt.review_ref is not None:
        review_record = read_autohic_parameter_artifact(state, attempt.review_ref)
        review_decision = review_record.get("decision")
        if not isinstance(review_decision, dict):
            raise ValueError("AutoHiC review evidence has no decision mapping")
        review = AutoHiCReviewEvidence(
            decision=review_decision.get("decision"),
            notes=list(review_decision.get("notes") or []),
        )

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


def _execution_status_detail(execution_evidence: ExecutionEvidence) -> str | None:
    """Return the bounded runner detail after an explicit optional narrowing."""

    detail = execution_evidence.status_detail or execution_evidence.error
    return detail[:500] if detail else None


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
        expected_manifest = {
            "schema_version": 3,
            "stage": "hic_scaffolding",
            **semantic,
            "sealed_at": value.get("sealed_at"),
            "seal_hash": digest,
        }
        if manifest != expected_manifest:
            raise ValueError("AutoHiC parameter attempt manifest seal mismatch")
        return value
    sealed_at = datetime.now(timezone.utc).isoformat()
    manifest_ref = write_autohic_parameter_artifact(
        state,
        int(value["attempt_id"]),
        "attempt_manifest.json",
        {
            "schema_version": 3,
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
