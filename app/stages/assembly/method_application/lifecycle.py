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
from pydantic import ValidationError

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.evidence import durable_execution_result
from app.execution.method_runner import run_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ASSEMBLY_PARAMETER_EXECUTION,
    ASSEMBLY_PARAMETER_FROZEN_ATTEMPT,
    ArtifactMeta,
    ArtifactRef,
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
from app.storage.publish import publish_file


@dataclass(frozen=True)
class MethodLifecycle:
    """State-key convention for one executable assembly method."""

    name: str
    method_name: str
    planned_status: str
    frozen_status: str
    ran_status: str
    frozen_attempt_key: str
    execution_result_key: str
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
            if assembly_output(state).get("status") == lifecycle.frozen_status
            else "done"
        ),
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: (
            "collect"
            if assembly_output(state).get("status") == lifecycle.ran_status
            else "done"
        ),
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def assembly_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("assembly", {}))


def update_assembly_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {**assembly_output(state), **payload}
    return {"stage_outputs": outputs}


def fail_update(state: TopLevelState, reason: str) -> dict:
    return update_assembly_output(state, {"status": "failed", "reason": reason})


def write_parameter_attempt_json_artifact(
    state: TopLevelState,
    attempt_id: int,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Publish an attempt-scoped JSON artifact through the v2 boundary."""

    return write_attempt_json_artifact(
        state,
        attempt_id,
        filename,
        payload,
        artifact_type=artifact_type,
        method_name=method_name,
    )


def current_parameter_attempt(
    output: dict[str, Any],
    *,
    method_id: str,
    label: str,
) -> tuple[ParameterAttempt | None, str | None]:
    """Return the typed current ParameterAttempt for freeze/read paths."""

    attempt_id = int(output.get("current_parameter_attempt_id") or 0)
    for item in output.get("parameter_attempts") or []:
        if not isinstance(item, dict) or item.get("attempt_id") != attempt_id:
            continue
        try:
            attempt = ParameterAttempt.model_validate(item)
        except ValidationError as exc:
            return (
                None,
                f"{label} current parameter attempt is not a valid ParameterAttempt: {exc}",
            )
        if attempt.method_id != method_id:
            return None, (
                f"{label} current parameter attempt method_id {attempt.method_id!r} "
                f"does not match {method_id!r}"
            )
        return attempt, None
    return (
        None,
        f"{label} requires a typed current ParameterAttempt with attempt_id={attempt_id}",
    )


def current_recommended_parameters(
    state: TopLevelState,
    output: dict[str, Any],
    *,
    method_id: str,
    label: str,
) -> tuple[dict[str, Any], str | None]:
    """Load current execution parameters without embedding them in state."""

    attempt, error = current_parameter_attempt(output, method_id=method_id, label=label)
    if error:
        return {}, error
    assert attempt is not None
    try:
        bundle = load_recommendation_bundle(state, attempt.recommendation_manifest_ref)
    except ParameterPersistenceError as exc:
        return {}, f"{label} recommendation evidence is invalid: {exc}"
    recommendation = dict(bundle["recommendation"].get("recommendation") or {})
    return dict(recommendation.get("parameters") or {}), None


def update_current_parameter_attempt(
    output: dict[str, Any],
    *,
    method_id: str,
    label: str,
    **updates: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """Apply lifecycle updates after validating the current attempt shape."""

    attempt, error = current_parameter_attempt(output, method_id=method_id, label=label)
    if error:
        return [], error
    assert attempt is not None
    if attempt.seal_hash:
        return [], f"{label} current parameter attempt is sealed and cannot be mutated"
    allowed = {
        "status",
        "frozen_parameters_ref",
        "command_ref",
        "execution_ref",
        "review_ref",
    }
    unexpected = set(updates) - allowed
    if unexpected:
        return [], (
            f"{label} attempted to embed non-cursor fields in ParameterAttempt: "
            + ", ".join(sorted(unexpected))
        )
    for field in allowed - {"status"}:
        if field not in updates or getattr(attempt, field) is None:
            continue
        if getattr(attempt, field) != updates[field]:
            return [], f"{label} current parameter attempt {field} is append-once"
    updated = ParameterAttempt.model_validate(
        {**attempt.model_dump(mode="json"), **updates}
    ).model_dump(mode="json")
    attempts: list[dict[str, Any]] = []
    for item in output.get("parameter_attempts") or []:
        if not isinstance(item, dict):
            continue
        if item.get("attempt_id") == attempt.attempt_id:
            attempts.append(updated)
        else:
            attempts.append(
                ParameterAttempt.model_validate(item).model_dump(mode="json")
            )
    return attempts, None


def persist_frozen_attempt(
    state: TopLevelState,
    attempt_id: int,
    attempt: FrozenAttemptSpec,
    *,
    method_name: str,
) -> ArtifactRef:
    """Persist the runner input so checkpoints carry only its verified ref."""

    return write_attempt_json_artifact(
        state,
        attempt_id,
        "frozen_attempt.json",
        {"schema_version": 2, "attempt": attempt.model_dump(mode="json")},
        artifact_type=ASSEMBLY_PARAMETER_FROZEN_ATTEMPT,
        method_name=method_name,
    )


def load_frozen_attempt(
    state: TopLevelState,
    value: ArtifactRef | dict[str, Any],
) -> FrozenAttemptSpec:
    """Verify and load one persisted runner input."""

    payload = read_json_artifact(
        state,
        value,
        expected_type=ASSEMBLY_PARAMETER_FROZEN_ATTEMPT,
    )
    return FrozenAttemptSpec.model_validate(payload["attempt"])


def run_method_attempt(state: TopLevelState, lifecycle: MethodLifecycle) -> dict:
    """Execute the frozen attempt and persist a method-namespaced result."""
    output = assembly_output(state)
    if output.get("status") != lifecycle.frozen_status:
        return {}

    attempt = load_frozen_attempt(state, output[lifecycle.frozen_attempt_key])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=lifecycle.name,
    )
    result = durable_execution_result(state, outcome.result)
    execution_ref = write_attempt_json_artifact(
        state,
        int(output.get("current_parameter_attempt_id") or 0),
        "execution.json",
        {
            "schema_version": 2,
            "attempt_id": int(output.get("current_parameter_attempt_id") or 0),
            "method_id": lifecycle.method_name,
            "result": result,
        },
        artifact_type=ASSEMBLY_PARAMETER_EXECUTION,
        method_name=lifecycle.method_name,
    )

    parameter_attempts, attempt_error = update_current_parameter_attempt(
        output,
        method_id=lifecycle.method_name,
        label=lifecycle.name,
        status="execution_failed" if not outcome.success else "executed",
        execution_ref=execution_ref.model_dump(mode="json"),
    )
    if attempt_error:
        return fail_update(state, attempt_error)

    if not outcome.success:
        updates = {
            "status": "failed",
            "reason": outcome.error,
            lifecycle.execution_result_key: execution_ref.model_dump(mode="json"),
        }
        updates["parameter_attempts"] = parameter_attempts
        return update_assembly_output(state, updates)

    updates = {
        "status": lifecycle.ran_status,
        lifecycle.execution_result_key: execution_ref.model_dump(mode="json"),
    }
    updates["parameter_attempts"] = parameter_attempts
    return update_assembly_output(state, updates)


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: MethodLifecycle,
    *,
    extra_artifacts: list[tuple[Path, str]],
    bundle_logs: dict[str, str],
    bundle_metadata: dict[str, Any],
) -> dict:
    """Register standard artifacts and materialize the selected candidate bundle."""
    output = assembly_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    attempt = load_frozen_attempt(state, output[lifecycle.frozen_attempt_key])
    execution_payload = read_json_artifact(
        state,
        output[lifecycle.execution_result_key],
        expected_type=ASSEMBLY_PARAMETER_EXECUTION,
    )
    result = dict(execution_payload.get("result") or {})
    draft_assembly = Path(output[lifecycle.draft_assembly_key])
    if not draft_assembly.exists():
        reason = f"expected {lifecycle.name} draft assembly missing: {draft_assembly}"
        Ledger(state.get("data_dir", "data")).record_attempt_failed(
            attempt.session_id, attempt, return_code=0, error=reason
        )
        return update_assembly_output(state, {"status": "failed", "reason": reason})

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
    for path, artifact_type in [
        (Path(result["stdout_path"]), "command.stdout"),
        (Path(result["stderr_path"]), "command.stderr"),
        *extra_artifacts,
    ]:
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
        return update_assembly_output(
            state,
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
        "stdout": str(result["stdout_path"]),
        "stderr": str(result["stderr_path"]),
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
    return update_assembly_output(
        state,
        {
            "status": "draft_ready",
            "draft_assembly_ref": str(draft_assembly),
            "candidate_assembly_refs": [str(draft_assembly)],
            "executed_candidate_refs": [str(executed_bundle_path)],
            "artifact_refs": unique_refs(
                [
                    *list(output.get("artifact_refs") or []),
                    *[str(item.file_path) for item in artifacts],
                ]
            ),
            "candidate_artifact_refs": unique_refs(
                [
                    *list(output.get("candidate_artifact_refs") or []),
                    str(executed_bundle_path),
                ]
            ),
            "substeps": substeps,
        },
    )


def unique_refs(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(refs))


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
