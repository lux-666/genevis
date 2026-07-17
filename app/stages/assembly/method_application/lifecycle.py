"""Shared lifecycle primitives for executable assembly-method subgraphs.

Each method module owns how an attempt is frozen.  This module owns the
method-independent execution, artifact registration, and state transition
protocol so another method (for example hifiasm) does not need to copy it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.evidence import (
    build_execution_evidence,
    persist_frozen_attempt,
    validate_execution_evidence,
)
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
    load_current_frozen_attempt,
    update_current_parameter_attempt,
)
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ASSEMBLY_PARAMETER_COMMAND,
    ASSEMBLY_PARAMETER_EXECUTION,
    ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    ArtifactMeta,
    AssemblyCandidateBundle,
    ParameterAttempt,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.assembly.parameters.storage import (
    ParameterPersistenceError,
    load_recommendation_bundle,
    read_json_artifact,
    write_attempt_json_artifact,
)
from app.stages.base import ordered_unique, stage_output, update_stage_output
from app.storage.publish import publish_file


@dataclass(frozen=True)
class MethodLifecycle:
    """State-key convention for one executable assembly method."""

    name: str
    method_name: str
    planned_status: str
    frozen_status: str
    ran_status: str
    draft_assembly_key: str
    substep_key: str


def build_method_graph(
    lifecycle: MethodLifecycle,
    freeze: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
):
    """Build the common freeze → run → collect method-application graph."""
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
            if stage_output(state, "assembly").get("status") == lifecycle.frozen_status
            else "done"
        ),
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: (
            "collect"
            if stage_output(state, "assembly").get("status") == lifecycle.ran_status
            else "done"
        ),
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def fail_update(state: TopLevelState, reason: str) -> dict:
    return update_stage_output(
        state,
        "assembly",
        {"status": "failed", "reason": reason},
    )


def freeze_parameterized_attempt(
    state: TopLevelState,
    output: dict[str, Any],
    lifecycle: MethodLifecycle,
    *,
    label: str,
    approved_attempt: ParameterAttempt,
    script_path: Path,
    working_dir: Path,
    expected_outputs: list[Path],
    env_spec: Any,
    command_payload: dict[str, Any],
    frozen_parameters_payload: dict[str, Any],
    frozen_attempt_metadata: dict[str, Any],
    stage_updates: dict[str, Any],
) -> dict:
    """Persist and attach one human-approved Assembly execution attempt."""

    if (
        approved_attempt.method_id != lifecycle.method_name
        or approved_attempt.status != "recommended"
    ):
        return fail_update(
            state,
            f"{label} current parameter recommendation is not approved for freeze",
        )
    parameter_attempt_id = approved_attempt.attempt_id
    command_ref = write_attempt_json_artifact(
        state,
        parameter_attempt_id,
        "command.json",
        {
            "attempt_id": parameter_attempt_id,
            "method_id": lifecycle.method_name,
            **command_payload,
        },
        artifact_type=ASSEMBLY_PARAMETER_COMMAND,
        method_name=lifecycle.method_name,
    )
    frozen_parameters_ref = write_attempt_json_artifact(
        state,
        parameter_attempt_id,
        "frozen_parameters.json",
        {
            "schema_version": 2,
            "attempt_id": parameter_attempt_id,
            "method_id": lifecycle.method_name,
            **frozen_parameters_payload,
        },
        artifact_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
        method_name=lifecycle.method_name,
    )
    session_id = state["session_id"]
    attempt = build_frozen_attempt(
        attempt_id=(
            f"{session_id}:assembly:{lifecycle.name}:"
            f"parameter_attempt_{parameter_attempt_id}"
        ),
        session_id=session_id,
        stage="assembly",
        method_name=lifecycle.method_name,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=expected_outputs,
        env=output.get("env"),
        env_spec=env_spec,
        metadata=frozen_attempt_metadata,
    )
    frozen_attempt_ref = persist_frozen_attempt(state, attempt)
    try:
        parameter_attempts = update_current_parameter_attempt(
            output,
            method_id=lifecycle.method_name,
            label=label,
            status="frozen",
            frozen_parameters_ref=frozen_parameters_ref.model_dump(mode="json"),
            command_ref=command_ref.model_dump(mode="json"),
            frozen_attempt_ref=frozen_attempt_ref.model_dump(mode="json"),
        )
    except ParameterAttemptStateError as exc:
        return fail_update(state, str(exc))
    return update_stage_output(
        state,
        "assembly",
        {
            "status": lifecycle.frozen_status,
            **stage_updates,
            "parameter_attempts": parameter_attempts,
        },
    )


def load_current_approved_recommendation(
    state: TopLevelState,
    output: dict[str, Any],
    *,
    method_id: str,
    label: str,
) -> tuple[ParameterAttempt | None, dict[str, Any], str | None]:
    """Load the exact recommendation approved by the human parameter gate."""

    try:
        attempt = current_parameter_attempt(
            output,
            method_id=method_id,
            label=label,
        )
        if attempt.status != "recommended":
            raise ParameterAttemptStateError(
                f"{label} current parameter recommendation is not approved for freeze"
            )
        bundle = load_recommendation_bundle(state, attempt.recommendation_manifest_ref)
    except (ParameterAttemptStateError, ParameterPersistenceError) as exc:
        return None, {}, f"{label} recommendation evidence is invalid: {exc}"
    recommendation = dict(bundle["recommendation"].get("recommendation") or {})
    return attempt, dict(recommendation.get("parameters") or {}), None


def run_method_attempt(state: TopLevelState, lifecycle: MethodLifecycle) -> dict:
    """Execute the frozen attempt and persist a method-namespaced result."""
    output = stage_output(state, "assembly")
    if output.get("status") != lifecycle.frozen_status:
        return {}

    try:
        parameter_attempt = current_parameter_attempt(
            output,
            method_id=lifecycle.method_name,
            label=lifecycle.name,
        )
        attempt = load_current_frozen_attempt(
            state,
            output,
            stage="assembly",
            method_id=lifecycle.method_name,
            label=lifecycle.name,
        )
    except (OSError, ValueError, ParameterAttemptStateError) as exc:
        return fail_update(state, f"{lifecycle.name} frozen attempt is invalid: {exc}")
    parameter_attempt_id = parameter_attempt.attempt_id
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=lifecycle.name,
    )
    result = build_execution_evidence(
        state,
        outcome.result,
        error=outcome.error,
    )
    execution_ref = write_attempt_json_artifact(
        state,
        parameter_attempt_id,
        "execution.json",
        {
            "schema_version": 2,
            "attempt_id": parameter_attempt_id,
            "method_id": lifecycle.method_name,
            "result": result.model_dump(mode="json"),
        },
        artifact_type=ASSEMBLY_PARAMETER_EXECUTION,
        method_name=lifecycle.method_name,
    )

    try:
        parameter_attempts = update_current_parameter_attempt(
            output,
            method_id=lifecycle.method_name,
            label=lifecycle.name,
            status="execution_failed" if not outcome.success else "executed",
            execution_ref=execution_ref.model_dump(mode="json"),
        )
    except ParameterAttemptStateError as exc:
        return fail_update(state, str(exc))

    if not outcome.success:
        updates = {
            "status": "failed",
            "reason": outcome.error,
        }
        updates["parameter_attempts"] = parameter_attempts
        return update_stage_output(state, "assembly", updates)

    updates = {"status": lifecycle.ran_status}
    updates["parameter_attempts"] = parameter_attempts
    return update_stage_output(state, "assembly", updates)


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: MethodLifecycle,
    *,
    attempt: FrozenAttemptSpec,
    extra_artifacts: list[tuple[Path, str]],
    bundle_logs: dict[str, str],
    bundle_metadata: dict[str, Any],
) -> dict:
    """Register standard artifacts and materialize the selected candidate bundle."""
    output = stage_output(state, "assembly")
    if output.get("status") != lifecycle.ran_status:
        return {}

    parameter_attempt = current_parameter_attempt(
        output,
        method_id=lifecycle.method_name,
        label=lifecycle.name,
    )
    if parameter_attempt.execution_ref is None:
        raise ParameterPersistenceError(
            "current parameter attempt has no execution_ref"
        )
    execution_payload = read_json_artifact(
        state,
        parameter_attempt.execution_ref,
        expected_type=ASSEMBLY_PARAMETER_EXECUTION,
    )
    if (
        execution_payload.get("attempt_id") != parameter_attempt.attempt_id
        or execution_payload.get("method_id") != lifecycle.method_name
    ):
        raise ParameterPersistenceError(
            "execution evidence has the wrong method identity"
        )
    result = validate_execution_evidence(
        state,
        execution_payload.get("result") or {},
        expected_attempt_id=attempt.attempt_id,
    )
    draft_assembly = Path(output[lifecycle.draft_assembly_key])
    if not draft_assembly.exists():
        reason = f"expected {lifecycle.name} draft assembly missing: {draft_assembly}"
        Ledger(state.get("data_dir", "data")).record_attempt_failed(
            attempt.session_id, attempt, return_code=0, error=reason
        )
        return update_stage_output(
            state,
            "assembly",
            {"status": "failed", "reason": reason},
        )

    artifacts = [
        ArtifactMeta.from_file(
            draft_assembly,
            stage="assembly",
            method_name=lifecycle.method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            metadata={
                "attempt_id": attempt.attempt_id,
                "candidate_id": f"{attempt.session_id}:{lifecycle.method_name}",
            },
        )
    ]
    for path, artifact_type in extra_artifacts:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="assembly",
                    method_name=lifecycle.method_name,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    bundle_path = _candidate_bundle_path(output, lifecycle.method_name)
    if bundle_path is None:
        return update_stage_output(
            state,
            "assembly",
            {
                "status": "failed",
                "reason": f"missing {lifecycle.name} candidate bundle ref",
            },
        )
    bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle_data["status"] = "executed"
    bundle_data["expected_outputs"] = {
        **dict(bundle_data.get("expected_outputs") or {}),
        "draft_assembly_fasta": str(draft_assembly),
    }
    bundle_data["logs"] = {
        **dict(bundle_data.get("logs") or {}),
        "stdout": result.stdout_ref.path_ref,
        "stderr": result.stderr_ref.path_ref,
        **bundle_logs,
    }
    bundle_data["metadata"] = {
        **dict(bundle_data.get("metadata") or {}),
        "execution_enabled": True,
        "attempt_id": attempt.attempt_id,
        "draft_assembly_artifact_ref": str(draft_assembly),
        "draft_assembly_ref_status": "materialized",
        **bundle_metadata,
    }
    bundle = AssemblyCandidateBundle.model_validate(bundle_data)
    executed_bundle_path = bundle_path.with_name(
        bundle_path.name.replace(".planned.json", ".executed.json")
    )
    if executed_bundle_path == bundle_path:
        executed_bundle_path = bundle_path.with_name(
            f"{bundle_path.stem}.executed.json"
        )
    temporary_bundle = Path(attempt.working_dir) / (
        f".{executed_bundle_path.name}.{uuid4().hex}.tmp"
    )
    temporary_bundle.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        publish_file(temporary_bundle, executed_bundle_path)
    finally:
        temporary_bundle.unlink(missing_ok=True)
    artifacts.append(
        ArtifactMeta.from_file(
            executed_bundle_path,
            stage="assembly",
            method_name=lifecycle.method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            metadata={
                "candidate_id": bundle.candidate_id,
                "execution_enabled": True,
                "attempt_id": attempt.attempt_id,
            },
        )
    )

    append_artifact_records(
        state.get("data_dir", "data"), attempt.session_id, artifacts
    )
    ledger = Ledger(state.get("data_dir", "data"))
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(
        attempt.session_id, attempt, artifacts=artifact_payloads
    )
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(
            attempt.session_id, artifact, attempt_id=attempt.attempt_id
        )

    substeps = dict(output.get("substeps") or {})
    substeps[lifecycle.substep_key] = {
        "name": lifecycle.substep_key,
        "status": "executed",
        "method_name": lifecycle.method_name,
        "draft_assembly_ref": str(draft_assembly),
        "candidate_ref": str(executed_bundle_path),
    }
    return update_stage_output(
        state,
        "assembly",
        {
            "status": "draft_ready",
            "draft_assembly_ref": str(draft_assembly),
            "candidate_assembly_refs": [str(draft_assembly)],
            "executed_candidate_refs": [str(executed_bundle_path)],
            "artifact_refs": ordered_unique(
                [
                    *list(output.get("artifact_refs") or []),
                    *[str(item.file_path) for item in artifacts],
                ]
            ),
            "candidate_artifact_refs": ordered_unique(
                [
                    *list(output.get("candidate_artifact_refs") or []),
                    str(executed_bundle_path),
                ]
            ),
            "substeps": substeps,
        },
    )


def positive_int(
    value: Any,
    name: str,
    missing: list[str],
    invalid: list[dict[str, Any]],
) -> int | None:
    """Parse a positive integer while preserving structured validation errors."""
    if value is None or value == "":
        missing.append(name)
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        invalid.append(
            {"param": name, "value": value, "reason": "must be a positive integer"}
        )
        return None
    if converted <= 0:
        invalid.append(
            {"param": name, "value": value, "reason": "must be a positive integer"}
        )
    return converted


def blocked_parameters(
    prefix: str,
    method_name: str,
    missing: list[str],
    invalid: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the shared blocked-param state payload."""
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if invalid:
        details.append("invalid: " + ", ".join(item["param"] for item in invalid))
    return {
        "status": "blocked_param",
        "reason": f"{prefix} incomplete ({'; '.join(details)})",
        "missing_params": missing,
        "invalid_params": invalid,
        "method_name": method_name,
    }


def _candidate_bundle_path(output: dict[str, Any], method_name: str) -> Path | None:
    for ref in output.get("selected_candidate_refs") or []:
        path = Path(ref)
        if (
            path.exists()
            and json.loads(path.read_text(encoding="utf-8")).get("method_name")
            == method_name
        ):
            return path
    return None
