"""Assembly review stage nodes.

Assembly review is the main human gate.  It does not invent thresholds.
It collects or references evaluation signals and routes to:
- deterministic safe path (existing assembly input only)
- human review gate (multiple candidates or insufficient signals)
- failure (no assembly available)
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.control.lifecycle import ResumeValidationError
from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    current_workflow_step,
    current_workflow_step_id,
    fail_workflow_step,
    workflow_step_by_id,
)
from app.execution.database_resolution import (
    DatabaseResolutionError,
    resolve_ncbi_reference_dir,
)
from app.integrity import SHA256_PATTERN
from app.reference.ncbi import (
    build_related_species_evidence_pack as _build_related_species_evidence_pack,
)
from app.reference.ncbi import (
    build_related_species_statistics_pack as _build_related_species_statistics_pack,
)
from app.reference.ncbi import species_name_from_metadata
from app.review.application import (
    ForegroundReviewMetadataV1,
    PreparedReviewApplication,
    ReviewIntentV1,
    RunRelativeFileRef,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest, ReviewPayload, ReviewSignal
from app.review.candidates import candidate_ids_from_review_request_payload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_CANDIDATE_COMPARISON,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ASSEMBLY_REVIEW_ACTION,
    ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ASSEMBLY_REVIEW_PACKET,
    ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
    ArtifactMeta,
    ArtifactRef,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
    AssemblyReviewActionArtifact,
    ReviewDecisionArtifact,
    canonical_artifact_refs,
)
from app.schemas.plan import AssemblyReviewPlan
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import (
    ActiveInterrupt,
    StateUpdate,
    TopLevelState,
    validate_workflow_steps,
)
from app.stages.assembly.method_application.support import (
    ASSEMBLY_EXECUTION_CONTRACT_KEY,
    AssemblyExecutionContract,
)
from app.stages.assembly_review.method_application.support import (
    read_eval_bundle as _read_eval_bundle,
)
from app.stages.assembly_review.method_application.support import (
    verified_review_candidate as _verified_review_candidate,
)
from app.stages.assembly_review.method_application.support import (
    write_json_artifact as _write_json_artifact,
)
from app.stages.assembly_review.notebook import (
    build_assembly_review_notebook,
    render_statistics_figures,
    write_notebook,
)
from app.stages.assembly_review.state import (
    ASSEMBLY_CANDIDATE_CURSOR_ADAPTER,
    ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER,
    ASSEMBLY_REVIEW_CURSOR_ADAPTER,
    ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER,
    AssemblyCandidateCursor,
    AssemblyEvalSignalCursor,
    AssemblyReviewCursor,
    assembly_review_output,
    update_assembly_review_output,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    resolve_run_ref,
    session_paths,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import ASSEMBLY_CANDIDATES_SPEC
from app.storage.artifacts import (
    PublishedArtifact,
    artifact_ref_for_path,
    publish_artifact_record,
    record_registered_artifacts,
    recover_published_artifact,
    register_artifact_records,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
    publish_derived_assembly_candidate,
)
from app.storage.ledger import Ledger, project_stage_terminal_events
from app.workflow import is_terminal_failure

logger = logging.getLogger(__name__)

ASSEMBLY_REVIEW_CONTRACT_METHOD = "assembly_review.contract"


def enter_assembly_review(state: TopLevelState) -> StateUpdate:
    """Mark assembly_review as running.  No side effects."""
    return {"supervision_state": "running_stage"}


def resolve_candidates(state: TopLevelState) -> StateUpdate:
    """Resolve current assembly candidates from the central resource table."""
    candidates: list[AssemblyCandidateCursor] = []
    try:
        _validate_review_target_handoff(state)
    except (TypeError, ValueError) as exc:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": f"assembly review producer binding is invalid: {exc}",
                "candidate_count": 0,
                "candidates": [],
            },
        )
    assembly = resolve_stage_resource(
        state,
        resource_spec=ASSEMBLY_CANDIDATES_SPEC,
    )
    current_ref = assembly.metadata.get("candidate_bundle_ref")
    if current_ref is None:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": "assembly resource has no executed candidate bundle ref",
                "candidate_count": 0,
                "candidates": [],
            },
        )

    candidate_inputs = [
        ("candidate_bundle_ref", current_ref, assembly.source),
    ]
    raw_ref = assembly.metadata.get("raw_candidate_bundle_ref")
    if raw_ref is not None:
        candidate_inputs.append(
            ("raw_candidate_bundle_ref", raw_ref, f"{assembly.source}:raw")
        )
    verified_by_id: dict[str, VerifiedExecutedCandidate] = {}
    try:
        for metadata_key, raw_candidate_ref, source in candidate_inputs:
            verified = load_executed_candidate_bundle(
                absolute_data_dir(state),
                state["session_id"],
                raw_candidate_ref,
            )
            if verified.ref.artifact_id in verified_by_id:
                continue
            verified_by_id[verified.ref.artifact_id] = verified
            candidates.append(
                ASSEMBLY_CANDIDATE_CURSOR_ADAPTER.validate_python(
                    {
                        "candidate_id": verified.bundle.candidate_id,
                        "method_name": verified.bundle.method_name,
                        "source": source,
                        "status": "executed",
                        "bundle_ref": verified.ref.model_dump(mode="json"),
                        "primary_output_ref": (
                            verified.primary_output_ref.model_dump(mode="json")
                        ),
                    }
                )
            )
            projected_primary_key = (
                "primary_output_ref"
                if metadata_key == "candidate_bundle_ref"
                else "raw_primary_output_ref"
            )
            projected_primary = assembly.metadata.get(projected_primary_key)
            if projected_primary is None:
                raise ValueError(f"assembly resource has no {projected_primary_key}")
            if (
                ArtifactRef.model_validate(projected_primary)
                != verified.primary_output_ref
            ):
                raise ValueError(
                    f"assembly resource {projected_primary_key} does not match "
                    f"{metadata_key}"
                )
    except (OSError, TypeError, ValueError) as exc:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": f"assembly candidate handoff is invalid: {exc}",
                "candidate_count": 0,
                "candidates": [],
            },
        )

    current = verified_by_id[ArtifactRef.model_validate(current_ref).artifact_id]
    if [str(current.primary_output_path)] != list(assembly.paths):
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": (
                    "assembly resource paths do not match the current executed "
                    "candidate primary output"
                ),
                "candidate_count": 0,
                "candidates": [],
            },
        )

    # Build review plan
    review_plan = AssemblyReviewPlan(
        status="evaluating" if candidates else "failed",
        candidate_ids=[c["candidate_id"] for c in candidates],
        notes=[],
    )

    payload: dict[str, object] = {
        "status": "resolved",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "rejected_candidates": dict(assembly.metadata.get("rejected_candidates") or {}),
        "review_plan": review_plan.model_dump(mode="json"),
    }

    if not candidates:
        payload["status"] = "failed"
        payload["reason"] = "no_assembly_candidates_found"

    return update_assembly_review_output(state, payload)


def _validate_review_target_handoff(state: TopLevelState) -> None:
    """Verify the frozen producer's immutable assembly handoff from Ledger."""

    step = current_workflow_step(state)
    if step is None or step["stage"] != "assembly_review":
        raise ValueError("the active workflow step is not assembly_review")
    target = step.get("review_target")
    if not isinstance(target, dict):
        raise ValueError("assembly_review has no frozen review_target")
    producer_step_id = target.get("producer_step_id")
    if not isinstance(producer_step_id, str) or not producer_step_id:
        raise ValueError("assembly_review review_target has no producer_step_id")
    producer_step = workflow_step_by_id(state, producer_step_id)

    raw_assembly_ref = (state.get("resource_refs") or {}).get("assembly")
    if raw_assembly_ref is None:
        raise ValueError("the central assembly ResourceRef is missing")
    assembly_ref = (
        ResourceRef.from_dict(raw_assembly_ref)
        if isinstance(raw_assembly_ref, dict)
        else raw_assembly_ref
    )
    if assembly_ref.resource_type != "assembly":
        raise ValueError("the central ResourceRef is not an assembly")
    if assembly_ref.workflow_step_id != producer_step_id:
        raise ValueError(
            "current assembly producer step does not match frozen review_target: "
            f"expected {producer_step_id!r}, got "
            f"{assembly_ref.workflow_step_id!r}"
        )
    if assembly_ref.source_stage != producer_step["stage"]:
        raise ValueError(
            "current assembly producer stage does not match frozen review_target: "
            f"expected {producer_step['stage']!r}, got "
            f"{assembly_ref.source_stage!r}"
        )
    try:
        candidate_ref = ArtifactRef.model_validate(
            assembly_ref.metadata.get("candidate_bundle_ref")
        )
        primary_ref = ArtifactRef.model_validate(
            assembly_ref.metadata.get("primary_output_ref")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "the central assembly ResourceRef has no canonical candidate provenance"
        ) from exc

    ledger = Ledger(absolute_data_dir(state))
    terminals, diagnostics = project_stage_terminal_events(
        ledger.read_event_rows(state["session_id"])
    )
    if any(
        diagnostic.get("workflow_step_id") == producer_step_id
        for diagnostic in diagnostics
    ):
        raise ValueError(
            "frozen assembly producer has non-authoritative terminal history"
        )
    canonical = terminals.get((producer_step_id, producer_step["stage"]))
    if canonical is None or canonical.get("event_type") != "stage_completed":
        raise ValueError(
            "frozen assembly producer does not have a canonical stage_completed event"
        )
    deliveries = [
        event
        for event in ledger.read_event_rows(state["session_id"])
        if event.get("event_type") == "resource_delivered"
        and event.get("workflow_step_id") == producer_step_id
        and event.get("stage") == producer_step["stage"]
        and (event.get("summary") or {}).get("resource_type") == "assembly"
    ]
    if len(deliveries) != 1:
        raise ValueError(
            "frozen assembly producer must have exactly one immutable assembly delivery"
        )
    if (deliveries[0].get("summary") or {}).get("delivery_mode") not in {
        "materialized",
        "reused",
    }:
        raise ValueError("assembly delivery has an invalid delivery mode")
    delivery_refs = {
        ref.get("role"): ref
        for ref in deliveries[0].get("refs") or []
        if isinstance(ref, Mapping)
    }
    try:
        delivered_candidate_ref = ArtifactRef.model_validate(
            {
                key: value
                for key, value in dict(
                    delivery_refs.get("candidate_bundle_ref") or {}
                ).items()
                if key != "role"
            }
        )
        delivered_primary_ref = ArtifactRef.model_validate(
            {
                key: value
                for key, value in dict(
                    delivery_refs.get("primary_output_ref") or {}
                ).items()
                if key != "role"
            }
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "assembly delivery has invalid immutable artifact refs"
        ) from exc
    if candidate_ref != delivered_candidate_ref or primary_ref != delivered_primary_ref:
        raise ValueError(
            "central assembly candidate provenance does not match frozen delivery"
        )
    if list(assembly_ref.paths) != [
        str(session_paths(state).resolve_ref(delivered_primary_ref.path_ref))
    ]:
        raise ValueError("central assembly path does not match frozen delivery")


def plan_review_metrics(state: TopLevelState) -> StateUpdate:
    """Plan evaluation signals based on available inputs.

    Plans signals only when inputs exist; does not execute tools.
    Thresholds are not stored here.
    """
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = assembly_review_output(state)

    if ar_out.get("status") == "failed":
        return {}

    # Determine available inputs from asset registry or prior stages
    _has_short_reads, _has_long_reads, available_inputs, missing_inputs = (
        _available_review_inputs(state)
    )

    read_qc_out = stage_outputs.get("read_qc", {})
    if read_qc_out:
        if read_qc_out.get("clean_long_reads") is not None:
            available_inputs.add("clean_long_reads")
        if read_qc_out.get("clean_short_dna_reads") is not None:
            available_inputs.add("clean_short_dna_reads")

    planned_signals: list[str] = ["quast", "busco"]

    review_plan = AssemblyReviewPlan.model_validate(
        {
            **ar_out.get("review_plan", {}),
            "planned_signals": planned_signals,
            "status": "evaluating",
        }
    )

    eval_signals: list[AssemblyEvalSignal] = []
    candidate_ids = [
        candidate.get("candidate_id", "unknown")
        for candidate in ar_out.get("candidates", [])
    ]
    for signal_name in planned_signals:
        method_name = _review_signal_method_name(signal_name)
        for candidate_id in candidate_ids:
            eval_signals.append(
                AssemblyEvalSignal(
                    signal_name=signal_name,
                    method_name=method_name,
                    candidate_id=candidate_id,
                    status="pending",
                    notes=["execution_deferred_to_method_specific_task"],
                )
            )

    eval_bundle = AssemblyEvalBundle(
        session_id=state["session_id"],
        candidate_ids=candidate_ids,
        signals=eval_signals,
        available_inputs=sorted(available_inputs),
        missing_inputs=sorted(missing_inputs),
        notes=["thresholds_not_applied_without_human_approval"],
    )
    eval_bundle_ref = _write_json_artifact(
        state,
        "assembly_eval_bundle.planned.json",
        eval_bundle.model_dump(mode="json"),
        artifact_type=ASSEMBLY_REVIEW_EVAL_BUNDLE,
    )

    ar_out["review_plan"] = ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER.validate_python(
        review_plan.model_dump(mode="json")
    )
    ar_out["planned_signals"] = planned_signals
    ar_out["signals"] = [
        ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER.validate_python(
            signal.model_dump(mode="json")
        )
        for signal in eval_signals
    ]
    ar_out["eval_bundle_ref"] = eval_bundle_ref

    return update_assembly_review_output(state, ar_out)


def evaluate_review_rules(state: TopLevelState) -> StateUpdate:
    """Evaluate structural rules and record candidate count for routing."""
    ar_out = assembly_review_output(state)

    if ar_out.get("status") == "failed":
        return {}

    candidates = ar_out.get("candidates", [])
    ar_out["candidate_count"] = len(candidates)

    return update_assembly_review_output(state, ar_out)


def parameter_quality_gate(state: TopLevelState) -> StateUpdate:
    """Record an Assembly Review-owned parameter-quality diagnosis."""

    stage_outputs = dict(state.get("stage_outputs") or {})
    assembly_out = dict(stage_outputs.get("assembly") or {})
    if not assembly_out.get("parameter_attempts"):
        return {}

    ar_out = assembly_review_output(state)
    metrics = _phase10_review_metrics(ar_out, assembly_out)
    diagnosis = _parameter_quality_diagnosis(metrics)
    update: dict[str, Any] = {
        "parameter_quality_gate": diagnosis,
        "parameter_quality_checked": True,
    }
    if metrics and not ar_out.get("metrics"):
        update["metrics"] = metrics
    return update_assembly_review_output(state, update)


def build_related_species_evidence_pack(state: TopLevelState) -> StateUpdate:
    """Build non-blocking local NCBI evidence for the review report and LLM."""
    ar_out = assembly_review_output(state)
    if is_terminal_failure(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out, state=state)
    if reference_dir is None:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": "frozen NCBI reference database is unavailable",
            },
        )
    evidence_pack = _build_related_species_evidence_pack(
        species_name, reference_dir=reference_dir
    )
    existing_ref = ar_out.get("related_species_evidence_pack_ref")
    if existing_ref and _json_artifact_payload_matches(existing_ref, evidence_pack):
        return {}

    evidence_ref = _write_json_artifact(
        state,
        "related_species_evidence_pack.json",
        evidence_pack,
        artifact_type=ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    )
    return update_assembly_review_output(
        state,
        {
            "related_species_evidence_pack": evidence_pack,
            "related_species_evidence_pack_ref": evidence_ref,
        },
    )


def _related_species_reference_dir(
    ar_out: AssemblyReviewCursor,
    *,
    state: TopLevelState | None = None,
) -> str | Path | None:
    """Return the NCBI reference path from the frozen database contract.

    The explicit stage-output value remains only for isolated node tests and
    old checkpoints. A committed run always has a frozen resolver, so runtime
    code never consults an environment variable or a module default path.
    """
    if state is not None:
        try:
            from app.execution.dependency_snapshot import (
                load_frozen_dependency_resolver,
            )

            resolver = load_frozen_dependency_resolver(state.get("data_dir", "data"))
            return resolve_ncbi_reference_dir(
                resolver,
                species_name=_target_species_name(state),
            )
        except (DatabaseResolutionError, OSError, TypeError, ValueError) as exc:
            try:
                from app.storage.frozen_inputs import has_frozen_input_contract
                from app.storage.paths import SessionPaths

                committed = has_frozen_input_contract(
                    SessionPaths.from_run_dir(state.get("data_dir", ""))
                )
            except (OSError, TypeError, ValueError):
                committed = False
            if committed:
                logger.error("frozen NCBI database unavailable: %s", exc)
                return None
            # Explicit path is retained only for isolated pre-freeze tests and
            # migration tooling; it is never consulted by a committed run.
    value = ar_out.get("related_species_reference_dir")
    if not value:
        return None
    ref_path = Path(value)
    if ref_path.is_dir() and (ref_path / "reference_manifest.json").is_file():
        return ref_path
    return None


def build_related_species_statistics_and_notebook(
    state: TopLevelState,
) -> StateUpdate:
    """Build statistics pack and comparative notebook for assembly review."""
    ar_out = assembly_review_output(state)
    if is_terminal_failure(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out, state=state)
    if reference_dir is None:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": "frozen NCBI reference database is unavailable",
            },
        )

    target_metrics = _extract_target_metrics_from_signals(ar_out)

    statistics_pack = _build_related_species_statistics_pack(
        species_name, reference_dir=reference_dir, target_metrics=target_metrics
    )

    existing_stats_ref = ar_out.get("related_species_statistics_pack_ref")
    statistics_pack_unchanged = existing_stats_ref and _json_artifact_payload_matches(
        existing_stats_ref, statistics_pack
    )

    if statistics_pack_unchanged:
        existing_notebook_ref = ar_out.get("assembly_review_comparative_notebook_ref")
        if existing_notebook_ref and Path(existing_notebook_ref).exists():
            comparative_figures = ar_out.get("comparative_figures", {})
            all_figures_exist = all(
                Path(p).exists() for p in comparative_figures.values()
            )
            if all_figures_exist:
                # Full skip: re-register artifacts for ledger consistency, then return
                assert isinstance(existing_stats_ref, str)
                data_dir = absolute_data_dir(state)
                session_id = state["session_id"]

                notebook_artifact = ArtifactMeta.from_file(
                    Path(existing_notebook_ref),
                    stage="assembly_review",
                    method_name="assembly_review.contract",
                    artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
                    metadata={"session_id": session_id},
                )
                register_artifact_records(data_dir, session_id, [notebook_artifact])

                figure_artifacts = [
                    ArtifactMeta.from_file(
                        Path(abs_path),
                        stage="assembly_review",
                        method_name="assembly_review.contract",
                        artifact_type=f"assembly_review.figure.{logical_name}",
                        metadata={"figure_type": logical_name},
                    )
                    for logical_name, abs_path in comparative_figures.items()
                ]
                if figure_artifacts:
                    register_artifact_records(data_dir, session_id, figure_artifacts)
                statistics_artifact_ref = artifact_ref_for_path(
                    data_dir,
                    session_id,
                    existing_stats_ref,
                    expected_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
                )
                record_registered_artifacts(
                    data_dir,
                    session_id,
                    [statistics_artifact_ref, notebook_artifact, *figure_artifacts],
                )

                return {}

    if statistics_pack_unchanged:
        statistics_ref = existing_stats_ref
    else:
        statistics_ref = _write_json_artifact(
            state,
            "related_species_statistics_pack.json",
            statistics_pack,
            artifact_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
        )

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    figures_dir = stage_artifacts_dir(state, "assembly_review", "figures")
    work_figures_dir = stage_work_dir(state, "assembly_review", "figures")
    figure_sources = render_statistics_figures(
        statistics_pack,
        work_figures_dir,
        target_metrics=target_metrics,
    )

    figure_paths: dict[str, str] = {}
    figure_refs: list[ArtifactRef] = []
    for logical_name, abs_path in figure_sources.items():
        source = Path(abs_path)
        artifact = ArtifactMeta.from_file(
            source,
            stage="assembly_review",
            method_name="assembly_review.contract",
            artifact_type=f"assembly_review.figure.{logical_name}",
            metadata={"figure_type": logical_name},
        )
        published = publish_artifact_record(
            data_dir,
            session_id,
            artifact,
            destination=figures_dir / source.name,
        )
        figure_paths[logical_name] = str(published.path)
        figure_refs.append(published.ref)

    out_dir = stage_artifacts_dir(state, "assembly_review")
    statistics_pack_relpath = "related_species_statistics_pack.json"
    figures_relpaths = {
        name: f"figures/{Path(path).name}" for name, path in figure_paths.items()
    }
    evidence_pack = ar_out.get("related_species_evidence_pack", {})

    notebook = build_assembly_review_notebook(
        statistics_pack=statistics_pack,
        evidence_pack=evidence_pack,
        target_metrics=target_metrics,
        session_id=session_id,
        statistics_pack_relpath=statistics_pack_relpath,
        figures_relpaths=figures_relpaths,
    )

    notebook_path = out_dir / "assembly_review_report.ipynb"
    notebook_source = stage_work_dir(
        state, "assembly_review", "assembly_review_report.ipynb"
    )
    write_notebook(notebook, notebook_source)

    notebook_artifact = ArtifactMeta.from_file(
        notebook_source,
        stage="assembly_review",
        method_name="assembly_review.contract",
        artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
        metadata={"session_id": session_id, "figure_count": len(figure_paths)},
    )
    published_notebook = publish_artifact_record(
        data_dir,
        session_id,
        notebook_artifact,
        destination=notebook_path,
    )
    notebook_path = published_notebook.path
    if not isinstance(statistics_ref, str):
        raise ValueError("assembly review statistics pack has no artifact path")
    statistics_artifact_ref = artifact_ref_for_path(
        data_dir,
        session_id,
        statistics_ref,
        expected_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
    )
    record_registered_artifacts(
        data_dir,
        session_id,
        [statistics_artifact_ref, *figure_refs, published_notebook.ref],
    )

    return update_assembly_review_output(
        state,
        {
            "related_species_statistics_pack": statistics_pack,
            "related_species_statistics_pack_ref": statistics_ref,
            "assembly_review_comparative_notebook_ref": str(notebook_path),
            "comparative_figures": figure_paths,
        },
    )


def _extract_target_metrics_from_signals(
    ar_out: AssemblyReviewCursor,
) -> dict[str, float] | None:
    """Extract target assembly metrics from available signals for percentile"""
    """computation.

    Returns None when multiple candidates exist (no single target to position).
    """
    candidates = ar_out.get("candidates", [])
    if len(candidates) != 1:
        return None

    candidate_id = candidates[0].get("candidate_id")
    if not candidate_id:
        return None

    signals = ar_out.get("signals", [])
    candidate_metrics = _candidate_metrics(candidate_id, signals)

    target_metrics: dict[str, float] = {}

    # Map QUAST metrics
    quast = candidate_metrics.get("quast", {})
    if quast.get("n50") is not None:
        target_metrics["n50_bp"] = float(quast["n50"])
    if quast.get("total_length") is not None:
        target_metrics["assembly_size_bp"] = float(quast["total_length"])
    if quast.get("gc_percent") is not None:
        target_metrics["gc_percent"] = float(quast["gc_percent"])

    # Map BUSCO metrics
    busco = candidate_metrics.get("busco", {})
    if busco.get("complete_percent") is not None:
        target_metrics["busco_complete_percent"] = float(busco["complete_percent"])
    if busco.get("duplicated_percent") is not None:
        target_metrics["busco_duplicated_percent"] = float(busco["duplicated_percent"])

    return target_metrics if target_metrics else None


def _phase10_review_metrics(
    ar_out: AssemblyReviewCursor, assembly_out: dict[str, Any]
) -> dict[str, Any]:
    if isinstance(ar_out.get("metrics"), dict):
        return dict(ar_out["metrics"])
    candidates = ar_out.get("candidates") or []
    signals = ar_out.get("signals") or []
    candidate_id = None
    if candidates and isinstance(candidates[0], dict):
        candidate_id = candidates[0].get("candidate_id")
    if not candidate_id and signals and isinstance(signals[0], dict):
        candidate_id = signals[0].get("candidate_id")
    if not candidate_id:
        return {}
    nested = _candidate_metrics(str(candidate_id), signals)
    quast = dict(nested.get("quast") or {})
    busco = dict(nested.get("busco") or {})
    metrics: dict[str, Any] = {}
    if quast.get("total_length") is not None:
        metrics["assembly_size"] = quast.get("total_length")
        genome_size = assembly_out.get("genome_size_prior")
        if genome_size:
            try:
                metrics["assembly_size_ratio"] = float(quast["total_length"]) / float(
                    genome_size
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    if quast.get("n50") is not None:
        metrics["contig_n50"] = quast.get("n50")
    if quast.get("contig_count") is not None:
        metrics["contig_count"] = quast.get("contig_count")
    if busco.get("complete_percent") is not None:
        metrics["busco_complete"] = busco.get("complete_percent")
    if busco.get("duplicated_percent") is not None:
        metrics["busco_duplicate"] = busco.get("duplicated_percent")
    return metrics


def _parameter_quality_diagnosis(metrics: dict[str, Any]) -> dict[str, Any]:
    """Classify review signals without changing Assembly attempt evidence."""
    complete = _float_metric(metrics, "busco_complete", "busco_complete_ratio")
    duplicate = _float_metric(metrics, "busco_duplicate", "busco_duplicate_ratio")
    size_ratio = _float_or_none(metrics.get("assembly_size_ratio"))
    n50 = _float_metric(metrics, "contig_n50", "n50")
    contig_count = _float_or_none(metrics.get("contig_count"))
    normalized_complete = _as_ratio(complete)
    normalized_duplicate = _as_ratio(duplicate)
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


def _float_metric(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(metrics.get(key))
        if value is not None:
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100.0 if value > 1 else value


def _assembly_review_accept_note(state: TopLevelState) -> str:
    """Describe the actual frozen continuation after this review occurrence."""

    steps = validate_workflow_steps(state.get("workflow_steps"))
    index = state.get("workflow_step_index")
    if type(index) is not int or not 0 <= index < len(steps):
        raise ValueError("workflow_step_index is outside workflow_steps")
    if index + 1 < len(steps):
        return (
            "Accept continues with the selected assembly to the frozen next stage: "
            f"{steps[index + 1]['stage']}."
        )
    return "Accept completes the frozen workflow with the selected assembly."


def human_review_gate(state: TopLevelState) -> StateUpdate:
    """Set an active interrupt for unresolved assembly review decisions."""
    ar_out = assembly_review_output(state)
    ar_out, report_refs, comparison_context = _ensure_assembly_review_reports(
        state, ar_out
    )
    candidates = ar_out.get("candidates", [])
    signals = ar_out.get("signals", [])
    candidate_payloads: list[dict[str, Any]] = [
        dict(candidate) for candidate in candidates
    ]

    payload = ReviewPayload(
        stage="assembly_review",
        gate_id="assembly_review",
        reason="assembly_review_human_decision_required",
        candidates=candidate_payloads,
        signals=[ReviewSignal.model_validate(s) for s in signals],
        decisions_allowed=[
            "accept",
            "stop_human_review",
        ],
        notes=[
            "Thresholds for BUSCO and QUAST require human approval before application.",
            _assembly_review_accept_note(state),
            (
                "Assembly execution-failure retries use the v3 recommendation, "
                "approval, and frozen-attempt workflow before this gate."
            ),
            "Stop leaves downstream stages unstarted.",
        ],
        auto_decidable=_assembly_review_auto_decidable(ar_out),
        report_refs=report_refs,
        candidate_comparison_context=comparison_context,
        related_species_evidence_pack=dict(
            ar_out.get("related_species_evidence_pack") or {}
        ),
    )

    persisted_request = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason="assembly_review_human_decision_required",
        payload=payload,
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type="assembly_review.request",
    )
    request_ref = persisted_request.request_ref
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    ledger.record_review_requested(
        state["session_id"],
        "assembly_review",
        reason="assembly_review_human_decision_required",
        signals=[s.model_dump(mode="json") for s in payload.signals],
        candidates=candidate_payloads,
        review_request_artifact_ref=request_ref.model_dump(mode="json"),
        auto_decidable=payload.auto_decidable,
        review_request_id=persisted_request.review_request_id,
    )

    review_update = update_assembly_review_output(
        state,
        {
            **ar_out,
            "status": "blocked_human",
            "review_request_artifact_ref": request_ref.model_dump(mode="json"),
            "auto_decidable": payload.auto_decidable,
        },
    )
    blocked_update: StateUpdate = {
        "active_interrupt": cast(
            ActiveInterrupt,
            persisted_request.compact_interrupt(
                stage="assembly_review",
                gate_id="assembly_review",
                reason="assembly_review_human_decision_required",
            ),
        ),
        "supervision_state": "blocked_human",
        **review_update,
    }
    return blocked_update


class AssemblyReviewIntentMetadataV1(ForegroundReviewMetadataV1):
    """Strict decision-owned metadata accepted by Assembly Review."""

    action_output_ref: RunRelativeFileRef | None = None
    evidence_refs: tuple[RunRelativeFileRef, ...] = Field(default=(), max_length=1_000)
    evidence_notes: tuple[str, ...] = Field(default=(), max_length=1_000)
    human_approved_action: Literal[True] | None = None
    approval: Literal["human"] | None = None
    method_name: str | None = None
    provenance_ref: RunRelativeFileRef | None = None
    short_read_evidence_refs: tuple[RunRelativeFileRef, ...] = Field(
        default=(), max_length=1_000
    )
    short_read_evidence_notes: tuple[str, ...] = Field(default=(), max_length=1_000)
    available_signals: tuple[str, ...] = Field(default=(), max_length=1_000)
    missing_or_pending_signals: tuple[str, ...] = Field(default=(), max_length=1_000)


class AssemblyReviewEffectV1(BaseModel):
    """The bounded, durable Assembly Review projection trusted by checkpoint apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision: Literal[
        "accept",
        "pass",
        "purge",
        "polish",
        "continue_low_confidence",
        "stop_human_review",
    ]
    review_request_id: str = Field(min_length=1)
    review_request_hash: str = Field(pattern=SHA256_PATTERN)
    review_request_ref: ArtifactRef
    rejected_candidates: tuple[str, ...] = ()
    selected_candidate_id: str | None = None
    pre_action_assembly_ref: RunRelativeFileRef | None = None
    final_assembly_ref: RunRelativeFileRef | None = None
    output_assembly_ref: RunRelativeFileRef | None = None
    action_type: Literal["pass", "pass_low_confidence", "purge", "polish"] | None = None
    action_execution_mode: Literal["none", "external"] | None = None
    action_method_name: str | None = None
    evidence_refs: tuple[RunRelativeFileRef, ...] = ()
    evidence_notes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    decided_by: Literal["human", "llm_provisional", "auto_rule"] = "human"
    rationale_artifact_ref: ArtifactRef | None = None
    decision_ref: ArtifactRef
    action_ref: ArtifactRef | None = None
    candidate_bundle_ref: ArtifactRef | None = None
    primary_output_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "AssemblyReviewEffectV1":
        if self.rejected_candidates != tuple(sorted(set(self.rejected_candidates))):
            raise ValueError(
                "assembly_review rejected_candidates must be sorted and unique"
            )
        if self.decision == "stop_human_review":
            stop_values = (
                self.selected_candidate_id,
                self.pre_action_assembly_ref,
                self.final_assembly_ref,
                self.output_assembly_ref,
                self.action_type,
                self.action_execution_mode,
                self.action_ref,
                self.candidate_bundle_ref,
                self.primary_output_ref,
            )
            if any(value is not None for value in stop_values):
                raise ValueError("stop_human_review effect cannot select an assembly")
            return self
        selected_values = (
            self.selected_candidate_id,
            self.pre_action_assembly_ref,
            self.final_assembly_ref,
            self.output_assembly_ref,
            self.action_type,
            self.action_execution_mode,
            self.action_ref,
            self.candidate_bundle_ref,
            self.primary_output_ref,
        )
        if any(value is None for value in selected_values):
            raise ValueError("selected assembly effect is missing durable provenance")
        return self


ASSEMBLY_REVIEW_EFFECT_KIND = "assembly_review.review_application.v1"


def prepare_assembly_review_application(
    state: TopLevelState, intent: ReviewIntentV1
) -> PreparedReviewApplication:
    """Finish all durable Assembly Review work before checkpoint projection.

    This is intentionally the only Assembly Review decision path that touches
    artifacts or candidate bundles.  It is recover-first: stable destinations
    are recovered before external action output is read or republished.
    """

    if intent.stage != "assembly_review" or intent.gate_id != "assembly_review":
        raise ResumeValidationError("assembly review application has the wrong gate")
    _require_assembly_review_intent_binding(state, intent)
    request = validate_assembly_review_intent_request(
        state,
        intent.request,
        load_review_request(
            state["data_dir"], intent.request_ref, session_id=state["session_id"]
        ),
    )
    if request.decision == "stop_human_review":
        return _prepare_stopped_assembly_review_application(state, intent, request)

    allowed = {"accept", "pass", "purge", "polish", "continue_low_confidence"}
    if request.decision not in allowed:
        raise ResumeValidationError(
            f"unsupported assembly_review decision: {request.decision}"
        )
    if not request.candidate_id:
        raise ResumeValidationError("assembly_review resume requires candidate_id")
    selected_decision = cast(
        Literal["accept", "pass", "purge", "polish", "continue_low_confidence"],
        request.decision,
    )

    ar_out = assembly_review_output(state)
    candidate = next(
        (
            candidate
            for candidate in ar_out.get("candidates") or []
            if candidate.get("candidate_id") == request.candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ResumeValidationError(
            f"unknown assembly_review candidate_id: {request.candidate_id}"
        )
    selected = _verified_review_candidate(state, candidate)
    paths = session_paths(state)
    pre_action_path = str(selected.primary_output_path)
    action_payload = _prepare_assembly_review_action_payload(
        state,
        request,
        pre_action_path,
    )
    action_type = cast(
        Literal["pass", "pass_low_confidence", "purge", "polish"],
        action_payload["action_type"],
    )
    action_execution_mode = cast(
        Literal["none", "external"], action_payload["action_execution_mode"]
    )

    output_path = pre_action_path
    output_candidate = selected
    if action_execution_mode == "external":
        published_action = _publish_review_action_output(
            state,
            selected,
            str(action_payload["final_assembly_ref"]),
            action_type=action_type,
            review_request_id=intent.review_request_id,
        )
        output_candidate = _recover_or_publish_derived_review_candidate(
            state,
            selected,
            primary_output_ref=published_action.ref,
            action_type=action_type,
            review_request_id=intent.review_request_id,
        )
        output_path = str(output_candidate.primary_output_path)

    pre_action_ref = RunRelativeFileRef(paths.to_ref(pre_action_path))
    final_assembly_ref = RunRelativeFileRef(paths.to_ref(output_path))
    evidence_refs = tuple(
        RunRelativeFileRef.model_validate(value)
        for value in action_payload.get("evidence_refs") or []
    )
    rationale_ref = request.rationale_artifact_ref
    action_metadata = {
        "action_type": action_type,
        "action_execution_mode": action_execution_mode,
        "pre_action_primary_output_ref": selected.primary_output_ref.model_dump(
            mode="json"
        ),
        "post_action_primary_output_ref": (
            output_candidate.primary_output_ref.model_dump(mode="json")
        ),
        "output_primary_ref": output_candidate.primary_output_ref.model_dump(
            mode="json"
        ),
        "method_name": action_payload.get("action_method_name"),
        "evidence_refs": [reference.path_ref for reference in evidence_refs],
        "evidence_notes": list(action_payload.get("evidence_notes") or []),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": (
            rationale_ref.model_dump(mode="json") if rationale_ref is not None else None
        ),
    }
    decision_ref = _persist_assembly_review_decision(
        state,
        intent,
        ReviewDecisionArtifact(
            session_id=state["session_id"],
            stage="assembly_review",
            decision=selected_decision,
            candidate_id=request.candidate_id,
            rejected_candidates=request.rejected_candidates,
            final_assembly_ref=final_assembly_ref.path_ref,
            notes=request.notes,
            metadata=action_metadata,
        ),
    )
    action_ref = _persist_assembly_review_action(
        state,
        AssemblyReviewActionArtifact(
            session_id=state["session_id"],
            decision=selected_decision,
            action_type=action_type,
            candidate_id=request.candidate_id,
            pre_action_assembly_ref=pre_action_ref.path_ref,
            post_action_assembly_ref=final_assembly_ref.path_ref,
            output_assembly_ref=final_assembly_ref.path_ref,
            action_execution_mode=action_execution_mode,
            method_name=cast(str | None, action_payload.get("action_method_name")),
            evidence_refs=[reference.path_ref for reference in evidence_refs],
            evidence_notes=list(action_payload.get("evidence_notes") or []),
            notes=request.notes,
            metadata={
                "review_decision_artifact_ref": decision_ref.model_dump(mode="json")
            },
        ),
    )
    effect = AssemblyReviewEffectV1(
        decision=selected_decision,
        review_request_id=intent.review_request_id,
        review_request_hash=intent.request_hash,
        review_request_ref=intent.request_ref,
        rejected_candidates=tuple(request.rejected_candidates),
        selected_candidate_id=request.candidate_id,
        pre_action_assembly_ref=pre_action_ref,
        final_assembly_ref=final_assembly_ref,
        output_assembly_ref=final_assembly_ref,
        action_type=action_type,
        action_execution_mode=action_execution_mode,
        action_method_name=cast(str | None, action_payload.get("action_method_name")),
        evidence_refs=evidence_refs,
        evidence_notes=tuple(action_payload.get("evidence_notes") or []),
        notes=tuple(request.notes),
        decided_by=request.decided_by,
        rationale_artifact_ref=rationale_ref,
        decision_ref=decision_ref,
        action_ref=action_ref,
        candidate_bundle_ref=output_candidate.ref,
        primary_output_ref=output_candidate.primary_output_ref,
    )
    return PreparedReviewApplication(
        effect_kind=ASSEMBLY_REVIEW_EFFECT_KIND,
        effect=effect,
        decision_ref=decision_ref,
        required_refs=canonical_artifact_refs(
            (
                action_ref,
                output_candidate.ref,
                output_candidate.primary_output_ref,
                *(() if rationale_ref is None else (rationale_ref,)),
            )
        ),
    )


def validate_assembly_review_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: Mapping[str, Any],
) -> ResumeReviewRequest:
    """Validate and canonicalize one Assembly Review intent request."""

    allowed = {
        "accept",
        "pass",
        "purge",
        "polish",
        "continue_low_confidence",
        "stop_human_review",
    }
    if request.decision not in allowed:
        raise ResumeValidationError(
            f"unsupported assembly_review decision: {request.decision}"
        )
    if request.decision == "stop_human_review":
        metadata = dict(request.metadata)
        for field_name in (
            "action_output_ref",
            "evidence_refs",
            "evidence_notes",
            "human_approved_action",
            "approval",
            "method_name",
            "provenance_ref",
            "short_read_evidence_refs",
            "short_read_evidence_notes",
        ):
            metadata.pop(field_name, None)
        return request.model_copy(
            update={
                "candidate_id": None,
                "candidate_ids": [],
                "rejected_candidates": [],
                "method_name": None,
                "metadata": metadata,
            }
        )
    payload = request_record.get("payload")
    immutable_candidate_ids = candidate_ids_from_review_request_payload(payload)
    state_candidates = [
        candidate
        for candidate in assembly_review_output(state).get("candidates") or []
        if isinstance(candidate, dict)
        and isinstance(candidate.get("candidate_id"), str)
    ]
    state_candidate_ids = {candidate["candidate_id"] for candidate in state_candidates}
    if not request.candidate_id:
        raise ResumeValidationError("assembly_review resume requires candidate_id")
    if request.candidate_id not in immutable_candidate_ids:
        raise ResumeValidationError(
            f"unknown assembly_review candidate_id: {request.candidate_id}"
        )
    if request.candidate_id not in state_candidate_ids:
        raise ResumeValidationError(
            "assembly_review selected candidate is no longer available"
        )
    rejected_candidates = tuple(sorted(set(request.rejected_candidates)))
    unknown_rejected = sorted(
        set(rejected_candidates).difference(immutable_candidate_ids)
    )
    if unknown_rejected:
        raise ResumeValidationError(
            "unknown assembly_review rejected_candidates: "
            f"{', '.join(unknown_rejected)}"
        )
    if request.candidate_id in rejected_candidates:
        raise ResumeValidationError(
            "assembly_review selected candidate cannot be rejected"
        )
    selected_candidate = next(
        candidate
        for candidate in state_candidates
        if candidate["candidate_id"] == request.candidate_id
    )
    selected = _verified_review_candidate(state, selected_candidate)
    metadata = dict(request.metadata)
    if metadata.pop("approval", None) == "human":
        metadata["human_approved_action"] = True
    canonical = request.model_copy(
        update={
            "rejected_candidates": list(rejected_candidates),
            "metadata": metadata,
        }
    )
    _prepare_assembly_review_action_payload(
        state,
        canonical,
        str(selected.primary_output_path),
    )
    return canonical


def apply_assembly_review_application(
    state: TopLevelState, application: VerifiedReviewApplication
) -> StateUpdate:
    """Purely project a verified Assembly Review effect into checkpoint state."""

    if application.application.effect_kind != ASSEMBLY_REVIEW_EFFECT_KIND:
        raise ResumeValidationError(
            "assembly_review application has the wrong effect kind"
        )
    if not isinstance(application.effect, AssemblyReviewEffectV1):
        raise ResumeValidationError(
            "assembly_review application has the wrong effect model"
        )
    effect = application.effect
    if (
        application.application.stage != "assembly_review"
        or application.application.gate_id != "assembly_review"
    ):
        raise ResumeValidationError("assembly_review application has the wrong gate")

    if effect.decision == "stop_human_review":
        reason = "assembly_review stopped by human review decision"
        review_update = update_assembly_review_output(
            state,
            {
                **assembly_review_output(state),
                "status": "failed_terminal",
                "decision": effect.decision,
                "review_request_id": effect.review_request_id,
                "review_request_hash": effect.review_request_hash,
                "review_request_artifact_ref": effect.review_request_ref.model_dump(
                    mode="json"
                ),
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
            **fail_workflow_step(state, "assembly_review", reason),
            "current_stage": "assembly_review",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            **review_update,
            "last_error": reason,
        }

    if (
        effect.action_ref is None
        or effect.candidate_bundle_ref is None
        or effect.primary_output_ref is None
    ):
        raise ResumeValidationError(
            "assembly_review selected effect is missing durable provenance"
        )
    if (
        effect.output_assembly_ref is None
        or effect.final_assembly_ref is None
        or effect.pre_action_assembly_ref is None
    ):
        raise ResumeValidationError(
            "assembly_review selected effect is missing run-relative file refs"
        )
    paths = session_paths(state)
    output_path = str(paths.resolve_ref(effect.output_assembly_ref.path_ref))
    final_path = str(paths.resolve_ref(effect.final_assembly_ref.path_ref))
    pre_action_path = str(paths.resolve_ref(effect.pre_action_assembly_ref.path_ref))
    review_payload: dict[str, object] = {
        **assembly_review_output(state),
        "status": "completed",
        "decision": effect.decision,
        "selected_candidate_id": effect.selected_candidate_id,
        "rejected_candidates": list(effect.rejected_candidates),
        "final_assembly_ref": final_path,
        "pre_action_assembly_ref": pre_action_path,
        "action_type": effect.action_type,
        "action_execution_mode": effect.action_execution_mode,
        "action_method_name": effect.action_method_name,
        "evidence_refs": [reference.path_ref for reference in effect.evidence_refs],
        "evidence_notes": list(effect.evidence_notes),
        "notes": list(effect.notes),
        "decided_by": effect.decided_by,
        "rationale_artifact_ref": (
            effect.rationale_artifact_ref.path_ref
            if effect.rationale_artifact_ref is not None
            else None
        ),
        "review_request_id": effect.review_request_id,
        "review_request_hash": effect.review_request_hash,
        "review_request_artifact_ref": effect.review_request_ref.model_dump(
            mode="json"
        ),
        "review_decision_artifact_ref": effect.decision_ref.model_dump(mode="json"),
        "assembly_review_action_artifact_ref": effect.action_ref.path_ref,
    }
    if effect.action_type == "purge":
        review_payload["purged_assembly"] = final_path
    if effect.action_type == "polish":
        review_payload["polished_assembly"] = final_path
    review_update = update_assembly_review_output(state, review_payload)
    return {
        **advance_workflow_step(
            state,
            "assembly_review",
            outcome="completed",
            deliveries=(
                assembly_delivery(
                    effect.candidate_bundle_ref.model_dump(mode="json"),
                    effect.primary_output_ref.model_dump(mode="json"),
                    delivery_mode=(
                        "reused"
                        if effect.action_execution_mode == "none"
                        else "materialized"
                    ),
                ),
            ),
        ),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, "assembly_review_stage"),
        "stage_outputs": review_update["stage_outputs"],
        "resource_refs": update_resource_ref(
            state,
            "assembly",
            [output_path],
            "assembly_review",
            {
                "selected_candidate_id": effect.selected_candidate_id,
                "decision": effect.decision,
                "action_type": effect.action_type,
                "candidate_bundle_ref": effect.candidate_bundle_ref.model_dump(
                    mode="json"
                ),
                "primary_output_ref": effect.primary_output_ref.model_dump(mode="json"),
            },
        ),
    }


def _prepare_assembly_review_action_payload(
    state: TopLevelState,
    request: ResumeReviewRequest,
    candidate_final_ref: str | None,
) -> dict[str, Any]:
    if request.decision in {"accept", "pass", "continue_low_confidence"}:
        if not candidate_final_ref:
            raise ResumeValidationError(
                "assembly_review selected candidate has no verified primary output"
            )
        action_type = (
            "pass_low_confidence"
            if request.decision == "continue_low_confidence"
            else "pass"
        )
        return {
            "final_assembly_ref": candidate_final_ref,
            "pre_action_assembly_ref": candidate_final_ref,
            "action_type": action_type,
            "action_execution_mode": "none",
            "evidence_refs": [
                _require_existing_review_file_ref(
                    state, value, "evidence_refs"
                ).path_ref
                for value in request.metadata.get("evidence_refs") or []
            ],
            "evidence_notes": list(request.metadata.get("evidence_notes") or []),
        }

    if not candidate_final_ref:
        raise ResumeValidationError(
            "assembly_review transform action requires selected candidate "
            "primary output"
        )
    action_output_value = request.metadata.get("action_output_ref")
    if not action_output_value:
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires action_output_ref"
        )
    action_output_ref = _require_existing_review_file_ref(
        state, action_output_value, "action_output_ref"
    )
    resolved_action_output = session_paths(state).resolve_ref(
        action_output_ref.path_ref
    )
    resolved_candidate = resolve_run_ref(state, candidate_final_ref)
    if resolved_action_output == resolved_candidate:
        raise ResumeValidationError(
            f"assembly_review {request.decision} output must differ from "
            "pre-action assembly"
        )
    # Trust boundary: human_approved_action is user-asserted, not system-verified.
    # This documents that a human performed and approved an external action.
    # Additional validation (output exists, differs from input, has evidence)
    # reduces risk.
    if request.metadata.get("human_approved_action") is not True:
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires human-approved action "
            "metadata"
        )
    evidence_refs = [
        _require_existing_review_file_ref(state, value, "evidence_refs").path_ref
        for value in request.metadata.get("evidence_refs") or []
    ]
    evidence_notes = list(request.metadata.get("evidence_notes") or [])
    if not evidence_refs and not evidence_notes:
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires action evidence metadata"
        )
    method_name = request.method_name
    provenance_ref = request.metadata.get("provenance_ref")
    if provenance_ref is not None:
        _require_existing_review_file_ref(state, provenance_ref, "provenance_ref")
    if not method_name and provenance_ref is None:
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires method or provenance metadata"
        )
    if request.decision == "polish" and method_name in {
        "nextpolish_shortread",
        "pilon_shortread",
    }:
        short_read_evidence_refs = [
            _require_existing_review_file_ref(
                state, value, "short_read_evidence_refs"
            ).path_ref
            for value in request.metadata.get("short_read_evidence_refs") or []
        ]
        if not short_read_evidence_refs and not request.metadata.get(
            "short_read_evidence_notes"
        ):
            raise ResumeValidationError(
                "short-read polishing requires explicit short-read evidence"
            )
    return {
        "final_assembly_ref": action_output_ref.path_ref,
        "pre_action_assembly_ref": str(resolved_candidate),
        "action_type": request.decision,
        "action_execution_mode": "external",
        "action_method_name": method_name,
        "evidence_refs": evidence_refs,
        "evidence_notes": evidence_notes,
    }


def _require_existing_review_file_ref(
    state: TopLevelState,
    value: object,
    field_name: str,
) -> RunRelativeFileRef:
    """Return an existing, normalized run-local file reference or fail closed."""

    try:
        reference = RunRelativeFileRef.model_validate(value)
        path = session_paths(state).resolve_ref(reference.path_ref)
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            f"assembly_review {field_name} must be a run-relative file ref"
        ) from exc
    if not path.is_file():
        raise ResumeValidationError(
            f"assembly_review {field_name} does not reference a materialized file"
        )
    return reference


def _prepare_stopped_assembly_review_application(
    state: TopLevelState,
    intent: ReviewIntentV1,
    request: ResumeReviewRequest,
) -> PreparedReviewApplication:
    """Persist the terminal decision; apply owns its checkpoint projection."""

    decision_ref = _persist_assembly_review_decision(
        state,
        intent,
        ReviewDecisionArtifact(
            session_id=state["session_id"],
            stage="assembly_review",
            decision="stop_human_review",
            candidate_id=request.candidate_id,
            rejected_candidates=request.rejected_candidates,
            final_assembly_ref=None,
            notes=request.notes,
            metadata={
                **request.metadata,
                "rationale_artifact_ref": (
                    request.rationale_artifact_ref.model_dump(mode="json")
                    if request.rationale_artifact_ref is not None
                    else None
                ),
            },
        ),
    )
    return PreparedReviewApplication(
        effect_kind=ASSEMBLY_REVIEW_EFFECT_KIND,
        effect=AssemblyReviewEffectV1(
            decision="stop_human_review",
            review_request_id=intent.review_request_id,
            review_request_hash=intent.request_hash,
            review_request_ref=intent.request_ref,
            rejected_candidates=tuple(request.rejected_candidates),
            notes=tuple(request.notes),
            decided_by=request.decided_by,
            rationale_artifact_ref=request.rationale_artifact_ref,
            decision_ref=decision_ref,
        ),
        decision_ref=decision_ref,
        required_refs=canonical_artifact_refs(
            (
                decision_ref,
                *(
                    ()
                    if request.rationale_artifact_ref is None
                    else (request.rationale_artifact_ref,)
                ),
            )
        ),
    )


def _require_assembly_review_intent_binding(
    state: TopLevelState, intent: ReviewIntentV1
) -> None:
    """Require the persisted intent to target this blocked checkpoint occurrence."""

    if intent.session_id != state["session_id"]:
        raise ResumeValidationError("assembly_review intent session does not match")
    if state.get("supervision_state") != "blocked_human":
        raise ResumeValidationError(
            "assembly_review application requires blocked_human"
        )
    interrupt = cast(ActiveInterrupt, state.get("active_interrupt") or {})
    expected = {
        "stage": "assembly_review",
        "gate_id": "assembly_review",
        "workflow_step_id": intent.workflow_step_id,
        "review_request_id": intent.review_request_id,
        "request_hash": intent.request_hash,
    }
    for field, value in expected.items():
        if interrupt.get(field) != value:
            raise ResumeValidationError(
                f"assembly_review active_interrupt {field} does not match intent"
            )
    try:
        interrupt_ref = ArtifactRef.model_validate(interrupt.get("request_ref"))
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "assembly_review active_interrupt has no valid request_ref"
        ) from exc
    if interrupt_ref != intent.request_ref:
        raise ResumeValidationError(
            "assembly_review active_interrupt request_ref does not match intent"
        )
    current_step_id = current_workflow_step_id(state, expected_stage="assembly_review")
    if current_step_id != intent.workflow_step_id:
        raise ResumeValidationError(
            "assembly_review current workflow step does not match intent"
        )


def _persist_assembly_review_decision(
    state: TopLevelState,
    intent: ReviewIntentV1,
    decision: ReviewDecisionArtifact,
) -> ArtifactRef:
    """Persist or recover the immutable decision record, without a Ledger event."""

    try:
        return persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=intent.review_request_id,
            stage="assembly_review",
            gate_id="assembly_review",
            decision=decision,
            request_hash=intent.request_hash,
            request_ref=intent.request_ref,
            method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
            artifact_type="assembly_review.decision",
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "assembly_review decision could not be persisted"
        ) from exc


def _persist_assembly_review_action(
    state: TopLevelState, action: AssemblyReviewActionArtifact
) -> ArtifactRef:
    """Persist or recover the action evidence and return its typed ref."""

    action_path = _write_json_artifact(
        state,
        "assembly_review_action.json",
        action.model_dump(mode="json"),
        artifact_type=ASSEMBLY_REVIEW_ACTION,
    )
    return artifact_ref_for_path(
        absolute_data_dir(state),
        state["session_id"],
        action_path,
        expected_type=ASSEMBLY_REVIEW_ACTION,
    )


def generate_candidate_comparison(state: TopLevelState) -> StateUpdate:
    """Generate a candidate comparison artifact when multiple candidates exist."""
    ar_out = assembly_review_output(state)
    candidates = ar_out.get("candidates", [])

    if len(candidates) < 2:
        return {}

    signals = ar_out.get("signals", [])
    assembly_out = (state.get("stage_outputs") or {}).get("assembly", {})
    comparison_candidates = []
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id", "unknown")
        entry = {
            "candidate_id": candidate_id,
            "method_name": candidate.get("method_name"),
            "source": candidate.get("source"),
            "status": candidate.get("status", "unknown"),
            "parameters": _candidate_parameters(candidate, assembly_out),
            "metrics": _candidate_metrics(candidate_id, signals),
        }
        comparison_candidates.append(entry)

    comparison_payload = {
        "session_id": state["session_id"],
        "candidates": comparison_candidates,
        "notes": ["thresholds_not_applied_without_human_approval"],
    }
    existing_ref = ar_out.get("candidate_comparison_ref")
    if existing_ref and _json_artifact_payload_matches(
        existing_ref, comparison_payload
    ):
        return {}

    comparison_ref = _write_json_artifact(
        state,
        "candidate_comparison.json",
        comparison_payload,
        artifact_type=ASSEMBLY_CANDIDATE_COMPARISON,
    )

    ledger = Ledger(state.get("data_dir", "data"))
    workflow_step_id = current_workflow_step_id(
        state,
        expected_stage="assembly_review",
    )
    ledger.record_occurrence_event(
        state["session_id"],
        "candidate_comparison_generated",
        {
            "candidate_ids": [c["candidate_id"] for c in comparison_candidates],
            "candidate_count": len(comparison_candidates),
            "comparison_artifact_ref": comparison_ref,
        },
        stage="assembly_review",
        workflow_step_id=workflow_step_id,
        subject_id=comparison_ref,
        idempotency_key=(f"candidate-comparison:{workflow_step_id}:{comparison_ref}"),
    )

    return update_assembly_review_output(
        state, {"candidate_comparison_ref": comparison_ref}
    )


def _json_artifact_payload_matches(artifact_ref: Any, payload: dict[str, Any]) -> bool:
    path = Path(str(artifact_ref))
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(existing == payload)


def _target_species_name(state: TopLevelState) -> str | None:
    """Read optional species metadata without making manifest parsing a gate."""
    manifest_ref = state.get("input_manifest_ref")
    if not manifest_ref:
        return None
    path = resolve_run_ref(state, str(manifest_ref))
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        raw = (
            json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        )
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    metadata = dict(raw.get("user_metadata") or {})
    return species_name_from_metadata(metadata)


def _ensure_assembly_review_reports(
    state: TopLevelState,
    ar_out: AssemblyReviewCursor,
) -> tuple[AssemblyReviewCursor, dict[str, str], dict[str, Any]]:
    candidates = ar_out.get("candidates", [])
    report_kind = (
        "candidate_comparison_review"
        if len(candidates) >= 2
        else "single_candidate_review"
    )
    payload = _assembly_review_report_payload(state, ar_out, report_kind=report_kind)
    data_ref = _write_report_json_artifact_if_changed(
        state,
        "assembly_review_packet.json",
        payload,
        artifact_type=ASSEMBLY_REVIEW_PACKET,
    )
    report_ref = _write_derived_report_text(
        state,
        "assembly_review.md",
        _render_assembly_review_markdown(payload, title="Assembly Review Report"),
    )

    updated = ASSEMBLY_REVIEW_CURSOR_ADAPTER.validate_python(
        {
            **ar_out,
            "assembly_review_report_ref": report_ref,
            "assembly_review_packet_ref": data_ref,
        }
    )
    report_refs = {
        "assembly_review_report": report_ref,
        "review_packet": data_ref,
    }
    if ar_out.get("candidate_comparison_ref"):
        report_refs["candidate_comparison"] = str(ar_out["candidate_comparison_ref"])
    if ar_out.get("related_species_evidence_pack_ref"):
        report_refs["related_species_evidence_pack"] = str(
            ar_out["related_species_evidence_pack_ref"]
        )

    comparison_context = _candidate_comparison_context(state, updated)
    return updated, report_refs, comparison_context


def _assembly_review_report_payload(
    state: TopLevelState,
    ar_out: AssemblyReviewCursor,
    *,
    report_kind: str,
) -> dict[str, Any]:
    candidates = ar_out.get("candidates", [])
    signals = ar_out.get("signals", [])
    assembly_out = (state.get("stage_outputs") or {}).get("assembly", {})
    eval_bundle = _read_eval_bundle(ar_out.get("eval_bundle_ref"))
    review_plan = ar_out.get("review_plan") or {}
    return {
        "artifact_type": ASSEMBLY_REVIEW_PACKET,
        "session_id": state["session_id"],
        "stage": "assembly_review",
        "report_kind": report_kind,
        "candidate_count": len(candidates),
        "candidates": [
            _candidate_report_row(state, candidate, signals, assembly_out)
            for candidate in candidates
        ],
        "route_evidence": {
            "planned_signals": list(review_plan.get("planned_signals") or []),
            "available_signals": list(review_plan.get("available_signals") or []),
            "missing_signals": list(review_plan.get("missing_signals") or []),
            "available_inputs": list(eval_bundle.get("available_inputs") or []),
            "missing_inputs": list(eval_bundle.get("missing_inputs") or []),
        },
        "raw_refs": {
            "eval_bundle_ref": ar_out.get("eval_bundle_ref"),
            "candidate_comparison_ref": ar_out.get("candidate_comparison_ref"),
            "related_species_evidence_pack_ref": ar_out.get(
                "related_species_evidence_pack_ref"
            ),
        },
        "related_species_sanity_check": dict(
            ar_out.get("related_species_evidence_pack") or {}
        ),
        "notes": [
            "thresholds_not_applied_without_human_approval",
            "report_informs_human_decision_only",
        ],
    }


def _candidate_report_row(
    state: TopLevelState,
    candidate: AssemblyCandidateCursor,
    signals: list[AssemblyEvalSignalCursor],
    assembly_out: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = candidate.get("candidate_id", "unknown")
    candidate_signals = [
        signal for signal in signals if signal.get("candidate_id") == candidate_id
    ]
    return {
        "candidate_id": candidate_id,
        "method_name": candidate.get("method_name"),
        "source": candidate.get("source"),
        "status": candidate.get("status", "unknown"),
        "final_assembly_ref": _candidate_primary_output_path(state, candidate),
        "parameters": _candidate_parameters(candidate, assembly_out),
        "metrics": _candidate_metrics(candidate_id, signals),
        "signals": _signal_status_buckets(candidate_signals),
        "metric_refs_by_signal": {
            str(signal.get("signal_name")): [
                str(ref) for ref in signal.get("metric_refs", [])
            ]
            for signal in candidate_signals
            if signal.get("metric_refs")
        },
        "signal_notes_by_signal": {
            str(signal.get("signal_name")): list(signal.get("notes") or [])
            for signal in candidate_signals
            if signal.get("notes")
        },
    }


def _signal_status_buckets(
    signals: list[AssemblyEvalSignalCursor],
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "available": [],
        "missing": [],
        "failed": [],
        "pending": [],
    }
    for signal in signals:
        status = signal.get("status", "pending")
        signal_name = signal.get("signal_name")
        if status in buckets and signal_name:
            buckets[status].append(str(signal_name))
    return {key: sorted(values) for key, values in buckets.items()}


def _candidate_comparison_context(
    state: TopLevelState,
    ar_out: AssemblyReviewCursor,
) -> dict[str, Any]:
    candidates = ar_out.get("candidates", [])
    context: dict[str, Any] = {
        "candidate_ids": [
            candidate.get("candidate_id")
            for candidate in candidates
            if candidate.get("candidate_id")
        ],
        "candidate_primary_output_refs": {
            str(candidate.get("candidate_id")): dict(candidate["primary_output_ref"])
            for candidate in candidates
            if candidate.get("candidate_id")
        },
    }
    for key in ("candidate_comparison_ref",):
        if ar_out.get(key):
            context[key] = ar_out[key]
    return context


def _render_assembly_review_markdown(payload: dict[str, Any], *, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        (
            "No automatic biological thresholds are applied. This report informs "
            "the human decision only."
        ),
        "",
        "## Candidate Metrics",
        "",
        (
            "| Candidate | Method | Source | Assembly ref | QUAST contigs | "
            "Total length | N50 | Largest contig | BUSCO C% | BUSCO S% | "
            "BUSCO D% | BUSCO F% | BUSCO M% | Available signals | "
            "Missing/failed/pending signals |"
        ),
        (
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
            "--- | --- | --- | --- | --- |"
        ),
    ]
    for candidate in payload.get("candidates", []):
        metrics = candidate.get("metrics") or {}
        quast = metrics.get("quast") or {}
        busco = metrics.get("busco") or {}
        signal_buckets = candidate.get("signals") or {}
        unavailable = []
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                unavailable.append(f"{signal_name} ({status})")
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in [
                    candidate.get("candidate_id"),
                    candidate.get("method_name"),
                    candidate.get("source"),
                    candidate.get("final_assembly_ref"),
                    quast.get("contig_count"),
                    quast.get("total_length"),
                    quast.get("n50"),
                    quast.get("largest_contig"),
                    busco.get("complete_percent"),
                    busco.get("single_copy_percent"),
                    busco.get("duplicated_percent"),
                    busco.get("fragmented_percent"),
                    busco.get("missing_percent"),
                    ", ".join(signal_buckets.get("available", [])),
                    ", ".join(unavailable),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Parameter Provenance",
            "",
            (
                "| Candidate | Read profile | Estimated genome size | Threads | "
                "Rerun index |"
            ),
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for candidate in payload.get("candidates", []):
        params = candidate.get("parameters") or {}
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in [
                    candidate.get("candidate_id"),
                    params.get("read_profile"),
                    params.get("estimated_genome_size"),
                    params.get("threads"),
                    params.get("rerun_index"),
                ]
            )
            + " |"
        )

    _append_related_species_sanity_check(
        lines, payload.get("related_species_sanity_check") or {}
    )

    lines.extend(["", "## Evidence Refs", ""])
    raw_refs = payload.get("raw_refs") or {}
    for key, value in raw_refs.items():
        if value:
            lines.append(f"- {key}: `{value}`")
    for candidate in payload.get("candidates", []):
        metric_refs = candidate.get("metric_refs_by_signal") or {}
        for signal_name, refs in metric_refs.items():
            for ref in refs:
                lines.append(
                    f"- {candidate.get('candidate_id')} {signal_name}: `{ref}`"
                )

    lines.extend(["", "## Missing Evidence", ""])
    for candidate in payload.get("candidates", []):
        signal_buckets = candidate.get("signals") or {}
        notes_by_signal = candidate.get("signal_notes_by_signal") or {}
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                notes = ", ".join(notes_by_signal.get(signal_name, []))
                suffix = f" — {notes}" if notes else ""
                lines.append(
                    f"- {candidate.get('candidate_id')} {signal_name}: {status}{suffix}"
                )

    lines.extend(["", "## Report Notes", ""])
    for note in payload.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _append_related_species_sanity_check(
    lines: list[str], pack: dict[str, Any]
) -> None:
    lines.extend(["", "## Related-species sanity check", ""])
    if not pack:
        lines.append("- Reference evidence was not generated.")
        return
    target = pack.get("target") or {}
    target_name = target.get("canonical_scientific_name") or target.get("input_name")
    lines.extend(
        [
            f"- Target: {_markdown_cell(target_name)}",
            f"- Matching mode: {_markdown_cell(pack.get('matching_mode'))}",
            (
                "- This evidence is comparative only; it is not an assembly prior "
                "or automatic-failure threshold."
            ),
            "",
        ]
    )
    records = pack.get("selected_records") or []
    if records:
        lines.extend(
            [
                (
                    "| Organism | Accession | Assembly | Total length | "
                    "Chromosomes | Contig N50 | Scaffold N50 | GC | "
                    "BUSCO lineage |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for record in records:
            busco = record.get("busco") or {}
            lines.append(
                "| "
                + " | ".join(
                    _markdown_cell(value)
                    for value in [
                        record.get("organism_name"),
                        record.get("accession"),
                        record.get("assembly_name"),
                        record.get("total_sequence_length_bp"),
                        record.get("chromosome_count"),
                        record.get("contig_n50_bp"),
                        record.get("scaffold_n50_bp"),
                        record.get("gc_percent"),
                        busco.get("lineage"),
                    ]
                )
                + " |"
            )
    else:
        lines.append("- No useful related-species primary assemblies were found.")
    excluded = pack.get("excluded_alternate_haplotype_records") or []
    if excluded:
        excluded_accessions = ", ".join(
            str(record.get("accession")) for record in excluded
        )
        lines.append(f"- Excluded alternate-haplotype records: {excluded_accessions}")
    summary = pack.get("computed_summary") or {}
    if summary:
        lines.extend(
            [
                "",
                "Computed summary:",
                "",
                "```json",
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                "```",
            ]
        )
    for note in pack.get("notes") or []:
        lines.append(f"- Reference note: {note}")


def _markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _write_report_json_artifact_if_changed(
    state: TopLevelState,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
) -> str:
    return _write_json_artifact(
        state,
        filename,
        payload,
        artifact_type=artifact_type,
    )


def _write_derived_report_text(
    state: TopLevelState,
    filename: str,
    text: str,
) -> str:
    """Overwrite the optional Markdown projection without indexing it."""

    path = session_paths(state).report_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = stage_work_dir(
        state,
        "assembly_review",
        "reports",
        f".{filename}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return str(path)


def _candidate_parameters(
    candidate: AssemblyCandidateCursor, assembly_out: dict[str, Any]
) -> dict[str, Any]:
    read_type_prior = assembly_out.get("read_type_prior") or {}
    execution_contract = assembly_out.get(ASSEMBLY_EXECUTION_CONTRACT_KEY)
    threads = None
    if execution_contract is not None:
        validated_contract = AssemblyExecutionContract.model_validate(
            execution_contract
        )
        if validated_contract.threads is not None:
            threads = validated_contract.threads.value
    method_key = str(candidate.get("method_name") or "").rsplit(".", 1)[-1]
    return {
        "read_profile": (
            read_type_prior.get(method_key)
            if isinstance(read_type_prior, dict)
            else None
        ),
        "estimated_genome_size": assembly_out.get("genome_size_prior"),
        "threads": threads,
    }


def _candidate_metrics(
    candidate_id: str, signals: list[AssemblyEvalSignalCursor]
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for signal in signals:
        if signal.get("candidate_id") != candidate_id:
            continue
        if signal.get("status") != "available":
            continue
        signal_name = signal.get("signal_name")
        signal_metrics = (signal.get("metadata") or {}).get("metrics")
        if signal_name and signal_metrics:
            metrics[signal_name] = signal_metrics
    return metrics


def fail_assembly_review(state: TopLevelState) -> StateUpdate:
    """Record assembly review failure and stop."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = assembly_review_output(state)
    assembly_out = stage_outputs.get("assembly", {})
    status = str(ar_out.get("status") or assembly_out.get("status") or "failed")
    default_reason = {
        "blocked_env": "assembly_review blocked on missing conda environment",
        "missing_tool": "assembly_review blocked on missing tool",
    }.get(status, "assembly_review failed")
    reason = ar_out.get("reason") or assembly_out.get("reason") or default_reason
    return {
        **fail_workflow_step(state, "assembly_review", str(reason)),
    }


def _publish_review_action_output(
    state: TopLevelState,
    selected: VerifiedExecutedCandidate,
    source: str,
    *,
    action_type: str,
    review_request_id: str,
) -> PublishedArtifact:
    """Publish one externally approved transformed assembly before handoff."""

    safe_request_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in review_request_id
    )
    if not safe_request_id:
        raise ResumeValidationError(
            "assembly_review action output has no review request identity"
        )
    filename = f"{safe_request_id}.{action_type}.fasta"
    destination = (
        stage_artifacts_dir(
            state,
            "assembly_review",
            "actions",
        )
        / filename
    )
    metadata = {
        "candidate_id": selected.bundle.candidate_id,
        "candidate_bundle_ref": selected.ref.model_dump(mode="json"),
        "dependency_refs": [selected.primary_output_ref.model_dump(mode="json")],
        "review_request_id": review_request_id,
    }
    recovered = recover_published_artifact(
        absolute_data_dir(state),
        state["session_id"],
        destination,
        stage="assembly_review",
        method_name=f"assembly_review.{action_type}",
        artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        metadata=metadata,
    )
    if recovered is not None:
        return recovered

    source_path = resolve_run_ref(state, source)
    if not source_path.is_file():
        raise ResumeValidationError(
            "assembly_review action output is not a materialized file"
        )
    if source_path.stat().st_size <= 0:
        raise ResumeValidationError("assembly_review action output is empty")
    temporary = stage_work_dir(
        state,
        "assembly_review",
        "actions",
        f".{filename}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, temporary)
    artifact = ArtifactMeta.from_file(
        temporary,
        stage="assembly_review",
        method_name=f"assembly_review.{action_type}",
        artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        metadata=metadata,
    )
    try:
        return publish_artifact_record(
            absolute_data_dir(state),
            state["session_id"],
            artifact,
            destination=destination,
        )
    finally:
        temporary.unlink(missing_ok=True)


def _recover_or_publish_derived_review_candidate(
    state: TopLevelState,
    selected: VerifiedExecutedCandidate,
    *,
    primary_output_ref: ArtifactRef,
    action_type: str,
    review_request_id: str,
) -> VerifiedExecutedCandidate:
    """Recover a completed derived candidate before creating its deterministic chain."""

    method_name = f"assembly_review.{action_type}"
    identity_payload = json.dumps(
        [selected.ref.artifact_id, "assembly_review", method_name, review_request_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identity_digest = sha256(identity_payload.encode("utf-8")).hexdigest()
    safe_method = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in method_name
    ).strip("_")
    executed_path = stage_artifacts_dir(
        state,
        "assembly_review",
        "derived_candidates",
        f"{safe_method[:64]}-{identity_digest[:16]}",
        "candidate.executed.json",
    )
    if executed_path.exists():
        recovered_ref = artifact_ref_for_path(
            absolute_data_dir(state),
            state["session_id"],
            executed_path,
            expected_type=ASSEMBLY_CANDIDATE_BUNDLE,
        )
        recovered = load_executed_candidate_bundle(
            absolute_data_dir(state),
            state["session_id"],
            recovered_ref,
            expected_method_name=method_name,
        )
        if recovered.primary_output_ref != primary_output_ref:
            raise ResumeValidationError(
                "assembly_review recovered derived candidate output does not match "
                "action"
            )
        return recovered
    return publish_derived_assembly_candidate(
        absolute_data_dir(state),
        state["session_id"],
        workflow_step_id=current_workflow_step_id(
            state,
            expected_stage="assembly_review",
        ),
        source_candidate=selected,
        primary_output_ref=primary_output_ref,
        stage="assembly_review",
        method_name=method_name,
        action_identity=review_request_id,
    )


def _candidate_primary_output_path(
    state: TopLevelState,
    candidate: AssemblyCandidateCursor,
) -> str:
    return str(_verified_review_candidate(state, candidate).primary_output_path)


_EVAL_SIGNAL_NAMES = {"quast", "busco"}


def _assembly_review_auto_decidable(ar_out: AssemblyReviewCursor) -> bool:
    """Require complete candidate-level QUAST and BUSCO evidence for LLM review."""

    candidates = ar_out.get("candidates") or []
    if not any(
        isinstance(candidate, dict) and candidate.get("candidate_id")
        for candidate in candidates
    ):
        return False

    signals = [
        signal for signal in (ar_out.get("signals") or []) if isinstance(signal, dict)
    ]
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        has_quast = any(
            signal.get("signal_name") == "quast"
            and signal.get("candidate_id") == candidate_id
            and signal.get("status") == "available"
            and signal.get("metric_refs")
            for signal in signals
        )
        has_busco = any(
            signal.get("signal_name") == "busco"
            and signal.get("candidate_id") == candidate_id
            and signal.get("status") == "available"
            and signal.get("metric_refs")
            for signal in signals
        )
        if not (has_quast and has_busco):
            return False
    return True


def _available_review_inputs(
    state: TopLevelState,
) -> tuple[bool, bool, set[str], set[str]]:
    available_inputs: set[str] = set()
    missing_inputs: set[str] = set()
    if state.get("asset_registry_ref"):
        from app.storage.asset_registry import load_asset_records

        asset_types = {asset.asset_type for asset in load_asset_records(state)}
        for asset_type in ["clean_short_dna_reads", "clean_long_reads"]:
            if asset_type in asset_types:
                available_inputs.add(asset_type)
            else:
                missing_inputs.add(asset_type)
    has_short_reads = "clean_short_dna_reads" in available_inputs
    has_long_reads = "clean_long_reads" in available_inputs
    return has_short_reads, has_long_reads, available_inputs, missing_inputs


def _review_signal_method_name(signal_name: str) -> str:
    return {
        "quast": "assembly_review.eval.quast",
        "busco": "assembly_review.eval.busco",
    }[signal_name]
