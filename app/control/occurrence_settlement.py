"""Authoritative workflow-occurrence settlement shared by run and recovery."""

from __future__ import annotations

from pathlib import Path

from app.control.workflow_plan import workflow_step_by_id
from app.schemas.artifacts import ArtifactRef
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import ResourceDelivery, TopLevelState
from app.storage.artifacts import read_artifact_records
from app.storage.assembly_candidates import load_executed_candidate_bundle
from app.storage.ledger import Ledger


def settle_workflow_step_outcome(
    state: TopLevelState,
    *,
    data_dir: str | Path,
    require_explicit_advanced_outcome: bool = True,
) -> None:
    """Append or replay the explicit delivery and terminal facts.

    The checkpoint handoff is the only input that may create an occurrence
    terminal.  Both the parent controller and terminal recovery call this
    boundary so a crash between delivery and terminal append replays the same
    immutable facts instead of inferring completion from cursor movement.
    """

    session_id = state.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("occurrence settlement requires a session_id")
    outcome = state.get("workflow_step_outcome")
    entered_index = state.get("entered_workflow_step_index")
    workflow_index = state.get("workflow_step_index")
    if not isinstance(outcome, dict):
        if not require_explicit_advanced_outcome:
            return
        if state.get("supervision_state") == "failed_terminal":
            raise ValueError("failed workflow occurrence has no explicit outcome")
        if (
            type(entered_index) is int
            and entered_index >= 0
            and type(workflow_index) is int
            and workflow_index == entered_index + 1
        ):
            raise ValueError("workflow cursor advanced without an explicit outcome")
        return

    settled_step_id = str(outcome.get("workflow_step_id") or "")
    settled_step = workflow_step_by_id(state, settled_step_id)
    settled_stage = settled_step["stage"]
    selected_method_names = settled_step.get("selected_method_names")
    if outcome.get("stage") != settled_stage:
        raise ValueError("workflow_step_outcome stage conflicts with frozen plan")
    settled_index = next(
        index
        for index, step in enumerate(state.get("workflow_steps") or [])
        if step["step_id"] == settled_step_id
    )
    terminal_outcome = outcome.get("outcome")
    if terminal_outcome == "failed":
        valid_cursor = (
            workflow_index == settled_index and entered_index == settled_index
        )
    else:
        valid_cursor = (
            workflow_index == settled_index + 1 and entered_index == settled_index
        )
    if not valid_cursor:
        raise ValueError("workflow_step_outcome conflicts with the occurrence cursor")

    ledger = Ledger(data_dir)
    settled_cursor = (state.get("stage_outputs") or {}).get(settled_stage, {})
    settled_refs = ledger.references_from(
        settled_cursor,
        role_prefix=f"stage.{settled_stage}",
    )
    if terminal_outcome == "completed":
        deliveries = outcome.get("deliveries") or []
        assembly_projection = (state.get("resource_refs") or {}).get("assembly")
        assembly_delivered_here = False
        if isinstance(assembly_projection, dict):
            projected = ResourceRef.from_dict(assembly_projection)
            assembly_delivered_here = (
                projected.source_stage == settled_stage
                and projected.workflow_step_id == settled_step_id
            )
        if assembly_delivered_here and not deliveries:
            raise ValueError(
                "completed assembly producer omitted its immutable delivery"
            )
        for raw_delivery in deliveries:
            delivery = raw_delivery
            candidate_ref, primary_ref = _validate_assembly_delivery_projection(
                state,
                delivery,
                stage=settled_stage,
                workflow_step_id=settled_step_id,
                data_dir=str(data_dir),
            )
            ledger.record_resource_delivered(
                session_id,
                stage=settled_stage,
                workflow_step_id=settled_step_id,
                resource_type=str(delivery["resource_type"]),
                delivery_mode=delivery["delivery_mode"],
                candidate_bundle_ref=candidate_ref,
                primary_output_ref=primary_ref,
            )
        ledger.record_stage_completed(
            session_id,
            settled_stage,
            workflow_step_id=settled_step_id,
            refs=settled_refs,
            idempotency_key=f"stage-completed:{settled_step_id}",
            selected_method_names=selected_method_names,
        )
    elif terminal_outcome == "skipped":
        ledger.record_stage_skipped(
            session_id,
            settled_stage,
            workflow_step_id=settled_step_id,
            reason=str(outcome.get("reason") or ""),
            refs=settled_refs,
            idempotency_key=f"stage-skipped:{settled_step_id}",
            selected_method_names=selected_method_names,
        )
    elif terminal_outcome == "failed":
        ledger.record_stage_failed(
            session_id,
            settled_stage,
            workflow_step_id=settled_step_id,
            error=str(outcome.get("reason") or ""),
            refs=settled_refs,
            idempotency_key=f"stage-failed:{settled_step_id}",
            selected_method_names=selected_method_names,
        )
    else:
        raise ValueError("workflow_step_outcome has an invalid outcome")


def _validate_assembly_delivery_projection(
    state: TopLevelState,
    delivery: ResourceDelivery,
    *,
    stage: str,
    workflow_step_id: str,
    data_dir: str,
) -> tuple[ArtifactRef, ArtifactRef]:
    """Verify a delivery against the current projection and immutable bundle."""

    if delivery.get("resource_type") != "assembly":
        raise ValueError("only the v1 assembly delivery contract is supported")
    if delivery.get("delivery_mode") not in {"materialized", "reused"}:
        raise ValueError("assembly delivery mode is invalid")
    raw_resource = (state.get("resource_refs") or {}).get("assembly")
    if not isinstance(raw_resource, dict):
        raise ValueError("assembly delivery has no current ResourceRef projection")
    resource = ResourceRef.from_dict(raw_resource)
    if resource.source_stage != stage or resource.workflow_step_id != workflow_step_id:
        raise ValueError(
            "assembly ResourceRef is not bound to the delivering occurrence"
        )

    candidate_ref = ArtifactRef.model_validate(delivery.get("candidate_bundle_ref"))
    primary_ref = ArtifactRef.model_validate(delivery.get("primary_output_ref"))
    metadata_candidate = ArtifactRef.model_validate(
        resource.metadata.get("candidate_bundle_ref")
    )
    metadata_primary = ArtifactRef.model_validate(
        resource.metadata.get("primary_output_ref")
    )
    if metadata_candidate != candidate_ref or metadata_primary != primary_ref:
        raise ValueError(
            "assembly ResourceRef candidate relation differs from its delivery"
        )

    verified = load_executed_candidate_bundle(
        data_dir,
        state["session_id"],
        candidate_ref,
    )
    if verified.ref != candidate_ref or verified.primary_output_ref != primary_ref:
        raise ValueError(
            "assembly delivery does not match the verified executed candidate"
        )
    if len(resource.paths) != 1 or Path(resource.paths[0]).resolve(
        strict=False
    ) != verified.primary_output_path.resolve(strict=False):
        raise ValueError(
            "assembly ResourceRef path differs from the delivered primary output"
        )

    if delivery["delivery_mode"] == "materialized":
        records = read_artifact_records(data_dir, state["session_id"])
        by_artifact_id = {str(record.get("artifact_id")): record for record in records}
        for role, ref in (
            ("candidate bundle", candidate_ref),
            ("primary output", primary_ref),
        ):
            record = by_artifact_id.get(ref.artifact_id)
            if record is None or any(
                record.get(field) != getattr(ref, field)
                for field in ("path_ref", "sha256", "size_bytes", "artifact_type")
            ):
                raise ValueError(f"materialized {role} has no exact artifact record")
            if (
                record.get("stage") != stage
                or record.get("workflow_step_id") != workflow_step_id
            ):
                raise ValueError(
                    f"materialized {role} is not owned by the delivering occurrence"
                )
    return candidate_ref, primary_ref
