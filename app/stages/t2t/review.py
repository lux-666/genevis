"""T2T human approval preparation and application, separate from execution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast, overload

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from app.control.errors import ResumeValidationError
from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    current_workflow_step_id,
    fail_workflow_step,
)
from app.execution.input_integrity import (
    artifact_input_dependency,
    verify_input_dependencies,
)
from app.integrity import SHA256_PATTERN
from app.review.application import (
    PreparedReviewApplication,
    ReviewIntentV1,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest
from app.review.candidates import candidate_ids_from_review_request_payload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
)
from app.schemas.artifacts import (
    T2T_ASSEMBLY_TELOMERE_SCAN,
    T2T_EXTENSION_CANDIDATES,
    T2T_FINISHING_REPORT,
    T2T_PATCH_RECORDS,
    T2T_PATCHED_ASSEMBLY,
    T2T_STATUS_SUMMARY,
    T2T_TELOMERIC_READS,
    ArtifactRef,
    T2TReviewDecisionArtifact,
    canonical_artifact_refs,
)
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import (
    StateUpdate,
    TopLevelState,
    validate_top_level_state,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    resolve_run_ref,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import assembly_resource_spec
from app.stages.t2t.nodes import (
    _ASSEMBLY_SCAN_FIELDS,
    T2T_CONTRACT_METHOD,
    _assembly_input_dependency,
    _canonical_path,
    _finalize_t2t_evidence,
    _materialize_t2t_patches,
    _output,
    _read_tsv,
    _scan_fasta_ends,
    _settle_terminal_alignment,
    _verify_t2t_adopted_candidate,
    _write_tsv,
)
from app.stages.t2t.parameters.execution import (
    T2TFrozenExecutionInputs,
    load_t2t_frozen_execution_inputs,
)
from app.stages.t2t.state import (
    T2TStageCursor,
    read_t2t_stage_output,
    update_t2t_stage_output,
)
from app.storage.artifacts import (
    artifact_ref_for_path,
    publish_or_recover_work_artifact,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    publish_derived_assembly_candidate,
)
from app.storage.paths import DataLayoutError


class T2TReviewEffectV1(BaseModel):
    """The bounded durable result of one T2T review application."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision: Literal[
        "accept_selected_candidates", "continue_without_patch", "stop_pipeline"
    ]
    review_request_id: str = Field(min_length=1)
    review_request_hash: str = Field(pattern=SHA256_PATTERN)
    review_request_ref: ArtifactRef
    selected_candidate_ids: tuple[str, ...] = ()
    decision_ref: ArtifactRef
    notes: tuple[str, ...] = ()
    decided_by: Literal["human", "llm_provisional", "auto_rule"] = "human"
    rationale_artifact_ref: ArtifactRef | None = None
    adopted_patch: bool = False
    output_candidate_bundle_ref: ArtifactRef | None = None
    output_primary_ref: ArtifactRef | None = None
    input_candidate_bundle_ref: ArtifactRef | None = None
    input_primary_ref: ArtifactRef | None = None
    patched_assembly_ref: ArtifactRef | None = None
    patch_records_ref: ArtifactRef | None = None
    patched_telomere_scan_ref: ArtifactRef | None = None
    telomere_status_summary_ref: ArtifactRef | None = None
    t2t_finishing_report_ref: ArtifactRef | None = None
    bundle_ref: ArtifactRef | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    status: Literal["completed", "completed_with_warnings"] | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "T2TReviewEffectV1":
        if self.selected_candidate_ids != tuple(
            sorted(set(self.selected_candidate_ids))
        ):
            raise ValueError("t2t selected_candidate_ids must be sorted and unique")
        if self.artifact_refs != canonical_artifact_refs(self.artifact_refs):
            raise ValueError("t2t artifact_refs must be sorted and unique")
        if self.decision == "stop_pipeline":
            terminal_values = (
                self.output_candidate_bundle_ref,
                self.output_primary_ref,
                self.telomere_status_summary_ref,
                self.t2t_finishing_report_ref,
                self.bundle_ref,
                self.status,
            )
            if any(value is not None for value in terminal_values):
                raise ValueError("stop_pipeline effect cannot contain a final handoff")
            return self
        required = (
            self.output_candidate_bundle_ref,
            self.output_primary_ref,
            self.telomere_status_summary_ref,
            self.t2t_finishing_report_ref,
            self.bundle_ref,
            self.status,
        )
        if any(value is None for value in required):
            raise ValueError("T2T review effect is missing final durable evidence")
        if self.adopted_patch and (
            self.input_candidate_bundle_ref is None
            or self.input_primary_ref is None
            or self.patched_assembly_ref is None
        ):
            raise ValueError("adopted patch effect is missing candidate provenance")
        return self


T2T_REVIEW_EFFECT_KIND = "t2t.review_application.v1"


class _PreparedT2TSelectedOutput(TypedDict):
    status: Literal["completed", "patched_validated"]
    output_assembly_ref: str
    patched_assembly_ref: str | None
    patch_records_ref: str | None
    patched_telomere_scan_ref: str | None


def prepare_t2t_review_application(
    state: TopLevelState, intent: ReviewIntentV1
) -> PreparedReviewApplication:
    """Persist the selected T2T result and all final evidence before apply."""

    if intent.stage != "t2t" or intent.gate_id != "t2t_review":
        raise ResumeValidationError("t2t review application has the wrong gate")
    _require_t2t_review_intent_binding(state, intent)
    request = validate_t2t_intent_request(
        state,
        intent.request,
        load_review_request(
            state["data_dir"], intent.request_ref, session_id=state["session_id"]
        ),
    )
    out = _output(state)
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
    except ValueError as exc:
        raise ResumeValidationError(
            f"T2T frozen execution inputs are invalid: {exc}"
        ) from exc
    decision = _t2t_review_decision(request)
    accepted_candidate_ids = (
        list(request.candidate_ids) if decision == "accept_selected_candidates" else []
    )
    if decision == "accept_selected_candidates" and not accepted_candidate_ids:
        raise ResumeValidationError(
            "t2t accept_selected_candidates requires candidate_ids"
        )

    if decision == "stop_pipeline":
        return _prepare_stopped_t2t_review_application(
            state, intent, execution_inputs, accepted_candidate_ids
        )

    current_assembly = _require_current_t2t_review_assembly(state, execution_inputs)
    source_candidate = _verify_t2t_adopted_candidate(
        state,
        current_assembly_metadata=current_assembly.metadata,
        assembly_path=execution_inputs.input_assembly_ref,
    )
    prepared = _prepare_t2t_selected_output(
        state,
        out,
        execution_inputs,
        decision=decision,
        accepted_candidate_ids=accepted_candidate_ids,
    )
    decision_ref = _persist_t2t_review_decision(
        state,
        intent,
        T2TReviewDecisionArtifact(
            session_id=state["session_id"],
            decision=decision,
            candidate_ids=accepted_candidate_ids,
            input_assembly_ref=execution_inputs.input_assembly_ref,
            output_assembly_ref=prepared["output_assembly_ref"],
            patched_assembly_ref=prepared.get("patched_assembly_ref"),
            notes=list(request.notes),
            metadata={
                **request.metadata,
                "decided_by": request.decided_by,
                "rationale_artifact_ref": (
                    request.rationale_artifact_ref.model_dump(mode="json")
                    if request.rationale_artifact_ref is not None
                    else None
                ),
            },
        ),
    )
    stage_payload = {
        "status": prepared["status"],
        "decision": decision,
        "selected_candidate_ids": accepted_candidate_ids,
        "selected_patch_count": len(accepted_candidate_ids),
        "patched_assembly_ref": prepared.get("patched_assembly_ref"),
        "patch_records_ref": prepared.get("patch_records_ref"),
        "output_assembly_ref": prepared["output_assembly_ref"],
        "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
        "notes": list(request.notes),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": (
            request.rationale_artifact_ref.path_ref
            if request.rationale_artifact_ref is not None
            else None
        ),
    }
    if prepared.get("patched_telomere_scan_ref") is not None:
        stage_payload["patched_telomere_scan_ref"] = prepared[
            "patched_telomere_scan_ref"
        ]
    stage_update = update_t2t_stage_output(state, stage_payload)
    reviewed_state = validate_top_level_state(
        {
            **state,
            **dict(stage_update),
            "active_interrupt": None,
            "supervision_state": "running_stage",
        }
    )
    try:
        final_evidence_update = _finalize_t2t_evidence(
            reviewed_state, read_t2t_stage_output(reviewed_state)
        )
    except (OSError, ValueError, DataLayoutError) as exc:
        raise ResumeValidationError(
            f"t2t final evidence could not be persisted: {exc}"
        ) from exc
    finalized_state = validate_top_level_state(
        {**reviewed_state, **dict(final_evidence_update)}
    )
    finalized = read_t2t_stage_output(finalized_state)
    adopted_patch = (
        prepared["output_assembly_ref"] != execution_inputs.input_assembly_ref
    )
    if adopted_patch:
        output_primary_ref = artifact_ref_for_path(
            absolute_data_dir(state),
            state["session_id"],
            prepared["output_assembly_ref"],
            expected_type=T2T_PATCHED_ASSEMBLY,
        )
        output_candidate = publish_derived_assembly_candidate(
            absolute_data_dir(state),
            state["session_id"],
            workflow_step_id=current_workflow_step_id(state, expected_stage="t2t"),
            source_candidate=source_candidate,
            primary_output_ref=output_primary_ref,
            stage="t2t",
            method_name=T2T_CONTRACT_METHOD,
            action_identity=decision_ref.artifact_id,
        )
    else:
        output_candidate = source_candidate
        output_primary_ref = source_candidate.primary_output_ref
    effect = _t2t_review_effect_from_finalized_output(
        state,
        intent,
        finalized,
        decision=decision,
        selected_candidate_ids=accepted_candidate_ids,
        decision_ref=decision_ref,
        request=request,
        adopted_patch=adopted_patch,
        source_candidate=source_candidate,
        output_candidate=output_candidate,
        output_primary_ref=output_primary_ref,
    )
    return PreparedReviewApplication(
        effect_kind=T2T_REVIEW_EFFECT_KIND,
        effect=effect,
        decision_ref=decision_ref,
        required_refs=canonical_artifact_refs(
            (
                decision_ref,
                *effect.artifact_refs,
                output_candidate.ref,
                output_primary_ref,
            )
        ),
    )


def apply_t2t_review_application(
    state: TopLevelState, application: VerifiedReviewApplication
) -> StateUpdate:
    """Purely project a verified T2T effect into its checkpoint state."""

    if application.application.effect_kind != T2T_REVIEW_EFFECT_KIND:
        raise ResumeValidationError("t2t review application has the wrong effect kind")
    if not isinstance(application.effect, T2TReviewEffectV1):
        raise ResumeValidationError("t2t review application has the wrong effect model")
    if (
        application.application.stage != "t2t"
        or application.application.gate_id != "t2t_review"
    ):
        raise ResumeValidationError("t2t review application has the wrong gate")
    effect = application.effect
    if effect.decision == "stop_pipeline":
        reason = "t2t stopped by human review decision"
        stage_update = update_t2t_stage_output(
            state,
            {
                "status": "failed_terminal",
                "decision": effect.decision,
                "selected_candidate_ids": [],
                "selected_patch_count": 0,
                "review_decision_artifact_ref": effect.decision_ref.model_dump(
                    mode="json"
                ),
                "notes": list(effect.notes),
                "decided_by": effect.decided_by,
                "rationale_artifact_ref": (
                    effect.rationale_artifact_ref.path_ref
                    if effect.rationale_artifact_ref is not None
                    else None
                ),
            },
        )
        return {
            **_settle_terminal_alignment(state, stage_update),
            **fail_workflow_step(state, "t2t", reason),
            "current_stage": "t2t",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            **stage_update,
            "last_error": reason,
        }
    if (
        effect.output_candidate_bundle_ref is None
        or effect.output_primary_ref is None
        or effect.telomere_status_summary_ref is None
        or effect.t2t_finishing_report_ref is None
        or effect.bundle_ref is None
        or effect.status is None
    ):
        raise ResumeValidationError(
            "t2t review effect is missing final durable evidence"
        )
    stage_payload = {
        "status": effect.status,
        "decision": effect.decision,
        "selected_candidate_ids": list(effect.selected_candidate_ids),
        "selected_patch_count": len(effect.selected_candidate_ids),
        "patched_assembly_ref": _effect_ref_path(state, effect.patched_assembly_ref),
        "patch_records_ref": _effect_ref_path(state, effect.patch_records_ref),
        "output_assembly_ref": _effect_ref_path(state, effect.output_primary_ref),
        "telomere_status_summary_ref": _effect_ref_path(
            state, effect.telomere_status_summary_ref
        ),
        "t2t_finishing_report_ref": _effect_ref_path(
            state, effect.t2t_finishing_report_ref
        ),
        "bundle_ref": _effect_ref_path(state, effect.bundle_ref),
        "finishing_evidence_phase": "final",
        "artifact_refs": [_effect_ref_path(state, ref) for ref in effect.artifact_refs],
        "review_decision_artifact_ref": effect.decision_ref.model_dump(mode="json"),
        "notes": list(effect.notes),
        "decided_by": effect.decided_by,
        "rationale_artifact_ref": (
            effect.rationale_artifact_ref.path_ref
            if effect.rationale_artifact_ref is not None
            else None
        ),
    }
    if effect.patched_telomere_scan_ref is not None:
        stage_payload["patched_telomere_scan_ref"] = _effect_ref_path(
            state, effect.patched_telomere_scan_ref
        )
    stage_update = update_t2t_stage_output(state, stage_payload)
    raw_assembly = (state.get("resource_refs") or {}).get("assembly")
    if raw_assembly is None:
        raise ResumeValidationError("t2t review application has no assembly resource")
    current_resource = (
        ResourceRef.from_dict(raw_assembly)
        if isinstance(raw_assembly, dict)
        else raw_assembly
    )
    metadata = {
        **dict(current_resource.metadata),
        "patched": effect.adopted_patch,
        "candidate_bundle_ref": effect.output_candidate_bundle_ref.model_dump(
            mode="json"
        ),
        "primary_output_ref": effect.output_primary_ref.model_dump(mode="json"),
    }
    if effect.adopted_patch:
        if (
            effect.input_candidate_bundle_ref is None
            or effect.input_primary_ref is None
        ):
            raise ResumeValidationError(
                "t2t adopted effect is missing input provenance"
            )
        metadata.update(
            {
                "input_candidate_bundle_ref": (
                    effect.input_candidate_bundle_ref.model_dump(mode="json")
                ),
                "input_primary_output_ref": effect.input_primary_ref.model_dump(
                    mode="json"
                ),
            }
        )
    output_path = _effect_ref_path(state, effect.output_primary_ref)
    return {
        **advance_workflow_step(
            state,
            "t2t",
            outcome="completed",
            deliveries=(
                assembly_delivery(
                    effect.output_candidate_bundle_ref.model_dump(mode="json"),
                    effect.output_primary_ref.model_dump(mode="json"),
                    delivery_mode=(
                        "materialized" if effect.adopted_patch else "reused"
                    ),
                ),
            ),
        ),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, "t2t_stage"),
        "stage_outputs": stage_update["stage_outputs"],
        "resource_refs": update_resource_ref(
            state,
            "assembly",
            [output_path],
            "t2t",
            metadata,
        ),
    }


def _t2t_review_decision(
    request: ResumeReviewRequest,
) -> Literal["accept_selected_candidates", "continue_without_patch", "stop_pipeline"]:
    if request.decision in {
        "accept_selected_candidates",
        "continue_without_patch",
        "stop_pipeline",
    }:
        return cast(
            Literal[
                "accept_selected_candidates",
                "continue_without_patch",
                "stop_pipeline",
            ],
            request.decision,
        )
    raise ResumeValidationError(
        f"unsupported t2t review decision: {request.decision!r}"
    )


def validate_t2t_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: Mapping[str, Any],
) -> ResumeReviewRequest:
    """Validate and canonicalize one T2T intent request."""

    decision = _t2t_review_decision(request)
    if decision != "accept_selected_candidates":
        return request.model_copy(update={"candidate_ids": []})
    if request.decided_by != "human":
        from app.review.automatic import validate_zero_wait_request

        validate_zero_wait_request(
            request, request_record, data_dir=state.get("data_dir", "")
        )
    candidate_ids = sorted(set(request.candidate_ids))
    if not candidate_ids:
        raise ResumeValidationError(
            "t2t accept_selected_candidates requires candidate_ids"
        )
    immutable_candidate_ids = candidate_ids_from_review_request_payload(
        request_record.get("payload")
    )
    unknown_immutable = sorted(set(candidate_ids).difference(immutable_candidate_ids))
    if unknown_immutable:
        raise ResumeValidationError(
            "unknown t2t candidate_ids in immutable review request: "
            f"{', '.join(unknown_immutable)}"
        )
    selectable_candidate_ids = _selectable_t2t_candidate_ids(state)
    unknown_selectable = sorted(set(candidate_ids).difference(selectable_candidate_ids))
    if unknown_selectable:
        raise ResumeValidationError(
            "unknown t2t candidate_ids in current selectable evidence: "
            f"{', '.join(unknown_selectable)}"
        )
    return request.model_copy(update={"candidate_ids": candidate_ids})


def _selectable_t2t_candidate_ids(state: TopLevelState) -> set[str]:
    out = _output(state)
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        candidate_rows = _read_tsv(Path(out["telomere_extension_candidates_ref"]))
    except (KeyError, OSError, ValueError) as exc:
        raise ResumeValidationError(
            f"t2t selectable candidate evidence is unavailable: {exc}"
        ) from exc
    allowed = {"auto_patch"} if execution_inputs.mode == "auto_patch" else {"proposed"}
    return {
        str(row["candidate_id"])
        for row in candidate_rows
        if row.get("candidate_id") and row.get("decision") in allowed
    }


def _require_t2t_review_intent_binding(
    state: TopLevelState, intent: ReviewIntentV1
) -> None:
    """Require the immutable intent to target this blocked T2T occurrence."""

    if intent.session_id != state["session_id"]:
        raise ResumeValidationError("t2t review intent session does not match")
    if state.get("supervision_state") != "blocked_human":
        raise ResumeValidationError("t2t review application requires blocked_human")
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    expected = {
        "stage": "t2t",
        "gate_id": "t2t_review",
        "workflow_step_id": intent.workflow_step_id,
        "review_request_id": intent.review_request_id,
        "request_hash": intent.request_hash,
    }
    for field, value in expected.items():
        if interrupt.get(field) != value:
            raise ResumeValidationError(
                f"t2t review active_interrupt {field} does not match intent"
            )
    try:
        interrupt_ref = ArtifactRef.model_validate(interrupt.get("request_ref"))
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "t2t review active_interrupt has no valid request_ref"
        ) from exc
    if interrupt_ref != intent.request_ref:
        raise ResumeValidationError(
            "t2t review active_interrupt request_ref does not match intent"
        )
    if current_workflow_step_id(state, expected_stage="t2t") != intent.workflow_step_id:
        raise ResumeValidationError(
            "t2t review current workflow step does not match intent"
        )


def _require_current_t2t_review_assembly(
    state: TopLevelState, execution_inputs: T2TFrozenExecutionInputs
) -> Any:
    try:
        current_assembly = resolve_stage_resource(
            state,
            resource_spec=assembly_resource_spec(),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ResumeValidationError(
            f"t2t assembly resource provenance is invalid: {exc}"
        ) from exc
    if (
        not current_assembly.found
        or not current_assembly.path
        or _canonical_path(current_assembly.path)
        != _canonical_path(execution_inputs.input_assembly_ref)
    ):
        raise ResumeValidationError(
            "t2t assembly resource changed since input resolution; refusing to apply "
            "a stale review decision"
        )
    return current_assembly


def _prepare_t2t_selected_output(
    state: TopLevelState,
    out: T2TStageCursor,
    execution_inputs: T2TFrozenExecutionInputs,
    *,
    decision: Literal["accept_selected_candidates", "continue_without_patch"],
    accepted_candidate_ids: list[str],
) -> _PreparedT2TSelectedOutput:
    """Validate or materialize the selected output before publishing the decision."""

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    if decision == "continue_without_patch":
        return {
            "status": "completed",
            "output_assembly_ref": execution_inputs.input_assembly_ref,
            "patched_assembly_ref": (
                out.get("patched_assembly_ref")
                if execution_inputs.mode == "auto_patch"
                else None
            ),
            "patch_records_ref": out.get("patch_records_ref"),
            "patched_telomere_scan_ref": (
                out.get("patched_telomere_scan_ref")
                if execution_inputs.mode == "auto_patch"
                else None
            ),
        }

    try:
        review_dependencies = [
            _assembly_input_dependency(state, execution_inputs.input_assembly_ref)
        ]
        review_evidence = [
            (out["telomere_extension_candidates_ref"], T2T_EXTENSION_CANDIDATES),
            *[
                (path, T2T_TELOMERIC_READS)
                for path in sorted(
                    (out.get("telomeric_reads_fasta_refs") or {}).values()
                )
            ],
        ]
        for path_value, artifact_type in review_evidence:
            artifact_ref = artifact_ref_for_path(
                data_dir,
                session_id,
                path_value,
                expected_type=artifact_type,
            )
            review_dependencies.append(
                artifact_input_dependency(state, artifact_ref, path_value)
            )
        verify_input_dependencies(state, review_dependencies)
    except (OSError, ValueError, DataLayoutError) as exc:
        raise ResumeValidationError(
            f"t2t review evidence changed before adoption: {exc}"
        ) from exc

    candidate_rows = _read_tsv(Path(out["telomere_extension_candidates_ref"]))
    allowed_candidate_decisions = (
        {"auto_patch"} if execution_inputs.mode == "auto_patch" else {"proposed"}
    )
    selectable = {
        row["candidate_id"]: row
        for row in candidate_rows
        if row.get("candidate_id")
        and row.get("decision") in allowed_candidate_decisions
    }
    unknown = sorted(set(accepted_candidate_ids).difference(selectable))
    if unknown:
        raise ResumeValidationError(f"unknown t2t candidate_ids: {', '.join(unknown)}")
    if execution_inputs.mode == "auto_patch":
        if set(accepted_candidate_ids) != set(selectable):
            raise ResumeValidationError(
                "t2t post-patch adoption must accept the complete validated candidate "
                "assembly"
            )
        patched_assembly_ref = out.get("patched_assembly_ref")
        patched_telomere_scan_ref = out.get("patched_telomere_scan_ref")
        patch_records_ref = out.get("patch_records_ref")
        if (
            not patched_assembly_ref
            or not Path(patched_assembly_ref).is_file()
            or not patched_telomere_scan_ref
            or not Path(patched_telomere_scan_ref).is_file()
        ):
            raise ResumeValidationError(
                "t2t validated candidate patch evidence is unavailable"
            )
        try:
            adoption_dependencies = []
            for path_value, artifact_type in (
                (patched_assembly_ref, T2T_PATCHED_ASSEMBLY),
                (patched_telomere_scan_ref, T2T_ASSEMBLY_TELOMERE_SCAN),
            ):
                artifact_ref = artifact_ref_for_path(
                    data_dir,
                    session_id,
                    path_value,
                    expected_type=artifact_type,
                )
                adoption_dependencies.append(
                    artifact_input_dependency(state, artifact_ref, path_value)
                )
            verify_input_dependencies(state, adoption_dependencies)
        except (OSError, ValueError, DataLayoutError) as exc:
            raise ResumeValidationError(
                f"t2t candidate patch evidence changed before adoption: {exc}"
            ) from exc
        return {
            "status": "patched_validated",
            "output_assembly_ref": patched_assembly_ref,
            "patched_assembly_ref": patched_assembly_ref,
            "patch_records_ref": patch_records_ref,
            "patched_telomere_scan_ref": patched_telomere_scan_ref,
        }

    materialized = _materialize_t2t_patches(
        state,
        out,
        execution_inputs=execution_inputs,
        selected_candidate_ids=set(accepted_candidate_ids),
        allowed_decisions={"proposed"},
    )
    if not materialized:
        raise ResumeValidationError(
            "t2t selected candidates could not be materialized into a patched assembly"
        )
    if materialized.get("error"):
        raise ResumeValidationError(
            "t2t selected candidates are internally inconsistent: "
            f"{materialized['error']}"
        )
    patched_assembly_ref = materialized["patched_assembly_ref"]
    try:
        scan_rows = list(
            _scan_fasta_ends(
                Path(patched_assembly_ref),
                execution_inputs.resolved_telomere_motif,
                execution_inputs.thresholds.model_dump(mode="json"),
            )
        )
    except (OSError, ValueError) as exc:
        raise ResumeValidationError(
            f"t2t patched assembly validation failed: {exc}"
        ) from exc
    scan_source = Path(execution_inputs.work_dir) / "patched_telomere_scan.tsv"
    _write_tsv(scan_source, scan_rows, _ASSEMBLY_SCAN_FIELDS)
    patched_telomere_scan_ref = str(
        publish_or_recover_work_artifact(
            data_dir,
            session_id,
            scan_source,
            stage="t2t",
            method_name=T2T_CONTRACT_METHOD,
            artifact_type=T2T_ASSEMBLY_TELOMERE_SCAN,
        ).path
    )
    return {
        "status": "patched_validated",
        "output_assembly_ref": patched_assembly_ref,
        "patched_assembly_ref": patched_assembly_ref,
        "patch_records_ref": materialized["patch_records_ref"],
        "patched_telomere_scan_ref": patched_telomere_scan_ref,
    }


def _prepare_stopped_t2t_review_application(
    state: TopLevelState,
    intent: ReviewIntentV1,
    execution_inputs: T2TFrozenExecutionInputs,
    accepted_candidate_ids: list[str],
) -> PreparedReviewApplication:
    request = intent.request
    decision_ref = _persist_t2t_review_decision(
        state,
        intent,
        T2TReviewDecisionArtifact(
            session_id=state["session_id"],
            decision="stop_pipeline",
            candidate_ids=accepted_candidate_ids,
            input_assembly_ref=execution_inputs.input_assembly_ref,
            output_assembly_ref=execution_inputs.input_assembly_ref,
            notes=list(request.notes),
            metadata={
                **request.metadata,
                "decided_by": request.decided_by,
                "rationale_artifact_ref": (
                    request.rationale_artifact_ref.model_dump(mode="json")
                    if request.rationale_artifact_ref is not None
                    else None
                ),
            },
        ),
    )
    return PreparedReviewApplication(
        effect_kind=T2T_REVIEW_EFFECT_KIND,
        effect=T2TReviewEffectV1(
            decision="stop_pipeline",
            review_request_id=intent.review_request_id,
            review_request_hash=intent.request_hash,
            review_request_ref=intent.request_ref,
            selected_candidate_ids=tuple(accepted_candidate_ids),
            decision_ref=decision_ref,
            notes=tuple(request.notes),
            decided_by=request.decided_by,
            rationale_artifact_ref=request.rationale_artifact_ref,
        ),
        decision_ref=decision_ref,
    )


def _persist_t2t_review_decision(
    state: TopLevelState,
    intent: ReviewIntentV1,
    decision: T2TReviewDecisionArtifact,
) -> ArtifactRef:
    try:
        return persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=intent.review_request_id,
            stage="t2t",
            gate_id="t2t_review",
            decision=decision,
            request_hash=intent.request_hash,
            request_ref=intent.request_ref,
            method_name=T2T_CONTRACT_METHOD,
            artifact_type="t2t.review_decision",
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "t2t review decision could not be persisted"
        ) from exc


def _t2t_review_effect_from_finalized_output(
    state: TopLevelState,
    intent: ReviewIntentV1,
    out: T2TStageCursor,
    *,
    decision: Literal["accept_selected_candidates", "continue_without_patch"],
    selected_candidate_ids: list[str],
    decision_ref: ArtifactRef,
    request: ResumeReviewRequest,
    adopted_patch: bool,
    source_candidate: VerifiedExecutedCandidate,
    output_candidate: VerifiedExecutedCandidate,
    output_primary_ref: ArtifactRef,
) -> T2TReviewEffectV1:
    def ref(key: str, artifact_type: str, *, required: bool) -> ArtifactRef | None:
        value = out.get(key)
        if not value:
            if required:
                raise ResumeValidationError(f"t2t final evidence has no {key}")
            return None
        return artifact_ref_for_path(
            absolute_data_dir(state),
            state["session_id"],
            str(value),
            expected_type=artifact_type,
        )

    patched_assembly_ref = ref(
        "patched_assembly_ref", T2T_PATCHED_ASSEMBLY, required=False
    )
    patch_records_ref = ref("patch_records_ref", T2T_PATCH_RECORDS, required=False)
    patched_scan_ref = ref(
        "patched_telomere_scan_ref", T2T_ASSEMBLY_TELOMERE_SCAN, required=False
    )
    summary_ref = ref("telomere_status_summary_ref", T2T_STATUS_SUMMARY, required=True)
    report_ref = ref("t2t_finishing_report_ref", T2T_FINISHING_REPORT, required=True)
    bundle_ref = ref("bundle_ref", "t2t.bundle", required=True)
    assert summary_ref is not None and report_ref is not None and bundle_ref is not None
    artifact_refs = canonical_artifact_refs(
        (
            ref_value
            for ref_value in (
                patched_assembly_ref,
                patch_records_ref,
                patched_scan_ref,
                summary_ref,
                report_ref,
                bundle_ref,
            )
            if ref_value is not None
        )
    )
    status = out.get("status")
    if status not in {"completed", "completed_with_warnings"}:
        raise ResumeValidationError(
            f"t2t final evidence has invalid status: {status!r}"
        )
    return T2TReviewEffectV1(
        decision=decision,
        review_request_id=intent.review_request_id,
        review_request_hash=intent.request_hash,
        review_request_ref=intent.request_ref,
        selected_candidate_ids=tuple(selected_candidate_ids),
        decision_ref=decision_ref,
        notes=tuple(request.notes),
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        adopted_patch=adopted_patch,
        output_candidate_bundle_ref=output_candidate.ref,
        output_primary_ref=output_primary_ref,
        input_candidate_bundle_ref=source_candidate.ref if adopted_patch else None,
        input_primary_ref=source_candidate.primary_output_ref
        if adopted_patch
        else None,
        patched_assembly_ref=patched_assembly_ref,
        patch_records_ref=patch_records_ref,
        patched_telomere_scan_ref=patched_scan_ref,
        telomere_status_summary_ref=summary_ref,
        t2t_finishing_report_ref=report_ref,
        bundle_ref=bundle_ref,
        artifact_refs=artifact_refs,
        status=status,
    )


@overload
def _effect_ref_path(state: TopLevelState, ref: ArtifactRef) -> str: ...


@overload
def _effect_ref_path(state: TopLevelState, ref: None) -> None: ...


def _effect_ref_path(state: TopLevelState, ref: ArtifactRef | None) -> str | None:
    return str(resolve_run_ref(state, ref.path_ref)) if ref is not None else None
