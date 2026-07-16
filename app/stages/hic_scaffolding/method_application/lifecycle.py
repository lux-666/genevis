"""Shared freeze/run/collect lifecycle for executable Hi-C methods."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.evidence import (
    ExecutionEvidence,
    build_execution_evidence,
    validate_execution_evidence,
)
from app.execution.method_runner import run_frozen_attempt
from app.schemas.artifacts import (
    HIC_PARAMETER_EXECUTION,
    HIC_QC_REPORT,
    HIC_REVIEW_BUNDLE,
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactMeta,
    ArtifactRef,
    ParameterAttempt,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.stages.hic_scaffolding.parameters.storage import (
    AUTOHIC_PARAMETER_COMMAND,
    AutoHiCParameterPersistenceError,
    read_autohic_parameter_artifact,
    write_autohic_parameter_artifact,
)
from app.storage.paths import SessionPaths


@dataclass(frozen=True)
class HiCMethodLifecycle:
    """State-key convention for one executable Hi-C scaffolding method."""

    name: str
    method_name: str
    frozen_status: str
    ran_status: str
    frozen_attempt_key: str
    execution_result_key: str


def build_method_graph(
    lifecycle: HiCMethodLifecycle,
    freeze: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
):
    """Build the reusable freeze -> run -> collect method subgraph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node("run", lambda state: run_method_attempt(state, lifecycle))
    graph.add_node("collect", collect)
    graph.add_node("done", lambda _state: {})
    graph.add_edge(START, "freeze")
    graph.add_conditional_edges(
        "freeze",
        lambda state: (
            "run"
            if hic_output(state).get("status") == lifecycle.frozen_status
            else "done"
        ),
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: (
            "collect"
            if hic_output(state).get("status") == lifecycle.ran_status
            else "done"
        ),
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def hic_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "hic_scaffolding")


def update_hic_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "hic_scaffolding", payload)


def run_method_attempt(state: TopLevelState, lifecycle: HiCMethodLifecycle) -> dict:
    """Run the approved frozen AutoHiC attempt and persist its outcome."""
    output = hic_output(state)
    if output.get("status") != lifecycle.frozen_status:
        return {}
    try:
        attempt = _load_frozen_attempt(
            state,
            output.get(lifecycle.frozen_attempt_key),
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "execution_evidence",
                "reason": f"AutoHiC frozen command is unavailable: {exc}",
            },
        )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=lifecycle.name,
        cleanup_outputs=False,
    )
    result = build_execution_evidence(
        state,
        outcome.result,
        error=outcome.error,
    )
    try:
        parameter_attempts, execution_ref = _record_parameter_execution(
            state,
            output,
            lifecycle=lifecycle,
            result=result,
            succeeded=outcome.success,
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "execution_evidence",
                "reason": f"AutoHiC execution evidence could not be persisted: {exc}",
            },
        )
    return update_hic_output(
        state,
        {
            "status": lifecycle.ran_status,
            lifecycle.execution_result_key: {
                "execution_ref": execution_ref.model_dump(mode="json"),
                "success": result.success,
                "return_code": result.return_code,
                "error": _bounded_text(result.error),
            },
            "parameter_attempts": parameter_attempts,
        },
    )


def _record_parameter_execution(
    state: TopLevelState,
    output: dict[str, Any],
    *,
    lifecycle: HiCMethodLifecycle,
    result: ExecutionEvidence,
    succeeded: bool,
) -> tuple[list[dict[str, Any]], ArtifactRef]:
    """Attach immutable execution evidence to the approved parameter attempt."""

    current_id = int(output.get("current_parameter_attempt_id") or 0)
    attempts = list(output.get("parameter_attempts") or [])
    matches = [
        item
        for item in attempts
        if isinstance(item, dict) and item.get("attempt_id") == current_id
    ]
    if len(matches) != 1:
        raise AutoHiCParameterPersistenceError(
            "AutoHiC execution requires one current parameter attempt"
        )
    current = ParameterAttempt.model_validate(matches[0])
    if current.method_id != lifecycle.method_name or current.status != "frozen":
        raise AutoHiCParameterPersistenceError(
            "AutoHiC current parameter attempt is not the approved frozen attempt"
        )
    execution_ref = write_autohic_parameter_artifact(
        state,
        current_id,
        "execution.json",
        {
            "schema_version": 2,
            "attempt_id": current_id,
            "method_id": lifecycle.method_name,
            "result": result.model_dump(mode="json"),
        },
        artifact_type=HIC_PARAMETER_EXECUTION,
        method_name=lifecycle.method_name,
        scope="attempt",
    )
    updated = ParameterAttempt.model_validate(
        {
            **current.model_dump(mode="json"),
            "status": "executed" if succeeded else "execution_failed",
            "execution_ref": execution_ref.model_dump(mode="json"),
        }
    ).model_dump(mode="json")
    updated_attempts = [
        updated
        if isinstance(item, dict) and item.get("attempt_id") == current_id
        else ParameterAttempt.model_validate(item).model_dump(mode="json")
        for item in attempts
    ]
    return updated_attempts, execution_ref


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: HiCMethodLifecycle,
    *,
    extra_artifacts: list[tuple[str, str]] = (),
    execution_metadata: dict[str, Any] | None = None,
) -> dict:
    """Register a Hi-C candidate, review bundle, and fallback state."""
    output = hic_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    try:
        attempt = _load_frozen_attempt(
            state,
            output.get(lifecycle.frozen_attempt_key),
        )
        execution_ref, result = _load_execution_evidence(
            state,
            output,
            lifecycle=lifecycle,
            expected_attempt_id=attempt.attempt_id,
        )
    except (OSError, ValueError, AutoHiCParameterPersistenceError) as exc:
        return update_hic_output(
            state,
            {
                "status": "failed",
                "failure_kind": "execution_evidence",
                "reason": f"AutoHiC execution evidence is unavailable: {exc}",
            },
        )
    data_dir = absolute_data_dir(state)
    artifact_dir = Path(str(attempt.metadata["artifact_dir"]))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[ArtifactMeta] = []
    notes = list(output.get("notes") or [])

    execution_succeeded = result.success
    execution_error = result.error
    if execution_succeeded:
        scaffold_fasta = Path(str(attempt.metadata["scaffold_fasta"]))
        if not scaffold_fasta.exists():
            reason = (
                f"expected {lifecycle.name} scaffold assembly missing: {scaffold_fasta}"
            )
            Ledger(data_dir).record_attempt_failed(
                attempt.session_id, attempt, return_code=0, error=reason
            )
            execution_succeeded = False
            execution_error = reason
        else:
            artifacts.append(
                ArtifactMeta.from_file(
                    scaffold_fasta,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_SCAFFOLDED_ASSEMBLY,
                    metadata={
                        "attempt_id": attempt.attempt_id,
                        "review_required": True,
                    },
                )
            )

    if execution_succeeded:
        for key, artifact_type in extra_artifacts:
            path_value = attempt.metadata.get(key)
            if path_value and Path(str(path_value)).exists():
                artifacts.append(
                    ArtifactMeta.from_file(
                        Path(str(path_value)),
                        stage="hic_scaffolding",
                        method_name=lifecycle.method_name,
                        artifact_type=artifact_type,
                        metadata={
                            "attempt_id": attempt.attempt_id,
                            "review_required": True,
                        },
                    )
                )

        review_bundle_ref = artifact_dir / "hic_review_bundle.json"
        review_bundle_ref.write_text(
            json.dumps(
                {
                    "session_id": attempt.session_id,
                    "method_name": lifecycle.method_name,
                    "status": f"{lifecycle.name}_executed_review_required",
                    "pre_hic_assembly_ref": output.get("pre_hic_assembly_ref"),
                    "final_assembly_for_annotation": output.get(
                        "final_assembly_for_annotation"
                    ),
                    "chromosome_scaffolded_assembly_candidate_ref": attempt.metadata.get(
                        "scaffold_fasta"
                    ),
                    "contact_map_ref": attempt.metadata.get("contact_map"),
                    "assembly_file_ref": attempt.metadata.get("assembly_file"),
                    "html_report_ref": attempt.metadata.get("html_report"),
                    "review_required": True,
                    "execution_ref": execution_ref.model_dump(mode="json"),
                    "execution_summary": _execution_summary(result),
                    "execution_metadata": execution_metadata or {},
                    "notes": [
                        "geneagent_review_required_before_promotion",
                        "pre_hic_assembly_preserved_as_annotation_fallback",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        qc_report_ref = artifact_dir / "hic_qc_report.json"
        qc_report_ref.write_text(
            json.dumps(
                {
                    "session_id": attempt.session_id,
                    "method_name": lifecycle.method_name,
                    "status": "available",
                    "pre_hic_assembly_ref": output.get("pre_hic_assembly_ref"),
                    "chromosome_scaffolded_assembly_candidate_ref": attempt.metadata.get(
                        "scaffold_fasta"
                    ),
                    "execution_ref": execution_ref.model_dump(mode="json"),
                    "execution_summary": _execution_summary(result),
                    "execution_metadata": execution_metadata or {},
                    "notes": [
                        "qc_thresholds_not_applied",
                        "geneagent_review_required_before_promotion",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts.extend(
            [
                ArtifactMeta.from_file(
                    review_bundle_ref,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_REVIEW_BUNDLE,
                    metadata={
                        "attempt_id": attempt.attempt_id,
                        "review_required": True,
                    },
                ),
                ArtifactMeta.from_file(
                    qc_report_ref,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_QC_REPORT,
                    metadata={
                        "attempt_id": attempt.attempt_id,
                        "review_required": True,
                    },
                ),
            ]
        )
        notes = unique_refs(
            [
                *notes,
                f"{lifecycle.name}_executed",
                "geneagent_review_required_before_promotion",
            ]
        )
        candidates = dict(output.get("method_candidates") or {})
        candidates[lifecycle.method_name] = {
            "status": "executed",
            "scaffold_fasta": str(attempt.metadata["scaffold_fasta"]),
            "contact_map": attempt.metadata.get("contact_map"),
            "assembly_file": attempt.metadata.get("assembly_file"),
            "html_report": attempt.metadata.get("html_report"),
            "review_bundle_ref": str(review_bundle_ref),
            "qc_report_ref": str(qc_report_ref),
        }
        update = {
            "status": "autohic_review_ready",
            "chromosome_scaffolded_assembly_candidate_ref": str(
                attempt.metadata["scaffold_fasta"]
            ),
            "hic_review_bundle_ref": str(review_bundle_ref),
            "hic_qc_report_ref": str(qc_report_ref),
            "selected_method_name": lifecycle.method_name,
            "notes": notes,
            "method_candidates": candidates,
        }
    else:
        notes = unique_refs([*notes, f"{lifecycle.name}_execution_failed"])
        candidates = dict(output.get("method_candidates") or {})
        candidates[lifecycle.method_name] = {
            "status": "failed",
            "error": execution_error,
        }
        update = {
            "status": "failed",
            "failure_kind": "execution",
            "reason": execution_error or f"{lifecycle.name} execution failed",
            "selected_method_name": lifecycle.method_name,
            "notes": notes,
            f"{lifecycle.name}_error": execution_error,
            "method_candidates": candidates,
        }

    if artifacts:
        append_artifact_records(data_dir, attempt.session_id, artifacts)
        payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
        ledger = Ledger(data_dir)
        if execution_succeeded:
            ledger.record_attempt_succeeded(
                attempt.session_id, attempt, artifacts=payloads
            )
        for artifact in payloads:
            ledger.record_artifact_registered(
                attempt.session_id, artifact, attempt_id=attempt.attempt_id
            )

    return update_hic_output(
        state,
        {
            **update,
            "artifact_refs": unique_refs(
                [
                    *list(output.get("artifact_refs") or []),
                    *[str(a.file_path) for a in artifacts],
                ]
            ),
            lifecycle.frozen_attempt_key: None,
            lifecycle.execution_result_key: None,
        },
    )


def unique_refs(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(refs))


def _load_frozen_attempt(
    state: TopLevelState,
    value: Any,
) -> FrozenAttemptSpec:
    command_ref = ArtifactRef.model_validate(value)
    command = read_autohic_parameter_artifact(
        state,
        command_ref,
        expected_type=AUTOHIC_PARAMETER_COMMAND,
    )
    frozen_attempt = command.get("frozen_attempt")
    if not isinstance(frozen_attempt, dict):
        raise AutoHiCParameterPersistenceError(
            "AutoHiC command artifact has no frozen attempt"
        )
    frozen_attempt = dict(frozen_attempt)
    if "created_at" not in frozen_attempt:
        command_path = SessionPaths.from_run_dir(absolute_data_dir(state)).resolve_ref(
            command_ref.path_ref
        )
        frozen_attempt["created_at"] = datetime.fromtimestamp(
            command_path.stat().st_mtime,
            tz=timezone.utc,
        )
    return FrozenAttemptSpec.model_validate(frozen_attempt)


def _load_execution_evidence(
    state: TopLevelState,
    output: dict[str, Any],
    *,
    lifecycle: HiCMethodLifecycle,
    expected_attempt_id: str,
) -> tuple[ArtifactRef, ExecutionEvidence]:
    cursor = output.get(lifecycle.execution_result_key)
    if not isinstance(cursor, dict):
        raise AutoHiCParameterPersistenceError(
            "AutoHiC execution checkpoint has no compact cursor"
        )
    execution_ref = ArtifactRef.model_validate(cursor.get("execution_ref"))
    current_id = int(output.get("current_parameter_attempt_id") or 0)
    attempts = [
        ParameterAttempt.model_validate(item)
        for item in (output.get("parameter_attempts") or [])
        if isinstance(item, dict) and item.get("attempt_id") == current_id
    ]
    if len(attempts) != 1 or attempts[0].execution_ref != execution_ref:
        raise AutoHiCParameterPersistenceError(
            "AutoHiC execution ref is not bound to the current parameter attempt"
        )
    record = read_autohic_parameter_artifact(
        state,
        execution_ref,
        expected_type=HIC_PARAMETER_EXECUTION,
    )
    if (
        record.get("attempt_id") != current_id
        or record.get("method_id") != lifecycle.method_name
    ):
        raise AutoHiCParameterPersistenceError(
            "AutoHiC execution artifact has the wrong method identity"
        )
    result = validate_execution_evidence(
        state,
        record.get("result") or {},
        expected_attempt_id=expected_attempt_id,
    )
    return execution_ref, result


def _execution_summary(result: ExecutionEvidence) -> dict[str, Any]:
    return {
        "success": result.success,
        "return_code": result.return_code,
        "error": _bounded_text(result.error),
    }


def _bounded_text(value: Any, *, limit: int = 2048) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
