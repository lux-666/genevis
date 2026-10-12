"""Nodes for the selected, AutoHiC-only Hi-C scaffolding stage."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import JsonValue as PydanticJsonValue

from app.configuration import load_descriptor_registry
from app.control.errors import ResumeValidationError
from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    current_workflow_step_id,
    fail_workflow_step,
)
from app.execution.method_outcomes import settle_method_outcomes
from app.integrity import SHA256_PATTERN
from app.review.application import (
    PreparedReviewApplication,
    ReviewIntentV1,
    RunRelativeFileRef,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactRef,
    HiCScaffoldDecisionArtifact,
    ParameterAttempt,
)
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, append_route_trace, session_paths
from app.stages.hic_scaffolding.method_application.support import (
    hic_output as _hic_output,
)
from app.stages.hic_scaffolding.method_application.support import (
    update_hic_output as _update_hic_output,
)
from app.stages.hic_scaffolding.parameters.workflow import (
    prepare_autohic_review_retry,
)
from app.stages.hic_scaffolding.state import HIC_SCAFFOLDING_CURSOR_ADAPTER
from app.stages.read_resolution import resolve_hic_pairs
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import assembly_resource_spec
from app.storage.artifacts import artifact_ref_for_path
from app.storage.assembly_candidates import (
    AssemblyCandidateBundleError,
    load_executed_candidate_bundle,
    publish_derived_assembly_candidate,
)
from app.storage.ledger import Ledger

AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
HIC_CONTRACT_METHOD = "hic_scaffolding.contract"


def _settle_terminal_autohic(
    state: TopLevelState,
    stage_update: StateUpdate | None = None,
) -> StateUpdate:
    """Settle AutoHiC before a direct terminal application bypasses the graph.

    Review application is outside the stage graph, so it cannot rely on its
    graph settlement nodes.  Apply the prospective cursor only for deriving
    the typed outcome; the shared execution boundary remains the only ledger
    disposition writer.
    """

    from app.stages.hic_scaffolding.method_application.autohic import (
        terminal_autohic_method_outcome,
    )

    projected = cast(TopLevelState, {**state, **(stage_update or {})})
    return settle_method_outcomes(
        projected,
        stage="hic_scaffolding",
        outcomes=terminal_autohic_method_outcome(projected),
        expected_method_names=(AUTOHIC_METHOD,),
    )


class HiCAssemblyResourceEffectV1(BaseModel):
    """One prepared assembly-resource replacement for a Hi-C review outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: RunRelativeFileRef
    metadata: dict[str, PydanticJsonValue] = Field(default_factory=dict)


class HiCReviewRetryRequestEffectV1(BaseModel):
    """Ref-only cursor for a sealed AutoHiC review retry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: Literal["autohic_review"]
    previous_attempt_id: int = Field(ge=0)
    previous_attempt_seal_hash: str = Field(pattern=SHA256_PATTERN)
    failure_ref: ArtifactRef


class HiCReviewRetryEffectV1(BaseModel):
    """Prepared domain projection after sealing reviewer feedback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "retry_parameters_after_review_failure",
        "review_parameter_retries_exhausted",
        "failed",
    ]
    reason: str | None = None
    failure_kind: str | None = None
    parameter_attempts: tuple[ParameterAttempt, ...] = Field(max_length=256)
    proposed_parameter_attempt_id: int | None = Field(default=None, ge=0)
    parameter_retry_request: HiCReviewRetryRequestEffectV1 | None = None

    @model_validator(mode="after")
    def validate_retry_projection(self) -> "HiCReviewRetryEffectV1":
        if self.status == "retry_parameters_after_review_failure":
            if self.parameter_retry_request is None:
                raise ValueError("AutoHiC review retry requires a retry cursor")
        elif self.parameter_retry_request is not None:
            raise ValueError(
                "terminal AutoHiC retry effect must not retain a retry cursor"
            )
        return self


class HiCScaffoldingReviewEffectV1(BaseModel):
    """Typed durable effect for the ``hic_scaffolding`` adoption gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision: Literal[
        "accept_candidate",
        "keep_prior_assembly",
        "request_parameter_retry",
        "stop_human_review",
    ]
    decision_ref: ArtifactRef
    pre_hic_assembly_ref: RunRelativeFileRef
    output_assembly_ref: RunRelativeFileRef
    hic_read_refs: tuple[str, ...] = Field(default=(), max_length=10_000)
    notes: tuple[str, ...] = Field(default=(), max_length=1_000)
    decided_by: Literal["human", "llm_provisional", "auto_rule"]
    rationale_artifact_ref: ArtifactRef | None = None
    assembly_resource: HiCAssemblyResourceEffectV1 | None = None
    retry: HiCReviewRetryEffectV1 | None = None

    @model_validator(mode="after")
    def validate_effect_branches(self) -> "HiCScaffoldingReviewEffectV1":
        if self.decision == "request_parameter_retry":
            if self.retry is None or self.assembly_resource is not None:
                raise ValueError("AutoHiC retry effect has invalid branch data")
        elif self.decision == "stop_human_review":
            if self.retry is not None or self.assembly_resource is not None:
                raise ValueError("AutoHiC stop effect has invalid branch data")
        elif self.retry is not None or self.assembly_resource is None:
            raise ValueError("AutoHiC adoption effect requires an assembly resource")
        return self


def enter_hic_scaffolding(state: TopLevelState) -> StateUpdate:
    """Mark the selected Hi-C stage as running."""

    return {"supervision_state": "running_stage"}


def resolve_hic_inputs(state: TopLevelState) -> StateUpdate:
    """Resolve and validate everything required before recommending parameters."""

    assembly = resolve_stage_resource(
        state,
        resource_spec=assembly_resource_spec(),
    )
    final_assembly_ref, source = assembly.path, assembly.source
    try:
        hic_assets = _hic_assets(state)
    except ValueError as exc:
        return _fail_update(
            state,
            f"Hi-C provenance is invalid: {exc}",
            failure_kind="invalid_input",
        )
    hic_read_refs = [
        str(asset.get("asset_id")) for asset in hic_assets if asset.get("asset_id")
    ]
    hic_read_paths = [
        str(asset.get("path")) for asset in hic_assets if asset.get("path")
    ]
    existing = _hic_output(state)
    resolved = {
        **existing,
        "pre_hic_assembly_ref": final_assembly_ref,
        "output_assembly_ref": final_assembly_ref,
        "hic_read_refs": hic_read_refs,
        "hic_read_paths": hic_read_paths,
        "hic_library_metadata": [
            dict(asset.get("metadata") or {})
            for asset in hic_assets
            if asset.get("metadata")
        ],
        "assembly_source": source,
        "method_name": AUTOHIC_METHOD,
    }

    missing: list[str] = []
    if not final_assembly_ref:
        missing.append("pre_hic_assembly_ref")
    elif not Path(str(final_assembly_ref)).is_file():
        missing.append("materialized pre_hic_assembly_ref")
    if len(hic_read_paths) < 2 or len(hic_read_paths) % 2:
        missing.append("paired hic_read_paths (R1/R2)")
    else:
        missing.extend(
            f"materialized hic_read_path: {path}"
            for path in hic_read_paths
            if not Path(path).is_file()
        )
    if existing.get("execution_mode") != "local":
        missing.append("hic_scaffolding local execution")
    try:
        descriptor_present = AUTOHIC_METHOD in load_descriptor_registry().methods
    except (OSError, ValueError):
        descriptor_present = False
    if not descriptor_present:
        missing.append(f"descriptor: {AUTOHIC_METHOD}")

    if missing:
        return _update_hic_output(
            state,
            {
                **resolved,
                "status": "blocked_missing_requirements",
                "missing_requirements": list(dict.fromkeys(missing)),
                "reason": "AutoHiC requirements are incomplete: "
                + ", ".join(dict.fromkeys(missing)),
            },
        )
    return _update_hic_output(
        state,
        {
            **resolved,
            "status": "resolved",
            "missing_requirements": [],
        },
    )


def prepare_autohic_adoption_review(state: TopLevelState) -> StateUpdate:
    """Open the separate final gate that adopts or rejects the AutoHiC result."""

    hic_out = _hic_output(state)
    if hic_out.get("status") != "autohic_review_ready":
        return {}
    candidate = dict((hic_out.get("method_candidates") or {}).get(AUTOHIC_METHOD) or {})
    if candidate.get("status") != "executed" or not candidate.get("scaffold_fasta"):
        return _fail_update(
            state,
            "AutoHiC completed without a reviewable chromosome assembly",
            failure_kind="execution",
        )

    payload = ReviewPayload(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason="autohic_adoption_review_required",
        candidates=[
            {
                "method_name": AUTOHIC_METHOD,
                "scaffold_fasta": candidate.get("scaffold_fasta"),
                "contact_map": candidate.get("contact_map"),
                "assembly_file": candidate.get("assembly_file"),
                "html_report": candidate.get("html_report"),
            }
        ],
        decisions_allowed=[
            "accept_candidate",
            "keep_prior_assembly",
            "request_parameter_retry",
            "stop_human_review",
        ],
        notes=list(hic_out.get("notes") or []),
        auto_decidable=False,
        report_refs={
            key: str(candidate[key])
            for key in ("contact_map", "assembly_file", "html_report")
            if candidate.get(key)
        },
    )
    persisted = persist_review_request(
        state,
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason="autohic_adoption_review_required",
        payload=payload,
        method_name=HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.adoption_review.request",
    )
    interrupt = persisted.compact_interrupt(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason="autohic_adoption_review_required",
    )
    Ledger(absolute_data_dir(state)).record_review_requested(
        state["session_id"],
        "hic_scaffolding",
        reason="autohic_adoption_review_required",
        candidates=payload.candidates,
        review_request_artifact_ref=persisted.request_ref.path_ref,
        review_request_id=persisted.review_request_id,
        auto_decidable=False,
    )
    return _update_hic_output(
        state,
        {
            "status": "blocked_human",
            "block_reason": "autohic_adoption_review_required",
            "active_interrupt": interrupt,
            "review_request_artifact_ref": persisted.request_ref.model_dump(
                mode="json"
            ),
        },
    )


def block_hic_scaffolding(state: TopLevelState) -> StateUpdate:
    """Surface an already-persisted parameter or adoption review interrupt."""

    hic_out = _hic_output(state)
    interrupt = dict(hic_out.get("active_interrupt") or {})
    if not interrupt:
        reason = "Hi-C review block has no persisted interrupt"
        update = _fail_update(
            state,
            reason,
            failure_kind="review",
        )
        return {
            **_settle_terminal_autohic(state, update),
            **fail_workflow_step(state, "hic_scaffolding", reason),
            **update,
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": reason,
        }
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "blocked_human",
        "active_interrupt": cast(ActiveInterrupt, interrupt),
    }


def block_hic_requirements(state: TopLevelState) -> StateUpdate:
    """Stop the selected stage with a distinct missing-requirements outcome."""

    hic_out = _hic_output(state)
    reason = str(hic_out.get("reason", "AutoHiC requirements are incomplete"))
    return {
        **fail_workflow_step(state, "hic_scaffolding", reason),
        "active_interrupt": None,
    }


def prepare_hic_scaffolding_review_application(
    state: TopLevelState,
    intent: ReviewIntentV1,
) -> PreparedReviewApplication:
    """Durably prepare one AutoHiC adoption decision before checkpoint apply."""

    _validate_hic_scaffolding_review_intent(state, intent)
    (
        review_request_id,
        review_request_hash,
        review_request_ref,
        request_record,
    ) = _hic_scaffolding_review_binding(state, intent)
    request = validate_hic_scaffolding_review_intent_request(
        state, intent.request, request_record
    )
    hic_out = _hic_output(state)
    candidate = dict((hic_out.get("method_candidates") or {}).get(AUTOHIC_METHOD) or {})
    pre_hic_assembly_path = hic_out.get("pre_hic_assembly_ref")
    if request.decision == "accept_candidate":
        final_assembly_path = str(candidate["scaffold_fasta"])
    else:
        final_assembly_path = str(pre_hic_assembly_path)
    try:
        paths = session_paths(state)
        pre_hic_assembly_ref = RunRelativeFileRef(
            paths.to_ref(str(pre_hic_assembly_path))
        )
        output_assembly_ref = RunRelativeFileRef(paths.to_ref(final_assembly_path))
    except ValueError as exc:
        raise ResumeValidationError(
            "AutoHiC review effect paths must be inside the run directory"
        ) from exc

    adoption_resource_metadata: dict[str, Any] | None = None
    if request.decision == "accept_candidate":
        try:
            current_assembly = resolve_stage_resource(
                state,
                resource_spec=assembly_resource_spec(),
            )
            primary_output_ref = artifact_ref_for_path(
                absolute_data_dir(state),
                state["session_id"],
                final_assembly_path,
                expected_type=HIC_SCAFFOLDED_ASSEMBLY,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ResumeValidationError(
                f"AutoHiC adopted assembly provenance is invalid: {exc}"
            ) from exc
        adoption_resource_metadata = {
            "decision": request.decision,
            "pre_hic_assembly_ref": pre_hic_assembly_ref.path_ref,
            "primary_output_ref": primary_output_ref.model_dump(mode="json"),
        }
        if current_assembly.metadata.get("candidate_bundle_ref") is not None:
            adoption_resource_metadata["input_candidate_bundle_ref"] = (
                current_assembly.metadata["candidate_bundle_ref"]
            )
        if current_assembly.metadata.get("primary_output_ref") is not None:
            adoption_resource_metadata["input_primary_output_ref"] = (
                current_assembly.metadata["primary_output_ref"]
            )

    decision_metadata = {
        **dict(request.metadata),
        **(
            {
                "rationale_artifact_ref": request.rationale_artifact_ref.model_dump(
                    mode="json"
                )
            }
            if request.rationale_artifact_ref is not None
            else {}
        ),
        "review_evidence": {
            key: candidate.get(key)
            for key in (
                "scaffold_fasta",
                "contact_map",
                "assembly_file",
                "html_report",
                "review_bundle_ref",
                "qc_report_ref",
            )
            if candidate.get(key)
        },
    }
    decision = cast(
        Literal[
            "accept_candidate",
            "keep_prior_assembly",
            "request_parameter_retry",
            "stop_human_review",
        ],
        request.decision,
    )
    artifact_payload = HiCScaffoldDecisionArtifact(
        session_id=state["session_id"],
        decision=decision,
        method_name=AUTOHIC_METHOD,
        pre_hic_assembly_ref=pre_hic_assembly_ref.path_ref,
        output_assembly_ref=output_assembly_ref.path_ref,
        hic_read_refs=list(hic_out.get("hic_read_refs") or []),
        notes=list(request.notes),
        metadata=decision_metadata,
    )
    try:
        decision_ref = persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=str(review_request_id),
            stage="hic_scaffolding",
            gate_id="hic_scaffolding",
            decision=artifact_payload,
            request_hash=str(review_request_hash),
            request_ref=review_request_ref,
            method_name=AUTOHIC_METHOD,
            artifact_type="hic_scaffolding.adoption_review.decision",
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC adoption decision could not be persisted"
        ) from exc
    if request.decision == "accept_candidate":
        if adoption_resource_metadata is None:
            raise ResumeValidationError("AutoHiC adoption metadata is unavailable")
        try:
            current_assembly = resolve_stage_resource(
                state,
                resource_spec=assembly_resource_spec(),
            )
            source_candidate_ref = current_assembly.metadata.get("candidate_bundle_ref")
            if source_candidate_ref is None:
                raise AssemblyCandidateBundleError(
                    "accepted AutoHiC scaffolding requires the current "
                    "candidate_bundle_ref"
                )
            source_candidate = load_executed_candidate_bundle(
                absolute_data_dir(state), state["session_id"], source_candidate_ref
            )
            if (
                current_assembly.path is None
                or source_candidate.primary_output_path.resolve(strict=False)
                != Path(current_assembly.path).resolve(strict=False)
            ):
                raise AssemblyCandidateBundleError(
                    "current candidate primary output does not match the "
                    "pre-Hi-C assembly"
                )
            primary_output_ref = ArtifactRef.model_validate(
                adoption_resource_metadata["primary_output_ref"]
            )
            derived_candidate = publish_derived_assembly_candidate(
                absolute_data_dir(state),
                state["session_id"],
                workflow_step_id=current_workflow_step_id(
                    state, expected_stage="hic_scaffolding"
                ),
                source_candidate=source_candidate,
                primary_output_ref=primary_output_ref,
                stage="hic_scaffolding",
                method_name=HIC_CONTRACT_METHOD,
                action_identity=decision_ref.artifact_id,
            )
        except (
            AssemblyCandidateBundleError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise ResumeValidationError(
                f"AutoHiC adopted candidate publication failed: {exc}"
            ) from exc
        adoption_resource_metadata.update(
            {
                "candidate_bundle_ref": derived_candidate.ref.model_dump(mode="json"),
                "primary_output_ref": derived_candidate.primary_output_ref.model_dump(
                    mode="json"
                ),
                "input_candidate_bundle_ref": source_candidate.ref.model_dump(
                    mode="json"
                ),
                "input_primary_output_ref": (
                    source_candidate.primary_output_ref.model_dump(mode="json")
                ),
            }
        )
    if request.decision == "request_parameter_retry":
        retry_update = prepare_autohic_review_retry(state, decision_ref)
        outputs = dict(retry_update.get("stage_outputs") or {})
        retry_out = HIC_SCAFFOLDING_CURSOR_ADAPTER.validate_python(
            outputs.get("hic_scaffolding") or {}
        )
        effect = HiCScaffoldingReviewEffectV1(
            decision="request_parameter_retry",
            decision_ref=decision_ref,
            pre_hic_assembly_ref=pre_hic_assembly_ref,
            output_assembly_ref=output_assembly_ref,
            hic_read_refs=tuple(str(ref) for ref in hic_out.get("hic_read_refs") or []),
            notes=tuple(request.notes),
            decided_by=request.decided_by,
            rationale_artifact_ref=request.rationale_artifact_ref,
            retry=_hic_review_retry_effect(retry_out),
        )
        return PreparedReviewApplication(
            effect_kind="hic_scaffolding.review_application.v1",
            effect=effect,
            decision_ref=decision_ref,
        )

    resource: HiCAssemblyResourceEffectV1 | None = None
    if request.decision == "keep_prior_assembly":
        try:
            current_assembly = resolve_stage_resource(
                state,
                resource_spec=assembly_resource_spec(),
            )
            source_candidate_ref = current_assembly.metadata.get("candidate_bundle_ref")
            if source_candidate_ref is None:
                raise AssemblyCandidateBundleError(
                    "kept AutoHiC assembly requires the current candidate_bundle_ref"
                )
            source_candidate = load_executed_candidate_bundle(
                absolute_data_dir(state), state["session_id"], source_candidate_ref
            )
            if (
                current_assembly.path is None
                or Path(current_assembly.path).resolve(strict=False)
                != Path(str(pre_hic_assembly_path)).resolve(strict=False)
                or source_candidate.primary_output_path.resolve(strict=False)
                != Path(str(pre_hic_assembly_path)).resolve(strict=False)
            ):
                raise AssemblyCandidateBundleError(
                    "current candidate primary output does not match the "
                    "kept pre-Hi-C assembly"
                )
        except (
            AssemblyCandidateBundleError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise ResumeValidationError(
                f"AutoHiC kept candidate provenance is invalid: {exc}"
            ) from exc
        resource = HiCAssemblyResourceEffectV1(
            path=pre_hic_assembly_ref,
            metadata={
                **dict(current_assembly.metadata),
                "decision": request.decision,
                "pre_hic_assembly_ref": pre_hic_assembly_ref.path_ref,
                "candidate_bundle_ref": source_candidate.ref.model_dump(mode="json"),
                "primary_output_ref": (
                    source_candidate.primary_output_ref.model_dump(mode="json")
                ),
            },
        )
    elif request.decision == "accept_candidate":
        if adoption_resource_metadata is None:
            raise ResumeValidationError("AutoHiC adoption metadata is unavailable")
        resource = HiCAssemblyResourceEffectV1(
            path=output_assembly_ref,
            metadata=adoption_resource_metadata,
        )

    effect = HiCScaffoldingReviewEffectV1(
        decision=decision,
        decision_ref=decision_ref,
        pre_hic_assembly_ref=pre_hic_assembly_ref,
        output_assembly_ref=output_assembly_ref,
        hic_read_refs=tuple(str(ref) for ref in hic_out.get("hic_read_refs") or []),
        notes=tuple(request.notes),
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        assembly_resource=resource,
    )
    return PreparedReviewApplication(
        effect_kind="hic_scaffolding.review_application.v1",
        effect=effect,
        decision_ref=decision_ref,
    )


def apply_hic_scaffolding_review_application(
    state: TopLevelState,
    application: VerifiedReviewApplication,
) -> StateUpdate:
    """Purely project a verified AutoHiC adoption application into state."""

    effect = application.effect
    if not isinstance(effect, HiCScaffoldingReviewEffectV1):
        raise TypeError("AutoHiC review application has an unexpected effect")
    if application.application.effect_kind != "hic_scaffolding.review_application.v1":
        raise ResumeValidationError("AutoHiC review application has an unexpected kind")
    if application.application.decision_ref != effect.decision_ref:
        raise ResumeValidationError(
            "AutoHiC review effect decision_ref does not match its envelope"
        )
    _validate_hic_scaffolding_application_state(state, application)

    if effect.decision == "request_parameter_retry":
        if effect.retry is None:
            raise ResumeValidationError(
                "AutoHiC retry application is missing retry data"
            )
        retry_payload: dict[str, object] = {
            "status": effect.retry.status,
            "parameter_attempts": [
                attempt.model_dump(mode="json")
                for attempt in effect.retry.parameter_attempts
            ],
            "proposed_parameter_attempt_id": effect.retry.proposed_parameter_attempt_id,
            "parameter_retry_request": (
                effect.retry.parameter_retry_request.model_dump(mode="json")
                if effect.retry.parameter_retry_request is not None
                else None
            ),
            "review_decision_artifact_ref": effect.decision_ref.model_dump(mode="json"),
            "active_interrupt": None,
        }
        if effect.retry.reason is not None:
            retry_payload["reason"] = effect.retry.reason
        if effect.retry.failure_kind is not None:
            retry_payload["failure_kind"] = effect.retry.failure_kind
        stage_update = _update_hic_output(state, retry_payload)
        retry_failed = effect.retry.status in {
            "review_parameter_retries_exhausted",
            "failed",
        }
        result: StateUpdate = {
            "current_stage": "hic_scaffolding",
            "supervision_state": (
                "failed_terminal" if retry_failed else "running_stage"
            ),
            "active_interrupt": None,
            **stage_update,
        }
        if retry_failed:
            reason = effect.retry.reason or "AutoHiC review retry budget exhausted"
            result = {
                **_settle_terminal_autohic(state, stage_update),
                **fail_workflow_step(state, "hic_scaffolding", reason),
                **result,
                "last_error": reason,
            }
        return result

    pre_hic_assembly_path = _rehydrate_hic_effect_path(
        state, effect.pre_hic_assembly_ref
    )
    output_assembly_path = _rehydrate_hic_effect_path(state, effect.output_assembly_ref)
    stage_update = _update_hic_output(
        state,
        {
            "status": (
                "failed_terminal"
                if effect.decision == "stop_human_review"
                else "completed"
            ),
            "decision": effect.decision,
            "selected_method_name": AUTOHIC_METHOD,
            "pre_hic_assembly_ref": pre_hic_assembly_path,
            "output_assembly_ref": output_assembly_path,
            "review_decision_artifact_ref": effect.decision_ref.model_dump(mode="json"),
            "notes": list(effect.notes),
            "decided_by": effect.decided_by,
            "rationale_artifact_ref": (
                effect.rationale_artifact_ref.path_ref
                if effect.rationale_artifact_ref is not None
                else None
            ),
            "active_interrupt": None,
        },
    )
    if effect.decision == "stop_human_review":
        reason = "hic_scaffolding stopped by human review decision"
        return {
            **_settle_terminal_autohic(state, stage_update),
            **fail_workflow_step(state, "hic_scaffolding", reason),
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            **stage_update,
            "last_error": reason,
        }
    if effect.assembly_resource is None:
        raise ResumeValidationError("AutoHiC adoption application has no resource")
    resource_path = _rehydrate_hic_effect_path(state, effect.assembly_resource.path)
    resource_metadata = dict(effect.assembly_resource.metadata)
    resource_metadata["pre_hic_assembly_ref"] = effect.pre_hic_assembly_ref.path_ref
    try:
        delivery_candidate_ref = ArtifactRef.model_validate(
            resource_metadata["candidate_bundle_ref"]
        )
        delivery_primary_ref = ArtifactRef.model_validate(
            resource_metadata["primary_output_ref"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "AutoHiC adoption application has invalid assembly delivery provenance"
        ) from exc
    return {
        **advance_workflow_step(
            state,
            "hic_scaffolding",
            outcome="completed",
            deliveries=(
                assembly_delivery(
                    delivery_candidate_ref.model_dump(mode="json"),
                    delivery_primary_ref.model_dump(mode="json"),
                    delivery_mode=(
                        "materialized"
                        if effect.decision == "accept_candidate"
                        else "reused"
                    ),
                ),
            ),
        ),
        "active_interrupt": None,
        **stage_update,
        "route_trace": append_route_trace(state, "hic_scaffolding_stage"),
        "resource_refs": update_resource_ref(
            state,
            "assembly",
            [resource_path],
            "hic_scaffolding",
            resource_metadata,
        ),
    }


def _rehydrate_hic_effect_path(state: TopLevelState, value: RunRelativeFileRef) -> str:
    """Restore a portable application path to the runtime-path contract."""

    path = session_paths(state).resolve_ref(value.path_ref)
    if not path.is_file():
        raise ResumeValidationError("AutoHiC review effect file is missing")
    return str(path)


def _validate_hic_scaffolding_review_intent(
    state: TopLevelState,
    intent: ReviewIntentV1,
) -> None:
    if intent.session_id != state["session_id"]:
        raise ResumeValidationError("AutoHiC adoption intent session_id mismatch")
    if intent.workflow_step_id != current_workflow_step_id(
        state, expected_stage="hic_scaffolding"
    ):
        raise ResumeValidationError("AutoHiC adoption intent workflow_step_id mismatch")


def validate_hic_scaffolding_review_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: Mapping[str, Any],
) -> ResumeReviewRequest:
    """Validate an AutoHiC review decision before its intent is persisted."""

    if request.session_id != state["session_id"]:
        raise ResumeValidationError("AutoHiC adoption request session_id mismatch")
    if request.stage != "hic_scaffolding" or request.gate_id != "hic_scaffolding":
        raise ResumeValidationError(
            "AutoHiC adoption review requires hic_scaffolding/hic_scaffolding"
        )
    workflow_step_id = current_workflow_step_id(state, expected_stage="hic_scaffolding")
    active_interrupt = state.get("active_interrupt")
    if not isinstance(active_interrupt, Mapping):
        raise ResumeValidationError(
            "AutoHiC adoption review requires an active_interrupt"
        )
    try:
        request_ref = ArtifactRef.model_validate(active_interrupt.get("request_ref"))
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "AutoHiC adoption active_interrupt has no valid request_ref"
        ) from exc
    if (
        request.review_request_id != active_interrupt.get("review_request_id")
        or request.review_request_hash != active_interrupt.get("request_hash")
        or request.review_request_hash != request_ref.sha256
        or active_interrupt.get("workflow_step_id") != workflow_step_id
        or request_record.get("review_request_id") != request.review_request_id
        or request_record.get("stage") != "hic_scaffolding"
        or request_record.get("gate_id") != "hic_scaffolding"
        or request_record.get("workflow_step_id") != workflow_step_id
    ):
        raise ResumeValidationError("AutoHiC adoption review request binding mismatch")
    if request.decision not in {
        "accept_candidate",
        "keep_prior_assembly",
        "request_parameter_retry",
        "stop_human_review",
    }:
        raise ResumeValidationError(
            f"unsupported AutoHiC adoption decision: {request.decision}"
        )
    if request.method_name and request.method_name != AUTOHIC_METHOD:
        raise ResumeValidationError(f"AutoHiC adoption method must be {AUTOHIC_METHOD}")
    hic_out = _hic_output(state)
    pre_hic_assembly_ref = hic_out.get("pre_hic_assembly_ref")
    if not pre_hic_assembly_ref:
        raise ResumeValidationError("AutoHiC adoption requires pre_hic_assembly_ref")
    if request.decision != "accept_candidate":
        return request
    if request.decided_by != "human":
        raise ResumeValidationError(
            "AutoHiC candidate adoption requires a human review decision"
        )

    candidate = dict((hic_out.get("method_candidates") or {}).get(AUTOHIC_METHOD) or {})
    if candidate.get("status") != "executed" or not candidate.get("scaffold_fasta"):
        raise ResumeValidationError(
            "accept_candidate requires a completed AutoHiC chromosome assembly"
        )
    try:
        current_assembly = resolve_stage_resource(
            state,
            resource_spec=assembly_resource_spec(),
        )
        if (
            not current_assembly.found
            or current_assembly.path is None
            or Path(current_assembly.path).resolve(strict=False)
            != Path(str(pre_hic_assembly_ref)).resolve(strict=False)
        ):
            raise ValueError("current assembly no longer matches pre_hic_assembly_ref")
        artifact_ref_for_path(
            absolute_data_dir(state),
            state["session_id"],
            str(candidate["scaffold_fasta"]),
            expected_type=HIC_SCAFFOLDED_ASSEMBLY,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ResumeValidationError(
            f"AutoHiC adopted assembly provenance is invalid: {exc}"
        ) from exc
    return request


def _validate_hic_scaffolding_application_state(
    state: TopLevelState,
    application: VerifiedReviewApplication,
) -> None:
    identity = application.application
    if identity.session_id != state["session_id"]:
        raise ResumeValidationError("AutoHiC application session_id mismatch")
    if identity.stage != "hic_scaffolding" or identity.gate_id != "hic_scaffolding":
        raise ResumeValidationError("AutoHiC application has an unexpected gate")
    if identity.workflow_step_id != current_workflow_step_id(
        state, expected_stage="hic_scaffolding"
    ):
        raise ResumeValidationError("AutoHiC application workflow_step_id mismatch")


def _hic_scaffolding_review_binding(
    state: TopLevelState,
    intent: ReviewIntentV1,
) -> tuple[str, str, ArtifactRef, dict[str, Any]]:
    hic_out = _hic_output(state)
    active_interrupt: Mapping[str, Any] = (
        state.get("active_interrupt") or hic_out.get("active_interrupt") or {}
    )
    request_ref_value = active_interrupt.get("request_ref") or hic_out.get(
        "review_request_artifact_ref"
    )
    try:
        request_ref = ArtifactRef.model_validate(request_ref_value)
        request_record = load_review_request(
            absolute_data_dir(state), request_ref, session_id=state["session_id"]
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC adoption review request is missing or changed"
        ) from exc
    if (
        request_ref.sha256 != intent.request_hash
        or request_record.get("review_request_id") != intent.review_request_id
        or request_record.get("stage") != "hic_scaffolding"
        or request_record.get("gate_id") != "hic_scaffolding"
        or request_record.get("workflow_step_id") != intent.workflow_step_id
        or active_interrupt.get("workflow_step_id") != intent.workflow_step_id
    ):
        raise ResumeValidationError("AutoHiC adoption review request binding mismatch")
    return intent.review_request_id, intent.request_hash, request_ref, request_record


def _hic_review_retry_effect(
    retry_out: Mapping[str, Any],
) -> HiCReviewRetryEffectV1:
    attempts = retry_out.get("parameter_attempts") or []
    if not isinstance(attempts, list):
        raise ResumeValidationError("AutoHiC retry produced invalid parameter attempts")
    status = retry_out.get("status")
    allowed_statuses = {
        "retry_parameters_after_review_failure",
        "review_parameter_retries_exhausted",
        "failed",
    }
    if status not in allowed_statuses:
        raise ResumeValidationError("AutoHiC retry produced an unsupported status")
    retry_request = retry_out.get("parameter_retry_request")
    return HiCReviewRetryEffectV1(
        status=cast(
            Literal[
                "retry_parameters_after_review_failure",
                "review_parameter_retries_exhausted",
                "failed",
            ],
            status,
        ),
        reason=retry_out.get("reason"),
        failure_kind=retry_out.get("failure_kind"),
        parameter_attempts=tuple(
            ParameterAttempt.model_validate(attempt) for attempt in attempts
        ),
        proposed_parameter_attempt_id=retry_out.get("proposed_parameter_attempt_id"),
        parameter_retry_request=(
            HiCReviewRetryRequestEffectV1.model_validate(retry_request)
            if retry_request is not None
            else None
        ),
    )


def fail_hic_scaffolding(state: TopLevelState) -> StateUpdate:
    """Stop after recommendation, execution, or adoption preparation fails."""

    hic_out = _hic_output(state)
    reason = str(hic_out.get("reason", "hic_scaffolding failed"))
    return {
        **_settle_terminal_autohic(state),
        **fail_workflow_step(state, "hic_scaffolding", reason),
        "active_interrupt": None,
    }


def _fail_update(
    state: TopLevelState,
    reason: str,
    *,
    failure_kind: str,
) -> StateUpdate:
    return _update_hic_output(
        state,
        {"status": "failed", "failure_kind": failure_kind, "reason": reason},
    )


def _hic_assets(state: TopLevelState) -> list[dict[str, Any]]:
    pairs = resolve_hic_pairs(state)
    assets: list[dict[str, Any]] = []
    for pair in pairs:
        for mate, member in (("R1", pair.r1), ("R2", pair.r2)):
            assets.append(
                {
                    "asset_id": member.member_id,
                    "asset_type": "hic_reads_registered",
                    "path": member.path,
                    "metadata": {
                        "manifest_entry_ordinal": pair.manifest_entry_ordinal,
                        "pair_index": pair.pair_index,
                        "mate": mate,
                        "platform": member.platform,
                        "format": member.format,
                    },
                }
            )
    return assets
